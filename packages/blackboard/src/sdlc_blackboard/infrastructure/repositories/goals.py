"""Goal persistence adapter (handoff §11).

``S608`` is suppressed package-wide (see ``_common`` docstring): trusted table
constants, ``$N``-bound values.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sdlc_blackboard.domain.goals import Goal, GoalState
from sdlc_blackboard.infrastructure.repositories._common import (
    cas_update,
    conn_of,
    map_actor,
)

if TYPE_CHECKING:
    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def _map_goal(row: asyncpg.Record) -> Goal:
    return Goal(
        goal_id=row["goal_id"],
        title=row["title"],
        objective=row["objective"],
        success_criteria=tuple(row["success_criteria"]),
        constraints=tuple(row["constraints"]),
        owner=map_actor(row["owner"]),
        state=GoalState(row["state"]),
        version=row["version"],
    )


class GoalRepository:
    async def insert(self, conn: Conn, goal: Goal) -> None:
        await conn_of(conn).execute(
            """
            insert into goals(goal_id, title, objective, success_criteria,
                              constraints, owner, state, version)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            goal.goal_id,
            goal.title,
            goal.objective,
            list(goal.success_criteria),
            list(goal.constraints),
            goal.owner.model_dump(mode="json"),
            goal.state.value,
            goal.version,
        )

    async def get(self, conn: Conn, goal_id: UUID) -> Goal | None:
        row = await conn_of(conn).fetchrow("select * from goals where goal_id = $1", goal_id)
        return _map_goal(row) if row is not None else None

    async def get_for_update(self, conn: Conn, goal_id: UUID) -> Goal | None:
        # Exclusive goal-row lock taken by authorize_goal_completion BEFORE it reads the
        # release gate: it conflicts with the FOR SHARE lock every gate-input writer takes
        # (lock_shared), serializing gate-input commits against the authorize evaluation
        # window so a concurrent blocking finding/review cannot slip a goal to SATISFIED
        # with an open blocker under READ COMMITTED (ADR-0012 write-skew fix).
        row = await conn_of(conn).fetchrow(
            "select * from goals where goal_id = $1 for update", goal_id
        )
        return _map_goal(row) if row is not None else None

    async def lock_shared(self, conn: Conn, goal_id: UUID) -> None:
        # FOR SHARE taken by every gate-input mutation (raise_finding, submit_review,
        # record_human_approval, promote_artifact) before it writes: shared locks coexist
        # with each other (writers stay concurrent) but block the FOR UPDATE authorize
        # holds, so gate inputs cannot commit inside authorize's read/CAS window.
        await conn_of(conn).execute("select 1 from goals where goal_id = $1 for share", goal_id)

    async def list_all(self, conn: Conn) -> tuple[Goal, ...]:
        rows = await conn_of(conn).fetch("select * from goals order by created_at")
        return tuple(_map_goal(r) for r in rows)

    async def set_state_cas(
        self, conn: Conn, goal_id: UUID, expected_version: int, new_state: str
    ) -> Goal | None:
        return await cas_update(
            conn,
            _map_goal,
            """
            update goals
               set state = $3, version = version + 1, updated_at = now()
             where goal_id = $1 and version = $2
            returning *
            """,
            goal_id,
            expected_version,
            new_state,
        )
