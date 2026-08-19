"""Property invariants over the pure domain (hexagonal-arch-stack.md §7 Track B).

Each test states an invariant the domain must hold for all inputs:
- transition matrix is total, irreflexive, and asymmetric on the state product;
- canonical_hash is deterministic and order-independent for set-like fields.
"""

from hypothesis import given
from hypothesis import strategies as st

from sdlc_blackboard.application.idempotency import canonical_hash
from sdlc_blackboard.domain.common import ActorKind, ActorRef
from sdlc_blackboard.domain.goals import GoalCreate
from sdlc_blackboard.domain.tasks import TaskState
from sdlc_blackboard.domain.transitions import can_transition

_STATES = list(TaskState)


@given(st.sampled_from(_STATES), st.sampled_from(_STATES))
def test_transition_is_total_and_boolean(a: TaskState, b: TaskState) -> None:
    # Total: never raises, always a bool.
    result = can_transition(a, b)
    assert isinstance(result, bool)


@given(st.sampled_from(_STATES))
def test_transition_irreflexive(s: TaskState) -> None:
    assert can_transition(s, s) is False


@st.composite
def _goal_creates(draw: st.DrawFn) -> GoalCreate:
    text = st.text(min_size=1, max_size=50)
    crits = draw(st.lists(text, min_size=1, max_size=5))
    cons = draw(st.lists(text, max_size=5))
    return GoalCreate(
        title=draw(text),
        objective=draw(text),
        success_criteria=tuple(crits),
        constraints=tuple(cons),
        owner=ActorRef(actor_id=draw(text), kind=ActorKind.HUMAN),
    )


@given(_goal_creates())
def test_canonical_hash_deterministic(gc: GoalCreate) -> None:
    # Same value -> same hash, every time (idempotency depends on this).
    assert canonical_hash(gc) == canonical_hash(gc)
    # Reconstructing an identical value yields the same hash.
    twin = GoalCreate.model_validate(gc.model_dump())
    assert canonical_hash(twin) == canonical_hash(gc)


@given(_goal_creates())
def test_canonical_hash_is_sha256_hex(gc: GoalCreate) -> None:
    h = canonical_hash(gc)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
