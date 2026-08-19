"""Task / assignment / runtime-run persistence adapters (handoff §11, §12).

The load-bearing operations are compare-and-set transitions and the partial unique
index ``one_active_assignment_per_task``. ``S608`` is suppressed package-wide (see
``_common`` docstring): trusted table constants, ``$N``-bound values.
"""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from asyncpg.exceptions import UniqueViolationError

from sdlc_blackboard.domain.common import ActorKind
from sdlc_blackboard.domain.errors import Conflict
from sdlc_blackboard.domain.events import RoutingClass, RunState, RuntimeRun
from sdlc_blackboard.domain.tasks import Task, TaskContractCreate, TaskState
from sdlc_blackboard.infrastructure.repositories._common import (
    cas_update,
    conn_of,
    map_bindings,
)

if TYPE_CHECKING:
    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def _map_task(row: asyncpg.Record) -> Task:
    contract = TaskContractCreate.model_validate(row["contract"])
    return Task(
        task_id=row["task_id"],
        goal_id=row["goal_id"],
        task_key=row["task_key"],
        title=row["title"],
        objective=row["objective"],
        required_actor_kind=ActorKind(row["required_actor_kind"]),
        state=TaskState(row["state"]),
        version=row["version"],
        assignment_epoch=row["assignment_epoch"],
        assigned_actor_id=row["assigned_actor_id"],
        omnigent_conversation_id=row["omnigent_conversation_id"],
        contract=contract,
    )


def _map_runtime_run(row: asyncpg.Record) -> RuntimeRun:
    routing = row["routing_class"]
    return RuntimeRun(
        run_id=row["run_id"],
        task_id=row["task_id"],
        assignment_epoch=row["assignment_epoch"],
        actor_id=row["actor_id"],
        omnigent_conversation_id=row["omnigent_conversation_id"],
        state=RunState(row["state"]),
        input_manifest=map_bindings(row["input_manifest"]),
        provider=row["provider"],
        model_id=row["model_id"],
        aws_region=row["aws_region"],
        routing_class=RoutingClass(routing) if routing else None,
        harness=row["harness"],
    )


class TaskRepository:
    async def insert(self, conn: Conn, task: Task) -> None:
        await conn_of(conn).execute(
            """
            insert into tasks(task_id, goal_id, task_key, title, objective,
                              required_actor_kind, contract, state, version,
                              assignment_epoch, assigned_actor_id, omnigent_conversation_id)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            task.task_id,
            task.goal_id,
            task.task_key,
            task.title,
            task.objective,
            task.required_actor_kind.value,
            task.contract.model_dump(mode="json"),
            task.state.value,
            task.version,
            task.assignment_epoch,
            task.assigned_actor_id,
            task.omnigent_conversation_id,
        )

    async def get(self, conn: Conn, task_id: UUID) -> Task | None:
        row = await conn_of(conn).fetchrow("select * from tasks where task_id = $1", task_id)
        return _map_task(row) if row is not None else None

    async def get_for_update(self, conn: Conn, task_id: UUID) -> Task | None:
        row = await conn_of(conn).fetchrow(
            "select * from tasks where task_id = $1 for update", task_id
        )
        return _map_task(row) if row is not None else None

    async def get_by_key(self, conn: Conn, goal_id: UUID, task_key: str) -> Task | None:
        row = await conn_of(conn).fetchrow(
            "select * from tasks where goal_id = $1 and task_key = $2", goal_id, task_key
        )
        return _map_task(row) if row is not None else None

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]:
        rows = await conn_of(conn).fetch(
            "select * from tasks where goal_id = $1 order by created_at", goal_id
        )
        return tuple(_map_task(r) for r in rows)

    async def add_dependencies(
        self, conn: Conn, task_id: UUID, depends_on: tuple[UUID, ...]
    ) -> None:
        for dep in depends_on:
            await conn_of(conn).execute(
                """
                insert into task_dependencies(task_id, depends_on_task_id)
                values ($1, $2)
                on conflict do nothing
                """,
                task_id,
                dep,
            )

    async def refresh_ready(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]:
        # Bulk, no version guard, returns many rows — deliberately NOT the cas_update
        # helper (see _common.cas_update docstring).
        rows = await conn_of(conn).fetch(
            """
            update tasks t
               set state = 'ready', version = version + 1, updated_at = now()
             where t.goal_id = $1
               and t.state = 'draft'
               and not exists (
                   select 1
                     from task_dependencies d
                     join tasks dependency on dependency.task_id = d.depends_on_task_id
                    where d.task_id = t.task_id
                      and dependency.state <> 'accepted'
               )
            returning *
            """,
            goal_id,
        )
        return tuple(_map_task(r) for r in rows)

    async def claim_cas(
        self, conn: Conn, task_id: UUID, expected_version: int, actor_id: str, next_epoch: int
    ) -> Task | None:
        return await cas_update(
            conn,
            _map_task,
            """
            update tasks
               set state = 'assigned', assigned_actor_id = $2,
                   assignment_epoch = $3, version = version + 1, updated_at = now()
             where task_id = $1 and state = 'ready' and version = $4
            returning *
            """,
            task_id,
            actor_id,
            next_epoch,
            expected_version,
        )

    async def transition_cas(
        self,
        conn: Conn,
        task_id: UUID,
        expected_version: int,
        expected_state: TaskState,
        new_state: TaskState,
        assigned_actor_id: str | None = None,
    ) -> Task | None:
        return await cas_update(
            conn,
            _map_task,
            """
            update tasks
               set state = $4, version = version + 1, updated_at = now(),
                   assigned_actor_id = coalesce($5, assigned_actor_id)
             where task_id = $1 and version = $2 and state = $3
            returning *
            """,
            task_id,
            expected_version,
            expected_state.value,
            new_state.value,
            assigned_actor_id,
        )

    async def bind_conversation(
        self, conn: Conn, task_id: UUID, epoch: int, conversation_id: str
    ) -> Task | None:
        row = await conn_of(conn).fetchrow(
            """
            update tasks
               set omnigent_conversation_id = $3, updated_at = now()
             where task_id = $1 and assignment_epoch = $2
            returning *
            """,
            task_id,
            epoch,
            conversation_id,
        )
        return _map_task(row) if row is not None else None


class AssignmentRepository:
    async def open_assignment(self, conn: Conn, task_id: UUID, epoch: int, actor_id: str) -> UUID:
        assignment_id = uuid4()
        try:
            await conn_of(conn).execute(
                """
                insert into task_assignments(
                    assignment_id, task_id, assignment_epoch, actor_id, state
                )
                values ($1, $2, $3, $4, 'assigned')
                """,
                assignment_id,
                task_id,
                epoch,
                actor_id,
            )
        except UniqueViolationError as exc:
            # The partial unique index one_active_assignment_per_task is the DB-level
            # defense against double-claim (handoff §11). If the task still has an
            # active assignment (e.g. recovery drift returned it to READY without
            # failing the prior assignment, §23), this INSERT loses the race. Translate
            # the asyncpg exception to a typed, non-retryable domain conflict at the
            # adapter edge (hexagonal §4) instead of leaking a raw 500.
            raise Conflict("task already has an active assignment") from exc
        return assignment_id

    async def complete_assignment(self, conn: Conn, task_id: UUID, epoch: int) -> None:
        await conn_of(conn).execute(
            """
            update task_assignments
               set state = 'completed', ended_at = now()
             where task_id = $1 and assignment_epoch = $2 and state in ('assigned', 'running')
            """,
            task_id,
            epoch,
        )


class RuntimeRunRepository:
    async def insert(self, conn: Conn, run: RuntimeRun) -> None:
        await conn_of(conn).execute(
            """
            insert into runtime_runs(run_id, task_id, assignment_epoch, actor_id,
                                     omnigent_conversation_id, state, input_manifest,
                                     provider, model_id, aws_region, routing_class, harness)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            run.run_id,
            run.task_id,
            run.assignment_epoch,
            run.actor_id,
            run.omnigent_conversation_id,
            run.state.value,
            [b.model_dump(mode="json") for b in run.input_manifest],
            run.provider,
            run.model_id,
            run.aws_region,
            run.routing_class.value if run.routing_class else None,
            run.harness,
        )

    async def get_for_update(self, conn: Conn, run_id: UUID) -> RuntimeRun | None:
        row = await conn_of(conn).fetchrow(
            "select * from runtime_runs where run_id = $1 for update", run_id
        )
        return _map_runtime_run(row) if row is not None else None

    async def set_state(
        self,
        conn: Conn,
        run_id: UUID,
        state: str,
        result_manifest: dict[str, object] | None = None,
    ) -> None:
        # result_manifest (jsonb) captures the run's structured outcome — disposition,
        # summary, and the reviewer-facing fields the SubmitTaskResult contract carries.
        # The pool-wide jsonb codec (ADR-0003) lets us pass a plain dict. Passing None
        # leaves any existing manifest untouched via coalesce.
        await conn_of(conn).execute(
            """
            update runtime_runs
               set state = $2,
                   result_manifest = coalesce($3, result_manifest),
                   updated_at = now()
             where run_id = $1
            """,
            run_id,
            state,
            result_manifest,
        )
