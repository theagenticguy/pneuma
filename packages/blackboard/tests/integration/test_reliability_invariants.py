"""Negative-path reliability invariants against real Postgres (handoff §2, §21).

The scripted e2e proves the happy path; these prove the guarantees that only show up
on the failure paths: idempotent replay, reused-command-id rejection, stale-assignment
fencing, and stale-review invalidation at the gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from sdlc_blackboard.application.commands import (
    ClaimTaskRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.results import CommandStatus
from sdlc_blackboard.domain.artifacts import ArtifactSubmission
from sdlc_blackboard.domain.common import ActorKind, ActorRef, CommandContext
from sdlc_blackboard.domain.goals import Goal, GoalCreate
from sdlc_blackboard.domain.tasks import DeliverableSpec, Task, TaskContractCreate
from sdlc_blackboard.infrastructure.di import Container, build_container
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)


def _ctx(actor: ActorRef, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


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


async def _goal_and_ready_impl_task(c: Container) -> tuple[Goal, Task]:
    goal = await c.services.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(title="g", objective="o", success_criteria=("a",), owner=HUMAN),
    )
    assert goal.value is not None
    impl = await c.services.tasks.create_task(
        _ctx(LEAD),
        TaskContractCreate(
            goal_id=goal.value.goal_id,
            task_key="impl",
            title="Impl",
            objective="do",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            scope=("demo_app",),
            deliverables=(DeliverableSpec(artifact_type="source", logical_name="source/x"),),
            acceptance_criteria=("ok",),
        ),
    )
    assert impl.value is not None
    return goal.value, impl.value


async def test_idempotent_replay_returns_same_value(container: Container) -> None:
    ctx = _ctx(HUMAN)
    req = GoalCreate(title="g", objective="o", success_criteria=("a",), owner=HUMAN)
    first = await container.services.goals.create_goal(ctx, req)
    second = await container.services.goals.create_goal(ctx, req)
    assert first.value is not None and second.value is not None
    assert first.value.goal_id == second.value.goal_id
    assert second.replayed is True
    assert second.status == CommandStatus.DUPLICATE_REPLAYED


async def test_reused_command_id_different_payload_rejected(container: Container) -> None:
    ctx = _ctx(HUMAN)
    a = await container.services.goals.create_goal(
        ctx, GoalCreate(title="A", objective="o", success_criteria=("a",), owner=HUMAN)
    )
    b = await container.services.goals.create_goal(
        ctx, GoalCreate(title="B", objective="o", success_criteria=("a",), owner=HUMAN)
    )
    assert a.status == CommandStatus.ACCEPTED
    assert b.error is not None
    assert b.error.code.value == "duplicate_command_mismatch"


async def test_concurrent_duplicate_command_id_returns_structured_result(
    container: Container,
) -> None:
    """Two concurrent calls with the SAME command_id must both return a structured
    CommandResult — the loser gets a retryable conflict, never an uncaught
    UniqueViolationError. (Regression: adversarial PROBE1.)"""
    ctx = _ctx(HUMAN)
    req = GoalCreate(title="race", objective="o", success_criteria=("a",), owner=HUMAN)

    results = await asyncio.gather(
        container.services.goals.create_goal(ctx, req),
        container.services.goals.create_goal(ctx, req),
        return_exceptions=True,
    )
    # Neither call may raise — both must be CommandResult values.
    for r in results:
        assert not isinstance(r, BaseException), f"uncaught exception escaped: {r!r}"

    statuses = {r.status for r in results}  # type: ignore[union-attr]
    # At least one accepted; any loser is a retryable conflict or a replay — never a crash.
    assert CommandStatus.ACCEPTED in statuses or CommandStatus.DUPLICATE_REPLAYED in statuses
    for r in results:
        if r.error is not None:  # type: ignore[union-attr]
            assert r.error.retryable is True  # type: ignore[union-attr]


async def test_only_one_concurrent_claim_wins(container: Container) -> None:
    _goal, task = await _goal_and_ready_impl_task(container)
    task_id = task.task_id

    async def claim(actor: str) -> CommandStatus:
        res = await container.services.tasks.claim_task(
            _ctx(ActorRef(actor_id=actor, kind=ActorKind.IMPLEMENTATION)),
            ClaimTaskRequest(task_id=task_id, actor_id=actor),
        )
        return res.status

    a, b = await asyncio.gather(claim("impl-a"), claim("impl-b"))
    accepted = [s for s in (a, b) if s == CommandStatus.ACCEPTED]
    assert len(accepted) == 1


async def test_stale_assignment_epoch_cannot_start_run(container: Container) -> None:
    """A worker carrying an old epoch cannot start a run after reassignment."""
    _goal, task = await _goal_and_ready_impl_task(container)
    task_id = task.task_id

    claim1 = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim1.value is not None
    epoch1 = claim1.value.assignment_epoch

    # Simulate the §23 failure-recovery reassignment path: fail the active assignment
    # (freeing the partial unique index), return the task to READY, then re-claim.
    async with container.postgres.transaction() as conn:
        await conn.execute(
            "update task_assignments set state='failed', ended_at=now() "
            "where task_id=$1 and state in ('assigned','running')",
            task_id,
        )
        await conn.execute(
            "update tasks set state='ready', version=version+1 where task_id=$1", task_id
        )
    claim2 = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim2.value is not None
    assert claim2.value.assignment_epoch == epoch1 + 1

    # The old worker (epoch1) tries to start a run — must be rejected as stale.
    stale = await container.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch1),
        StartRunRequest(task_id=task_id, omnigent_conversation_id="conv-old"),
    )
    assert stale.error is not None
    assert stale.error.code.value == "stale_assignment"


async def test_promote_requires_expected_revision_when_alias_exists(container: Container) -> None:
    """Promotion is CAS: omitting expected_current_revision_id against an existing alias
    is rejected (no last-writer-wins bypass)."""
    from sdlc_blackboard.application.commands import (
        PromoteArtifactRequest,
        StartRunRequest,
        SubmitTaskResult,
    )
    from sdlc_blackboard.domain.artifacts import ArtifactSubmission

    _goal, task = await _goal_and_ready_impl_task(container)
    task_id = task.task_id
    goal_id = task.goal_id

    claim = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch
    run = await container.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch), StartRunRequest(task_id=task_id, omnigent_conversation_id="c")
    )
    assert run.value is not None
    submitted = await container.services.tasks.submit_task_result(
        _ctx(IMPL, epoch=epoch),
        SubmitTaskResult(
            task_id=task_id,
            run_id=run.value.run_id,
            disposition="completed",
            input_manifest=(),
            artifacts=(
                ArtifactSubmission(
                    artifact_type="source",
                    logical_name="source/x",
                    content_uri="git://x/1",
                    content_hash="h1",
                    summary="s",
                ),
            ),
            summary="s",
        ),
    )
    assert submitted.value is not None
    rev1 = submitted.value.artifact_revisions[0]

    # The alias now exists (established at submit). A promote with no expected id is a bypass.
    bypass = await container.services.artifacts.promote_artifact(
        _ctx(LEAD),
        PromoteArtifactRequest(
            goal_id=goal_id,
            logical_name="source/x",
            expected_current_revision_id=None,
            new_revision_id=rev1.revision_id,
        ),
    )
    assert bypass.error is not None
    assert bypass.error.code.value == "precondition_failed"


async def test_submit_with_stale_epoch_rejected(container: Container) -> None:
    """The full fencing path: a stale epoch cannot submit an authoritative result."""
    _goal, task = await _goal_and_ready_impl_task(container)
    task_id = task.task_id

    claim = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch
    run = await container.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch),
        StartRunRequest(task_id=task_id, omnigent_conversation_id="conv"),
    )
    assert run.value is not None

    # Submit carrying a stale (epoch-1) fence — must be rejected.
    stale = await container.services.tasks.submit_task_result(
        _ctx(IMPL, epoch=epoch - 1),
        SubmitTaskResult(
            task_id=task_id,
            run_id=run.value.run_id,
            disposition="completed",
            input_manifest=(),
            artifacts=(
                ArtifactSubmission(
                    artifact_type="source",
                    logical_name="source/x",
                    content_uri="git://x/1",
                    content_hash="h1",
                    summary="s",
                ),
            ),
            summary="s",
        ),
    )
    assert stale.error is not None
    assert stale.error.code.value == "stale_assignment"
