"""Review aggregate + binding fingerprint (handoff §6, §11).

Reviews are immutable and bind exact artifact revisions. A review is unique by
(reviewer, review_type, binding). When the current artifact alias changes, reviews
bound to the superseded revision are marked stale.
"""

from __future__ import annotations

import hashlib
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


class ReviewDisposition(StrEnum):
    APPROVED = "approved"
    FINDINGS = "findings"
    REQUEST_REVISION = "request_revision"
    ABSTAINED = "abstained"


def binding_fingerprint(bindings: tuple[ArtifactBinding, ...]) -> str:
    """Order-independent fingerprint of a set of artifact bindings (handoff §11).

    Pure function: sorts the (artifact_id, revision_id, content_hash) triples so a
    review's identity is stable regardless of binding order, then SHA-256s them.
    """
    values = sorted(f"{b.artifact_id}:{b.revision_id}:{b.content_hash}" for b in bindings)
    return hashlib.sha256("|".join(values).encode()).hexdigest()


def single_binding_fingerprint(binding: ArtifactBinding) -> str:
    """Fingerprint of a single binding — the one-element case of ``binding_fingerprint``.

    The gate compares an individual review/approval binding against the current
    implementation binding; this is exactly ``binding_fingerprint((binding,))`` and
    keeps the gate on the same order-independent SHA-256 identity the reviews and
    approvals tables persist, rather than an ad-hoc string.
    """
    return binding_fingerprint((binding,))


class ReviewSubmission(DomainModel):
    goal_id: UUID
    review_task_id: UUID
    reviewer: ActorRef
    review_type: NonEmptyStr
    artifact_bindings: tuple[ArtifactBinding, ...]
    disposition: ReviewDisposition
    summary: NonEmptyStr
    evidence: tuple[EvidenceRef, ...] = ()
    finding_ids: tuple[UUID, ...] = ()


class Review(DomainModel):
    review_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    review_task_id: UUID
    reviewer: ActorRef
    review_type: NonEmptyStr
    binding_fingerprint: NonEmptyStr
    artifact_bindings: tuple[ArtifactBinding, ...]
    disposition: ReviewDisposition
    summary: NonEmptyStr
    evidence: tuple[EvidenceRef, ...]
    finding_ids: tuple[UUID, ...]
    stale: bool = False
