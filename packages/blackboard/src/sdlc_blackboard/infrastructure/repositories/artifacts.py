"""Artifact revision + alias persistence adapter (handoff §6).

The alias promotion is a revision-token compare-and-set with a two-branch shape
(initial set vs. expected-token guard) — deliberately NOT the shared ``cas_update``
helper. ``S608`` is suppressed package-wide (see ``_common`` docstring).
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sdlc_blackboard.domain.artifacts import (
    ArtifactAlias,
    ArtifactRevision,
    ArtifactStatus,
)
from sdlc_blackboard.domain.common import ArtifactBinding
from sdlc_blackboard.infrastructure.repositories._common import conn_of, map_evidence

if TYPE_CHECKING:
    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def _map_revision(row: asyncpg.Record) -> ArtifactRevision:
    return ArtifactRevision(
        artifact_id=row["artifact_id"],
        revision_id=row["revision_id"],
        artifact_type=row["artifact_type"],
        logical_name=row["logical_name"],
        content_uri=row["content_uri"],
        content_hash=row["content_hash"],
        summary=row["summary"],
        produced_by_task_id=row["produced_by_task_id"],
        produced_by_run_id=row["produced_by_run_id"],
        parent_revision_ids=tuple(row["parent_revision_ids"]),
        evidence=map_evidence(row["evidence"]),
        status=ArtifactStatus(row["status"]),
    )


class ArtifactRepository:
    async def insert_revision(self, conn: Conn, goal_id: UUID, revision: ArtifactRevision) -> None:
        # ArtifactRevision (handoff §6) carries no goal_id; the service resolves it
        # from the producing task and passes it here for the FK column.
        await conn_of(conn).execute(
            """
            insert into artifact_revisions(revision_id, artifact_id, goal_id,
                                           produced_by_task_id, produced_by_run_id,
                                           artifact_type, logical_name, content_uri,
                                           content_hash, summary, parent_revision_ids,
                                           evidence, status)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            revision.revision_id,
            revision.artifact_id,
            goal_id,
            revision.produced_by_task_id,
            revision.produced_by_run_id,
            revision.artifact_type,
            revision.logical_name,
            revision.content_uri,
            revision.content_hash,
            revision.summary,
            list(revision.parent_revision_ids),
            [e.model_dump(mode="json") for e in revision.evidence],
            revision.status.value,
        )

    async def get_revision(self, conn: Conn, revision_id: UUID) -> ArtifactRevision | None:
        row = await conn_of(conn).fetchrow(
            "select * from artifact_revisions where revision_id = $1", revision_id
        )
        return _map_revision(row) if row is not None else None

    async def get_revision_by_hash(
        self, conn: Conn, artifact_id: UUID, content_hash: str
    ) -> ArtifactRevision | None:
        row = await conn_of(conn).fetchrow(
            "select * from artifact_revisions where artifact_id = $1 and content_hash = $2",
            artifact_id,
            content_hash,
        )
        return _map_revision(row) if row is not None else None

    async def get_alias(self, conn: Conn, goal_id: UUID, logical_name: str) -> ArtifactAlias | None:
        row = await conn_of(conn).fetchrow(
            "select * from artifact_aliases where goal_id = $1 and logical_name = $2",
            goal_id,
            logical_name,
        )
        if row is None:
            return None
        return ArtifactAlias(
            goal_id=row["goal_id"],
            logical_name=row["logical_name"],
            current_revision_id=row["current_revision_id"],
            version=row["version"],
        )

    async def upsert_alias_initial(self, conn: Conn, alias: ArtifactAlias) -> None:
        await conn_of(conn).execute(
            """
            insert into artifact_aliases(goal_id, logical_name, current_revision_id, version)
            values ($1, $2, $3, $4)
            on conflict (goal_id, logical_name) do nothing
            """,
            alias.goal_id,
            alias.logical_name,
            alias.current_revision_id,
            alias.version,
        )

    async def promote_alias_cas(
        self,
        conn: Conn,
        goal_id: UUID,
        logical_name: str,
        expected_revision_id: UUID | None,
        new_revision_id: UUID,
    ) -> ArtifactAlias | None:
        # Revision-token guard (not version) with a two-branch shape — deliberately
        # verbatim, not the shared cas_update helper (see _common.cas_update docstring).
        if expected_revision_id is None:
            row = await conn_of(conn).fetchrow(
                """
                update artifact_aliases
                   set current_revision_id = $3, version = version + 1, updated_at = now()
                 where goal_id = $1 and logical_name = $2
                returning *
                """,
                goal_id,
                logical_name,
                new_revision_id,
            )
        else:
            row = await conn_of(conn).fetchrow(
                """
                update artifact_aliases
                   set current_revision_id = $4, version = version + 1, updated_at = now()
                 where goal_id = $1 and logical_name = $2 and current_revision_id = $3
                returning *
                """,
                goal_id,
                logical_name,
                expected_revision_id,
                new_revision_id,
            )
        if row is None:
            return None
        return ArtifactAlias(
            goal_id=row["goal_id"],
            logical_name=row["logical_name"],
            current_revision_id=row["current_revision_id"],
            version=row["version"],
        )

    async def list_aliases(self, conn: Conn, goal_id: UUID) -> tuple[ArtifactBinding, ...]:
        rows = await conn_of(conn).fetch(
            """
            select a.logical_name, a.current_revision_id, r.artifact_id, r.content_hash
              from artifact_aliases a
              join artifact_revisions r on r.revision_id = a.current_revision_id
             where a.goal_id = $1
            """,
            goal_id,
        )
        return tuple(
            ArtifactBinding(
                artifact_id=r["artifact_id"],
                revision_id=r["current_revision_id"],
                logical_name=r["logical_name"],
                content_hash=r["content_hash"],
            )
            for r in rows
        )
