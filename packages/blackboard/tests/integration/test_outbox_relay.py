"""Outbox relay integration proof against real Postgres (handoff §12).

The transactional outbox accumulates a row per domain event; nothing drained it
until the ``blackboard outbox-relay`` command. These tests prove the consumer:
pending rows go published, published rows are not re-claimed, the attempts counter
is bumped on claim, and the SKIP-LOCKED claim is safe under concurrent relays.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from sdlc_blackboard.domain.common import ActorKind, ActorRef, CommandContext
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.infrastructure.di import Container, build_container
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)


def _ctx() -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=HUMAN, assignment_epoch=None)


@pytest_asyncio.fixture(loop_scope="function")
async def container(pg_dsn: str) -> AsyncIterator[Container]:
    from sdlc_blackboard.domain.settings import Settings

    c = await build_container(Settings(database_url=pg_dsn))
    async with c.postgres.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    try:
        yield c
    finally:
        await c.postgres.stop()


async def _make_events(c: Container, n: int) -> None:
    """Each create_goal command appends one team_event + one outbox row (§12)."""
    for i in range(n):
        result = await c.services.goals.create_goal(
            _ctx(),
            GoalCreate(title=f"g{i}", objective="o", success_criteria=("a",), owner=HUMAN),
        )
        assert result.value is not None


async def _pending_count(c: Container) -> int:
    async with c.postgres.connection() as conn:
        row = await conn.fetchrow("select count(*) as n from outbox where published_at is null")
    assert row is not None
    return int(row["n"])


async def test_drain_publishes_pending_rows(container: Container) -> None:
    await _make_events(container, 3)
    assert await _pending_count(container) == 3

    drained = await container.services.outbox.drain_outbox(limit=100)
    assert drained == 3
    assert await _pending_count(container) == 0


async def test_published_rows_not_reclaimed(container: Container) -> None:
    await _make_events(container, 2)

    first = await container.services.outbox.drain_outbox(limit=100)
    assert first == 2
    # A second pass finds nothing: published_at is set, so the partial index /
    # WHERE published_at is null clause excludes them.
    second = await container.services.outbox.drain_outbox(limit=100)
    assert second == 0


async def test_drain_bumps_attempts_on_claim(container: Container) -> None:
    await _make_events(container, 1)
    drained = await container.services.outbox.drain_outbox(limit=100)
    assert drained == 1
    async with container.postgres.connection() as conn:
        row = await conn.fetchrow("select attempts, published_at from outbox limit 1")
    assert row is not None
    assert row["attempts"] == 1
    assert row["published_at"] is not None


async def test_batch_size_limits_claim(container: Container) -> None:
    await _make_events(container, 5)
    drained = await container.services.outbox.drain_outbox(limit=2)
    assert drained == 2
    assert await _pending_count(container) == 3


async def test_concurrent_relays_do_not_double_publish(container: Container) -> None:
    import asyncio

    await _make_events(container, 6)
    # Two relays racing on the same pending set: FOR UPDATE SKIP LOCKED partitions the
    # rows between them, so their drained counts sum to exactly the pending total with
    # no row published twice.
    a, b = await asyncio.gather(
        container.services.outbox.drain_outbox(limit=100),
        container.services.outbox.drain_outbox(limit=100),
    )
    assert a + b == 6
    assert await _pending_count(container) == 0
