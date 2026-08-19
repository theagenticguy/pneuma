"""Finding aggregate (handoff §6).

A finding assertion is immutable; its resolution state is versioned. A blocking
finding prevents the release gate from being satisfied until it is remediated or
accepted-as-risk.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import (
    ArtifactBinding,
    DomainModel,
    EvidenceRef,
    NonEmptyStr,
)


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    REMEDIATED = "remediated"
    VERIFIED = "verified"
    ACCEPTED_RISK = "accepted_risk"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


#: A blocking finding in one of these states no longer blocks the gate.
RESOLVED_FINDING_STATES: frozenset[FindingState] = frozenset(
    {
        FindingState.VERIFIED,
        FindingState.ACCEPTED_RISK,
        FindingState.REJECTED,
        FindingState.SUPERSEDED,
    }
)


class FindingCreate(DomainModel):
    goal_id: UUID
    task_id: UUID
    category: NonEmptyStr
    severity: FindingSeverity
    statement: NonEmptyStr
    affected_artifacts: tuple[ArtifactBinding, ...]
    evidence: tuple[EvidenceRef, ...]
    blocking: bool
    resolution_criteria: tuple[NonEmptyStr, ...]


class Finding(DomainModel):
    finding_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    task_id: UUID
    category: NonEmptyStr
    severity: FindingSeverity
    statement: NonEmptyStr
    affected_artifacts: tuple[ArtifactBinding, ...]
    evidence: tuple[EvidenceRef, ...]
    blocking: bool
    resolution_criteria: tuple[NonEmptyStr, ...]
    state: FindingState
    version: int
