"""MCP read tools (handoff §14): inspect authoritative state. No mutation, no SQL.

Each tool is a thin translation to one QueryService/GateService call.
"""

from __future__ import annotations

from uuid import UUID

from fastmcp import Context
from fastmcp.exceptions import ToolError

from sdlc_blackboard.application.query_models import GoalSnapshot
from sdlc_blackboard.domain.artifacts import ArtifactRevision
from sdlc_blackboard.domain.events import GateResult, TeamEvent
from sdlc_blackboard.interfaces.mcp.server import mcp, services_from


@mcp.tool
async def get_goal_snapshot(goal_id: UUID, ctx: Context) -> GoalSnapshot:
    """Return a compact snapshot of a goal: its tasks, current artifact aliases,
    open findings, reviews, approvals, and ready task ids. Read-only."""
    snapshot = await services_from(ctx).query.goal_snapshot(goal_id)
    if snapshot is None:
        raise ToolError(f"goal {goal_id} not found")
    return snapshot


@mcp.tool
async def get_task_contract(goal_id: UUID, task_id: UUID, ctx: Context) -> dict[str, object]:
    """Return the exact task contract (objective, scope, deliverables, acceptance
    criteria, review requirements, epoch) for one task. Read-only."""
    snapshot = await services_from(ctx).query.goal_snapshot(goal_id)
    if snapshot is None:
        raise ToolError(f"goal {goal_id} not found")
    for task in snapshot.tasks:
        if task.task_id == task_id:
            return task.model_dump(mode="json")
    raise ToolError(f"task {task_id} not found in goal {goal_id}")


@mcp.tool
async def get_artifact_revision(revision_id: UUID, ctx: Context) -> ArtifactRevision:
    """Return one immutable artifact revision by id. Read-only."""
    revision = await services_from(ctx).query.get_artifact_revision(revision_id)
    if revision is None:
        raise ToolError(f"artifact revision {revision_id} not found")
    return revision


@mcp.tool
async def read_relevant_events(
    goal_id: UUID, ctx: Context, limit: int = 100
) -> tuple[TeamEvent, ...]:
    """Return the goal's event log in occurrence order (keyset-paginated). Read-only."""
    return await services_from(ctx).query.read_relevant_events(goal_id, limit=limit)


@mcp.tool
async def get_gate_status(goal_id: UUID, ctx: Context) -> GateResult:
    """Evaluate the release gate for a goal: current implementation binding, missing
    or stale reviews, open blocking findings, and missing approvals. Read-only."""
    return await services_from(ctx).gate.get_gate_status(goal_id)
