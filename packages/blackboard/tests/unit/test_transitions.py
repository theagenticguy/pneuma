"""Task transition-matrix unit tests (handoff §21 + Appendix B)."""

import pytest

from sdlc_blackboard.domain.tasks import TERMINAL_TASK_STATES, TaskState
from sdlc_blackboard.domain.transitions import can_transition


@pytest.mark.parametrize(
    ("from_state", "to_state", "allowed"),
    [
        (TaskState.DRAFT, TaskState.READY, True),
        (TaskState.READY, TaskState.ASSIGNED, True),
        (TaskState.ASSIGNED, TaskState.RUNNING, True),
        (TaskState.RUNNING, TaskState.AWAITING_INPUT, True),
        (TaskState.AWAITING_INPUT, TaskState.RUNNING, True),
        (TaskState.RUNNING, TaskState.SUBMITTED, True),
        (TaskState.SUBMITTED, TaskState.UNDER_REVIEW, True),
        (TaskState.UNDER_REVIEW, TaskState.ACCEPTED, True),
        (TaskState.UNDER_REVIEW, TaskState.REVISION_REQUIRED, True),
        (TaskState.REVISION_REQUIRED, TaskState.READY, True),
        # illegal
        (TaskState.ACCEPTED, TaskState.RUNNING, False),
        (TaskState.CANCELLED, TaskState.SUBMITTED, False),
        (TaskState.DRAFT, TaskState.ACCEPTED, False),
        (TaskState.READY, TaskState.SUBMITTED, False),
    ],
)
def test_transition_matrix(from_state: TaskState, to_state: TaskState, allowed: bool) -> None:
    assert can_transition(from_state, to_state) is allowed


def test_no_self_transitions() -> None:
    for s in TaskState:
        assert can_transition(s, s) is False


def test_nonterminal_can_block_fail_cancel() -> None:
    for s in TaskState:
        if s in TERMINAL_TASK_STATES:
            continue
        # Self-transitions are always illegal (irreflexive rule), so a state can
        # only reach BLOCKED/FAILED/CANCELLED when it is not already that state.
        for target in (TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED):
            if s == target:
                continue
            assert can_transition(s, target) is True


def test_terminal_states_have_no_exits_except_supersede() -> None:
    for s in TERMINAL_TASK_STATES:
        for t in TaskState:
            if s in {TaskState.SUBMITTED, TaskState.ACCEPTED} and t == TaskState.SUPERSEDED:
                continue  # submitted/accepted -> superseded is allowed (handled below)
            # ACCEPTED is terminal; ACCEPTED -> SUPERSEDED is explicitly allowed.
            if s == TaskState.ACCEPTED and t == TaskState.SUPERSEDED:
                assert can_transition(s, t) is True
                continue
            assert can_transition(s, t) is False
