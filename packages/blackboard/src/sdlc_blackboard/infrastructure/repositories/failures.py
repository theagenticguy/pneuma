"""Command-failure ledger persistence adapter (spec 001-routing-thrash, T1).

Append-only: ``record`` inserts one row per failed command attempt and never dedups
(the opposite of ``processed_commands``). ``count_by_error_code_for_goal`` is the
read side the thrash report aggregates — it counts a goal's failures grouped by
error code, resolving task-scoped rows (``goal_id`` NULL) back to their goal through
the ``tasks`` table so double-claim conflicts (recorded with only a ``task_id``) still
count. ``S608`` is suppressed package-wide (see ``_common`` docstring): the table names
are trusted module constants and every value is ``$N``-bound.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from sdlc_blackboard.infrastructure.repositories._common import conn_of

if TYPE_CHECKING:
    from sdlc_blackboard.application.ports import Conn


class CommandFailureRepository:
    async def record(
        self,
        conn: Conn,
        *,
        command_id: UUID,
        tool_name: str,
        actor_id: str,
        goal_id: UUID | None,
        task_id: UUID | None,
        error_code: str,
    ) -> None:
        await conn_of(conn).execute(
            """
            insert into command_failures(
                command_id, tool_name, actor_id, goal_id, task_id, error_code
            )
            values ($1, $2, $3, $4, $5, $6)
            """,
            command_id,
            tool_name,
            actor_id,
            goal_id,
            task_id,
            error_code,
        )

    async def count_by_error_code_for_goal(self, conn: Conn, goal_id: UUID) -> Mapping[str, int]:
        # Goal scope (spec T1): a failure belongs to the goal when it was recorded with
        # this goal_id directly, OR when it is task-scoped (goal_id NULL) and its task
        # belongs to the goal. Commands like claim_task/start_runtime_run carry a task_id
        # but no goal_id, so the task->goal resolution is load-bearing for conflicts.
        rows = await conn_of(conn).fetch(
            """
            select error_code, count(*) as n
              from command_failures
             where goal_id = $1
                or task_id in (select task_id from tasks where goal_id = $1)
             group by error_code
            """,
            goal_id,
        )
        return {row["error_code"]: row["n"] for row in rows}
