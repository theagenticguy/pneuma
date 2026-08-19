"""ThrashService unit tests over fake ports (spec T1/T2/T5, no Docker).

These reach the pure aggregation branches ThrashService owns: zero-on-empty (T2), the
goal-scope frame property (T1), and monotonicity under new signals (T5, the Python
mirror of Thrash.lean report_mono for one extension step). The honest FakeCommandFailureRepo
does the real task->goal scope join, so a foreign goal's rows cannot leak into the report.
The same branches are re-proved against real Postgres in the integration tier.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sdlc_blackboard.application.query_models import ThrashReport
from sdlc_blackboard.application.results import ErrorCode
from sdlc_blackboard.application.use_cases.thrash_service import ThrashService
from sdlc_blackboard.domain.common import ActorKind, ActorRef
from sdlc_blackboard.domain.events import TeamEvent
from sdlc_blackboard.domain.reviews import Review, ReviewDisposition
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    Task,
    TaskContractCreate,
    TaskState,
)
from tests.unit.fakes import FakeEnv

SYSTEM = ActorRef(actor_id="sys", kind=ActorKind.SYSTEM)


def _seed_task(env: FakeEnv, goal_id: object, *, epoch: int = 0, key: str = "t") -> Task:
    task = Task(
        goal_id=goal_id,  # type: ignore[arg-type]
        task_key=key,
        title=key,
        objective="o",
        required_actor_kind=ActorKind.IMPLEMENTATION,
        state=TaskState.READY,
        version=0,
        assignment_epoch=epoch,
        contract=_contract(goal_id, key),
    )
    env.tasks.tasks[task.task_id] = task
    return task


def _contract(goal_id: object, key: str) -> TaskContractCreate:
    return TaskContractCreate(
        goal_id=goal_id,  # type: ignore[arg-type]
        task_key=key,
        title=key,
        objective="o",
        required_actor_kind=ActorKind.IMPLEMENTATION,
        scope=("s",),
        deliverables=(DeliverableSpec(artifact_type="source", logical_name="x"),),
        acceptance_criteria=("ok",),
    )


def _review(goal_id: object, disposition: ReviewDisposition) -> Review:
    return Review(
        goal_id=goal_id,  # type: ignore[arg-type]
        review_task_id=uuid4(),
        reviewer=ActorRef(actor_id="qa", kind=ActorKind.QUALITY),
        review_type="quality",
        binding_fingerprint="fp",
        artifact_bindings=(),
        disposition=disposition,
        summary="s",
        evidence=(),
        finding_ids=(),
    )


async def _record(
    env: FakeEnv,
    *,
    goal_id: UUID | None = None,
    task_id: UUID | None = None,
    code: ErrorCode,
) -> None:
    await env.command_failures.record(
        object(),
        command_id=uuid4(),
        tool_name="t",
        actor_id="a",
        goal_id=goal_id,
        task_id=task_id,
        error_code=code.value,
    )


class TestZeroOnEmpty:
    async def test_empty_goal_returns_all_zeros(self) -> None:
        """T2: a goal with no events/failures yields the zero report, not an error."""
        env = FakeEnv()
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(uuid4())
        assert report == ThrashReport(
            goal_id=report.goal_id,
            conflicts=0,
            stale_versions=0,
            review_rejections=0,
            reclaims=0,
        )

    async def test_unknown_goal_does_not_error(self) -> None:
        """Totality: an unknown goal_id is indistinguishable from a quiet one -> zeros."""
        env = FakeEnv()
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(uuid4())
        assert report.conflicts == 0
        assert report.reclaims == 0


class TestCounters:
    async def test_counts_conflicts_and_stale_versions_by_error_code(self) -> None:
        env = FakeEnv()
        goal_id = uuid4()
        await _record(env, goal_id=goal_id, code=ErrorCode.CONFLICT)
        await _record(env, goal_id=goal_id, code=ErrorCode.CONFLICT)
        await _record(env, goal_id=goal_id, code=ErrorCode.STALE_VERSION)
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(goal_id)
        assert report.conflicts == 2
        assert report.stale_versions == 1

    async def test_task_scoped_conflict_counts_for_the_task_goal(self) -> None:
        """A double-claim conflict is recorded with only a task_id (goal_id None); it must
        still count for the task's goal via the task->goal scope join."""
        env = FakeEnv()
        goal_id = uuid4()
        task = _seed_task(env, goal_id)
        await _record(env, task_id=task.task_id, code=ErrorCode.CONFLICT)
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(goal_id)
        assert report.conflicts == 1

    async def test_review_rejections_count_everything_but_approved(self) -> None:
        env = FakeEnv()
        goal_id = uuid4()
        env.reviews.reviews[uuid4()] = _review(goal_id, ReviewDisposition.APPROVED)
        env.reviews.reviews[uuid4()] = _review(goal_id, ReviewDisposition.FINDINGS)
        env.reviews.reviews[uuid4()] = _review(goal_id, ReviewDisposition.REQUEST_REVISION)
        env.reviews.reviews[uuid4()] = _review(goal_id, ReviewDisposition.ABSTAINED)
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(goal_id)
        assert report.review_rejections == 3  # all but the one APPROVED

    async def test_reclaims_sum_reopened_events_and_extra_claims(self) -> None:
        env = FakeEnv()
        goal_id = uuid4()
        # A task claimed 3 times -> epoch 3 -> 2 extra claims beyond the first.
        _seed_task(env, goal_id, epoch=3, key="a")
        # A task claimed once -> epoch 1 -> 0 extra claims.
        _seed_task(env, goal_id, epoch=1, key="b")
        # Two review-task reopen events.
        for _ in range(2):
            env.events.events.append(
                TeamEvent(
                    goal_id=goal_id,
                    task_id=None,
                    aggregate_type="task",
                    aggregate_id=uuid4(),
                    aggregate_version=0,
                    event_type="review_task.reopened",
                    actor=SYSTEM,
                    correlation_id=uuid4(),
                    causation_id=None,
                )
            )
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(goal_id)
        assert report.reclaims == 2 + 2  # 2 reopened events + 2 extra claims


class TestGoalScopeFrame:
    async def test_other_goal_signals_do_not_move_the_report(self) -> None:
        """T1 frame property: failures, reviews, and events for a DIFFERENT goal never
        change this goal's report."""
        env = FakeEnv()
        goal_id = uuid4()
        other = uuid4()
        # Everything below belongs to `other`, not `goal_id`.
        await _record(env, goal_id=other, code=ErrorCode.CONFLICT)
        await _record(env, goal_id=other, code=ErrorCode.STALE_VERSION)
        env.reviews.reviews[uuid4()] = _review(other, ReviewDisposition.FINDINGS)
        _seed_task(env, other, epoch=5, key="other")
        env.events.events.append(
            TeamEvent(
                goal_id=other,
                task_id=None,
                aggregate_type="task",
                aggregate_id=uuid4(),
                aggregate_version=0,
                event_type="review_task.reopened",
                actor=SYSTEM,
                correlation_id=uuid4(),
                causation_id=None,
            )
        )
        svc = ThrashService(env.ports)
        report = await svc.get_thrash_report(goal_id)
        assert report == ThrashReport(goal_id=goal_id)  # all defaults = zeros


class TestMonotonicity:
    async def test_counters_never_decrease_when_a_signal_is_added(self) -> None:
        """T5 (Thrash.lean report_mono, one extension step): extending the history with a
        new signal leaves every counter >= its prior value."""
        env = FakeEnv()
        goal_id = uuid4()
        svc = ThrashService(env.ports)
        before = await svc.get_thrash_report(goal_id)

        await _record(env, goal_id=goal_id, code=ErrorCode.CONFLICT)
        env.reviews.reviews[uuid4()] = _review(goal_id, ReviewDisposition.FINDINGS)
        after = await svc.get_thrash_report(goal_id)

        assert after.conflicts >= before.conflicts
        assert after.stale_versions >= before.stale_versions
        assert after.review_rejections >= before.review_rejections
        assert after.reclaims >= before.reclaims
        # And at least one strictly increased (the signals we added).
        assert (after.conflicts, after.review_rejections) == (1, 1)
