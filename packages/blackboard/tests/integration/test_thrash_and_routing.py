"""Routing-default + failure-ledger + thrash-report invariants against real Postgres.

Guards the fakes used by the unit tier against the real adapters (honest-fakes lesson):
- R2: start_runtime_run with no routing_class persists the policy default for the actor.
- T1: a forced conflict writes a command_failures row and the thrash report counts it,
  scoped to the goal; a review with a rejecting disposition bumps review_rejections.
- T3: the thrash report issues no writes and takes no row locks (SELECT-only).
- T4: the derived report round-trips through the service the `blackboard thrash` CLI uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from sdlc_blackboard.application.commands import ClaimTaskRequest, StartRunRequest
from sdlc_blackboard.domain.common import ActorKind, ActorRef, CommandContext
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.reviews import ReviewDisposition, ReviewSubmission
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    ReviewRequirement,
    TaskContractCreate,
)
from sdlc_blackboard.infrastructure.di import Container, build_container
from tests.integration.conftest import INTEGRATION_READY

pytestmark = pytest.mark.skipif(not INTEGRATION_READY, reason="needs Docker + dbmate")

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)
QA = ActorRef(actor_id="qa-1", kind=ActorKind.QUALITY)
SEC = ActorRef(actor_id="sec-1", kind=ActorKind.SECURITY)

IMPL_LOGICAL = "source/x"


def _ctx(actor: ActorRef, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


@pytest_asyncio.fixture(loop_scope="function")
async def container(pg_dsn: str) -> AsyncIterator[Container]:
    from sdlc_blackboard.domain.settings import Settings

    c = await build_container(Settings(database_url=pg_dsn))
    async with c.postgres.transaction() as conn:
        await conn.execute(
            "truncate goals, processed_commands, outbox, team_events, command_failures "
            "restart identity cascade"
        )
    try:
        yield c
    finally:
        await c.postgres.stop()


async def _goal_and_ready_impl_task(
    c: Container, *, with_reviews: bool = False
) -> tuple[UUID, UUID]:
    goal = await c.services.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(title="g", objective="o", success_criteria=("a",), owner=HUMAN),
    )
    assert goal.value is not None
    review_reqs = (
        (
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
        )
        if with_reviews
        else ()
    )
    impl = await c.services.tasks.create_task(
        _ctx(LEAD),
        TaskContractCreate(
            goal_id=goal.value.goal_id,
            task_key="impl",
            title="Impl",
            objective="do",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            scope=("demo_app",),
            deliverables=(DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),),
            acceptance_criteria=("ok",),
            review_requirements=review_reqs,
        ),
    )
    assert impl.value is not None
    return goal.value.goal_id, impl.value.task_id


# --------------------------------------------------------------------------- #
# R2 — start_runtime_run without routing_class persists the policy default     #
# --------------------------------------------------------------------------- #


async def test_start_run_persists_policy_default_routing_class(container: Container) -> None:
    """R2: an IMPLEMENTATION task with no explicit routing_class gets geo_inference_profile
    (the Lean-certified default) persisted on its runtime run."""
    _goal_id, task_id = await _goal_and_ready_impl_task(container)
    claim = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    run = await container.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=claim.value.assignment_epoch),
        StartRunRequest(task_id=task_id, omnigent_conversation_id="conv"),
    )
    assert run.value is not None
    async with container.postgres.connection() as conn:
        stored = await conn.fetchval(
            "select routing_class from runtime_runs where run_id=$1", run.value.run_id
        )
    assert stored == "geo_inference_profile"


# --------------------------------------------------------------------------- #
# T1 — a forced conflict writes a ledger row; thrash counts it, goal-scoped    #
# --------------------------------------------------------------------------- #


async def test_conflict_writes_ledger_row_and_thrash_counts_it(container: Container) -> None:
    """The double-claim drift setup forces open_assignment to a structured Conflict; that
    DomainError must land one command_failures row (error_code='conflict'), and the thrash
    report must count it under the task's goal (the failure is recorded task-scoped)."""
    goal_id, task_id = await _goal_and_ready_impl_task(container)

    claim1 = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim1.value is not None

    # Drift: return the task to READY but leave the assignment active.
    async with container.postgres.transaction() as conn:
        await conn.execute(
            "update tasks set state='ready', version=version+1 where task_id=$1", task_id
        )
    conflicted = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert conflicted.error is not None
    assert conflicted.error.code.value == "conflict"

    # The ledger row was written (second txn, best-effort but must succeed here).
    async with container.postgres.connection() as conn:
        rows = await conn.fetch(
            "select error_code, task_id from command_failures where task_id=$1", task_id
        )
    assert len(rows) == 1
    assert rows[0]["error_code"] == "conflict"

    report = await container.services.thrash.get_thrash_report(goal_id)
    assert report.conflicts >= 1

    # Goal-scope frame (T1): a different goal sees zero.
    other = await container.services.thrash.get_thrash_report(uuid4())
    assert other.conflicts == 0


async def test_thrash_zero_on_empty_goal(container: Container) -> None:
    """T2: a goal with no failures/events reports all zeros, not an error."""
    goal_id, _task_id = await _goal_and_ready_impl_task(container)
    report = await container.services.thrash.get_thrash_report(goal_id)
    assert (report.conflicts, report.stale_versions, report.review_rejections) == (0, 0, 0)
    # A never-created goal is indistinguishable from a quiet one at this read layer.
    unknown = await container.services.thrash.get_thrash_report(uuid4())
    assert unknown.reclaims == 0


# --------------------------------------------------------------------------- #
# T1 — a rejecting review disposition bumps review_rejections                  #
# --------------------------------------------------------------------------- #


async def test_rejecting_review_bumps_review_rejections(container: Container) -> None:
    from sdlc_blackboard.application.commands import SubmitTaskResult
    from sdlc_blackboard.domain.artifacts import ArtifactSubmission
    from sdlc_blackboard.domain.common import ArtifactBinding

    goal_id, task_id = await _goal_and_ready_impl_task(container, with_reviews=True)
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
                    logical_name=IMPL_LOGICAL,
                    content_uri="git://x/h1",
                    content_hash="h1",
                    summary="s",
                ),
            ),
            summary="run summary",
        ),
    )
    assert submitted.value is not None
    rev = submitted.value.artifact_revisions[0]
    binding = ArtifactBinding(
        artifact_id=rev.artifact_id,
        revision_id=rev.revision_id,
        logical_name=rev.logical_name,
        content_hash=rev.content_hash,
    )
    async with container.postgres.connection() as conn:
        row = await conn.fetchrow(
            "select task_id from tasks where goal_id=$1 and required_actor_kind='quality'",
            goal_id,
        )
    assert row is not None
    review = await container.services.reviews.submit_review(
        _ctx(QA),
        ReviewSubmission(
            goal_id=goal_id,
            review_task_id=row["task_id"],
            reviewer=QA,
            review_type="quality",
            artifact_bindings=(binding,),
            disposition=ReviewDisposition.REQUEST_REVISION,
            summary="please revise",
        ),
    )
    assert review.value is not None
    report = await container.services.thrash.get_thrash_report(goal_id)
    assert report.review_rejections >= 1


# --------------------------------------------------------------------------- #
# T3 — the thrash read takes no row locks                                     #
# --------------------------------------------------------------------------- #


async def test_thrash_report_takes_no_row_locks(container: Container) -> None:
    """T3 read-only: while another connection holds FOR UPDATE on the goal row, the thrash
    report still completes (it never contends for a lock — it issues SELECT-only queries
    and does not lock the goal row)."""
    goal_id, _task_id = await _goal_and_ready_impl_task(container)
    conn_a = await container.postgres.pool.acquire()
    try:
        txn = conn_a.transaction()
        await txn.start()
        await conn_a.execute("select * from goals where goal_id=$1 for update", goal_id)
        # Must not hang or error despite A's exclusive lock on the goal row.
        report = await container.services.thrash.get_thrash_report(goal_id)
        assert report.goal_id == goal_id
        await txn.rollback()
    finally:
        await container.postgres.pool.release(conn_a)
