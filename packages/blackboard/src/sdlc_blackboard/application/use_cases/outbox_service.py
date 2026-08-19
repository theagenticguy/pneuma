"""Outbox relay use case (handoff §12).

``EventRepository.append`` writes an outbox row transactionally with every domain
event; nothing drained it. This service is the minimal POC consumer the handoff
worker spec calls for: claim a batch of unpublished rows inside one unit-of-work
transaction (``FOR UPDATE SKIP LOCKED`` so parallel relays never double-publish),
structured-log each as the "publish" step (§12: "publishing may mean structured
logging ... and marking published_at" — no Kafka needed), mark them published, and
commit. Returns the number of rows drained.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from sdlc_blackboard.application.ports import Conn, OutboxEntry

if TYPE_CHECKING:
    from sdlc_blackboard.application.use_cases.wiring import ServicePorts


class OutboxService:
    def __init__(self, ports: ServicePorts) -> None:
        self._p = ports

    async def drain_outbox(self, limit: int = 100) -> int:
        """Drain up to ``limit`` unpublished outbox rows in one transaction.

        At-least-once publish: the claim (which bumps ``attempts``), the publish
        (structured log), and the ``published_at`` write all commit atomically as one
        batch. A row is only marked published if its log line was emitted. A crash
        mid-batch rolls the WHOLE transaction back — including the ``attempts`` bump — so
        the rows are re-claimable and re-published (a benign duplicate). ``attempts`` is
        therefore a count of committed delivery batches, not of every attempt: it only
        persists when the batch commits.
        """
        log = structlog.get_logger()
        async with self._p.uow.begin() as conn:
            rows = await self._p.outbox.claim_unpublished(conn, limit)
            for row in rows:
                await self._publish(log, conn, row)
        return len(rows)

    async def _publish(
        self, log: structlog.typing.FilteringBoundLogger, conn: Conn, row: OutboxEntry
    ) -> None:
        log.info(
            "outbox.published",
            outbox_id=row.outbox_id,
            event_id=str(row.event_id),
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=str(row.aggregate_id),
            attempts=row.attempts,
        )
        await self._p.outbox.mark_published(conn, row.outbox_id)
