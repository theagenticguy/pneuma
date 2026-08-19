"""Integration tier: skips cleanly when Docker + dbmate are absent, thorough in CI.

hexagonal-arch-stack.md §6: integration skips, never fails, when Docker is missing.
Provides a session-scoped Postgres container (migrated once) and a per-test pool.
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from sdlc_blackboard.infrastructure.migrations import run_dbmate
from sdlc_blackboard.infrastructure.postgres import Postgres


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True


INTEGRATION_READY = (
    importlib.util.find_spec("testcontainers") is not None
    and shutil.which("dbmate") is not None
    and _docker_available()
)

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    # driver=None -> plain postgresql:// URL (asyncpg + dbmate reject +psycopg2).
    with PostgresContainer("postgres:18-alpine", driver=None) as pg:
        dsn = pg.get_connection_url()
        run_dbmate(dsn)
        yield dsn


@pytest_asyncio.fixture(loop_scope="function")
async def postgres(pg_dsn: str) -> AsyncIterator[Postgres]:
    pg = Postgres(pg_dsn, min_size=1, max_size=8)
    await pg.start()
    # Clean slate per test: truncate all domain tables (goals cascade covers most).
    async with pg.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    try:
        yield pg
    finally:
        await pg.stop()
