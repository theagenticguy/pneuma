"""Input DTOs for command use cases (handoff §11).

One request model per mutating use case. These are the payloads MCP command tools
accept (alongside the ``CommandContext`` envelope). They stay pure Pydantic and
never reference infrastructure types.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field

from sdlc_blackboard.domain.artifacts import ArtifactSubmission
from sdlc_blackboard.domain.common import ArtifactBinding, DomainModel, NonEmptyStr
from sdlc_blackboard.domain.findings import FindingState

#: Collection-count cap for the model-supplied (prompt-injectable) result-manifest and
#: manifest tuples. Bounds the jsonb write / INSERT-loop fan-out so a single command
#: cannot amplify storage or work without bound (SEC-01/SEC-02). Elements are already
#: NonEmptyStr-capped (10k each), so this caps count; 50 is well above any real submission.
_MAX_COLLECTION = 50


class ClaimTaskRequest(DomainModel):
    task_id: UUID
    actor_id: NonEmptyStr


class BindRuntimeSessionRequest(DomainModel):
    task_id: UUID
    omnigent_conversation_id: NonEmptyStr


class StartRunRequest(DomainModel):
    task_id: UUID
    omnigent_conversation_id: NonEmptyStr
    input_manifest: Annotated[tuple[ArtifactBinding, ...], Field(max_length=_MAX_COLLECTION)] = ()
    # Model provenance (handoff §15A.8) — recorded on the runtime run.
    provider: str | None = None
    model_id: str | None = None
    aws_region: str | None = None
    routing_class: str | None = None
    harness: str | None = None


class SubmitTaskResult(DomainModel):
    task_id: UUID
    run_id: UUID
    disposition: NonEmptyStr
    input_manifest: Annotated[tuple[ArtifactBinding, ...], Field(max_length=_MAX_COLLECTION)]
    artifacts: Annotated[tuple[ArtifactSubmission, ...], Field(max_length=_MAX_COLLECTION)]
    finding_ids: Annotated[tuple[UUID, ...], Field(max_length=_MAX_COLLECTION)] = ()
    # Model-supplied free text (prompt-injectable) — cap element size (NonEmptyStr, 10k)
    # AND collection count so an unbounded jsonb write cannot amplify storage (SEC-01).
    assumptions: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    unresolved_questions: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    residual_risks: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    summary: NonEmptyStr


class ResolveFindingRequest(DomainModel):
    finding_id: UUID
    new_state: FindingState
    note: str | None = None


class PromoteArtifactRequest(DomainModel):
    goal_id: UUID
    logical_name: NonEmptyStr
    expected_current_revision_id: UUID | None = None
    new_revision_id: UUID


class RefreshReadyTasksRequest(DomainModel):
    goal_id: UUID


class AcceptTaskRequest(DomainModel):
    """Accept a SUBMITTED producer task, advancing it to ACCEPTED.

    The lead calls this after it has verified the submitted deliverable against the
    task's acceptance criteria. It drives the two legal transitions SUBMITTED ->
    UNDER_REVIEW -> ACCEPTED so the release gate can see an accepted binding, without
    the lead reaching into the store directly (the finalize-wedge fix)."""

    task_id: UUID
