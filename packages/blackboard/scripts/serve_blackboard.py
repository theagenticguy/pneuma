"""Boot the blackboard MCP server on an explicit host/port (live-run launcher).

``fastmcp run`` defaults the port to 8000 regardless of the CLI flag in this
environment (8000 is occupied by another MCP server), so we call mcp.run() directly
with the port passed through — the programmatic form research-fastmcp.yaml verified.
"""

from __future__ import annotations

import os

from sdlc_blackboard.interfaces.mcp.server import mcp

if __name__ == "__main__":
    port = int(os.environ.get("BLACKBOARD_MCP_PORT", "8010"))
    host = os.environ.get("BLACKBOARD_HOST", "127.0.0.1")
    mcp.run(transport="http", host=host, port=port)
