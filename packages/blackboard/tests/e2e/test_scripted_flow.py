"""Scripted deterministic E2E — the full report-export SDLC lifecycle, no LLMs.

Proves the kernel independently of Omnigent and model behavior (handoff §21 "Scripted
E2E without LLMs"). Drives every reliability invariant end-to-end:

  goal -> analysis -> impl rev1 -> QA approves rev1 -> security blocks rev1
  -> remediation -> impl rev2 -> promote rev2 (rev1 reviews go stale)
  -> QA+security approve rev2 -> gate HUMAN_REQUIRED -> human approval -> gate SATISFIED
  -> authorize completion -> goal satisfied.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sdlc_blackboard.application.commands import (
    ClaimTaskRequest,
    PromoteArtifactRequest,
    ResolveFindingRequest,
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
from sdlc_blackboard.domain.events import GateStatus
from sdlc_blackboard.domain.findings import (
    FindingCreate,
    FindingSeverity,
    FindingState,
)
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.reviews import ReviewDisposition, ReviewSubmission
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    ReviewRequirement,
    TaskContractCreate,
)
from sdlc_blackboard.infrastructure.di import Container
from tests.e2e.conftest import E2E_READY

pytestmark = pytest.mark.skipif(not E2E_READY, reason="needs Docker + dbmate")

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)
QA = ActorRef(actor_id="qa-1", kind=ActorKind.QUALITY)
SEC = ActorRef(actor_id="sec-1", kind=ActorKind.SECURITY)

IMPL_LOGICAL = "source/report-export"


def _ctx(actor: ActorRef, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


async def test_scripted_report_export_flow(container: Container) -> None:
    svc = container.services

    # 1. Human goal.
    goal_res = await svc.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(
            title="Authenticated CSV report export",
            objective="Add POST /api/v1/reports/export with permission + injection safety",
            success_criteria=("QA approved", "security approved", "human approved"),
            owner=HUMAN,
        ),
    )
    assert goal_res.status == CommandStatus.ACCEPTED
    assert goal_res.value is not None
    goal_id = goal_res.value.goal_id

    # 2. Implementation task with QA + security review requirements.
    impl_contract = TaskContractCreate(
        goal_id=goal_id,
        task_key="implement-export",
        title="Implement export endpoint",
        objective="Implement the CSV export endpoint",
        required_actor_kind=ActorKind.IMPLEMENTATION,
        scope=("demo_app",),
        deliverables=(DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),),
        acceptance_criteria=("endpoint exists", "tests pass"),
        review_requirements=(
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
        ),
    )
    impl_res = await svc.tasks.create_task(_ctx(LEAD), impl_contract)
    assert impl_res.status == CommandStatus.ACCEPTED
    assert impl_res.value is not None
    impl_task_id = impl_res.value.task_id

    # 3. Claim + run + submit revision 1 (with the CSV-injection risk).
    rev1 = await _run_impl(container, goal_id, impl_task_id, content_hash="rev1sha")
    assert rev1.status == CommandStatus.ACCEPTED
    assert rev1.value is not None
    review_task_ids = rev1.value.review_task_ids
    assert len(review_task_ids) == 2
    rev1_binding = _binding(rev1.value.artifact_revisions[0])

    # 4. QA approves revision 1.
    qa_task_id, sec_task_id = await _review_task_ids(container, goal_id)
    qa1 = await svc.reviews.submit_review(
        _ctx(QA),
        ReviewSubmission(
            goal_id=goal_id,
            review_task_id=qa_task_id,
            reviewer=QA,
            review_type="quality",
            artifact_bindings=(rev1_binding,),
            disposition=ReviewDisposition.APPROVED,
            summary="QA passed rev1",
        ),
    )
    assert qa1.status == CommandStatus.ACCEPTED

    # 5. Security opens a BLOCKING finding on revision 1 and requests revision.
    finding_res = await svc.reviews.open_finding(
        _ctx(SEC),
        FindingCreate(
            goal_id=goal_id,
            task_id=sec_task_id,
            category="csv-injection",
            severity=FindingSeverity.HIGH,
            statement="Cells starting with = + - @ are not neutralized",
            affected_artifacts=(rev1_binding,),
            evidence=(),
            blocking=True,
            resolution_criteria=("neutralize formula-leading characters",),
        ),
    )
    assert finding_res.status == CommandStatus.ACCEPTED
    assert finding_res.value is not None
    finding_id = finding_res.value.finding_id

    sec1 = await svc.reviews.submit_review(
        _ctx(SEC),
        ReviewSubmission(
            goal_id=goal_id,
            review_task_id=sec_task_id,
            reviewer=SEC,
            review_type="security",
            artifact_bindings=(rev1_binding,),
            disposition=ReviewDisposition.REQUEST_REVISION,
            summary="blocking injection finding",
            finding_ids=(finding_id,),
        ),
    )
    assert sec1.status == CommandStatus.ACCEPTED

    # Gate must be UNSATISFIED with an open blocking finding.
    gate1 = await svc.gate.get_gate_status(goal_id)
    assert gate1.status == GateStatus.UNSATISFIED
    assert finding_id in gate1.open_blocking_finding_ids

    # 6. Remediation: a new implementation run produces revision 2.
    rev2 = await _run_impl(container, goal_id, impl_task_id, content_hash="rev2sha", reset=True)
    assert rev2.value is not None
    rev2_binding = _binding(rev2.value.artifact_revisions[0])
    assert rev2_binding.revision_id != rev1_binding.revision_id
    assert rev2_binding.artifact_id == rev1_binding.artifact_id  # same logical artifact

    # 7. Promote revision 2 -> revision 1 reviews go stale.
    promo = await svc.artifacts.promote_artifact(
        _ctx(LEAD),
        PromoteArtifactRequest(
            goal_id=goal_id,
            logical_name=IMPL_LOGICAL,
            expected_current_revision_id=rev1_binding.revision_id,
            new_revision_id=rev2_binding.revision_id,
        ),
    )
    assert promo.status == CommandStatus.ACCEPTED

    # 8. Remediate the finding.
    resolve = await svc.reviews.resolve_finding(
        _ctx(SEC),
        ResolveFindingRequest(finding_id=finding_id, new_state=FindingState.VERIFIED),
    )
    assert resolve.status == CommandStatus.ACCEPTED

    # 9. QA + security approve revision 2.
    for reviewer, task_id, rtype in (
        (QA, qa_task_id, "quality"),
        (SEC, sec_task_id, "security"),
    ):
        res = await svc.reviews.submit_review(
            _ctx(reviewer),
            ReviewSubmission(
                goal_id=goal_id,
                review_task_id=task_id,
                reviewer=reviewer,
                review_type=rtype,
                artifact_bindings=(rev2_binding,),
                disposition=ReviewDisposition.APPROVED,
                summary=f"{rtype} passed rev2",
            ),
        )
        assert res.status == CommandStatus.ACCEPTED

    # 10. Gate now HUMAN_REQUIRED (all reviews current + approved, no blockers).
    gate2 = await svc.gate.get_gate_status(goal_id)
    assert gate2.status == GateStatus.HUMAN_REQUIRED
    assert gate2.missing_approvals == ("human_release",)
    assert not gate2.missing_reviews
    assert not gate2.open_blocking_finding_ids

    # 11. Human approval bound to revision 2.
    appr = await svc.reviews.record_human_approval(
        _ctx(HUMAN),
        ApprovalSubmission(
            goal_id=goal_id,
            approval_type=ApprovalType.HUMAN_RELEASE,
            approver=HUMAN,
            artifact_bindings=(rev2_binding,),
        ),
    )
    assert appr.status == CommandStatus.ACCEPTED

    # 12. Gate SATISFIED -> authorize completion -> goal satisfied.
    gate3 = await svc.gate.get_gate_status(goal_id)
    assert gate3.status == GateStatus.SATISFIED

    done = await svc.goals.authorize_goal_completion(_ctx(HUMAN), goal_id)
    assert done.status == CommandStatus.ACCEPTED
    assert done.value is not None
    assert done.value.state.value == "satisfied"

    # Event trace sanity: the key lifecycle events all landed.
    events = await svc.query.read_relevant_events(goal_id, limit=200)
    types = [e.event_type for e in events]
    for expected in (
        "goal.created",
        "task.created",
        "artifact.created",
        "task.submitted",
        "finding.created",
        "review.submitted",
        "artifact.promoted",
        "review.invalidated",
        "finding.verified",
        "approval.created",
        "goal.satisfied",
    ):
        assert expected in types, f"missing event {expected} in {types}"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _binding(revision: ArtifactRevision) -> ArtifactBinding:
    return ArtifactBinding(
        artifact_id=revision.artifact_id,
        revision_id=revision.revision_id,
        logical_name=revision.logical_name,
        content_hash=revision.content_hash,
    )


async def _review_task_ids(container: Container, goal_id: UUID) -> tuple[UUID, UUID]:
    """Return (qa_task_id, security_task_id) created by submit_task_result."""
    async with container.postgres.connection() as conn:
        rows = await conn.fetch(
            "select task_id, required_actor_kind from tasks "
            "where goal_id=$1 and task_key like '%:review:%'",
            goal_id,
        )
    qa = next(r["task_id"] for r in rows if r["required_actor_kind"] == "quality")
    sec = next(r["task_id"] for r in rows if r["required_actor_kind"] == "security")
    return qa, sec


async def _run_impl(
    container: Container,
    goal_id: UUID,
    impl_task_id: UUID,
    *,
    content_hash: str,
    reset: bool = False,
) -> CommandResult[TaskSubmissionReceipt]:
    """Claim (or re-ready) the impl task, start a run, submit a source artifact."""
    svc = container.services
    if reset:
        # Remediation: return the accepted/submitted task to READY for a new epoch.
        async with container.postgres.transaction() as conn:
            await conn.execute(
                "update tasks set state='ready', version=version+1 where task_id=$1",
                impl_task_id,
            )

    claim = await svc.tasks.claim_task(
        _ctx(LEAD), ClaimTaskRequest(task_id=impl_task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch

    run = await svc.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch),
        StartRunRequest(
            task_id=impl_task_id,
            omnigent_conversation_id=f"conv-{content_hash}",
            input_manifest=(),
            provider="amazon-bedrock",
            model_id="global.anthropic.claude-opus-4-8",
            harness="claude-sdk",
        ),
    )
    assert run.value is not None
    run_id = run.value.run_id

    return await svc.tasks.submit_task_result(
        _ctx(IMPL, epoch=epoch),
        SubmitTaskResult(
            task_id=impl_task_id,
            run_id=run_id,
            disposition="completed",
            input_manifest=(),
            artifacts=(
                ArtifactSubmission(
                    artifact_type="source",
                    logical_name=IMPL_LOGICAL,
                    content_uri=f"git://demo_app/{content_hash}",
                    content_hash=content_hash,
                    summary="report export implementation",
                ),
            ),
            summary="implemented export endpoint",
        ),
    )
