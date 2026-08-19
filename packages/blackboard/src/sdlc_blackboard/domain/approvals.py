"""Approval + decision aggregates (handoff §6, §11).

Approvals are immutable and bind an exact artifact revision. The release gate
requires a non-revoked human approval whose binding matches the current
implementation revision.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import (
    ActorRef,
    ArtifactBinding,
    DomainModel,
    EvidenceRef,
    NonEmptyStr,
)


class ApprovalType(StrEnum):
    HUMAN_RELEASE = "human_release"


class ApprovalSubmission(DomainModel):
    goal_id: UUID
    approval_type: ApprovalType
    approver: ActorRef
    artifact_bindings: tuple[ArtifactBinding, ...]
    conditions: tuple[NonEmptyStr, ...] = ()


class Approval(DomainModel):
    approval_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    approval_type: ApprovalType
    approver: ActorRef
    binding_fingerprint: NonEmptyStr
    artifact_bindings: tuple[ArtifactBinding, ...]
    conditions: tuple[NonEmptyStr, ...]
    revoked: bool = False


class Decision(DomainModel):
    decision_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    question: NonEmptyStr
    selected_option: NonEmptyStr
    rationale: NonEmptyStr
    evidence: tuple[EvidenceRef, ...]
    decided_by: ActorRef
    affected_artifacts: tuple[ArtifactBinding, ...]
    supersedes: tuple[UUID, ...] = ()
