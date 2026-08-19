"""Query use cases (handoff §13): goal snapshot, artifact revision, relevant events.

Read-side: no idempotency wrapper, no mutation. Runs on a pooled connection.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sdlc_blackboard.application.query_models import GoalSnapshot
from sdlc_blackboard.application.use_cases.wiring import ServicePorts
from sdlc_blackboard.domain.artifacts import ArtifactRevision
from sdlc_blackboard.domain.events import TeamEvent
from sdlc_blackboard.domain.findings import RESOLVED_FINDING_STATES
from sdlc_blackboard.domain.goals import Goal
from sdlc_blackboard.domain.tasks import TaskState


class QueryService:
    def __init__(self, ports: ServicePorts) -> None:
        self._p = ports

    async def goal_snapshot(self, goal_id: UUID) -> GoalSnapshot | None:
        async with self._p.uow.begin() as conn:
            goal = await self._p.goals.get(conn, goal_id)
            if goal is None:
                return None
            tasks = await self._p.tasks.list_for_goal(conn, goal_id)
            aliases = await self._p.artifacts.list_aliases(conn, goal_id)
            findings = await self._p.findings.list_for_goal(conn, goal_id)
            reviews = await self._p.reviews.list_for_goal(conn, goal_id)
            approvals = await self._p.approvals.list_for_goal(conn, goal_id)
            open_findings = tuple(f for f in findings if f.state not in RESOLVED_FINDING_STATES)
            ready_ids = tuple(t.task_id for t in tasks if t.state == TaskState.READY)
            return GoalSnapshot(
                goal=goal,
                tasks=tasks,
                artifact_aliases=aliases,
                open_findings=open_findings,
                reviews=reviews,
                approvals=approvals,
                ready_task_ids=ready_ids,
            )

    async def get_artifact_revision(self, revision_id: UUID) -> ArtifactRevision | None:
        async with self._p.uow.begin() as conn:
            return await self._p.artifacts.get_revision(conn, revision_id)

    async def read_relevant_events(
        self,
        goal_id: UUID,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 100,
    ) -> tuple[TeamEvent, ...]:
        after_at = after[0] if after else None
        after_id = after[1] if after else None
        async with self._p.uow.begin() as conn:
            return await self._p.events.read_relevant(conn, goal_id, after_at, after_id, limit)

    async def list_goals(self) -> tuple[Goal, ...]:
        async with self._p.uow.begin() as conn:
            return await self._p.goals.list_all(conn)
