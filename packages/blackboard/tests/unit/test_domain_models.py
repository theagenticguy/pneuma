"""Domain model + result DTO unit tests (handoff §6, §7)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from sdlc_blackboard.application.results import (
    CommandError,
    CommandResult,
    CommandStatus,
    ErrorCode,
)
from sdlc_blackboard.domain.common import ActorKind, ActorRef, ArtifactBinding
from sdlc_blackboard.domain.errors import StaleAssignment, StaleVersion
from sdlc_blackboard.domain.goals import Goal, GoalCreate, GoalState
from sdlc_blackboard.domain.reviews import binding_fingerprint


def test_domain_models_are_frozen() -> None:
    goal = Goal(
        title="t",
        objective="o",
        success_criteria=("a",),
        constraints=(),
        owner=ActorRef(actor_id="u", kind=ActorKind.HUMAN),
        state=GoalState.ACTIVE,
        version=0,
    )
    with pytest.raises(ValidationError):
        goal.title = "changed"  # type: ignore[misc]


def test_domain_models_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        GoalCreate(
            title="t",
            objective="o",
            success_criteria=("a",),
            owner=ActorRef(actor_id="u", kind=ActorKind.HUMAN),
            bogus="x",  # type: ignore[call-arg]
        )


def test_nonempty_str_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        ActorRef(actor_id="", kind=ActorKind.HUMAN)


def test_binding_fingerprint_order_independent() -> None:
    a, b = uuid4(), uuid4()
    b1 = ArtifactBinding(artifact_id=a, revision_id=b, logical_name="x", content_hash="h1")
    b2 = ArtifactBinding(artifact_id=b, revision_id=a, logical_name="y", content_hash="h2")
    assert binding_fingerprint((b1, b2)) == binding_fingerprint((b2, b1))


def test_binding_fingerprint_sensitive_to_hash() -> None:
    a = uuid4()
    b1 = ArtifactBinding(artifact_id=a, revision_id=a, logical_name="x", content_hash="h1")
    b2 = ArtifactBinding(artifact_id=a, revision_id=a, logical_name="x", content_hash="h2")
    assert binding_fingerprint((b1,)) != binding_fingerprint((b2,))


def test_command_result_accepted() -> None:
    goal = Goal(
        title="t",
        objective="o",
        success_criteria=("a",),
        constraints=(),
        owner=ActorRef(actor_id="u", kind=ActorKind.HUMAN),
        state=GoalState.ACTIVE,
        version=0,
    )
    r = CommandResult[Goal].accepted(goal)
    assert r.status == CommandStatus.ACCEPTED
    assert r.replayed is False
    assert r.value is not None and r.value.goal_id == goal.goal_id


def test_command_result_from_domain_error_maps_codes() -> None:
    r1 = CommandResult[Goal].from_domain_error(StaleVersion())
    assert r1.error is not None
    assert r1.error.code == ErrorCode.STALE_VERSION
    assert r1.status == CommandStatus.STALE_VERSION

    r2 = CommandResult[Goal].from_domain_error(StaleAssignment())
    assert r2.error is not None
    assert r2.error.code == ErrorCode.STALE_ASSIGNMENT
    assert r2.status == CommandStatus.STALE_ASSIGNMENT


def test_command_result_generic_roundtrip() -> None:
    goal = Goal(
        title="t",
        objective="o",
        success_criteria=("a", "b"),
        constraints=(),
        owner=ActorRef(actor_id="u", kind=ActorKind.HUMAN),
        state=GoalState.ACTIVE,
        version=0,
    )
    r = CommandResult[Goal].accepted(goal)
    js = r.model_dump_json()
    back = CommandResult[Goal].model_validate_json(js)
    assert back.value is not None
    assert isinstance(back.value, Goal)
    assert back.value.goal_id == goal.goal_id


def test_command_error_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        CommandError(code=ErrorCode.CONFLICT, message="x", bogus=1)  # type: ignore[call-arg]
