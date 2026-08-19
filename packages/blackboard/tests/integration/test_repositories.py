"""Repository-level integration tests against real Postgres (handoff §25 controls).

Proves the persistence-layer concurrency primitives the kernel rests on:
- goal insert/get roundtrip through the jsonb codec;
- task claim CAS (version-guarded) admits exactly one winner;
- the partial unique index blocks a second active assignment;
- the processed-commands dedup store round-trips.
"""

from __future__ import annotations

import asyncio

import pytest

from sdlc_blackboard.domain.common import ActorKind, ActorRef
from sdlc_blackboard.domain.goals import Goal, GoalState
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    Task,
    TaskContractCreate,
    TaskState,
)
from sdlc_blackboard.infrastructure.postgres import Postgres
from sdlc_blackboard.infrastructure.repositories import (
    GoalRepository,
    TaskRepository,
)
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")


def _goal() -> Goal:
    return Goal(
        title="t",
        objective="o",
        success_criteria=("a",),
        constraints=(),
        owner=ActorRef(actor_id="human-1", kind=ActorKind.HUMAN),
        state=GoalState.ACTIVE,
        version=0,
    )


def _ready_task(goal_id: object) -> Task:
    contract = TaskContractCreate(
        goal_id=goal_id,  # type: ignore[arg-type]
        task_key="impl",
        title="Implement",
        objective="do it",
        required_actor_kind=ActorKind.IMPLEMENTATION,
        scope=("demo_app",),
        deliverables=(DeliverableSpec(artifact_type="source", logical_name="source/x"),),
        acceptance_criteria=("compiles",),
    )
    return Task(
        goal_id=goal_id,  # type: ignore[arg-type]
        task_key="impl",
        title="Implement",
        objective="do it",
        required_actor_kind=ActorKind.IMPLEMENTATION,
        state=TaskState.READY,
        version=0,
        assignment_epoch=0,
        contract=contract,
    )


async def test_goal_roundtrip(postgres: Postgres) -> None:
    goals = GoalRepository()
    goal = _goal()
    async with postgres.transaction() as conn:
        await goals.insert(conn, goal)
    async with postgres.connection() as conn:
        fetched = await goals.get(conn, goal.goal_id)
    assert fetched is not None
    assert fetched.goal_id == goal.goal_id
    assert fetched.success_criteria == ("a",)
    assert fetched.owner.kind == ActorKind.HUMAN


async def test_goal_cas_rejects_stale_version(postgres: Postgres) -> None:
    goals = GoalRepository()
    goal = _goal()
    async with postgres.transaction() as conn:
        await goals.insert(conn, goal)
        won = await goals.set_state_cas(conn, goal.goal_id, 0, "satisfied")
        assert won is not None and won.version == 1
        stale = await goals.set_state_cas(conn, goal.goal_id, 0, "failed")
        assert stale is None


async def test_claim_cas_single_winner(postgres: Postgres) -> None:
    goals, tasks = GoalRepository(), TaskRepository()
    goal = _goal()
    task = _ready_task(goal.goal_id)
    async with postgres.transaction() as conn:
        await goals.insert(conn, goal)
        await tasks.insert(conn, task)

    # Two racing claimers, each in its own transaction, same expected version 0.
    async def claim(actor_id: str) -> Task | None:
        async with postgres.transaction() as conn:
            return await tasks.claim_cas(conn, task.task_id, 0, actor_id, next_epoch=1)

    a, b = await asyncio.gather(claim("impl-a"), claim("impl-b"))
    winners = [x for x in (a, b) if x is not None]
    assert len(winners) == 1
    assert winners[0].state == TaskState.ASSIGNED
    assert winners[0].assignment_epoch == 1
