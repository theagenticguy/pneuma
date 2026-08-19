"""Opt-in live acceptance smoke against a running blackboard MCP server.

SKIPPED by default. To run it the operator must:

    # 1. boot the server (in another shell)
    uv run python scripts/serve_blackboard.py
    # 2. opt in and run
    RUN_BEDROCK_ACCEPTANCE=1 uv run pytest tests/acceptance -q

This is the cheapest meaningful assertion of the LIVE stack contract shape — the same
thing the ``scripts/live_*_create_goal.py`` launchers prove, minus the full task graph:
the /health route answers on ``BLACKBOARD_MCP_PORT``, and a ``create_goal`` →
``get_goal_snapshot`` roundtrip through the REAL MCP command tools (thin adapter +
asyncpg + Postgres, no direct service calls) returns a structured ``CommandResult`` whose
value survives a read-back. It is a real test that CAN pass locally when opted in, not a
placeholder.
"""

from __future__ import annotations

import os
import uuid

import pytest

RUN_LIVE = os.environ.get("RUN_BEDROCK_ACCEPTANCE") == "1"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="live acceptance: set RUN_BEDROCK_ACCEPTANCE=1 with a running blackboard server",
    ),
]

_PORT = os.environ.get("BLACKBOARD_MCP_PORT", "8010")
_HOST = os.environ.get("BLACKBOARD_HOST", "127.0.0.1")
_BASE = f"http://{_HOST}:{_PORT}"
_MCP_URL = os.environ.get("BLACKBOARD_MCP_URL", f"{_BASE}/mcp/")


async def test_health_route_reachable() -> None:
    """The /health custom route answers 200 {"status": "ok"} on the configured port."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{_BASE}/health", timeout=5.0)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_create_goal_roundtrips_through_live_mcp_tools() -> None:
    """A goal created via the live create_goal tool reads back via get_goal_snapshot.

    Exercises the real thin adapter end-to-end: MCP transport -> CommandContext envelope
    -> application service -> asyncpg -> Postgres, and the read side back.
    """
    from fastmcp import Client

    human = {"actor_id": "acceptance-smoke", "kind": "human"}
    title = f"acceptance smoke {uuid.uuid4()}"

    async with Client(_MCP_URL) as client:
        created = await client.call_tool(
            "create_goal",
            {
                "command": {"command_id": str(uuid.uuid4()), "actor": human},
                "goal": {
                    "title": title,
                    "objective": "smoke the live stack",
                    "success_criteria": ["server answers", "goal reads back"],
                },
            },
        )
        payload = created.structured_content
        assert payload is not None, f"no structured_content: {created}"
        assert payload.get("error") is None, payload["error"]
        assert payload["status"] == "accepted"
        goal_value = payload["value"]
        assert goal_value["title"] == title
        goal_id = goal_value["goal_id"]

        snapshot = await client.call_tool("get_goal_snapshot", {"goal_id": goal_id})
        snap = snapshot.structured_content
        assert snap is not None
        assert snap["goal"]["goal_id"] == goal_id
        assert snap["goal"]["state"] == "active"
