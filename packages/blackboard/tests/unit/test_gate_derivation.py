"""Unit tests for the data-driven gate derivation (bounded-context roster).

These are the pure functions that let any reviewing context declared in a task contract
become a gate requirement with no kernel change (HANDOFF §28).
"""

from uuid import uuid4

from sdlc_blackboard.application.use_cases.gate_service import (
    DEFAULT_REQUIRED_REVIEW_TYPES,
    implementation_logical_name,
    required_review_types,
)
from sdlc_blackboard.domain.common import ActorKind
from sdlc_blackboard.domain.tasks import (
    DeliverableSpec,
    ReviewRequirement,
    Task,
    TaskContractCreate,
    TaskState,
)


def _task(
    task_key: str,
    *,
    kind: ActorKind,
    deliverables: tuple[DeliverableSpec, ...] = (),
    reviews: tuple[ReviewRequirement, ...] = (),
) -> Task:
    goal_id = uuid4()
    contract = TaskContractCreate(
        goal_id=goal_id,
        task_key=task_key,
        title=task_key,
        objective="o",
        required_actor_kind=kind,
        scope=("x",),
        deliverables=deliverables,
        acceptance_criteria=("ok",),
        review_requirements=reviews,
    )
    return Task(
        goal_id=goal_id,
        task_key=task_key,
        title=task_key,
        objective="o",
        required_actor_kind=kind,
        state=TaskState.READY,
        version=0,
        assignment_epoch=0,
        contract=contract,
    )


def test_required_types_default_when_no_blocking_reviews() -> None:
    tasks = (_task("impl", kind=ActorKind.IMPLEMENTATION),)
    assert required_review_types(tasks) == DEFAULT_REQUIRED_REVIEW_TYPES


def test_required_types_union_across_contracts_order_stable() -> None:
    impl = _task(
        "impl",
        kind=ActorKind.IMPLEMENTATION,
        deliverables=(DeliverableSpec(artifact_type="source", logical_name="source/x"),),
        reviews=(
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
            ReviewRequirement(reviewer_kind=ActorKind.FINOPS, review_type="finops"),
            ReviewRequirement(reviewer_kind=ActorKind.COMPLIANCE, review_type="compliance"),
        ),
    )
    # non-blocking reviews are excluded from the gate
    doc = _task(
        "doc",
        kind=ActorKind.DOCUMENTATION,
        reviews=(
            ReviewRequirement(
                reviewer_kind=ActorKind.SUPPORT, review_type="support", blocking=False
            ),
        ),
    )
    got = required_review_types((impl, doc))
    assert got == ("security", "quality", "finops", "compliance")
    assert "support" not in got  # non-blocking review is not a gate condition


def test_implementation_logical_name_is_most_reviewed_deliverable() -> None:
    impl = _task(
        "impl",
        kind=ActorKind.IMPLEMENTATION,
        deliverables=(DeliverableSpec(artifact_type="source", logical_name="source/report"),),
        reviews=(
            ReviewRequirement(reviewer_kind=ActorKind.SECURITY, review_type="security"),
            ReviewRequirement(reviewer_kind=ActorKind.QUALITY, review_type="quality"),
        ),
    )
    design = _task(
        "design",
        kind=ActorKind.ARCHITECT,
        deliverables=(DeliverableSpec(artifact_type="design", logical_name="design/report"),),
        reviews=(ReviewRequirement(reviewer_kind=ActorKind.ARCHITECT, review_type="architecture"),),
    )
    assert implementation_logical_name((design, impl)) == "source/report"


def test_implementation_logical_name_none_when_no_blocking_reviews() -> None:
    tasks = (_task("impl", kind=ActorKind.IMPLEMENTATION),)
    assert implementation_logical_name(tasks) is None
