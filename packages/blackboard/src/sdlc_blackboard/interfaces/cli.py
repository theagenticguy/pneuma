"""Developer CLI (handoff §20). Typer, for operators — never agent access.

Destructive maintenance (reset-demo) lives here, never on the MCP surface.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import orjson
import structlog
import typer

from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.infrastructure.di import build_container
from sdlc_blackboard.infrastructure.logging import configure_logging
from sdlc_blackboard.infrastructure.migrations import run_dbmate

app = typer.Typer(no_args_is_help=True, help="SDLC Blackboard developer CLI")


@app.callback()
def _configure() -> None:
    """Wire structlog for the operator CLI (pretty console output) before any command."""
    configure_logging(Settings().log_level, "console")


@app.command()
def migrate() -> None:
    """Apply pending SQL migrations via dbmate."""
    run_dbmate(Settings().database_url)
    typer.echo("migrations applied")


@app.command("list-goals")
def list_goals() -> None:
    """List all goals (id, title, state)."""

    async def _run() -> None:
        container = await build_container()
        try:
            goals = await container.services.query.list_goals()
            for g in goals:
                typer.echo(f"{g.goal_id}  {g.state.value:<10}  {g.title}")
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command()
def snapshot(goal_id: UUID) -> None:
    """Print the goal snapshot as JSON."""

    async def _run() -> None:
        container = await build_container()
        try:
            snap = await container.services.query.goal_snapshot(goal_id)
            if snap is None:
                typer.echo(f"goal {goal_id} not found")
                raise typer.Exit(1)
            payload = orjson.dumps(snap.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
            typer.echo(payload.decode())
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command()
def events(goal_id: UUID) -> None:
    """Print the goal's event trace in order."""

    async def _run() -> None:
        container = await build_container()
        try:
            evts = await container.services.query.read_relevant_events(goal_id, limit=500)
            for e in evts:
                typer.echo(f"{e.event_type}")
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command()
def gate(goal_id: UUID) -> None:
    """Print the release-gate status as JSON."""

    async def _run() -> None:
        container = await build_container()
        try:
            result = await container.services.gate.get_gate_status(goal_id)
            typer.echo(
                orjson.dumps(result.model_dump(mode="json"), option=orjson.OPT_INDENT_2).decode()
            )
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command()
def thrash(goal_id: UUID) -> None:
    """Print the goal's coordination-thrash report as JSON (operator-only, spec T4).

    Read-only derived counters — conflicts, stale versions, review rejections, reclaims
    — so operators can detect swarm thrash before gate time. Deliberately NOT an MCP
    tool: agents must not be able to read (and game) their own thrash metric."""

    async def _run() -> None:
        container = await build_container()
        try:
            report = await container.services.thrash.get_thrash_report(goal_id)
            typer.echo(
                orjson.dumps(report.model_dump(mode="json"), option=orjson.OPT_INDENT_2).decode()
            )
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command("outbox-relay")
def outbox_relay(
    batch_size: int = typer.Option(100, help="Max unpublished rows to drain per pass."),
    loop: bool = typer.Option(
        False,
        "--loop/--once",
        help="Poll forever (--loop) or drain a single batch and exit (--once, default).",
    ),
    interval: float = typer.Option(2.0, help="Seconds between passes when --loop."),
) -> None:
    """Drain the transactional outbox: publish (structured-log) pending events and
    mark them published (handoff §12). ``--once`` (default) drains one batch and exits;
    ``--loop`` polls every ``--interval`` seconds until interrupted."""

    async def _run() -> None:
        log = structlog.get_logger()
        container = await build_container()
        try:
            while True:
                drained = await container.services.outbox.drain_outbox(batch_size)
                typer.echo(f"drained {drained} outbox row(s)")
                if not loop:
                    break
                if drained == 0:
                    await asyncio.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - operator ctrl-c on --loop
            log.info("outbox.relay_stopped")
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


@app.command("reset-demo")
def reset_demo() -> None:
    """Truncate all domain state (destructive — CLI only, never on MCP)."""

    async def _run() -> None:
        container = await build_container()
        try:
            async with container.postgres.transaction() as conn:
                await conn.execute(
                    "truncate goals, processed_commands, outbox, team_events, "
                    "command_failures restart identity cascade"
                )
            typer.echo("demo state reset")
        finally:
            await container.postgres.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
