"""Team event + runtime run + gate result domain types (handoff §6, §11, §12).

Events are an immutable append-only log. Runtime runs are execution attempts (one
task can have many). The gate result is a derived read, never a stored boolean.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from sdlc_blackboard.domain.common import ActorRef, ArtifactBinding, DomainModel, NonEmptyStr


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUBMITTED = "submitted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RoutingClass(StrEnum):
    """Bedrock routing provenance (handoff §15A.8)."""

    GLOBAL_INFERENCE_PROFILE = "global_inference_profile"
    GEO_INFERENCE_PROFILE = "geo_inference_profile"
    IN_REGION_RUNTIME = "in_region_runtime"
    REGIONAL_MANTLE = "regional_mantle"


class RuntimeRun(DomainModel):
    run_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    assignment_epoch: int
    actor_id: NonEmptyStr
    omnigent_conversation_id: str | None = None
    state: RunState
    input_manifest: tuple[ArtifactBinding, ...]
    # Model provenance (handoff §15A.8) — required for cost/repro/failure analysis.
    provider: str | None = None
    model_id: str | None = None
    aws_region: str | None = None
    routing_class: RoutingClass | None = None
    harness: str | None = None


class TeamEvent(DomainModel):
    event_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    task_id: UUID | None
    aggregate_type: NonEmptyStr
    aggregate_id: UUID
    aggregate_version: int
    event_type: NonEmptyStr
    actor: ActorRef
    correlation_id: UUID
    causation_id: UUID | None
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    payload: dict[str, object] = Field(default_factory=dict)


class GateStatus(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    HUMAN_REQUIRED = "human_required"


class GateResult(DomainModel):
    status: GateStatus
    implementation_binding: ArtifactBinding | None = None
    missing_reviews: tuple[str, ...] = ()
    open_blocking_finding_ids: tuple[UUID, ...] = ()
    stale_review_ids: tuple[UUID, ...] = ()
    missing_approvals: tuple[str, ...] = ()
