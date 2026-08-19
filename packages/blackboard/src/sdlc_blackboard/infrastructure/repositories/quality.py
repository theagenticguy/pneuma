"""Finding / review / approval persistence adapters (handoff §7, §8).

``FindingRepository.set_state_cas`` uses the shared version-guard CAS helper;
``ReviewRepository.insert`` translates the ``one_review_per_actor_type_binding``
unique-index violation to a typed domain ``Conflict`` at the adapter edge.
``S608`` is suppressed package-wide (see ``_common`` docstring).
"""

from typing import TYPE_CHECKING
from uuid import UUID

from asyncpg.exceptions import UniqueViolationError

from sdlc_blackboard.domain.approvals import Approval, ApprovalType
from sdlc_blackboard.domain.common import ArtifactBinding
from sdlc_blackboard.domain.errors import Conflict
from sdlc_blackboard.domain.findings import Finding, FindingSeverity, FindingState
from sdlc_blackboard.domain.reviews import Review, ReviewDisposition
from sdlc_blackboard.infrastructure.repositories._common import (
    cas_update,
    conn_of,
    map_actor,
    map_bindings,
    map_evidence,
)

if TYPE_CHECKING:
    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def _map_finding(row: asyncpg.Record) -> Finding:
    return Finding(
        finding_id=row["finding_id"],
        goal_id=row["goal_id"],
        task_id=row["task_id"],
        category=row["category"],
        severity=FindingSeverity(row["severity"]),
        statement=row["statement"],
        affected_artifacts=map_bindings(row["affected_artifacts"]),
        evidence=map_evidence(row["evidence"]),
        blocking=row["blocking"],
        resolution_criteria=tuple(row["resolution_criteria"]),
        state=FindingState(row["state"]),
        version=row["version"],
    )


def _map_review(row: asyncpg.Record, bindings: tuple[ArtifactBinding, ...]) -> Review:
    return Review(
        review_id=row["review_id"],
        goal_id=row["goal_id"],
        review_task_id=row["review_task_id"],
        reviewer=map_actor(row["reviewer"]),
        review_type=row["review_type"],
        binding_fingerprint=row["binding_fingerprint"],
        artifact_bindings=bindings,
        disposition=ReviewDisposition(row["disposition"]),
        summary=row["summary"],
        evidence=map_evidence(row["evidence"]),
        finding_ids=tuple(row["finding_ids"]),
        stale=row["stale"],
    )


def _map_approval(row: asyncpg.Record, bindings: tuple[ArtifactBinding, ...]) -> Approval:
    return Approval(
        approval_id=row["approval_id"],
        goal_id=row["goal_id"],
        approval_type=ApprovalType(row["approval_type"]),
        approver=map_actor(row["approver"]),
        binding_fingerprint=row["binding_fingerprint"],
        artifact_bindings=bindings,
        conditions=tuple(row["conditions"]),
        revoked=row["revoked"],
    )


class FindingRepository:
    async def insert(self, conn: Conn, finding: Finding) -> None:
        await conn_of(conn).execute(
            """
            insert into findings(finding_id, goal_id, task_id, category, severity,
                                 statement, affected_artifacts, evidence, blocking,
                                 resolution_criteria, state, version)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            finding.finding_id,
            finding.goal_id,
            finding.task_id,
            finding.category,
            finding.severity.value,
            finding.statement,
            [b.model_dump(mode="json") for b in finding.affected_artifacts],
            [e.model_dump(mode="json") for e in finding.evidence],
            finding.blocking,
            list(finding.resolution_criteria),
            finding.state.value,
            finding.version,
        )

    async def get(self, conn: Conn, finding_id: UUID) -> Finding | None:
        row = await conn_of(conn).fetchrow(
            "select * from findings where finding_id = $1", finding_id
        )
        return _map_finding(row) if row is not None else None

    async def set_state_cas(
        self, conn: Conn, finding_id: UUID, expected_version: int, new_state: FindingState
    ) -> Finding | None:
        return await cas_update(
            conn,
            _map_finding,
            """
            update findings
               set state = $3, version = version + 1, updated_at = now()
             where finding_id = $1 and version = $2
            returning *
            """,
            finding_id,
            expected_version,
            new_state.value,
        )

    async def list_open_blocking(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]:
        rows = await conn_of(conn).fetch(
            """
            select * from findings
             where goal_id = $1 and blocking = true
               and state not in ('verified', 'accepted_risk', 'rejected', 'superseded')
            """,
            goal_id,
        )
        return tuple(_map_finding(r) for r in rows)

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]:
        rows = await conn_of(conn).fetch(
            "select * from findings where goal_id = $1 order by created_at", goal_id
        )
        return tuple(_map_finding(r) for r in rows)


class ReviewRepository:
    async def insert(self, conn: Conn, review: Review) -> None:
        try:
            await conn_of(conn).execute(
                """
                insert into reviews(review_id, goal_id, review_task_id, reviewer, review_type,
                                   binding_fingerprint, disposition, summary, evidence,
                                   finding_ids, stale)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                review.review_id,
                review.goal_id,
                review.review_task_id,
                review.reviewer.model_dump(mode="json"),
                review.review_type,
                review.binding_fingerprint,
                review.disposition.value,
                review.summary,
                [e.model_dump(mode="json") for e in review.evidence],
                list(review.finding_ids),
                review.stale,
            )
        except UniqueViolationError as exc:
            # one_review_per_actor_type_binding: a reviewer submits at most one review
            # per (review_task, review_type, binding_fingerprint, actor) (handoff §8).
            # submit_review has no pre-check, so a plain double-submit with a fresh
            # command_id lands here. Translate to a typed, non-retryable domain conflict
            # at the adapter edge rather than leaking a raw UniqueViolationError.
            raise Conflict("duplicate review for (task, type, binding, actor)") from exc
        for b in review.artifact_bindings:
            await conn_of(conn).execute(
                """
                insert into review_artifact_bindings(
                    review_id, artifact_id, revision_id, content_hash
                )
                values ($1, $2, $3, $4)
                """,
                review.review_id,
                b.artifact_id,
                b.revision_id,
                b.content_hash,
            )

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Review, ...]:
        rows = await conn_of(conn).fetch(
            "select * from reviews where goal_id = $1 order by created_at", goal_id
        )
        reviews: list[Review] = []
        for row in rows:
            binding_rows = await conn_of(conn).fetch(
                """
                select b.artifact_id, b.revision_id, b.content_hash, r.logical_name
                  from review_artifact_bindings b
                  left join artifact_revisions r on r.revision_id = b.revision_id
                 where b.review_id = $1
                """,
                row["review_id"],
            )
            bindings = tuple(
                ArtifactBinding(
                    artifact_id=br["artifact_id"],
                    revision_id=br["revision_id"],
                    logical_name=br["logical_name"] or "unknown",
                    content_hash=br["content_hash"],
                )
                for br in binding_rows
            )
            reviews.append(_map_review(row, bindings))
        return tuple(reviews)

    async def mark_stale_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]:
        rows = await conn_of(conn).fetch(
            """
            update reviews r
               set stale = true
              from review_artifact_bindings b
             where b.review_id = r.review_id
               and b.artifact_id = $1
               and b.revision_id <> $2
               and r.stale = false
            returning r.review_id
            """,
            artifact_id,
            current_revision_id,
        )
        return tuple(row["review_id"] for row in rows)


class ApprovalRepository:
    async def insert(self, conn: Conn, approval: Approval) -> None:
        await conn_of(conn).execute(
            """
            insert into approvals(approval_id, goal_id, approval_type, approver,
                                 binding_fingerprint, conditions, revoked)
            values ($1, $2, $3, $4, $5, $6, $7)
            """,
            approval.approval_id,
            approval.goal_id,
            approval.approval_type.value,
            approval.approver.model_dump(mode="json"),
            approval.binding_fingerprint,
            list(approval.conditions),
            approval.revoked,
        )
        for b in approval.artifact_bindings:
            await conn_of(conn).execute(
                """
                insert into approval_artifact_bindings(
                    approval_id, artifact_id, revision_id, content_hash
                )
                values ($1, $2, $3, $4)
                """,
                approval.approval_id,
                b.artifact_id,
                b.revision_id,
                b.content_hash,
            )

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Approval, ...]:
        rows = await conn_of(conn).fetch(
            "select * from approvals where goal_id = $1 order by created_at", goal_id
        )
        approvals: list[Approval] = []
        for row in rows:
            binding_rows = await conn_of(conn).fetch(
                """
                select b.artifact_id, b.revision_id, b.content_hash, r.logical_name
                  from approval_artifact_bindings b
                  left join artifact_revisions r on r.revision_id = b.revision_id
                 where b.approval_id = $1
                """,
                row["approval_id"],
            )
            bindings = tuple(
                ArtifactBinding(
                    artifact_id=br["artifact_id"],
                    revision_id=br["revision_id"],
                    logical_name=br["logical_name"] or "unknown",
                    content_hash=br["content_hash"],
                )
                for br in binding_rows
            )
            approvals.append(_map_approval(row, bindings))
        return tuple(approvals)

    async def mark_revoked_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]:
        rows = await conn_of(conn).fetch(
            """
            update approvals a
               set revoked = true
              from approval_artifact_bindings b
             where b.approval_id = a.approval_id
               and b.artifact_id = $1
               and b.revision_id <> $2
               and a.revoked = false
            returning a.approval_id
            """,
            artifact_id,
            current_revision_id,
        )
        return tuple(row["approval_id"] for row in rows)
