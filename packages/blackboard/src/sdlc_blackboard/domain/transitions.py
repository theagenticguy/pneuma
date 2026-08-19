"""Task state-transition matrix as a pure rule (handoff Appendix B).

"Do not accept arbitrary transitions from the model." Every task state change in
the application layer is gated by ``can_transition``. This module is pure and is
the canonical target for both the Hypothesis property suite and the Lean proof.
"""

from __future__ import annotations

from sdlc_blackboard.domain.tasks import TERMINAL_TASK_STATES, TaskState

#: Explicit allowed (from -> to) edges. Anything absent is illegal.
#: Mirrors Appendix B; "nonterminal -> blocked/failed/cancelled" is expanded to
#: every non-terminal source below.
_EXPLICIT_EDGES: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {
        (TaskState.DRAFT, TaskState.READY),
        (TaskState.READY, TaskState.ASSIGNED),
        (TaskState.ASSIGNED, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.AWAITING_INPUT),
        (TaskState.AWAITING_INPUT, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.SUBMITTED),
        (TaskState.SUBMITTED, TaskState.UNDER_REVIEW),
        (TaskState.UNDER_REVIEW, TaskState.ACCEPTED),
        (TaskState.UNDER_REVIEW, TaskState.REVISION_REQUIRED),
        (TaskState.REVISION_REQUIRED, TaskState.READY),
        # A blocked task returns to a prior workable state.
        (TaskState.BLOCKED, TaskState.READY),
        (TaskState.BLOCKED, TaskState.ASSIGNED),
        (TaskState.BLOCKED, TaskState.RUNNING),
        (TaskState.BLOCKED, TaskState.AWAITING_INPUT),
        (TaskState.BLOCKED, TaskState.UNDER_REVIEW),
        # submitted/accepted may be superseded by a replacement.
        (TaskState.SUBMITTED, TaskState.SUPERSEDED),
        (TaskState.ACCEPTED, TaskState.SUPERSEDED),
    }
)

#: Non-terminal states may transition to blocked / failed / cancelled.
_NONTERMINAL_UNIVERSAL_TARGETS: frozenset[TaskState] = frozenset(
    {TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
)


def _nonterminal_states() -> frozenset[TaskState]:
    return frozenset(s for s in TaskState if s not in TERMINAL_TASK_STATES)


def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """True iff ``from_state -> to_state`` is a permitted task transition.

    Pure, total function over the ``TaskState`` product. No self-loops.
    """
    if from_state == to_state:
        return False
    if (from_state, to_state) in _EXPLICIT_EDGES:
        return True
    return from_state in _nonterminal_states() and to_state in _NONTERMINAL_UNIVERSAL_TARGETS
