"""MCP atomic command tools (handoff §14): coarse-grained domain intentions.

Every tool is a thin application-service call. No state transitions, no SQL, no
in-memory authority — the kernel owns semantics. Each mutation carries a
``CommandContext`` (idempotency + optimistic version + fencing epoch) and returns a
structured ``CommandResult`` so callers never infer success from prose.

Multiple Pydantic-model parameters arrive as named JSON keys (fastmcp 3.4.4), e.g.
``create_task`` is invoked with ``{"command": {...}, "task": {...}}``.
"""

from __future__ import annotations

from uuid import UUID

from fastmcp import Context

from sdlc_blackboard.application.commands import (
    AcceptTaskRequest,
    BindRuntimeSessionRequest,
    ClaimTaskRequest,
    PromoteArtifactRequest,
    RefreshReadyTasksRequest,
    ResolveFindingRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.receipts import (
    ClaimReceipt,
    TaskListReceipt,
    TaskSubmissionReceipt,
)
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.domain.approvals import Approval, ApprovalSubmission
from sdlc_blackboard.domain.artifacts import ArtifactAlias
from sdlc_blackboard.domain.common import CommandContext
from sdlc_blackboard.domain.events import RuntimeRun
from sdlc_blackboard.domain.findings import Finding, FindingCreate
from sdlc_blackboard.domain.goals import Goal, GoalCreate
from sdlc_blackboard.domain.reviews import Review, ReviewSubmission
from sdlc_blackboard.domain.tasks import Task, TaskContractCreate
from sdlc_blackboard.interfaces.mcp.server import mcp, services_from


@mcp.tool
async def create_goal(
    command: CommandContext, goal: GoalCreate, ctx: Context
) -> CommandResult[Goal]:
    """Create a goal with explicit success criteria and constraints. Idempotent by
    command.command_id."""
    return await services_from(ctx).goals.create_goal(command, goal)


@mcp.tool
async def create_task(
    command: CommandContext, task: TaskContractCreate, ctx: Context
) -> CommandResult[Task]:
    """Create one bounded task contract. The goal must exist and dependencies must
    belong to it. Idempotent by command.command_id; a same-key task returns only when
    the contract matches, else a conflict."""
    return await services_from(ctx).tasks.create_task(command, task)


@mcp.tool
async def refresh_ready_tasks(
    command: CommandContext, request: RefreshReadyTasksRequest, ctx: Context
) -> CommandResult[TaskListReceipt]:
    """Promote draft tasks whose dependencies are all accepted to READY. Returns the
    newly-ready tasks. Idempotent and safe to re-run."""
    return await services_from(ctx).tasks.refresh_ready_tasks(command, request)


@mcp.tool
async def claim_task(
    command: CommandContext, request: ClaimTaskRequest, ctx: Context
) -> CommandResult[ClaimReceipt]:
    """Atomically assign one READY task and return the fencing epoch. Later worker
    mutations must carry that epoch. Concurrent or stale claims return a structured
    conflict; the database partial unique index is the final defense."""
    return await services_from(ctx).tasks.claim_task(command, request)


@mcp.tool
async def bind_runtime_session(
    command: CommandContext, request: BindRuntimeSessionRequest, ctx: Context
) -> CommandResult[Task]:
    """Bind an Omnigent child conversation id to the current assignment. Validates the
    assignment epoch; a stale assignment returns a structured conflict."""
    return await services_from(ctx).tasks.bind_runtime_session(command, request)


@mcp.tool
async def start_runtime_run(
    command: CommandContext, request: StartRunRequest, ctx: Context
) -> CommandResult[RuntimeRun]:
    """Start one runtime execution attempt for an assigned task. Validates epoch +
    actor, transitions the task to RUNNING, and records model provenance."""
    return await services_from(ctx).tasks.start_runtime_run(command, request)


@mcp.tool
async def submit_task_result(
    command: CommandContext, request: SubmitTaskResult, ctx: Context
) -> CommandResult[TaskSubmissionReceipt]:
    """Atomically submit a task result: insert immutable artifact revisions, complete
    the run + assignment, transition the task to SUBMITTED, and create/reopen the
    required review tasks. Validates epoch, actor, active run, and input manifest."""
    return await services_from(ctx).tasks.submit_task_result(command, request)


@mcp.tool
async def accept_task(
    command: CommandContext, request: AcceptTaskRequest, ctx: Context
) -> CommandResult[Task]:
    """Accept a SUBMITTED producer task, advancing it SUBMITTED -> UNDER_REVIEW ->
    ACCEPTED through the legal transition matrix. Call this after verifying the
    deliverable against its acceptance criteria; it lets the release gate see an
    accepted binding without the lead applying the transition at the store. Idempotent:
    an already-ACCEPTED task returns unchanged, and a partially-advanced (UNDER_REVIEW)
    task resumes to ACCEPTED."""
    return await services_from(ctx).tasks.accept_task(command, request)


@mcp.tool
async def open_finding(
    command: CommandContext, finding: FindingCreate, ctx: Context
) -> CommandResult[Finding]:
    """Open a finding against artifact revisions. A blocking finding requires a task
    whose contract permits it (quality/security reviewer). The assertion is immutable;
    its resolution state is versioned."""
    return await services_from(ctx).reviews.open_finding(command, finding)


@mcp.tool
async def resolve_finding(
    command: CommandContext, request: ResolveFindingRequest, ctx: Context
) -> CommandResult[Finding]:
    """Transition a finding's resolution state (e.g. remediated, verified,
    accepted_risk) via optimistic version. Idempotent by command.command_id."""
    return await services_from(ctx).reviews.resolve_finding(command, request)


@mcp.tool
async def submit_review(
    command: CommandContext, review: ReviewSubmission, ctx: Context
) -> CommandResult[Review]:
    """Submit a review bound to exact artifact revisions. Unique by reviewer, type, and
    binding fingerprint. An approved review may not carry an unresolved blocking
    finding it created."""
    return await services_from(ctx).reviews.submit_review(command, review)


@mcp.tool
async def promote_artifact(
    command: CommandContext, request: PromoteArtifactRequest, ctx: Context
) -> CommandResult[ArtifactAlias]:
    """Promote the current alias for a logical artifact to a new revision via
    compare-and-set. Reviews and approvals bound to the superseded revision become
    stale/revoked in the same transaction."""
    return await services_from(ctx).artifacts.promote_artifact(command, request)


@mcp.tool
async def record_human_approval(
    command: CommandContext, approval: ApprovalSubmission, ctx: Context
) -> CommandResult[Approval]:
    """Record a human approval bound to exact artifact revisions. Immutable and
    revision-bound; the gate requires a non-revoked approval for the current binding."""
    return await services_from(ctx).reviews.record_human_approval(command, approval)


@mcp.tool
async def authorize_goal_completion(
    command: CommandContext, goal_id: UUID, ctx: Context
) -> CommandResult[Goal]:
    """Mark a goal SATISFIED. The caller must first confirm the gate is satisfied via
    get_gate_status; this transitions goal state via optimistic version."""
    return await services_from(ctx).goals.authorize_goal_completion(command, goal_id)
