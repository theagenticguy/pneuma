"""Goal aggregate — the top-level organizational objective (handoff §6)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import ActorRef, DomainModel, NonEmptyStr


class GoalState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    BLOCKED = "blocked"
    SATISFIED = "satisfied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalCreate(DomainModel):
    title: NonEmptyStr
    objective: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...] = ()
    owner: ActorRef


class Goal(DomainModel):
    goal_id: UUID = Field(default_factory=uuid4)
    title: NonEmptyStr
    objective: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...]
    owner: ActorRef
    state: GoalState
    version: int
