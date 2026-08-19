"""Goal use cases (handoff §11)."""

from __future__ import annotations

from uuid import UUID

from sdlc_blackboard.application.events import append_domain_event
from sdlc_blackboard.application.ports import Conn
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.application.use_cases.base import CommandService
from sdlc_blackboard.application.use_cases.gate_service import GateService
from sdlc_blackboard.domain.common import CommandContext, DomainModel
from sdlc_blackboard.domain.errors import NotFound, PreconditionFailed, StaleVersion
from sdlc_blackboard.domain.events import GateStatus
from sdlc_blackboard.domain.goals import Goal, GoalCreate, GoalState


class _GoalIdRequest(DomainModel):
    """Canonical-hashable payload for goal-id-only commands (idempotency §10)."""

    goal_id: UUID


class GoalService(CommandService):
    async def create_goal(
        self, context: CommandContext, request: GoalCreate
    ) -> CommandResult[Goal]:
        async def body(conn: Conn) -> Goal:
            goal = Goal(
                title=request.title,
                objective=request.objective,
                success_criteria=request.success_criteria,
                constraints=request.constraints,
                owner=request.owner,
                state=GoalState.ACTIVE,
                version=0,
            )
            await self._p.goals.insert(conn, goal)
            await append_domain_event(
                self._p.events,
                conn,
                event_type="goal.created",
                aggregate_type="goal",
                aggregate_id=goal.goal_id,
                aggregate_version=goal.version,
                goal_id=goal.goal_id,
                task_id=None,
                context=context,
                payload=goal.model_dump(mode="json"),
            )
            return goal

        return await self._command(context, "create_goal", request, Goal, body)

    async def authorize_goal_completion(
        self, context: CommandContext, goal_id: UUID
    ) -> CommandResult[Goal]:
        """Flip a goal to SATISFIED, enforcing the release gate in the same transaction.

        The gate is re-checked on the SAME unit of work that performs the CAS, so no
        TOCTOU window opens between an out-of-band ``get_gate_status`` read and this
        state change: authorize is enforcing, not advisory. Raises ``PreconditionFailed``
        unless the gate is SATISFIED.

        Under READ COMMITTED a same-conn re-check alone does not close concurrent-gate-input
        write skew (a blocking finding/review commits without touching the goal row). So we
        take an exclusive ``FOR UPDATE`` lock on the goal row FIRST; every gate-input writer
        takes ``FOR SHARE`` on the same row before writing (``GoalRepo.lock_shared``). FOR
        SHARE conflicts with FOR UPDATE, so gate-input commits serialize against this
        evaluation window while writers stay concurrent with each other (ADR-0012).
        """
        gate = GateService(self._p)

        async def body(conn: Conn) -> Goal:
            current = await self._p.goals.get_for_update(conn, goal_id)
            if current is None:
                raise NotFound("goal", goal_id)
            gate_result = await gate.evaluate_on_conn(conn, goal_id)
            if gate_result.status != GateStatus.SATISFIED:
                raise PreconditionFailed("release gate not satisfied")
            updated = await self._p.goals.set_state_cas(
                conn, goal_id, current.version, GoalState.SATISFIED.value
            )
            if updated is None:
                raise StaleVersion()
            await append_domain_event(
                self._p.events,
                conn,
                event_type="goal.satisfied",
                aggregate_type="goal",
                aggregate_id=updated.goal_id,
                aggregate_version=updated.version,
                goal_id=updated.goal_id,
                task_id=None,
                context=context,
                payload=updated.model_dump(mode="json"),
            )
            return updated

        return await self._command(
            context,
            "authorize_goal_completion",
            _GoalIdRequest(goal_id=goal_id),
            Goal,
            body,
        )
