"""E2E tier: same Docker + dbmate guard as integration, plus a built Container."""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.infrastructure.di import Container, build_container
from sdlc_blackboard.infrastructure.migrations import run_dbmate


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True


E2E_READY = (
    importlib.util.find_spec("testcontainers") is not None
    and shutil.which("dbmate") is not None
    and _docker_available()
)

pytestmark = pytest.mark.skipif(not E2E_READY, reason="needs Docker + dbmate")


@pytest.fixture(scope="session")
def e2e_dsn() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18-alpine", driver=None) as pg:
        dsn = pg.get_connection_url()
        run_dbmate(dsn)
        yield dsn


@pytest_asyncio.fixture(loop_scope="function")
async def container(e2e_dsn: str) -> AsyncIterator[Container]:
    c = await build_container(Settings(database_url=e2e_dsn))
    async with c.postgres.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    try:
        yield c
    finally:
        await c.postgres.stop()
