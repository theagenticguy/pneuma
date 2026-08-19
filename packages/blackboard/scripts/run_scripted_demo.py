"""Operator-runnable scripted demo (no LLMs) — the full report-export lifecycle.

Mirrors the E2E test as a standalone script so an operator can watch the kernel drive
the whole flow against a live Postgres without Omnigent or any model. Run via
``mise run demo`` (or ``uv run python scripts/run_scripted_demo.py``) after ``migrate``.

Prints each step and the final gate + goal state.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sdlc_blackboard.application.commands import (
    ClaimTaskRequest,
    PromoteArtifactRequest,
    ResolveFindingRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.domain.approvals import ApprovalSubmission, ApprovalType
from sdlc_blackboard.domain.artifacts import ArtifactSubmission
from sdlc_blackboard.domain.common import ActorKind, ActorRef, ArtifactBinding, CommandContext
from sdlc_blackboard.domain.findings import FindingCreate, FindingSeverity, FindingState
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.reviews import ReviewDisposition, ReviewSubmission
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    ReviewRequirement,
    TaskContractCreate,
)
from sdlc_blackboard.infrastructure.di import Container, build_container

HUMAN = ActorRef(actor_id="human-1", kind=ActorKind.HUMAN)
LEAD = ActorRef(actor_id="lead-1", kind=ActorKind.LEAD)
IMPL = ActorRef(actor_id="impl-1", kind=ActorKind.IMPLEMENTATION)
QA = ActorRef(actor_id="qa-1", kind=ActorKind.QUALITY)
SEC = ActorRef(actor_id="sec-1", kind=ActorKind.SECURITY)
IMPL_LOGICAL = "source/report-export"


def _ctx(actor: ActorRef, *, epoch: int | None = None) -> CommandContext:
    return CommandContext(command_id=uuid4(), actor=actor, assignment_epoch=epoch)


def _step(msg: str) -> None:
    print(f"  → {msg}")


async def _run_impl(
    c: Container, task_id: UUID, content_hash: str, *, reset: bool
) -> ArtifactBinding:
    svc = c.services
    if reset:
        async with c.postgres.transaction() as conn:
            await conn.execute(
                "update tasks set state='ready', version=version+1 where task_id=$1", task_id
            )
    claim = await svc.tasks.claim_task(
        _ctx(LEAD), ClaimTaskRequest(task_id=task_id, actor_id=IMPL.actor_id)
    )
    assert claim.value is not None
    epoch = claim.value.assignment_epoch
    run = await svc.tasks.start_runtime_run(
        _ctx(IMPL, epoch=epoch),
        StartRunRequest(
            task_id=task_id,
            omnigent_conversation_id=f"conv-{content_hash}",
            provider="amazon-bedrock",
            model_id="global.anthropic.claude-opus-4-8",
            harness="claude-sdk",
        ),
    )
    assert run.value is not None
    submitted = await svc.tasks.submit_task_result(
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
                    content_uri=f"git://demo_app/{content_hash}",
                    content_hash=content_hash,
                    summary="report export implementation",
                ),
            ),
            summary="implemented export endpoint",
        ),
    )
    assert submitted.value is not None
    rev = submitted.value.artifact_revisions[0]
    return ArtifactBinding(
        artifact_id=rev.artifact_id,
        revision_id=rev.revision_id,
        logical_name=rev.logical_name,
        content_hash=rev.content_hash,
    )


async def main() -> None:
    c = await build_container()
    svc = c.services
    try:
        _step("create goal")
        goal = await svc.goals.create_goal(
            _ctx(HUMAN),
            GoalCreate(
                title="Authenticated CSV report export",
                objective="Add POST /api/v1/reports/export with permission + injection safety",
                success_criteria=("QA approved", "security approved", "human approved"),
                owner=HUMAN,
            ),
        )
        assert goal.value is not None
        goal_id = goal.value.goal_id

        _step("create implementation task (QA + security reviews required)")
        impl = await svc.tasks.create_task(
            _ctx(LEAD),
            TaskContractCreate(
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
            ),
        )
        assert impl.value is not None
        impl_id = impl.value.task_id

        _step("implement revision 1 (with CSV-injection risk)")
        rev1 = await _run_impl(c, impl_id, "rev1sha", reset=False)

        async with c.postgres.connection() as conn:
            rows = await conn.fetch(
                "select task_id, required_actor_kind from tasks "
                "where goal_id=$1 and task_key like '%:review:%'",
                goal_id,
            )
        qa_id = next(r["task_id"] for r in rows if r["required_actor_kind"] == "quality")
        sec_id = next(r["task_id"] for r in rows if r["required_actor_kind"] == "security")

        _step("QA approves revision 1")
        await svc.reviews.submit_review(
            _ctx(QA),
            ReviewSubmission(
                goal_id=goal_id,
                review_task_id=qa_id,
                reviewer=QA,
                review_type="quality",
                artifact_bindings=(rev1,),
                disposition=ReviewDisposition.APPROVED,
                summary="QA rev1",
            ),
        )

        _step("security opens BLOCKING finding + requests revision")
        finding = await svc.reviews.open_finding(
            _ctx(SEC),
            FindingCreate(
                goal_id=goal_id,
                task_id=sec_id,
                category="csv-injection",
                severity=FindingSeverity.HIGH,
                statement="formula-leading chars not neutralized",
                affected_artifacts=(rev1,),
                evidence=(),
                blocking=True,
                resolution_criteria=("neutralize = + - @",),
            ),
        )
        assert finding.value is not None
        await svc.reviews.submit_review(
            _ctx(SEC),
            ReviewSubmission(
                goal_id=goal_id,
                review_task_id=sec_id,
                reviewer=SEC,
                review_type="security",
                artifact_bindings=(rev1,),
                disposition=ReviewDisposition.REQUEST_REVISION,
                summary="blocking finding",
                finding_ids=(finding.value.finding_id,),
            ),
        )
        gate1 = await svc.gate.get_gate_status(goal_id)
        _step(
            f"gate after blocker: {gate1.status.value} (open blockers: {len(gate1.open_blocking_finding_ids)})"
        )

        _step("remediation: implement revision 2")
        rev2 = await _run_impl(c, impl_id, "rev2sha", reset=True)
        _step("promote revision 2 (revision 1 reviews go stale)")
        await svc.artifacts.promote_artifact(
            _ctx(LEAD),
            PromoteArtifactRequest(
                goal_id=goal_id,
                logical_name=IMPL_LOGICAL,
                expected_current_revision_id=rev1.revision_id,
                new_revision_id=rev2.revision_id,
            ),
        )
        await svc.reviews.resolve_finding(
            _ctx(SEC),
            ResolveFindingRequest(
                finding_id=finding.value.finding_id, new_state=FindingState.VERIFIED
            ),
        )

        _step("QA + security approve revision 2")
        for reviewer, tid, rtype in ((QA, qa_id, "quality"), (SEC, sec_id, "security")):
            await svc.reviews.submit_review(
                _ctx(reviewer),
                ReviewSubmission(
                    goal_id=goal_id,
                    review_task_id=tid,
                    reviewer=reviewer,
                    review_type=rtype,
                    artifact_bindings=(rev2,),
                    disposition=ReviewDisposition.APPROVED,
                    summary=f"{rtype} rev2",
                ),
            )
        gate2 = await svc.gate.get_gate_status(goal_id)
        _step(
            f"gate before human: {gate2.status.value} (missing approvals: {gate2.missing_approvals})"
        )

        _step("human approves release (bound to revision 2)")
        await svc.reviews.record_human_approval(
            _ctx(HUMAN),
            ApprovalSubmission(
                goal_id=goal_id,
                approval_type=ApprovalType.HUMAN_RELEASE,
                approver=HUMAN,
                artifact_bindings=(rev2,),
            ),
        )
        gate3 = await svc.gate.get_gate_status(goal_id)
        _step(f"gate final: {gate3.status.value}")

        done = await svc.goals.authorize_goal_completion(_ctx(HUMAN), goal_id)
        assert done.value is not None
        print(f"\n✅ goal {goal_id} -> {done.value.state.value}")
        print(
            f"   gate: {gate3.status.value}; run `blackboard events {goal_id}` for the full trace"
        )
    finally:
        await c.postgres.stop()


if __name__ == "__main__":
    asyncio.run(main())
