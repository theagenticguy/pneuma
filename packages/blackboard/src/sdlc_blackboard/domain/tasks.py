"""Task contract aggregate + state machine (handoff §6, Appendix B)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import (
    ActorKind,
    ArtifactBinding,
    DomainModel,
    NonEmptyStr,
)


class TaskState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    REVISION_REQUIRED = "revision_required"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


#: Terminal states — no outbound transitions except SUPERSEDED bookkeeping.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.ACCEPTED, TaskState.FAILED, TaskState.CANCELLED, TaskState.SUPERSEDED}
)


class DeliverableSpec(DomainModel):
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    required: bool = True


class ReviewRequirement(DomainModel):
    reviewer_kind: ActorKind
    review_type: NonEmptyStr
    blocking: bool = True


class TaskContractCreate(DomainModel):
    goal_id: UUID
    task_key: NonEmptyStr
    title: NonEmptyStr
    objective: NonEmptyStr
    required_actor_kind: ActorKind
    scope: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...] = ()
    inputs: tuple[ArtifactBinding, ...] = ()
    deliverables: tuple[DeliverableSpec, ...]
    acceptance_criteria: tuple[NonEmptyStr, ...]
    dependency_task_ids: tuple[UUID, ...] = ()
    review_requirements: tuple[ReviewRequirement, ...] = ()
    may_create_blocking_finding: bool = False
    may_modify_repository: bool = False


class Task(DomainModel):
    task_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    task_key: NonEmptyStr
    title: NonEmptyStr
    objective: NonEmptyStr
    required_actor_kind: ActorKind
    state: TaskState
    version: int
    assignment_epoch: int
    assigned_actor_id: str | None = None
    omnigent_conversation_id: str | None = None
    contract: TaskContractCreate
