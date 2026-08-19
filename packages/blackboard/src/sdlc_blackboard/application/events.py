"""Event-append helper shared by every command service (handoff §12).

``append_domain_event`` writes a ``team_events`` row and its transactional outbox
row in the same connection/transaction as the domain mutation, so state and events
commit atomically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sdlc_blackboard.domain.common import ArtifactBinding, CommandContext
from sdlc_blackboard.domain.events import TeamEvent

if TYPE_CHECKING:
    from sdlc_blackboard.application.ports import Conn, EventRepo


async def append_domain_event(
    events: EventRepo,
    conn: Conn,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    goal_id: UUID,
    task_id: UUID | None,
    context: CommandContext,
    payload: dict[str, object],
    artifact_bindings: tuple[ArtifactBinding, ...] = (),
) -> UUID:
    event = TeamEvent(
        goal_id=goal_id,
        task_id=task_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        actor=context.actor,
        correlation_id=context.correlation_id,
        causation_id=context.causation_id,
        artifact_bindings=artifact_bindings,
        payload=payload,
    )
    return await events.append(conn, event)
