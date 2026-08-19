"""In-memory fake ports for use-case unit tests (no Docker, no asyncpg).

Every fake implements one ``application.ports`` Protocol by shape, so pyright-strict
accepts a ``FakeEnv().ports`` bundle wherever a real ``ServicePorts`` is expected. The
fakes are deliberately HONEST about the two contracts the services lean on:

- **CAS is real.** ``*_cas`` methods return ``None`` when the caller's expected
  version / expected state / expected revision no longer matches — exactly the
  None-on-miss shape ``_common.cas_update`` documents. A fake that always succeeded
  would hide the StaleVersion / Conflict branches these tests exist to reach.
- **The partial unique index is real.** ``open_assignment`` raises ``Conflict`` when an
  assignment for the task is already open, mirroring the DB-level double-claim defense.

They are dict-backed and single-transaction: the UoW hands out one sentinel conn that
the repos ignore (there is no real transaction to bind to), which is enough because unit
tests target the pure decision logic, not transactional isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID, uuid4

from sdlc_blackboard.application.ports import Conn, OutboxEntry
from sdlc_blackboard.domain.approvals import Approval
from sdlc_blackboard.domain.artifacts import ArtifactAlias, ArtifactRevision
from sdlc_blackboard.domain.common import ArtifactBinding
from sdlc_blackboard.domain.errors import Conflict
from sdlc_blackboard.domain.events import RuntimeRun, TeamEvent
from sdlc_blackboard.domain.findings import Finding, FindingState
from sdlc_blackboard.domain.goals import Goal, GoalState
from sdlc_blackboard.domain.reviews import Review
from sdlc_blackboard.domain.tasks import Task, TaskState

#: The single sentinel handle the fake UoW yields. Repos never call methods on it.
_SENTINEL_CONN: Conn = object()


class FakeClock:
    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._fixed


class _FakeTxn:
    async def __aenter__(self) -> Conn:
        return _SENTINEL_CONN

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return None


class FakeUnitOfWork:
    """Hands out one sentinel connection; there is no real transaction to scope."""

    def begin(self) -> _FakeTxn:
        return _FakeTxn()


class FakeGoalRepo:
    def __init__(self) -> None:
        self.goals: dict[UUID, Goal] = {}

    async def insert(self, conn: Conn, goal: Goal) -> None:
        self.goals[goal.goal_id] = goal

    async def get(self, conn: Conn, goal_id: UUID) -> Goal | None:
        return self.goals.get(goal_id)

    async def get_for_update(self, conn: Conn, goal_id: UUID) -> Goal | None:
        # Single-txn fake: the FOR UPDATE row lock has no observable effect here (no real
        # concurrency), so it behaves like get. The lock discipline is exercised at the
        # integration tier (test_contract_correctness) against real Postgres.
        return self.goals.get(goal_id)

    async def lock_shared(self, conn: Conn, goal_id: UUID) -> None:
        # No-op in the single-txn fake (see get_for_update): there is no concurrent
        # authorize to serialize against.
        return None

    async def list_all(self, conn: Conn) -> tuple[Goal, ...]:
        return tuple(self.goals.values())

    async def set_state_cas(
        self, conn: Conn, goal_id: UUID, expected_version: int, new_state: str
    ) -> Goal | None:
        current = self.goals.get(goal_id)
        if current is None or current.version != expected_version:
            return None
        updated = current.model_copy(
            update={"state": GoalState(new_state), "version": current.version + 1}
        )
        self.goals[goal_id] = updated
        return updated


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[UUID, Task] = {}
        self.dependencies: dict[UUID, tuple[UUID, ...]] = {}

    async def insert(self, conn: Conn, task: Task) -> None:
        self.tasks[task.task_id] = task

    async def get(self, conn: Conn, task_id: UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def get_for_update(self, conn: Conn, task_id: UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def get_by_key(self, conn: Conn, goal_id: UUID, task_key: str) -> Task | None:
        for task in self.tasks.values():
            if task.goal_id == goal_id and task.task_key == task_key:
                return task
        return None

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]:
        return tuple(t for t in self.tasks.values() if t.goal_id == goal_id)

    async def add_dependencies(
        self, conn: Conn, task_id: UUID, depends_on: tuple[UUID, ...]
    ) -> None:
        self.dependencies[task_id] = self.dependencies.get(task_id, ()) + depends_on

    async def refresh_ready(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]:
        newly_ready: list[Task] = []
        for task in list(self.tasks.values()):
            if task.goal_id != goal_id or task.state != TaskState.DRAFT:
                continue
            deps = self.dependencies.get(task.task_id, ())
            if all(
                (d := self.tasks.get(dep)) is not None and d.state == TaskState.ACCEPTED
                for dep in deps
            ):
                promoted = task.model_copy(
                    update={"state": TaskState.READY, "version": task.version + 1}
                )
                self.tasks[task.task_id] = promoted
                newly_ready.append(promoted)
        return tuple(newly_ready)

    async def claim_cas(
        self, conn: Conn, task_id: UUID, expected_version: int, actor_id: str, next_epoch: int
    ) -> Task | None:
        current = self.tasks.get(task_id)
        # Mirror the real SQL guard: `where task_id=$1 and state='ready' and version=$4`.
        # A version match on a non-READY row is a no-op (None) in Postgres, not a claim.
        if (
            current is None
            or current.version != expected_version
            or current.state != TaskState.READY
        ):
            return None
        claimed = current.model_copy(
            update={
                "state": TaskState.ASSIGNED,
                "assigned_actor_id": actor_id,
                "assignment_epoch": next_epoch,
                "version": current.version + 1,
            }
        )
        self.tasks[task_id] = claimed
        return claimed

    async def transition_cas(
        self,
        conn: Conn,
        task_id: UUID,
        expected_version: int,
        expected_state: TaskState,
        new_state: TaskState,
        assigned_actor_id: str | None = None,
    ) -> Task | None:
        current = self.tasks.get(task_id)
        if (
            current is None
            or current.version != expected_version
            or current.state != expected_state
        ):
            return None
        update: dict[str, object] = {"state": new_state, "version": current.version + 1}
        if assigned_actor_id is not None:
            update["assigned_actor_id"] = assigned_actor_id
        transitioned = current.model_copy(update=update)
        self.tasks[task_id] = transitioned
        return transitioned

    async def bind_conversation(
        self, conn: Conn, task_id: UUID, epoch: int, conversation_id: str
    ) -> Task | None:
        current = self.tasks.get(task_id)
        if current is None or current.assignment_epoch != epoch:
            return None
        bound = current.model_copy(update={"omnigent_conversation_id": conversation_id})
        self.tasks[task_id] = bound
        return bound


class FakeAssignmentRepo:
    def __init__(self) -> None:
        #: task_id -> epoch for the currently-open assignment (models the partial index).
        self.open: dict[UUID, int] = {}

    async def open_assignment(self, conn: Conn, task_id: UUID, epoch: int, actor_id: str) -> UUID:
        if task_id in self.open:
            raise Conflict(f"assignment already open for task {task_id}")
        self.open[task_id] = epoch
        return uuid4()

    async def complete_assignment(self, conn: Conn, task_id: UUID, epoch: int) -> None:
        self.open.pop(task_id, None)


class FakeRuntimeRunRepo:
    def __init__(self) -> None:
        self.runs: dict[UUID, RuntimeRun] = {}
        #: run_id -> the result_manifest last written by set_state (nullable jsonb col).
        self.manifests: dict[UUID, dict[str, object] | None] = {}
        #: run_id -> the state last written by set_state.
        self.states: dict[UUID, str] = {}

    async def insert(self, conn: Conn, run: RuntimeRun) -> None:
        self.runs[run.run_id] = run

    async def get_for_update(self, conn: Conn, run_id: UUID) -> RuntimeRun | None:
        return self.runs.get(run_id)

    async def set_state(
        self,
        conn: Conn,
        run_id: UUID,
        state: str,
        result_manifest: dict[str, object] | None = None,
    ) -> None:
        # Mirror the real SQL: `set state=$2, result_manifest=coalesce($3, result_manifest)`.
        # The state always advances; a None manifest LEAVES the prior manifest untouched.
        self.states[run_id] = state
        if result_manifest is not None:
            self.manifests[run_id] = result_manifest
        else:
            self.manifests.setdefault(run_id, None)


class FakeArtifactRepo:
    def __init__(self) -> None:
        self.revisions: dict[UUID, ArtifactRevision] = {}
        #: (goal_id, logical_name) -> alias
        self.aliases: dict[tuple[UUID, str], ArtifactAlias] = {}

    async def insert_revision(self, conn: Conn, goal_id: UUID, revision: ArtifactRevision) -> None:
        self.revisions[revision.revision_id] = revision

    async def get_revision(self, conn: Conn, revision_id: UUID) -> ArtifactRevision | None:
        return self.revisions.get(revision_id)

    async def get_revision_by_hash(
        self, conn: Conn, artifact_id: UUID, content_hash: str
    ) -> ArtifactRevision | None:
        for rev in self.revisions.values():
            if rev.artifact_id == artifact_id and rev.content_hash == content_hash:
                return rev
        return None

    async def get_alias(self, conn: Conn, goal_id: UUID, logical_name: str) -> ArtifactAlias | None:
        return self.aliases.get((goal_id, logical_name))

    async def upsert_alias_initial(self, conn: Conn, alias: ArtifactAlias) -> None:
        # Initial set only: never overwrite an existing alias (promotion is CAS-only).
        self.aliases.setdefault((alias.goal_id, alias.logical_name), alias)

    async def promote_alias_cas(
        self,
        conn: Conn,
        goal_id: UUID,
        logical_name: str,
        expected_revision_id: UUID | None,
        new_revision_id: UUID,
    ) -> ArtifactAlias | None:
        key = (goal_id, logical_name)
        existing = self.aliases.get(key)
        if existing is None:
            # Mirror the real adapter (repositories/artifacts.py:124-135): promote_alias_cas
            # ONLY ever UPDATEs an existing alias row. When no row matches, the UPDATE ...
            # RETURNING yields no row -> None (the service raises Conflict). The initial
            # alias is created solely via upsert_alias_initial (at submit time), never here.
            return None
        if existing.current_revision_id != expected_revision_id:
            return None
        promoted = existing.model_copy(
            update={
                "current_revision_id": new_revision_id,
                "version": existing.version + 1,
            }
        )
        self.aliases[key] = promoted
        return promoted

    async def list_aliases(self, conn: Conn, goal_id: UUID) -> tuple[ArtifactBinding, ...]:
        bindings: list[ArtifactBinding] = []
        for (g, logical_name), alias in self.aliases.items():
            if g != goal_id:
                continue
            rev = self.revisions.get(alias.current_revision_id)
            if rev is None:
                continue
            bindings.append(
                ArtifactBinding(
                    artifact_id=rev.artifact_id,
                    revision_id=rev.revision_id,
                    logical_name=logical_name,
                    content_hash=rev.content_hash,
                )
            )
        return tuple(bindings)


class FakeFindingRepo:
    def __init__(self) -> None:
        self.findings: dict[UUID, Finding] = {}

    async def insert(self, conn: Conn, finding: Finding) -> None:
        self.findings[finding.finding_id] = finding

    async def get(self, conn: Conn, finding_id: UUID) -> Finding | None:
        return self.findings.get(finding_id)

    async def set_state_cas(
        self, conn: Conn, finding_id: UUID, expected_version: int, new_state: FindingState
    ) -> Finding | None:
        current = self.findings.get(finding_id)
        if current is None or current.version != expected_version:
            return None
        updated = current.model_copy(update={"state": new_state, "version": current.version + 1})
        self.findings[finding_id] = updated
        return updated

    async def list_open_blocking(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]:
        from sdlc_blackboard.domain.findings import RESOLVED_FINDING_STATES

        return tuple(
            f
            for f in self.findings.values()
            if f.goal_id == goal_id and f.blocking and f.state not in RESOLVED_FINDING_STATES
        )

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings.values() if f.goal_id == goal_id)


class FakeReviewRepo:
    def __init__(self) -> None:
        self.reviews: dict[UUID, Review] = {}

    async def insert(self, conn: Conn, review: Review) -> None:
        self.reviews[review.review_id] = review

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Review, ...]:
        return tuple(r for r in self.reviews.values() if r.goal_id == goal_id)

    async def mark_stale_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]:
        stale: list[UUID] = []
        for review_id, review in list(self.reviews.items()):
            for b in review.artifact_bindings:
                if b.artifact_id == artifact_id and b.revision_id != current_revision_id:
                    if not review.stale:
                        self.reviews[review_id] = review.model_copy(update={"stale": True})
                        stale.append(review_id)
                    break
        return tuple(stale)


class FakeApprovalRepo:
    def __init__(self) -> None:
        self.approvals: dict[UUID, Approval] = {}

    async def insert(self, conn: Conn, approval: Approval) -> None:
        self.approvals[approval.approval_id] = approval

    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Approval, ...]:
        return tuple(a for a in self.approvals.values() if a.goal_id == goal_id)

    async def mark_revoked_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]:
        revoked: list[UUID] = []
        for approval_id, approval in list(self.approvals.items()):
            for b in approval.artifact_bindings:
                if b.artifact_id == artifact_id and b.revision_id != current_revision_id:
                    if not approval.revoked:
                        self.approvals[approval_id] = approval.model_copy(update={"revoked": True})
                        revoked.append(approval_id)
                    break
        return tuple(revoked)


class FakeEventRepo:
    def __init__(self) -> None:
        self.events: list[TeamEvent] = []

    async def append(self, conn: Conn, event: TeamEvent) -> UUID:
        self.events.append(event)
        return event.event_id

    async def read_relevant(
        self,
        conn: Conn,
        goal_id: UUID,
        after_occurred_at: datetime | None,
        after_event_id: UUID | None,
        limit: int,
    ) -> tuple[TeamEvent, ...]:
        return tuple(e for e in self.events if e.goal_id == goal_id)[:limit]

    async def count_by_type(self, conn: Conn, goal_id: UUID, event_type: str) -> int:
        # Mirror the real SQL count(*) filter: goal-scoped AND event_type match. A quiet
        # or unknown goal has zero matching events -> 0 (never null/error).
        return sum(1 for e in self.events if e.goal_id == goal_id and e.event_type == event_type)

    def types(self) -> list[str]:
        """Convenience for tests: the event_type of every appended event, in order."""
        return [e.event_type for e in self.events]


class FakeOutboxRepo:
    def __init__(self) -> None:
        self.published: list[int] = []

    async def claim_unpublished(self, conn: Conn, limit: int) -> tuple[OutboxEntry, ...]:
        return ()

    async def mark_published(self, conn: Conn, outbox_id: int) -> None:
        self.published.append(outbox_id)


class FakeProcessedCommandStore:
    def __init__(self) -> None:
        #: command_id -> (request_hash, response_json)
        self.records: dict[UUID, tuple[str, str]] = {}

    async def get(self, conn: Conn, command_id: UUID) -> tuple[str, str] | None:
        return self.records.get(command_id)

    async def put(
        self,
        conn: Conn,
        command_id: UUID,
        actor_id: str,
        tool_name: str,
        request_hash: str,
        response: str,
    ) -> None:
        self.records[command_id] = (request_hash, response)


class FakeCommandFailureRepo:
    """In-memory command-failure ledger. HONEST about the two adapter contracts:

    - **append-only**: ``record`` appends a row every call (never dedups), mirroring the
      real INSERT with no unique constraint.
    - **the goal-scope join is real**: ``count_by_error_code_for_goal`` counts a row when
      it was recorded with the goal_id directly OR when it is task-scoped (goal_id None)
      and its task belongs to the goal — exactly the ``goal_id = $1 OR task_id in (select
      task_id from tasks where goal_id = $1)`` predicate. It reaches into the shared
      FakeTaskRepo for the task->goal resolution, so a task-scoped conflict counts for the
      right goal and a foreign goal's rows never leak in (T1 frame property).
    """

    def __init__(self, tasks: FakeTaskRepo) -> None:
        self._tasks = tasks
        #: append-only list of recorded failure rows.
        self.rows: list[dict[str, object]] = []

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
        self.rows.append(
            {
                "command_id": command_id,
                "tool_name": tool_name,
                "actor_id": actor_id,
                "goal_id": goal_id,
                "task_id": task_id,
                "error_code": error_code,
            }
        )

    async def count_by_error_code_for_goal(self, conn: Conn, goal_id: UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            in_scope = row["goal_id"] == goal_id
            if not in_scope and row["task_id"] is not None:
                task = self._tasks.tasks.get(row["task_id"])  # type: ignore[arg-type]
                in_scope = task is not None and task.goal_id == goal_id
            if in_scope:
                code = str(row["error_code"])
                counts[code] = counts.get(code, 0) + 1
        return counts


class FakeEnv:
    """Bundles every fake into a ``ServicePorts`` plus handles to the concrete fakes.

    Access ``env.ports`` to construct a service; access the named attributes
    (``env.tasks``, ``env.events``, ...) to seed state and assert side effects.
    """

    def __init__(self) -> None:
        from sdlc_blackboard.application.use_cases.wiring import ServicePorts

        self.uow = FakeUnitOfWork()
        self.clock = FakeClock()
        self.goals = FakeGoalRepo()
        self.tasks = FakeTaskRepo()
        self.assignments = FakeAssignmentRepo()
        self.runs = FakeRuntimeRunRepo()
        self.artifacts = FakeArtifactRepo()
        self.findings = FakeFindingRepo()
        self.reviews = FakeReviewRepo()
        self.approvals = FakeApprovalRepo()
        self.events = FakeEventRepo()
        self.outbox = FakeOutboxRepo()
        self.processed_commands = FakeProcessedCommandStore()
        self.command_failures = FakeCommandFailureRepo(self.tasks)
        self.ports = ServicePorts(
            uow=self.uow,
            clock=self.clock,
            goals=self.goals,
            tasks=self.tasks,
            assignments=self.assignments,
            runs=self.runs,
            artifacts=self.artifacts,
            findings=self.findings,
            reviews=self.reviews,
            approvals=self.approvals,
            events=self.events,
            outbox=self.outbox,
            processed_commands=self.processed_commands,
            command_failures=self.command_failures,
        )
