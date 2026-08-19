"""Thin FastMCP server — the driving adapter (handoff §14, hexagonal §1).

Interfaces translate transport <-> use case and hold NO logic. The lifespan builds
the process-lifetime DI container (asyncpg pool + Services); every tool resolves the
Services facade from ``ctx.lifespan_context`` and delegates to one application call.

Grounded against fastmcp 3.4.4 (research-fastmcp.yaml):
- lifespan receives the FastMCP instance and yields a dict;
- tools read it via ``ctx.lifespan_context`` (canonical, null-safe path);
- ``@mcp.custom_route`` registers the /health route;
- the server binds to loopback (HANDOFF §3, §24).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from sdlc_blackboard.application.use_cases.services import Services
from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.infrastructure.di import Container, build_container
from sdlc_blackboard.infrastructure.logging import configure_logging

if TYPE_CHECKING:
    from fastmcp import Context

#: Key under which the DI container is stashed in the lifespan context dict.
CONTAINER_KEY = "container"

_log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncGenerator[dict[str, Container]]:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    _log.info("server.starting", env=settings.env, host=settings.host, port=settings.port)
    container = await build_container(settings)
    _log.info("server.started")
    try:
        yield {CONTAINER_KEY: container}
    finally:
        await container.postgres.stop()
        _log.info("server.stopped")


def services_from(ctx: Context) -> Services:
    """Resolve the Services facade from the lifespan context (thin-adapter helper)."""
    container: Container = ctx.lifespan_context[CONTAINER_KEY]
    return container.services


mcp: FastMCP = FastMCP(
    name="SDLC Blackboard",
    instructions=(
        "Transactional organizational blackboard. Use read tools to inspect state and "
        "atomic command tools to mutate it. Never infer success from prose — the "
        "structured CommandResult is authoritative."
    ),
    lifespan=lifespan,
    mask_error_details=True,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _register_tools() -> None:
    """Import the tool modules for their @mcp.tool registration side effects.

    Called at import time below. Kept as a function so the imports are an explicit,
    acknowledged side effect rather than apparently-unused module-level imports.
    """
    from sdlc_blackboard.interfaces.mcp import tools_commands, tools_read

    _ = (tools_commands, tools_read)


_register_tools()
