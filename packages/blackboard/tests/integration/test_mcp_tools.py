"""MCP integration tests via the FastMCP in-memory client (handoff §21).

Drives the thin adapter end-to-end: tool registration, create/read roundtrip,
idempotent replay, and the structured-conflict path — all through the MCP surface.

Uses ``result.structured_content`` (a plain dict) for assertions, since ``.data`` is
a client-side schema-rebuilt model, not the server class (research-fastmcp.yaml).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.infrastructure.di import build_container
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")

MCPClient = Client[FastMCPTransport]


@pytest_asyncio.fixture(loop_scope="function")
async def mcp_client(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MCPClient]:
    # Point the server lifespan's build_container() at the test DB.
    monkeypatch.setenv("BLACKBOARD_DATABASE_URL", pg_dsn)
    # Reset state so assertions are deterministic.
    container = await build_container(Settings(database_url=pg_dsn))
    async with container.postgres.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    await container.postgres.stop()

    from sdlc_blackboard.interfaces.mcp.server import mcp

    async with Client(mcp) as client:
        yield client


def _cmd(actor_id: str = "human-1") -> dict[str, object]:
    return {"command_id": str(uuid.uuid4()), "actor": {"actor_id": actor_id, "kind": "human"}}


def _goal() -> dict[str, object]:
    return {
        "title": "t",
        "objective": "o",
        "success_criteria": ["a"],
        "owner": {"actor_id": "human-1", "kind": "human"},
    }


async def test_tool_surface_registered(mcp_client: MCPClient) -> None:
    tools = await mcp_client.list_tools()
    names = {t.name for t in tools}
    # 5 read + 14 command tools (handoff §14; accept_task added to close the finalize wedge).
    assert "create_goal" in names
    assert "get_goal_snapshot" in names
    assert "get_gate_status" in names
    assert "submit_task_result" in names
    assert "accept_task" in names
    assert len(names) == 19


async def test_create_and_read_goal(mcp_client: MCPClient) -> None:
    created = await mcp_client.call_tool("create_goal", {"command": _cmd(), "goal": _goal()})
    assert created.structured_content is not None
    assert created.structured_content["status"] == "accepted"
    goal_id = created.structured_content["value"]["goal_id"]

    snapshot = await mcp_client.call_tool("get_goal_snapshot", {"goal_id": goal_id})
    assert snapshot.structured_content is not None
    assert snapshot.structured_content["goal"]["goal_id"] == goal_id
    assert snapshot.structured_content["goal"]["state"] == "active"


async def test_idempotent_replay_through_mcp(mcp_client: MCPClient) -> None:
    args = {"command": _cmd(), "goal": _goal()}
    first = await mcp_client.call_tool("create_goal", args)
    second = await mcp_client.call_tool("create_goal", args)
    assert first.structured_content is not None
    assert second.structured_content is not None
    assert second.structured_content["replayed"] is True
    assert (
        first.structured_content["value"]["goal_id"]
        == second.structured_content["value"]["goal_id"]
    )


async def test_reused_command_id_different_payload_rejected(mcp_client: MCPClient) -> None:
    cmd = _cmd()
    a = await mcp_client.call_tool(
        "create_goal", {"command": cmd, "goal": {**_goal(), "title": "A"}}
    )
    b = await mcp_client.call_tool(
        "create_goal", {"command": cmd, "goal": {**_goal(), "title": "B"}}
    )
    assert a.structured_content is not None
    assert b.structured_content is not None
    assert b.structured_content["error"]["code"] == "duplicate_command_mismatch"
