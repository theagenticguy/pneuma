"""Ports — the load-bearing seam (hexagonal-arch-stack.md §2).

Every external capability is a ``@runtime_checkable Protocol`` defined next to its
consumers (the application layer), never next to its implementers (infrastructure).
Adapters in ``infrastructure/`` implement these by shape; only the composition root
(``infrastructure/di.py``) knows both sides.

Ports speak in DOMAIN types. A repo returns a domain ``Goal``, never an asyncpg
``Record``. No SQL, no asyncpg type leaks through any signature here.

The repositories here take an explicit ``conn`` (an opaque transaction handle typed
as ``object`` at the port; the concrete adapter narrows it to ``asyncpg.Connection``)
because the unit of work owns transaction scope and every command runs inside one
transaction spanning domain mutation + event + outbox + processed-command write.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import TracebackType
from typing import Protocol, runtime_checkable
from uuid import UUID

from sdlc_blackboard.domain.approvals import Approval
from sdlc_blackboard.domain.artifacts import ArtifactAlias, ArtifactRevision
from sdlc_blackboard.domain.common import ArtifactBinding, DomainModel
from sdlc_blackboard.domain.events import RuntimeRun, TeamEvent
from sdlc_blackboard.domain.findings import Finding, FindingState
from sdlc_blackboard.domain.goals import Goal
from sdlc_blackboard.domain.reviews import Review
from sdlc_blackboard.domain.tasks import Task, TaskState

#: Opaque transaction handle. The adapter narrows this to asyncpg.Connection; the
#: application never calls asyncpg methods on it, only threads it into repo calls.
type Conn = object


@runtime_checkable
class Clock(Protocol):
    """Time source behind a port so use cases stay deterministic in tests."""

    def now(self) -> datetime: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Owns transaction scope. ``begin()`` yields a connection bound to one txn."""

    def begin(self) -> AbstractAsyncTxn: ...


class AbstractAsyncTxn(Protocol):
    """Async context manager yielding the transaction connection handle."""

    async def __aenter__(self) -> Conn: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


@runtime_checkable
class GoalRepo(Protocol):
    async def insert(self, conn: Conn, goal: Goal) -> None: ...
    async def get(self, conn: Conn, goal_id: UUID) -> Goal | None: ...
    async def get_for_update(self, conn: Conn, goal_id: UUID) -> Goal | None: ...
    async def lock_shared(self, conn: Conn, goal_id: UUID) -> None: ...
    async def list_all(self, conn: Conn) -> tuple[Goal, ...]: ...
    async def set_state_cas(
        self, conn: Conn, goal_id: UUID, expected_version: int, new_state: str
    ) -> Goal | None: ...


@runtime_checkable
class TaskRepo(Protocol):
    async def insert(self, conn: Conn, task: Task) -> None: ...
    async def get(self, conn: Conn, task_id: UUID) -> Task | None: ...
    async def get_for_update(self, conn: Conn, task_id: UUID) -> Task | None: ...
    async def get_by_key(self, conn: Conn, goal_id: UUID, task_key: str) -> Task | None: ...
    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]: ...
    async def add_dependencies(
        self, conn: Conn, task_id: UUID, depends_on: tuple[UUID, ...]
    ) -> None: ...
    async def refresh_ready(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]: ...
    async def claim_cas(
        self, conn: Conn, task_id: UUID, expected_version: int, actor_id: str, next_epoch: int
    ) -> Task | None: ...
    async def transition_cas(
        self,
        conn: Conn,
        task_id: UUID,
        expected_version: int,
        expected_state: TaskState,
        new_state: TaskState,
        assigned_actor_id: str | None = None,
    ) -> Task | None: ...
    async def bind_conversation(
        self, conn: Conn, task_id: UUID, epoch: int, conversation_id: str
    ) -> Task | None: ...


@runtime_checkable
class AssignmentRepo(Protocol):
    async def open_assignment(
        self, conn: Conn, task_id: UUID, epoch: int, actor_id: str
    ) -> UUID: ...
    async def complete_assignment(self, conn: Conn, task_id: UUID, epoch: int) -> None: ...


@runtime_checkable
class RuntimeRunRepo(Protocol):
    async def insert(self, conn: Conn, run: RuntimeRun) -> None: ...
    async def get_for_update(self, conn: Conn, run_id: UUID) -> RuntimeRun | None: ...
    async def set_state(
        self,
        conn: Conn,
        run_id: UUID,
        state: str,
        result_manifest: dict[str, object] | None = None,
    ) -> None: ...


@runtime_checkable
class ArtifactRepo(Protocol):
    async def insert_revision(
        self, conn: Conn, goal_id: UUID, revision: ArtifactRevision
    ) -> None: ...
    async def get_revision(self, conn: Conn, revision_id: UUID) -> ArtifactRevision | None: ...
    async def get_revision_by_hash(
        self, conn: Conn, artifact_id: UUID, content_hash: str
    ) -> ArtifactRevision | None: ...
    async def get_alias(
        self, conn: Conn, goal_id: UUID, logical_name: str
    ) -> ArtifactAlias | None: ...
    async def upsert_alias_initial(self, conn: Conn, alias: ArtifactAlias) -> None: ...
    async def promote_alias_cas(
        self,
        conn: Conn,
        goal_id: UUID,
        logical_name: str,
        expected_revision_id: UUID | None,
        new_revision_id: UUID,
    ) -> ArtifactAlias | None: ...
    async def list_aliases(self, conn: Conn, goal_id: UUID) -> tuple[ArtifactBinding, ...]: ...


@runtime_checkable
class FindingRepo(Protocol):
    async def insert(self, conn: Conn, finding: Finding) -> None: ...
    async def get(self, conn: Conn, finding_id: UUID) -> Finding | None: ...
    async def set_state_cas(
        self, conn: Conn, finding_id: UUID, expected_version: int, new_state: FindingState
    ) -> Finding | None: ...
    async def list_open_blocking(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]: ...
    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Finding, ...]: ...


@runtime_checkable
class ReviewRepo(Protocol):
    async def insert(self, conn: Conn, review: Review) -> None: ...
    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Review, ...]: ...
    async def mark_stale_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]: ...


@runtime_checkable
class ApprovalRepo(Protocol):
    async def insert(self, conn: Conn, approval: Approval) -> None: ...
    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Approval, ...]: ...
    async def mark_revoked_for_artifact(
        self, conn: Conn, artifact_id: UUID, current_revision_id: UUID
    ) -> tuple[UUID, ...]: ...


@runtime_checkable
class EventRepo(Protocol):
    """Appends a domain event AND an outbox row in the same transaction (§12)."""

    async def append(self, conn: Conn, event: TeamEvent) -> UUID: ...
    async def read_relevant(
        self,
        conn: Conn,
        goal_id: UUID,
        after_occurred_at: datetime | None,
        after_event_id: UUID | None,
        limit: int,
    ) -> tuple[TeamEvent, ...]: ...
    async def count_by_type(self, conn: Conn, goal_id: UUID, event_type: str) -> int: ...


class OutboxEntry(DomainModel):
    """A typed projection of one claimed outbox row (the port speaks this, not a raw row).

    Keeps the outbox table's column names and jsonb key set inside the adapter — the
    application publish loop indexes typed fields, not raw strings (hexagonal §2).
    """

    outbox_id: int
    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    attempts: int


@runtime_checkable
class OutboxRepo(Protocol):
    async def claim_unpublished(self, conn: Conn, limit: int) -> tuple[OutboxEntry, ...]: ...
    async def mark_published(self, conn: Conn, outbox_id: int) -> None: ...


@runtime_checkable
class ProcessedCommandStore(Protocol):
    """Idempotency dedup table access, used by execute_idempotently."""

    async def get(self, conn: Conn, command_id: UUID) -> tuple[str, str] | None: ...
    async def put(
        self,
        conn: Conn,
        command_id: UUID,
        actor_id: str,
        tool_name: str,
        request_hash: str,
        response: str,
    ) -> None: ...


@runtime_checkable
class CommandFailureRepo(Protocol):
    """Append-only command-failure ledger (spec T1 prerequisite).

    Deliberately outside idempotency: ``record`` inserts one row per failed attempt
    (never dedups). ``count_by_error_code_for_goal`` is the read side the thrash report
    aggregates — it resolves task-scoped failures (goal_id NULL) back to their goal via
    the ``tasks`` table, so a double-claim conflict recorded with only a ``task_id``
    still counts toward the right goal.
    """

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
    ) -> None: ...
    async def count_by_error_code_for_goal(
        self, conn: Conn, goal_id: UUID
    ) -> Mapping[str, int]: ...


__all__ = [
    "AbstractAsyncTxn",
    "ApprovalRepo",
    "ArtifactRepo",
    "AssignmentRepo",
    "Clock",
    "CommandFailureRepo",
    "Conn",
    "EventRepo",
    "FindingRepo",
    "GoalRepo",
    "OutboxEntry",
    "OutboxRepo",
    "ProcessedCommandStore",
    "ReviewRepo",
    "RuntimeRunRepo",
    "TaskRepo",
    "UnitOfWork",
]
