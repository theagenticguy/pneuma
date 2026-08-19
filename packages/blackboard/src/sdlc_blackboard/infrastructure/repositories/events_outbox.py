"""Event log + transactional outbox persistence adapters (handoff §12).

``EventRepository.append`` writes the domain event AND its outbox row in the same
transaction; ``OutboxRepository`` is the SKIP-LOCKED consumer side drained by the
``blackboard outbox-relay`` command. ``S608`` is suppressed package-wide (see
``_common`` docstring).
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sdlc_blackboard.application.ports import OutboxEntry
from sdlc_blackboard.domain.events import TeamEvent
from sdlc_blackboard.infrastructure.repositories._common import (
    conn_of,
    map_actor,
    map_bindings,
)

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def _map_event(row: asyncpg.Record) -> TeamEvent:
    return TeamEvent(
        event_id=row["event_id"],
        goal_id=row["goal_id"],
        task_id=row["task_id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        event_type=row["event_type"],
        actor=map_actor(row["actor"]),
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        artifact_bindings=map_bindings(row["artifact_bindings"]),
        payload=dict(row["payload"]),
    )


class EventRepository:
    async def append(self, conn: Conn, event: TeamEvent) -> UUID:
        await conn_of(conn).execute(
            """
            insert into team_events(event_id, goal_id, task_id, aggregate_type,
                                   aggregate_id, aggregate_version, event_type, actor,
                                   correlation_id, causation_id, artifact_bindings, payload)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            event.event_id,
            event.goal_id,
            event.task_id,
            event.aggregate_type,
            event.aggregate_id,
            event.aggregate_version,
            event.event_type,
            event.actor.model_dump(mode="json"),
            event.correlation_id,
            event.causation_id,
            [b.model_dump(mode="json") for b in event.artifact_bindings],
            event.payload,
        )
        # Transactional outbox row committed in the same txn (handoff §12).
        await conn_of(conn).execute(
            """
            insert into outbox(event_id, event_type, aggregate_type, aggregate_id, payload)
            values ($1, $2, $3, $4, $5)
            """,
            event.event_id,
            event.event_type,
            event.aggregate_type,
            event.aggregate_id,
            event.payload,
        )
        return event.event_id

    async def read_relevant(
        self,
        conn: Conn,
        goal_id: UUID,
        after_occurred_at: datetime | None,
        after_event_id: UUID | None,
        limit: int,
    ) -> tuple[TeamEvent, ...]:
        if after_occurred_at is None or after_event_id is None:
            rows = await conn_of(conn).fetch(
                """
                select * from team_events
                 where goal_id = $1
                 order by occurred_at, event_id
                 limit $2
                """,
                goal_id,
                limit,
            )
        else:
            rows = await conn_of(conn).fetch(
                """
                select * from team_events
                 where goal_id = $1 and (occurred_at, event_id) > ($2, $3)
                 order by occurred_at, event_id
                 limit $4
                """,
                goal_id,
                after_occurred_at,
                after_event_id,
                limit,
            )
        return tuple(_map_event(r) for r in rows)

    async def count_by_type(self, conn: Conn, goal_id: UUID, event_type: str) -> int:
        # Goal-scoped count of one event type (thrash reclaim component, spec T1). Returns
        # 0 for a quiet or unknown goal — count(*) over zero rows is 0, never null/error.
        count = await conn_of(conn).fetchval(
            "select count(*) from team_events where goal_id = $1 and event_type = $2",
            goal_id,
            event_type,
        )
        return int(count or 0)


def _map_outbox_entry(row: asyncpg.Record) -> OutboxEntry:
    return OutboxEntry(
        outbox_id=row["outbox_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        attempts=row["attempts"],
    )


class OutboxRepository:
    async def claim_unpublished(self, conn: Conn, limit: int) -> tuple[OutboxEntry, ...]:
        # Claim a batch of unpublished rows and bump their delivery-attempt counter in
        # one statement: the CTE locks the batch (FOR UPDATE SKIP LOCKED, honoring the
        # HANDOFF §12 worker query) so concurrent relays never claim the same rows, then
        # the outer UPDATE increments attempts and RETURNS the full row for publishing.
        rows = await conn_of(conn).fetch(
            """
            with claimed as (
                select outbox_id from outbox
                 where published_at is null
                 order by outbox_id
                 for update skip locked
                 limit $1
            )
            update outbox o
               set attempts = o.attempts + 1
              from claimed c
             where o.outbox_id = c.outbox_id
            returning o.*
            """,
            limit,
        )
        # RETURNING order is unspecified; sort by outbox_id so the relay publishes in
        # append order (matches the original SELECT ... order by outbox_id contract). The
        # adapter is the only layer that knows the row shape — it maps to the typed
        # OutboxEntry the port speaks (hexagonal §2).
        return tuple(_map_outbox_entry(r) for r in sorted(rows, key=lambda r: r["outbox_id"]))

    async def mark_published(self, conn: Conn, outbox_id: int) -> None:
        await conn_of(conn).execute(
            "update outbox set published_at = now() where outbox_id = $1",
            outbox_id,
        )
