"""Multi-bounded-context gate: the gate requires EVERY blocking review the contract
declares (quality + security + compliance + finops), derived, not hardcoded.

Proves HANDOFF §28 end-to-end: a governing context becomes a gate condition purely by
being declared in the task contract — no kernel change per persona.
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
from sdlc_blackboard.domain.approvals import ApprovalSubmission, ApprovalType
from sdlc_blackboard.domain.artifacts import ArtifactSubmission
from sdlc_blackboard.domain.common import (
    ActorKind,
    ActorRef,
    ArtifactBinding,
    CommandContext,
)
from sdlc_blackboard.domain.events import GateStatus
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.reviews import ReviewDisposition, ReviewSubmission
from sdlc_blackboard.domain.settings import Settings
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
IMPL_LOGICAL = "source/report-export"

# The governing contexts this goal engages, beyond quality+security.
GOVERNING = (
    (ActorKind.QUALITY, "quality"),
    (ActorKind.SECURITY, "security"),
    (ActorKind.COMPLIANCE, "compliance"),
    (ActorKind.FINOPS, "finops"),
)


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


async def test_gate_requires_every_declared_governing_review(container: Container) -> None:
    svc = container.services

    goal = await svc.goals.create_goal(
        _ctx(HUMAN),
        GoalCreate(
            title="Regulated CSV export",
            objective="Export with privacy + cost governance",
            success_criteria=("all governing reviews pass",),
            owner=HUMAN,
        ),
    )
    assert goal.value is not None
    goal_id = goal.value.goal_id

    impl = await svc.tasks.create_task(
        _ctx(LEAD),
        TaskContractCreate(
            goal_id=goal_id,
            task_key="implement-export",
            title="Implement export",
            objective="implement",
            required_actor_kind=ActorKind.IMPLEMENTATION,
            scope=("demo_app",),
            deliverables=(DeliverableSpec(artifact_type="source", logical_name=IMPL_LOGICAL),),
            acceptance_criteria=("works",),
            review_requirements=tuple(
                ReviewRequirement(reviewer_kind=kind, review_type=rtype)
                for kind, rtype in GOVERNING
            ),
        ),
    )
    assert impl.value is not None
    impl_id = impl.value.task_id

    # Implement rev1.
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
    binding = _binding(submitted.value.artifact_revisions[0])

    # The gate must now require ALL FOUR governing reviews (derived from the contract).
    gate0 = await svc.gate.get_gate_status(goal_id)
    assert set(gate0.missing_reviews) == {"quality", "security", "compliance", "finops"}

    # Look up the four review-task ids.
    async with container.postgres.connection() as conn:
        rows = await conn.fetch(
            "select task_id, required_actor_kind from tasks "
            "where goal_id=$1 and task_key like '%:review:%'",
            goal_id,
        )
    review_task: dict[str, UUID] = {r["required_actor_kind"]: r["task_id"] for r in rows}
    assert set(review_task) == {"quality", "security", "compliance", "finops"}

    # Approve them one at a time; the gate stays UNSATISFIED until the last lands.
    approved: list[str] = []
    for kind, rtype in GOVERNING:
        await svc.reviews.submit_review(
            _ctx(ActorRef(actor_id=f"{rtype}-1", kind=kind)),
            ReviewSubmission(
                goal_id=goal_id,
                review_task_id=review_task[rtype],
                reviewer=ActorRef(actor_id=f"{rtype}-1", kind=kind),
                review_type=rtype,
                artifact_bindings=(binding,),
                disposition=ReviewDisposition.APPROVED,
                summary=f"{rtype} ok",
            ),
        )
        approved.append(rtype)
        gate = await svc.gate.get_gate_status(goal_id)
        if len(approved) < len(GOVERNING):
            assert gate.status == GateStatus.UNSATISFIED
            assert set(gate.missing_reviews) == {r for _, r in GOVERNING if r not in approved}
        else:
            # All governing reviews in -> only the human approval remains.
            assert gate.status == GateStatus.HUMAN_REQUIRED
            assert gate.missing_approvals == ("human_release",)

    # Human approves -> satisfied.
    await svc.reviews.record_human_approval(
        _ctx(HUMAN),
        ApprovalSubmission(
            goal_id=goal_id,
            approval_type=ApprovalType.HUMAN_RELEASE,
            approver=HUMAN,
            artifact_bindings=(binding,),
        ),
    )
    final = await svc.gate.get_gate_status(goal_id)
    assert final.status == GateStatus.SATISFIED


def _binding(revision: object) -> ArtifactBinding:
    from sdlc_blackboard.domain.artifacts import ArtifactRevision

    assert isinstance(revision, ArtifactRevision)
    return ArtifactBinding(
        artifact_id=revision.artifact_id,
        revision_id=revision.revision_id,
        logical_name=revision.logical_name,
        content_hash=revision.content_hash,
    )
