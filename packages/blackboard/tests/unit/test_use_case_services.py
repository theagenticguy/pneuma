"""Use-case unit tests over fake ports (no Docker, no asyncpg).

These target the PURE decision branches the services own — draft-vs-ready derivation,
claim preconditions, state-transition gating, review fan-out, CAS-miss handling,
promote compare-and-set, blocking-finding authority, and idempotent replay/mismatch.
Round-trip / SQL behavior is covered by the integration tier; here we reach the
branches a full stack only touches by accident.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sdlc_blackboard.application.commands import (
    AcceptTaskRequest,
    BindRuntimeSessionRequest,
    ClaimTaskRequest,
    PromoteArtifactRequest,
    RefreshReadyTasksRequest,
    ResolveFindingRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.results import CommandStatus
from sdlc_blackboard.application.use_cases.artifact_service import ArtifactService
from sdlc_blackboard.application.use_cases.goal_service import GoalService
from sdlc_blackboard.application.use_cases.review_service import ReviewService
from sdlc_blackboard.application.use_cases.task_service import TaskService
from sdlc_blackboard.domain.approvals import ApprovalSubmission, ApprovalType
from sdlc_blackboard.domain.artifacts import (
    ArtifactAlias,
    ArtifactRevision,
    ArtifactStatus,
    ArtifactSubmission,
)
from sdlc_blackboard.domain.common import (
    ActorKind,
    ActorRef,
    ArtifactBinding,
    CommandContext,
)
from sdlc_blackboard.domain.findings import (
    Finding,
    FindingCreate,
    FindingSeverity,
    FindingState,
)
from sdlc_blackboard.domain.goals import Goal, GoalCreate, GoalState
from sdlc_blackboard.domain.reviews import ReviewDisposition, ReviewSubmission
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    ReviewRequirement,
    Task,
    TaskContractCreate,
    TaskState,
)
from tests.unit.fakes import FakeEnv

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)
OTHER = ActorRef(actor_id="impl-2", kind=ActorKind.IMPLEMENTATION)
QA = ActorRef(actor_id="qa-1", kind=ActorKind.QUALITY)

IMPL_LOGICAL = "source/x"


def _ctx(actor: ActorRef = LEAD, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


def _seed_goal(env: FakeEnv) -> Goal:
    goal = Goal(
        title="g",
        objective="o",
        success_criteria=("a",),
        constraints=(),
        owner=HUMAN,
        state=GoalState.ACTIVE,
        version=0,
    )
    env.goals.goals[goal.goal_id] = goal
    return goal


def _contract(
    goal_id: object,
    task_key: str = "impl",
    *,
    kind: ActorKind = ActorKind.IMPLEMENTATION,
    deliverables: tuple[DeliverableSpec, ...] = (
        DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),
    ),
    reviews: tuple[ReviewRequirement, ...] = (),
    dependency_task_ids: tuple[object, ...] = (),
) -> TaskContractCreate:
    return TaskContractCreate(
        goal_id=goal_id,  # type: ignore[arg-type]
        task_key=task_key,
        title=task_key,
        objective="do",
        required_actor_kind=kind,
        scope=("demo",),
        deliverables=deliverables,
        acceptance_criteria=("ok",),
        review_requirements=reviews,
        dependency_task_ids=dependency_task_ids,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# TaskService.create_task — draft-vs-ready + dependency + conflict branches    #
# --------------------------------------------------------------------------- #


class TestCreateTask:
    async def test_create_without_dependencies_is_ready_and_emits_ready_event(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        res = await svc.create_task(_ctx(), _contract(goal.goal_id))
        assert res.value is not None
        assert res.value.state == TaskState.READY
        assert "task.created" in env.events.types()
        assert "task.ready" in env.events.types()

    async def test_create_with_dependencies_is_draft_and_no_ready_event(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        dep = await svc.create_task(_ctx(), _contract(goal.goal_id, "dep"))
        assert dep.value is not None
        env.events.events.clear()
        res = await svc.create_task(
            _ctx(),
            _contract(goal.goal_id, "child", dependency_task_ids=(dep.value.task_id,)),
        )
        assert res.value is not None
        assert res.value.state == TaskState.DRAFT
        assert "task.ready" not in env.events.types()
        # The dependency edge was recorded.
        assert env.tasks.dependencies[res.value.task_id] == (dep.value.task_id,)

    async def test_create_missing_goal_is_not_found(self) -> None:
        env = FakeEnv()
        svc = TaskService(env.ports)
        res = await svc.create_task(_ctx(), _contract(uuid4()))
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_dependency_outside_goal_is_precondition_failed(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        res = await svc.create_task(
            _ctx(), _contract(goal.goal_id, "child", dependency_task_ids=(uuid4(),))
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_same_key_same_contract_returns_existing(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        c = _contract(goal.goal_id)
        first = await svc.create_task(_ctx(), c)
        assert first.value is not None
        second = await svc.create_task(_ctx(), c)
        assert second.value is not None
        assert second.value.task_id == first.value.task_id

    async def test_same_key_different_contract_is_conflict(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        await svc.create_task(_ctx(), _contract(goal.goal_id))
        res = await svc.create_task(_ctx(), _contract(goal.goal_id, "impl", kind=ActorKind.ANALYST))
        assert res.error is not None
        assert res.status == CommandStatus.CONFLICT_CREATED


# --------------------------------------------------------------------------- #
# TaskService.refresh_ready_tasks                                              #
# --------------------------------------------------------------------------- #


class TestRefreshReady:
    async def test_draft_promoted_only_when_all_deps_accepted(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        dep = await svc.create_task(_ctx(), _contract(goal.goal_id, "dep"))
        assert dep.value is not None
        child = await svc.create_task(
            _ctx(),
            _contract(goal.goal_id, "child", dependency_task_ids=(dep.value.task_id,)),
        )
        assert child.value is not None

        # Dependency not yet accepted -> no promotion.
        res = await svc.refresh_ready_tasks(_ctx(), RefreshReadyTasksRequest(goal_id=goal.goal_id))
        assert res.value is not None
        assert res.value.tasks == ()

        # Accept the dependency, then refresh promotes the child.
        env.tasks.tasks[dep.value.task_id] = env.tasks.tasks[dep.value.task_id].model_copy(
            update={"state": TaskState.ACCEPTED}
        )
        res2 = await svc.refresh_ready_tasks(_ctx(), RefreshReadyTasksRequest(goal_id=goal.goal_id))
        assert res2.value is not None
        assert tuple(t.task_id for t in res2.value.tasks) == (child.value.task_id,)
        assert "task.ready" in env.events.types()


# --------------------------------------------------------------------------- #
# TaskService.claim_task — precondition + fencing + CAS-miss                   #
# --------------------------------------------------------------------------- #


class TestClaim:
    async def _ready_task(self, env: FakeEnv) -> Task:
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        res = await svc.create_task(_ctx(), _contract(goal.goal_id))
        assert res.value is not None
        return res.value

    async def test_claim_bumps_epoch_and_assigns(self) -> None:
        env = FakeEnv()
        task = await self._ready_task(env)
        svc = TaskService(env.ports)
        res = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=task.task_id, actor_id=IMPL.actor_id)
        )
        assert res.value is not None
        assert res.value.assignment_epoch == 1
        assert res.value.task.state == TaskState.ASSIGNED
        assert res.value.task.assigned_actor_id == IMPL.actor_id

    async def test_claim_missing_task_is_not_found(self) -> None:
        env = FakeEnv()
        svc = TaskService(env.ports)
        res = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=uuid4(), actor_id=IMPL.actor_id)
        )
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_claim_rejected_when_task_not_ready(self) -> None:
        env = FakeEnv()
        task = await self._ready_task(env)
        env.tasks.tasks[task.task_id] = task.model_copy(update={"state": TaskState.RUNNING})
        svc = TaskService(env.ports)
        res = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=task.task_id, actor_id=IMPL.actor_id)
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_second_claim_hits_open_assignment_conflict(self) -> None:
        env = FakeEnv()
        task = await self._ready_task(env)
        svc = TaskService(env.ports)
        first = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=task.task_id, actor_id=IMPL.actor_id)
        )
        assert first.value is not None
        # Drift the task back to READY but leave the assignment open (recovery drift):
        # the next claim's open_assignment collides with the partial unique index.
        env.tasks.tasks[task.task_id] = env.tasks.tasks[task.task_id].model_copy(
            update={"state": TaskState.READY, "version": 5}
        )
        res = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=task.task_id, actor_id=IMPL.actor_id)
        )
        assert res.error is not None
        assert res.status == CommandStatus.CONFLICT_CREATED


# --------------------------------------------------------------------------- #
# TaskService.bind + start_runtime_run — epoch/actor guards + routing_class    #
# --------------------------------------------------------------------------- #


class TestRunLifecycle:
    async def _assigned(self, env: FakeEnv) -> tuple[Task, int]:
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        created = await svc.create_task(_ctx(), _contract(goal.goal_id))
        assert created.value is not None
        claim = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=created.value.task_id, actor_id=IMPL.actor_id)
        )
        assert claim.value is not None
        return claim.value.task, claim.value.assignment_epoch

    async def test_bind_stale_epoch_is_stale_assignment(self) -> None:
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        svc = TaskService(env.ports)
        res = await svc.bind_runtime_session(
            _ctx(IMPL, epoch=epoch + 99),
            BindRuntimeSessionRequest(task_id=task.task_id, omnigent_conversation_id="conv"),
        )
        assert res.error is not None
        assert res.status == CommandStatus.STALE_ASSIGNMENT

    async def test_start_run_wrong_actor_is_unauthorized(self) -> None:
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        svc = TaskService(env.ports)
        res = await svc.start_runtime_run(
            _ctx(OTHER, epoch=epoch),
            StartRunRequest(task_id=task.task_id, omnigent_conversation_id="conv"),
        )
        assert res.error is not None
        assert res.status == CommandStatus.UNAUTHORIZED

    async def test_start_run_wrong_state_is_precondition_failed(self) -> None:
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        env.tasks.tasks[task.task_id] = env.tasks.tasks[task.task_id].model_copy(
            update={"state": TaskState.SUBMITTED}
        )
        svc = TaskService(env.ports)
        res = await svc.start_runtime_run(
            _ctx(IMPL, epoch=epoch),
            StartRunRequest(task_id=task.task_id, omnigent_conversation_id="conv"),
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_start_run_transitions_to_running_and_persists_routing_class(self) -> None:
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        svc = TaskService(env.ports)
        res = await svc.start_runtime_run(
            _ctx(IMPL, epoch=epoch),
            StartRunRequest(
                task_id=task.task_id,
                omnigent_conversation_id="conv",
                routing_class="regional_mantle",
            ),
        )
        assert res.value is not None
        assert res.value.routing_class is not None
        assert res.value.routing_class.value == "regional_mantle"
        assert env.tasks.tasks[task.task_id].state == TaskState.RUNNING

    async def test_start_run_without_routing_class_derives_policy_default(self) -> None:
        """R2: with no explicit routing_class, the run gets the policy default for the
        task's required actor kind. The _assigned helper builds an IMPLEMENTATION task,
        which the Lean-certified policy maps to geo_inference_profile."""
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        svc = TaskService(env.ports)
        res = await svc.start_runtime_run(
            _ctx(IMPL, epoch=epoch),
            StartRunRequest(task_id=task.task_id, omnigent_conversation_id="conv"),
        )
        assert res.value is not None
        assert res.value.routing_class is not None
        assert res.value.routing_class.value == "geo_inference_profile"

    async def test_start_run_bad_routing_class_is_validation_failed(self) -> None:
        env = FakeEnv()
        task, epoch = await self._assigned(env)
        svc = TaskService(env.ports)
        res = await svc.start_runtime_run(
            _ctx(IMPL, epoch=epoch),
            StartRunRequest(
                task_id=task.task_id,
                omnigent_conversation_id="conv",
                routing_class="not_a_real_class",
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.VALIDATION_FAILED


# --------------------------------------------------------------------------- #
# TaskService.submit_task_result — manifest mismatch + fan-out + result_manifest#
# --------------------------------------------------------------------------- #


class TestSubmit:
    async def _running(
        self, env: FakeEnv, *, reviews: tuple[ReviewRequirement, ...] = ()
    ) -> tuple[Task, int, UUID]:
        goal = _seed_goal(env)
        svc = TaskService(env.ports)
        created = await svc.create_task(_ctx(), _contract(goal.goal_id, reviews=reviews))
        assert created.value is not None
        claim = await svc.claim_task(
            _ctx(), ClaimTaskRequest(task_id=created.value.task_id, actor_id=IMPL.actor_id)
        )
        assert claim.value is not None
        epoch = claim.value.assignment_epoch
        run = await svc.start_runtime_run(
            _ctx(IMPL, epoch=epoch),
            StartRunRequest(task_id=created.value.task_id, omnigent_conversation_id="conv"),
        )
        assert run.value is not None
        return claim.value.task, epoch, run.value.run_id

    def _submission(self, task_id: UUID, run_id: UUID) -> SubmitTaskResult:
        return SubmitTaskResult(
            task_id=task_id,
            run_id=run_id,
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
            assumptions=("assumed stable",),
            unresolved_questions=("rate limits?",),
            residual_risks=("perf",),
        )

    async def test_submit_persists_full_result_manifest(self) -> None:
        env = FakeEnv()
        task, epoch, run_id = await self._running(env)
        svc = TaskService(env.ports)
        res = await svc.submit_task_result(
            _ctx(IMPL, epoch=epoch), self._submission(task.task_id, run_id)
        )
        assert res.value is not None
        assert res.value.task.state == TaskState.SUBMITTED
        manifest = env.runs.manifests[run_id]
        assert manifest is not None
        assert manifest["disposition"] == "completed"
        assert manifest["summary"] == "run summary"
        assert manifest["assumptions"] == ["assumed stable"]
        assert manifest["unresolved_questions"] == ["rate limits?"]
        assert manifest["residual_risks"] == ["perf"]
        # Assignment was completed.
        assert task.task_id not in env.assignments.open

    async def test_submit_input_manifest_mismatch_is_precondition_failed(self) -> None:
        env = FakeEnv()
        task, epoch, run_id = await self._running(env)
        svc = TaskService(env.ports)
        binding = ArtifactBinding(
            artifact_id=uuid4(), revision_id=uuid4(), logical_name="x", content_hash="z"
        )
        sub = self._submission(task.task_id, run_id).model_copy(
            update={"input_manifest": (binding,)}
        )
        res = await svc.submit_task_result(_ctx(IMPL, epoch=epoch), sub)
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_submit_fans_out_one_review_task_per_requirement(self) -> None:
        env = FakeEnv()
        reviews = (
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
        )
        task, epoch, run_id = await self._running(env, reviews=reviews)
        svc = TaskService(env.ports)
        res = await svc.submit_task_result(
            _ctx(IMPL, epoch=epoch), self._submission(task.task_id, run_id)
        )
        assert res.value is not None
        assert len(res.value.review_task_ids) == 2
        review_keys = {env.tasks.tasks[rid].task_key for rid in res.value.review_task_ids}
        assert review_keys == {"impl:review:quality", "impl:review:security"}
        # The review tasks are READY for a reviewer to claim.
        assert all(
            env.tasks.tasks[rid].state == TaskState.READY for rid in res.value.review_task_ids
        )


# --------------------------------------------------------------------------- #
# TaskService.accept_task — idempotency + transition path + wrong-state        #
# --------------------------------------------------------------------------- #


class TestAccept:
    def _submitted_task(self, env: FakeEnv) -> Task:
        goal = _seed_goal(env)
        task = Task(
            goal_id=goal.goal_id,
            task_key="impl",
            title="impl",
            objective="o",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            state=TaskState.SUBMITTED,
            version=0,
            assignment_epoch=1,
            contract=_contract(goal.goal_id),
        )
        env.tasks.tasks[task.task_id] = task
        return task

    async def test_accept_from_submitted_drives_two_transitions(self) -> None:
        env = FakeEnv()
        task = self._submitted_task(env)
        svc = TaskService(env.ports)
        res = await svc.accept_task(_ctx(), AcceptTaskRequest(task_id=task.task_id))
        assert res.value is not None
        assert res.value.state == TaskState.ACCEPTED
        types = env.events.types()
        assert "task.under_review" in types
        assert "task.accepted" in types

    async def test_accept_is_idempotent_on_already_accepted(self) -> None:
        env = FakeEnv()
        task = self._submitted_task(env)
        env.tasks.tasks[task.task_id] = task.model_copy(update={"state": TaskState.ACCEPTED})
        svc = TaskService(env.ports)
        res = await svc.accept_task(_ctx(), AcceptTaskRequest(task_id=task.task_id))
        assert res.value is not None
        assert res.value.state == TaskState.ACCEPTED
        assert "task.accepted" not in env.events.types()  # no new event, pure no-op

    async def test_accept_resumes_from_under_review(self) -> None:
        env = FakeEnv()
        task = self._submitted_task(env)
        env.tasks.tasks[task.task_id] = task.model_copy(update={"state": TaskState.UNDER_REVIEW})
        svc = TaskService(env.ports)
        res = await svc.accept_task(_ctx(), AcceptTaskRequest(task_id=task.task_id))
        assert res.value is not None
        assert res.value.state == TaskState.ACCEPTED
        assert "task.under_review" not in env.events.types()  # skipped the first hop

    async def test_accept_wrong_state_is_precondition_failed(self) -> None:
        env = FakeEnv()
        task = self._submitted_task(env)
        env.tasks.tasks[task.task_id] = task.model_copy(update={"state": TaskState.READY})
        svc = TaskService(env.ports)
        res = await svc.accept_task(_ctx(), AcceptTaskRequest(task_id=task.task_id))
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED


# --------------------------------------------------------------------------- #
# GoalService.authorize_goal_completion — gate re-check on the same UoW         #
# --------------------------------------------------------------------------- #


class TestAuthorize:
    async def test_missing_goal_is_not_found(self) -> None:
        env = FakeEnv()
        svc = GoalService(env.ports)
        res = await svc.authorize_goal_completion(_ctx(HUMAN), uuid4())
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_unsatisfied_gate_rejects_and_leaves_goal_active(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = GoalService(env.ports)
        res = await svc.authorize_goal_completion(_ctx(HUMAN), goal.goal_id)
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED
        assert env.goals.goals[goal.goal_id].state == GoalState.ACTIVE

    async def test_satisfied_gate_flips_goal_to_satisfied(self) -> None:
        # Drive the fake ports to a SATISFIED gate: an impl task with blocking quality+
        # security reviews, a promoted implementation binding, matching non-stale APPROVED
        # reviews, and a non-revoked human approval on the current binding.
        env = FakeEnv()
        goal = _seed_goal(env)
        reviews = (
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
        )
        impl_task = Task(
            goal_id=goal.goal_id,
            task_key="impl",
            title="impl",
            objective="o",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            state=TaskState.ACCEPTED,
            version=0,
            assignment_epoch=1,
            contract=_contract(goal.goal_id, reviews=reviews),
        )
        env.tasks.tasks[impl_task.task_id] = impl_task

        artifact_id = uuid4()
        revision = ArtifactRevision(
            artifact_id=artifact_id,
            artifact_type="source",
            logical_name=IMPL_LOGICAL,
            content_uri="git://x/h1",
            content_hash="h1",
            summary="s",
            produced_by_task_id=impl_task.task_id,
            produced_by_run_id=uuid4(),
            parent_revision_ids=(),
            status=ArtifactStatus.CANDIDATE,
        )
        env.artifacts.revisions[revision.revision_id] = revision
        # Initial alias is created at submit time via upsert_alias_initial, not by promote.
        await env.artifacts.upsert_alias_initial(
            object(),
            ArtifactAlias(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                current_revision_id=revision.revision_id,
                version=0,
            ),
        )
        binding = ArtifactBinding(
            artifact_id=artifact_id,
            revision_id=revision.revision_id,
            logical_name=IMPL_LOGICAL,
            content_hash="h1",
        )

        review_svc = ReviewService(env.ports)
        for reviewer, rtype in (
            (QA, "quality"),
            (ActorRef(actor_id="sec", kind=ActorKind.SECURITY), "security"),
        ):
            r = await review_svc.submit_review(
                _ctx(reviewer),
                ReviewSubmission(
                    goal_id=goal.goal_id,
                    review_task_id=impl_task.task_id,
                    reviewer=reviewer,
                    review_type=rtype,
                    artifact_bindings=(binding,),
                    disposition=ReviewDisposition.APPROVED,
                    summary=f"{rtype} ok",
                ),
            )
            assert r.value is not None
        appr = await review_svc.record_human_approval(
            _ctx(HUMAN),
            ApprovalSubmission(
                goal_id=goal.goal_id,
                approval_type=ApprovalType.HUMAN_RELEASE,
                approver=HUMAN,
                artifact_bindings=(binding,),
            ),
        )
        assert appr.value is not None

        goal_svc = GoalService(env.ports)
        res = await goal_svc.authorize_goal_completion(_ctx(HUMAN), goal.goal_id)
        assert res.value is not None, res.error
        assert res.value.state == GoalState.SATISFIED


# --------------------------------------------------------------------------- #
# ArtifactService.promote_artifact — CAS + null-expected rejection             #
# --------------------------------------------------------------------------- #


class TestPromote:
    def _revision(self, env: FakeEnv, goal_id: object, *, content_hash: str) -> ArtifactRevision:
        rev = ArtifactRevision(
            artifact_id=uuid4(),
            artifact_type="source",
            logical_name=IMPL_LOGICAL,
            content_uri=f"git://x/{content_hash}",
            content_hash=content_hash,
            summary="s",
            produced_by_task_id=uuid4(),
            produced_by_run_id=uuid4(),
            parent_revision_ids=(),
            status=ArtifactStatus.CANDIDATE,
        )
        env.artifacts.revisions[rev.revision_id] = rev
        return rev

    async def test_promote_missing_revision_is_not_found(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = ArtifactService(env.ports)
        res = await svc.promote_artifact(
            _ctx(),
            PromoteArtifactRequest(
                goal_id=goal.goal_id, logical_name=IMPL_LOGICAL, new_revision_id=uuid4()
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_first_promote_null_expected_on_missing_alias_is_conflict(self) -> None:
        # No alias row exists yet (the initial alias is created at submit time via
        # upsert_alias_initial, never by promote). A null-expected promote against a
        # missing alias is an UPDATE that matches no row -> promote_alias_cas returns
        # None -> the service raises Conflict. This mirrors the real adapter
        # (repositories/artifacts.py:124-135); the fake used to auto-create here, which
        # made the old assertion vacuous.
        env = FakeEnv()
        goal = _seed_goal(env)
        rev = self._revision(env, goal.goal_id, content_hash="h1")
        svc = ArtifactService(env.ports)
        res = await svc.promote_artifact(
            _ctx(),
            PromoteArtifactRequest(
                goal_id=goal.goal_id, logical_name=IMPL_LOGICAL, new_revision_id=rev.revision_id
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.CONFLICT_CREATED

    async def test_null_expected_on_existing_alias_is_precondition_failed(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        rev1 = self._revision(env, goal.goal_id, content_hash="h1")
        rev2 = self._revision(env, goal.goal_id, content_hash="h2")
        svc = ArtifactService(env.ports)
        # Seed the initial alias the way the real system does (submit-time upsert).
        await env.artifacts.upsert_alias_initial(
            object(),
            ArtifactAlias(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                current_revision_id=rev1.revision_id,
                version=0,
            ),
        )
        # Alias now exists; a null-expected promote is a last-writer-wins bypass -> rejected.
        res = await svc.promote_artifact(
            _ctx(),
            PromoteArtifactRequest(
                goal_id=goal.goal_id, logical_name=IMPL_LOGICAL, new_revision_id=rev2.revision_id
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_stale_expected_revision_is_conflict(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        rev1 = self._revision(env, goal.goal_id, content_hash="h1")
        rev2 = self._revision(env, goal.goal_id, content_hash="h2")
        svc = ArtifactService(env.ports)
        # Seed the initial alias (submit-time upsert), current revision = rev1.
        await env.artifacts.upsert_alias_initial(
            object(),
            ArtifactAlias(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                current_revision_id=rev1.revision_id,
                version=0,
            ),
        )
        # Supply a wrong expected_current_revision_id -> CAS misses -> Conflict.
        res = await svc.promote_artifact(
            _ctx(),
            PromoteArtifactRequest(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                expected_current_revision_id=uuid4(),
                new_revision_id=rev2.revision_id,
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.CONFLICT_CREATED

    async def test_promote_marks_prior_binding_reviews_stale(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        rev1 = self._revision(env, goal.goal_id, content_hash="h1")
        # revision 2 shares the artifact_id (same logical artifact, new content).
        rev2 = ArtifactRevision(
            artifact_id=rev1.artifact_id,
            artifact_type="source",
            logical_name=IMPL_LOGICAL,
            content_uri="git://x/h2",
            content_hash="h2",
            summary="s",
            produced_by_task_id=uuid4(),
            produced_by_run_id=uuid4(),
            parent_revision_ids=(),
            status=ArtifactStatus.CANDIDATE,
        )
        env.artifacts.revisions[rev2.revision_id] = rev2
        svc = ArtifactService(env.ports)
        # Seed the initial alias (submit-time upsert), current revision = rev1.
        await env.artifacts.upsert_alias_initial(
            object(),
            ArtifactAlias(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                current_revision_id=rev1.revision_id,
                version=0,
            ),
        )
        # A review bound to rev1.
        review_svc = ReviewService(env.ports)
        review_task = Task(
            goal_id=goal.goal_id,
            task_key="impl:review:quality",
            title="review",
            objective="o",
            required_actor_kind=ActorKind.QUALITY,
            state=TaskState.RUNNING,
            version=0,
            assignment_epoch=1,
            contract=_contract(goal.goal_id, "impl:review:quality", kind=ActorKind.QUALITY),
        )
        env.tasks.tasks[review_task.task_id] = review_task
        binding1 = ArtifactBinding(
            artifact_id=rev1.artifact_id,
            revision_id=rev1.revision_id,
            logical_name=IMPL_LOGICAL,
            content_hash="h1",
        )
        r = await review_svc.submit_review(
            _ctx(QA),
            ReviewSubmission(
                goal_id=goal.goal_id,
                review_task_id=review_task.task_id,
                reviewer=QA,
                review_type="quality",
                artifact_bindings=(binding1,),
                disposition=ReviewDisposition.APPROVED,
                summary="ok",
            ),
        )
        assert r.value is not None
        # Promote to rev2 -> the rev1-bound review goes stale.
        res = await svc.promote_artifact(
            _ctx(),
            PromoteArtifactRequest(
                goal_id=goal.goal_id,
                logical_name=IMPL_LOGICAL,
                expected_current_revision_id=rev1.revision_id,
                new_revision_id=rev2.revision_id,
            ),
        )
        assert res.value is not None
        assert env.reviews.reviews[r.value.review_id].stale is True
        assert "review.invalidated" in env.events.types()


# --------------------------------------------------------------------------- #
# ReviewService — blocking-finding authority + approved-with-open-blocker       #
# --------------------------------------------------------------------------- #


class TestReview:
    def _review_task(self, env: FakeEnv, *, kind: ActorKind, may_block: bool) -> Task:
        goal = _seed_goal(env)
        contract = TaskContractCreate(
            goal_id=goal.goal_id,
            task_key="rev",
            title="rev",
            objective="o",
            required_actor_kind=kind,
            scope=("x",),
            deliverables=(),
            acceptance_criteria=("ok",),
            may_create_blocking_finding=may_block,
        )
        task = Task(
            goal_id=goal.goal_id,
            task_key="rev",
            title="rev",
            objective="o",
            required_actor_kind=kind,
            state=TaskState.RUNNING,
            version=0,
            assignment_epoch=1,
            contract=contract,
        )
        env.tasks.tasks[task.task_id] = task
        return task

    def _finding_create(self, task: Task, *, blocking: bool) -> FindingCreate:
        return FindingCreate(
            goal_id=task.goal_id,
            task_id=task.task_id,
            category="correctness",
            severity=FindingSeverity.HIGH,
            statement="broken",
            affected_artifacts=(),
            evidence=(),
            blocking=blocking,
            resolution_criteria=("fix it",),
        )

    async def test_blocking_finding_allowed_for_authorized_reviewer(self) -> None:
        env = FakeEnv()
        task = self._review_task(env, kind=ActorKind.QUALITY, may_block=True)
        svc = ReviewService(env.ports)
        res = await svc.open_finding(_ctx(QA), self._finding_create(task, blocking=True))
        assert res.value is not None
        assert res.value.blocking is True

    async def test_blocking_finding_rejected_without_contract_flag(self) -> None:
        env = FakeEnv()
        task = self._review_task(env, kind=ActorKind.QUALITY, may_block=False)
        svc = ReviewService(env.ports)
        res = await svc.open_finding(_ctx(QA), self._finding_create(task, blocking=True))
        assert res.error is not None
        assert res.status == CommandStatus.UNAUTHORIZED

    async def test_blocking_finding_rejected_for_non_reviewer_kind(self) -> None:
        env = FakeEnv()
        # A producer kind cannot open a blocking finding even with the flag set.
        task = self._review_task(env, kind=ActorKind.IMPLEMENTATION, may_block=True)
        svc = ReviewService(env.ports)
        res = await svc.open_finding(_ctx(IMPL), self._finding_create(task, blocking=True))
        assert res.error is not None
        assert res.status == CommandStatus.UNAUTHORIZED

    async def test_non_blocking_finding_needs_no_authority(self) -> None:
        env = FakeEnv()
        task = self._review_task(env, kind=ActorKind.IMPLEMENTATION, may_block=False)
        svc = ReviewService(env.ports)
        res = await svc.open_finding(_ctx(IMPL), self._finding_create(task, blocking=False))
        assert res.value is not None

    async def test_open_finding_missing_task_is_not_found(self) -> None:
        env = FakeEnv()
        goal = _seed_goal(env)
        svc = ReviewService(env.ports)
        req = FindingCreate(
            goal_id=goal.goal_id,
            task_id=uuid4(),
            category="c",
            severity=FindingSeverity.LOW,
            statement="s",
            affected_artifacts=(),
            evidence=(),
            blocking=False,
            resolution_criteria=("x",),
        )
        res = await svc.open_finding(_ctx(QA), req)
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_review_without_bindings_is_precondition_failed(self) -> None:
        env = FakeEnv()
        task = self._review_task(env, kind=ActorKind.QUALITY, may_block=True)
        svc = ReviewService(env.ports)
        res = await svc.submit_review(
            _ctx(QA),
            ReviewSubmission(
                goal_id=task.goal_id,
                review_task_id=task.task_id,
                reviewer=QA,
                review_type="quality",
                artifact_bindings=(),
                disposition=ReviewDisposition.APPROVED,
                summary="ok",
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    async def test_approved_review_with_open_blocker_is_precondition_failed(self) -> None:
        env = FakeEnv()
        task = self._review_task(env, kind=ActorKind.QUALITY, may_block=True)
        finding = Finding(
            goal_id=task.goal_id,
            task_id=task.task_id,
            category="c",
            severity=FindingSeverity.HIGH,
            statement="s",
            affected_artifacts=(),
            evidence=(),
            blocking=True,
            resolution_criteria=("x",),
            state=FindingState.OPEN,
            version=0,
        )
        env.findings.findings[finding.finding_id] = finding
        binding = ArtifactBinding(
            artifact_id=uuid4(), revision_id=uuid4(), logical_name="x", content_hash="h"
        )
        svc = ReviewService(env.ports)
        res = await svc.submit_review(
            _ctx(QA),
            ReviewSubmission(
                goal_id=task.goal_id,
                review_task_id=task.task_id,
                reviewer=QA,
                review_type="quality",
                artifact_bindings=(binding,),
                disposition=ReviewDisposition.APPROVED,
                summary="ok",
                finding_ids=(finding.finding_id,),
            ),
        )
        assert res.error is not None
        assert res.status == CommandStatus.PRECONDITION_FAILED

    def _seed_finding(self, env: FakeEnv, *, version: int = 0) -> Finding:
        goal = _seed_goal(env)
        finding = Finding(
            goal_id=goal.goal_id,
            task_id=uuid4(),
            category="c",
            severity=FindingSeverity.HIGH,
            statement="s",
            affected_artifacts=(),
            evidence=(),
            blocking=True,
            resolution_criteria=("x",),
            state=FindingState.OPEN,
            version=version,
        )
        env.findings.findings[finding.finding_id] = finding
        return finding

    async def test_resolve_finding_transitions_state(self) -> None:
        env = FakeEnv()
        finding = self._seed_finding(env)
        svc = ReviewService(env.ports)
        res = await svc.resolve_finding(
            _ctx(QA),
            ResolveFindingRequest(finding_id=finding.finding_id, new_state=FindingState.VERIFIED),
        )
        assert res.value is not None
        assert res.value.state == FindingState.VERIFIED
        assert res.value.version == finding.version + 1

    async def test_resolve_finding_missing_is_not_found(self) -> None:
        env = FakeEnv()
        svc = ReviewService(env.ports)
        res = await svc.resolve_finding(
            _ctx(QA),
            ResolveFindingRequest(finding_id=uuid4(), new_state=FindingState.VERIFIED),
        )
        assert res.error is not None
        assert res.status == CommandStatus.NOT_FOUND

    async def test_resolve_finding_cas_miss_is_stale_version(self) -> None:
        # A concurrent bump between the service's read and its CAS write: model it with a
        # repo whose set_state_cas always misses (the None-on-version-mismatch contract).
        from tests.unit.fakes import FakeFindingRepo

        class StaleFindingRepo(FakeFindingRepo):
            async def set_state_cas(self, conn, finding_id, expected_version, new_state):  # type: ignore[no-untyped-def]
                return None

        env = FakeEnv()
        env.findings = StaleFindingRepo()
        env.ports = env.ports.__class__(
            uow=env.uow,
            clock=env.clock,
            goals=env.goals,
            tasks=env.tasks,
            assignments=env.assignments,
            runs=env.runs,
            artifacts=env.artifacts,
            findings=env.findings,
            reviews=env.reviews,
            approvals=env.approvals,
            events=env.events,
            outbox=env.outbox,
            processed_commands=env.processed_commands,
            command_failures=env.command_failures,
        )
        finding = self._seed_finding(env)
        svc = ReviewService(env.ports)
        res = await svc.resolve_finding(
            _ctx(QA),
            ResolveFindingRequest(finding_id=finding.finding_id, new_state=FindingState.VERIFIED),
        )
        assert res.error is not None
        assert res.status == CommandStatus.STALE_VERSION


# --------------------------------------------------------------------------- #
# Idempotency — replay hit + payload mismatch (through the base envelope)       #
# --------------------------------------------------------------------------- #


class TestIdempotency:
    async def test_duplicate_command_same_payload_replays(self) -> None:
        env = FakeEnv()
        goal_create = GoalCreate(title="g", objective="o", success_criteria=("a",), owner=HUMAN)
        svc = GoalService(env.ports)
        ctx = _ctx(HUMAN)
        first = await svc.create_goal(ctx, goal_create)
        assert first.value is not None
        assert first.status == CommandStatus.ACCEPTED
        # Same command_id + same payload -> replay of the stored response, not a re-run.
        goals_before = len(env.goals.goals)
        second = await svc.create_goal(ctx, goal_create)
        assert second.status == CommandStatus.DUPLICATE_REPLAYED
        assert second.replayed is True
        assert second.value is not None
        assert second.value.goal_id == first.value.goal_id
        assert len(env.goals.goals) == goals_before  # body did NOT run again

    async def test_reused_command_id_different_payload_is_mismatch(self) -> None:
        env = FakeEnv()
        svc = GoalService(env.ports)
        ctx = _ctx(HUMAN)
        await svc.create_goal(
            ctx, GoalCreate(title="g", objective="o", success_criteria=("a",), owner=HUMAN)
        )
        # Same command_id, different payload -> rejected without running the body.
        goals_before = len(env.goals.goals)
        res = await svc.create_goal(
            ctx,
            GoalCreate(title="DIFFERENT", objective="o", success_criteria=("a",), owner=HUMAN),
        )
        assert res.error is not None
        assert res.error.code.value == "duplicate_command_mismatch"
        assert res.status == CommandStatus.VALIDATION_FAILED
        assert len(env.goals.goals) == goals_before


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
