"""accept_task use case: SUBMITTED -> UNDER_REVIEW -> ACCEPTED through the legal
transition matrix, so the lead never applies the accept at the store by hand.

This closes the "finalize wedge" seen across resort revisions rev2/rev3/rev4: after
every review approved, the lead consistently stalled because there was no command tool
to accept the producer task, forcing a raw store write it hesitated to perform.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from sdlc_blackboard.application.commands import (
    AcceptTaskRequest,
    ClaimTaskRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.results import CommandStatus
from sdlc_blackboard.domain.artifacts import ArtifactSubmission
from sdlc_blackboard.domain.common import ActorKind, ActorRef, CommandContext
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.domain.tasks import DeliverableSpec, TaskContractCreate, TaskState
from sdlc_blackboard.infrastructure.di import Container, build_container
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)
IMPL_LOGICAL = "source/impl"


def _ctx(actor: ActorRef, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


@pytest_asyncio.fixture(loop_scope="function")
async def container(pg_dsn: str) -> AsyncIterator[Container]:
    c = await build_container(Settings(database_url=pg_dsn))
    async with c.postgres.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    try:
        yield c
    finally:
        await c.postgres.stop()


async def _submit_impl(container: Container):
    """Drive a fresh implementation task all the way to SUBMITTED; return its id."""
    svc = container.services
    goal = await svc.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(
            title="Accept flow",
            objective="prove accept_task",
            success_criteria=("accepted",),
            owner=HUMAN,
        ),
    )
    assert goal.value is not None
    impl = await svc.tasks.create_task(
        _ctx(LEAD),
        TaskContractCreate(
            goal_id=goal.value.goal_id,
            task_key="implement",
            title="Implement",
            objective="implement",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            scope=("demo_app",),
            deliverables=(DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),),
            acceptance_criteria=("works",),
            review_requirements=(),
        ),
    )
    assert impl.value is not None
    impl_id = impl.value.task_id
    claim = await svc.tasks.claim_task(
        _ctx(LEAD), ClaimTaskRequest(task_id=impl_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch
    run = await svc.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch),
        StartRunRequest(task_id=impl_id, omnigent_conversation_id="c"),
    )
    assert run.value is not None
    submitted = await svc.tasks.submit_task_result(
        _ctx(IMPL, epoch=epoch),
        SubmitTaskResult(
            task_id=impl_id,
            run_id=run.value.run_id,
            disposition="completed",
            input_manifest=(),
            artifacts=(
                ArtifactSubmission(
                    artifact_type="source",
                    logical_name=IMPL_LOGICAL,
                    content_uri="git://demo_app/rev1",
                    content_hash="rev1",
                    summary="impl",
                ),
            ),
            summary="impl",
        ),
    )
    assert submitted.value is not None
    assert submitted.value.task.state == TaskState.SUBMITTED
    return impl_id


async def test_accept_task_advances_submitted_to_accepted(container: Container) -> None:
    svc = container.services
    impl_id = await _submit_impl(container)

    res = await svc.tasks.accept_task(_ctx(LEAD), AcceptTaskRequest(task_id=impl_id))
    assert res.status == CommandStatus.ACCEPTED
    assert res.value is not None
    assert res.value.state == TaskState.ACCEPTED

    # The store agrees, and the two legal transition events were emitted.
    async with container.postgres.connection() as conn:
        state = await conn.fetchval("select state from tasks where task_id=$1", impl_id)
        assert state == "accepted"
        events = await conn.fetch(
            "select event_type from team_events where task_id=$1 order by occurred_at",
            impl_id,
        )
    types = [e["event_type"] for e in events]
    assert "task.under_review" in types
    assert "task.accepted" in types


async def test_accept_task_is_idempotent(container: Container) -> None:
    svc = container.services
    impl_id = await _submit_impl(container)

    first = await svc.tasks.accept_task(_ctx(LEAD), AcceptTaskRequest(task_id=impl_id))
    assert first.value is not None and first.value.state == TaskState.ACCEPTED
    # Re-accepting an already-accepted task is a no-op success, not an illegal transition.
    second = await svc.tasks.accept_task(_ctx(LEAD), AcceptTaskRequest(task_id=impl_id))
    assert second.status == CommandStatus.ACCEPTED
    assert second.value is not None and second.value.state == TaskState.ACCEPTED


async def test_accept_task_rejects_non_submitted(container: Container) -> None:
    """A task that is not SUBMITTED/UNDER_REVIEW/ACCEPTED cannot be accepted."""
    svc = container.services
    goal = await svc.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(
            title="Reject flow",
            objective="x",
            success_criteria=("x",),
            owner=HUMAN,
        ),
    )
    assert goal.value is not None
    impl = await svc.tasks.create_task(
        _ctx(LEAD),
        TaskContractCreate(
            goal_id=goal.value.goal_id,
            task_key="implement",
            title="Implement",
            objective="implement",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            scope=("demo_app",),
            deliverables=(DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),),
            acceptance_criteria=("works",),
            review_requirements=(),
        ),
    )
    assert impl.value is not None
    # Task is READY (never claimed/run/submitted) -> accept must fail as a precondition.
    res = await svc.tasks.accept_task(_ctx(LEAD), AcceptTaskRequest(task_id=impl.value.task_id))
    assert res.status != CommandStatus.ACCEPTED
    assert res.value is None
