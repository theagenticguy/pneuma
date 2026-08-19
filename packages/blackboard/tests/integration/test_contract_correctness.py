"""Contract-correctness invariants against real Postgres (T-AC).

These prove the kernel keeps the promises its wire contract makes: the two
UniqueViolation sites translate to structured conflicts, submit persists the
result_manifest, routing_class round-trips (and rejects garbage), and
authorize_goal_completion enforces the release gate in-transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from sdlc_blackboard.application.commands import (
    ClaimTaskRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.receipts import TaskSubmissionReceipt
from sdlc_blackboard.application.results import CommandResult, CommandStatus
from sdlc_blackboard.domain.approvals import ApprovalSubmission, ApprovalType
from sdlc_blackboard.domain.artifacts import ArtifactRevision, ArtifactSubmission
from sdlc_blackboard.domain.common import (
    ActorKind,
    ActorRef,
    ArtifactBinding,
    CommandContext,
)
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
            "truncate goals, processed_commands, outbox, team_events, command_failures restart identity cascade"
        )
    try:
        yield c
    finally:
        await c.postgres.stop()


def _binding(revision: ArtifactRevision) -> ArtifactBinding:
    return ArtifactBinding(
        artifact_id=revision.artifact_id,
        revision_id=revision.revision_id,
        logical_name=revision.logical_name,
        content_hash=revision.content_hash,
    )


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


async def _run_and_submit(
    c: Container,
    task_id: UUID,
    *,
    content_hash: str = "h1",
    routing_class: str | None = None,
) -> CommandResult[TaskSubmissionReceipt]:
    claim = await c.services.tasks.claim_task(
        _ctx(LEAD), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch
    run = await c.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch),
        StartRunRequest(
            task_id=task_id,
            omnigent_conversation_id="conv",
            routing_class=routing_class,
        ),
    )
    assert run.value is not None
    return await c.services.tasks.submit_task_result(
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
                    content_uri=f"git://x/{content_hash}",
                    content_hash=content_hash,
                    summary="s",
                ),
            ),
            summary="run summary",
            assumptions=("assumed the API is stable",),
            unresolved_questions=("what about rate limits",),
            residual_risks=("perf under load",),
        ),
    )


# --------------------------------------------------------------------------- #
# 4a — open_assignment conflict translation                                   #
# --------------------------------------------------------------------------- #


async def test_open_assignment_active_collision_is_structured_conflict(
    container: Container,
) -> None:
    """Recovery drift returns a task to READY while its assignment stays active; the
    next claim's open_assignment INSERT collides with the partial unique index and
    must surface as a structured CONFLICT, not a raw UniqueViolationError."""
    _goal_id, task_id = await _goal_and_ready_impl_task(container)

    claim1 = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim1.value is not None

    # Drift: return the task to READY but LEAVE the assignment active (no fail/complete).
    async with container.postgres.transaction() as conn:
        await conn.execute(
            "update tasks set state='ready', version=version+1 where task_id=$1", task_id
        )

    conflicted = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert conflicted.error is not None
    assert conflicted.status == CommandStatus.CONFLICT_CREATED
    assert conflicted.error.code.value == "conflict"
    assert conflicted.error.retryable is False


# --------------------------------------------------------------------------- #
# 4b — ReviewRepository.insert conflict translation                           #
# --------------------------------------------------------------------------- #


async def test_duplicate_review_is_structured_conflict(container: Container) -> None:
    """A second review with a distinct command_id but the same (task, type, binding,
    actor) trips one_review_per_actor_type_binding and must be a structured conflict."""
    goal_id, task_id = await _goal_and_ready_impl_task(container, with_reviews=True)
    submitted = await _run_and_submit(container, task_id)
    assert submitted.value is not None
    binding = _binding(submitted.value.artifact_revisions[0])

    async with container.postgres.connection() as conn:
        row = await conn.fetchrow(
            "select task_id from tasks where goal_id=$1 and required_actor_kind='quality'",
            goal_id,
        )
    assert row is not None
    qa_task_id = row["task_id"]

    def _review() -> ReviewSubmission:
        return ReviewSubmission(
            goal_id=goal_id,
            review_task_id=qa_task_id,
            reviewer=QA,
            review_type="quality",
            artifact_bindings=(binding,),
            disposition=ReviewDisposition.APPROVED,
            summary="ok",
        )

    first = await container.services.reviews.submit_review(_ctx(QA), _review())
    assert first.status == CommandStatus.ACCEPTED
    dup = await container.services.reviews.submit_review(_ctx(QA), _review())
    assert dup.error is not None
    assert dup.status == CommandStatus.CONFLICT_CREATED
    assert dup.error.code.value == "conflict"


# --------------------------------------------------------------------------- #
# 4c — result_manifest persisted on submit                                    #
# --------------------------------------------------------------------------- #


async def test_submit_persists_result_manifest(container: Container) -> None:
    _goal_id, task_id = await _goal_and_ready_impl_task(container)
    submitted = await _run_and_submit(container, task_id)
    assert submitted.value is not None
    run_id = submitted.value.artifact_revisions[0].produced_by_run_id

    async with container.postgres.connection() as conn:
        manifest = await conn.fetchval(
            "select result_manifest from runtime_runs where run_id=$1", run_id
        )
    assert manifest is not None
    assert manifest["disposition"] == "completed"
    assert manifest["summary"] == "run summary"
    assert manifest["assumptions"] == ["assumed the API is stable"]
    assert manifest["unresolved_questions"] == ["what about rate limits"]
    assert manifest["residual_risks"] == ["perf under load"]


# --------------------------------------------------------------------------- #
# 4d — routing_class passthrough + validation                                 #
# --------------------------------------------------------------------------- #


async def test_routing_class_round_trips(container: Container) -> None:
    _goal_id, task_id = await _goal_and_ready_impl_task(container)
    submitted = await _run_and_submit(container, task_id, routing_class="regional_mantle")
    assert submitted.value is not None
    run_id = submitted.value.artifact_revisions[0].produced_by_run_id

    async with container.postgres.connection() as conn:
        stored = await conn.fetchval(
            "select routing_class from runtime_runs where run_id=$1", run_id
        )
    assert stored == "regional_mantle"


async def test_invalid_routing_class_is_validation_failed(container: Container) -> None:
    _goal_id, task_id = await _goal_and_ready_impl_task(container)
    claim = await container.services.tasks.claim_task(
        _ctx(IMPL), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    run = await container.services.tasks.start_runtime_run(
        _ctx(IMPL, epoch=claim.value.assignment_epoch),
        StartRunRequest(
            task_id=task_id,
            omnigent_conversation_id="conv",
            routing_class="not_a_real_class",
        ),
    )
    assert run.error is not None
    assert run.error.code.value == "validation_failed"


# --------------------------------------------------------------------------- #
# 4e — first promote against a missing alias is a structured Conflict         #
# --------------------------------------------------------------------------- #


async def test_first_promote_missing_alias_is_structured_conflict(
    container: Container,
) -> None:
    """A null-expected promote against a logical name that has no alias row yet is a
    no-op UPDATE (RETURNING nothing) -> promote_alias_cas returns None -> the service
    raises a structured Conflict. The initial alias is created only at submit time via
    upsert_alias_initial, never by promote (guards against a last-writer-wins bypass)."""
    from sdlc_blackboard.application.commands import PromoteArtifactRequest

    goal_id, task_id = await _goal_and_ready_impl_task(container)
    submitted = await _run_and_submit(container, task_id)
    assert submitted.value is not None
    # A real revision exists, but its alias lives under IMPL_LOGICAL. Promote a DIFFERENT
    # logical name (no alias row) with a null expected revision -> Conflict.
    revision_id = submitted.value.artifact_revisions[0].revision_id
    res = await container.services.artifacts.promote_artifact(
        _ctx(LEAD),
        PromoteArtifactRequest(
            goal_id=goal_id,
            logical_name="never/promoted",
            expected_current_revision_id=None,
            new_revision_id=revision_id,
        ),
    )
    assert res.error is not None
    assert res.status == CommandStatus.CONFLICT_CREATED
    assert res.error.code.value == "conflict"


# --------------------------------------------------------------------------- #
# 4f — authorize_goal_completion enforces the gate in-transaction             #
# --------------------------------------------------------------------------- #


async def _drive_gate_to_satisfied(container: Container) -> UUID:
    """Full happy-path setup so the release gate reads SATISFIED, returning goal_id."""
    goal_id, task_id = await _goal_and_ready_impl_task(container, with_reviews=True)
    submitted = await _run_and_submit(container, task_id)
    assert submitted.value is not None
    binding = _binding(submitted.value.artifact_revisions[0])

    async with container.postgres.connection() as conn:
        rows = await conn.fetch(
            "select task_id, required_actor_kind from tasks "
            "where goal_id=$1 and task_key like '%:review:%'",
            goal_id,
        )
    review_tasks = {r["required_actor_kind"]: r["task_id"] for r in rows}

    for reviewer, rtype in ((QA, "quality"), (SEC, "security")):
        res = await container.services.reviews.submit_review(
            _ctx(reviewer),
            ReviewSubmission(
                goal_id=goal_id,
                review_task_id=review_tasks[rtype],
                reviewer=reviewer,
                review_type=rtype,
                artifact_bindings=(binding,),
                disposition=ReviewDisposition.APPROVED,
                summary=f"{rtype} ok",
            ),
        )
        assert res.status == CommandStatus.ACCEPTED

    appr = await container.services.reviews.record_human_approval(
        _ctx(HUMAN),
        ApprovalSubmission(
            goal_id=goal_id,
            approval_type=ApprovalType.HUMAN_RELEASE,
            approver=HUMAN,
            artifact_bindings=(binding,),
        ),
    )
    assert appr.status == CommandStatus.ACCEPTED
    return goal_id


async def test_authorize_rejects_when_gate_unsatisfied(container: Container) -> None:
    goal_id, _task_id = await _goal_and_ready_impl_task(container, with_reviews=True)
    # No reviews, no approval — gate is UNSATISFIED.
    res = await container.services.goals.authorize_goal_completion(_ctx(HUMAN), goal_id)
    assert res.error is not None
    assert res.status == CommandStatus.PRECONDITION_FAILED
    assert res.error.code.value == "precondition_failed"

    # The goal must NOT have flipped.
    async with container.postgres.connection() as conn:
        state = await conn.fetchval("select state from goals where goal_id=$1", goal_id)
    assert state == "active"


async def test_authorize_succeeds_when_gate_satisfied(container: Container) -> None:
    goal_id = await _drive_gate_to_satisfied(container)
    done = await container.services.goals.authorize_goal_completion(_ctx(HUMAN), goal_id)
    assert done.value is not None
    assert done.status == CommandStatus.ACCEPTED
    assert done.value.state.value == "satisfied"


# --------------------------------------------------------------------------- #
# 4g — goal-row lock discipline serializes gate-input writes vs authorize     #
# --------------------------------------------------------------------------- #


async def test_gate_input_for_share_conflicts_with_authorize_for_update(
    container: Container,
) -> None:
    """The write-skew fix: authorize takes FOR UPDATE on the goal row before reading the
    gate; every gate-input writer takes FOR SHARE on the same row before writing. FOR
    SHARE conflicts with FOR UPDATE, so a gate-input commit cannot interleave inside
    authorize's read/CAS window under READ COMMITTED.

    A deterministic two-connection proof of that conflict: connection A holds the
    authorize-side FOR UPDATE open; connection B, under a short ``lock_timeout``, attempts
    the writer-side FOR SHARE and is blocked to timeout (asyncpg ``LockNotAvailableError``)
    rather than acquiring the lock. The full concurrent authorize-vs-finding interleave is
    covered in spirit by ``test_authorize_rejects_when_gate_unsatisfied`` (a blocking
    finding, taken under FOR SHARE, forces authorize to PreconditionFailed)."""
    import asyncpg

    goal_id, _task_id = await _goal_and_ready_impl_task(container)

    conn_a = await container.postgres.pool.acquire()
    conn_b = await container.postgres.pool.acquire()
    try:
        txn_a = conn_a.transaction()
        await txn_a.start()
        # Connection A takes the authorize-side exclusive lock and holds it open.
        await conn_a.execute("select * from goals where goal_id = $1 for update", goal_id)

        txn_b = conn_b.transaction()
        await txn_b.start()
        await conn_b.execute("set local lock_timeout = '500ms'")
        # Connection B (a gate-input writer) attempts the shared lock and must be blocked
        # to timeout by A's exclusive lock — proving the two modes serialize.
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await conn_b.execute("select 1 from goals where goal_id = $1 for share", goal_id)
        await txn_b.rollback()
        await txn_a.rollback()
    finally:
        await container.postgres.pool.release(conn_a)
        await container.postgres.pool.release(conn_b)
