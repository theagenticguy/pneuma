"""bb — a thin blackboard MCP CLI for specialist agents (live run).

Every call goes through the real MCP tool surface at $BLACKBOARD_MCP_URL. Specialists
use this to start a runtime run, submit an artifact, open a finding, or submit a review
without hand-rolling an MCP client. JSON in, JSON out.

Usage:
  uv run python scripts/bb.py <tool> '<json-args>'
  uv run python scripts/bb.py get_task_contract '{"goal_id":"...","task_id":"..."}'
  uv run python scripts/bb.py claim_task '{"command":{...},"request":{...}}'

The single positional JSON is the tool's argument object exactly as the MCP tool
expects (command envelope + payload). Prints the structured_content as JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from fastmcp import Client

MCP_URL = os.environ.get("BLACKBOARD_MCP_URL", "http://127.0.0.1:8010/mcp/")


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: bb.py <tool> '<json-args>'", file=sys.stderr)
        raise SystemExit(2)
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    async with Client(MCP_URL) as c:
        result = await c.call_tool(tool, args)
        out = result.structured_content
        print(json.dumps(out, indent=2, default=str))
        # Non-zero exit if the command returned a structured error.
        if isinstance(out, dict) and out.get("error"):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
