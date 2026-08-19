"""Artifact revision aggregate (handoff §6).

Revisions are immutable; only the alias pointer (``artifact_aliases``) is mutable,
promoted by compare-and-set. A revision is uniquely keyed by (artifact_id, content_hash).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import DomainModel, EvidenceRef, NonEmptyStr


class ArtifactStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class ArtifactSubmission(DomainModel):
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    content_uri: NonEmptyStr
    content_hash: NonEmptyStr
    summary: NonEmptyStr
    evidence: tuple[EvidenceRef, ...] = ()
    parent_revision_ids: tuple[UUID, ...] = ()


class ArtifactRevision(DomainModel):
    artifact_id: UUID
    revision_id: UUID = Field(default_factory=uuid4)
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    content_uri: NonEmptyStr
    content_hash: NonEmptyStr
    summary: NonEmptyStr
    produced_by_task_id: UUID
    produced_by_run_id: UUID
    parent_revision_ids: tuple[UUID, ...]
    evidence: tuple[EvidenceRef, ...] = ()
    status: ArtifactStatus


class ArtifactAlias(DomainModel):
    """Mutable pointer from a logical name to its current revision (CAS-promoted)."""

    goal_id: UUID
    logical_name: NonEmptyStr
    current_revision_id: UUID
    version: int
