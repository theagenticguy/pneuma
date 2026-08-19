"""Dead-end voicing and the no-progress halt: dithering as data, not a burned budget.

The live experiment's real defect was legal looping — the agent cycled between valid
states until `max_steps` stopped it, and the only witness was a prompt marker no code
could read. Two mechanisms fix the two halves. `Run.revisits` (mirrored mid-run by
`interpreter.revisits()`) records each re-entry as a typed `Revisit`, so the fact the
`[REVISIT]` marker voiced to the model is also voiced to everything else. And
`max_revisits` halts a run after N consecutive revisits with `NoProgress` — an outcome
that names the limit it hit, on `detect.discrimination`'s principle that a measurement
stopping short must say which bound stopped it. `NoProgress` must stay a `ProcessError`
subclass (`casestudy/live.py` counts `ProcessError` as blocked) while staying
distinguishable from `Deadlock` and from the exhausted-budget message.
"""

from __future__ import annotations

import pytest

from pneuma.process import interpreter
from pneuma.process.ir import Process, State, Transition


def ping_pong() -> Process:
    """A two-state cycle with an exit the decider is never going to take.

    `B` has a real branch (`BtoA` back, `BtoT` out), so a decider that always turns
    back is dithering by choice — the shape the live experiment measured — and each
    turn back is a revisit with a named alternative.
    """
    return Process(
        name="PingPong",
        description="A cycle with an exit",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="Branch"),
            State(name="T", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoA", source="B", target="A"),
            Transition(name="BtoT", source="B", target="T"),
        ],
    )


async def _turn_back(
    _state: str, enabled: list[Transition], _variables: dict[str, int | str]
) -> str:
    """Always choose the cycle edge, dithering until something stops it."""
    for transition in enabled:
        if transition.name == "BtoA":
            return "BtoA"
    return enabled[0].name


# ── The early halt ──


async def test_a_cycling_run_halts_early_with_no_progress_naming_its_limit() -> None:
    """A→B→A→B forever is stopped at the revisit limit, not at the step budget.

    The message must name the limit that fired — `max_revisits`, not `max_steps` —
    because an outcome that stops short without naming its bound is unreadable:
    `detect.discrimination` makes the same argument with `withheld`.
    """
    with pytest.raises(interpreter.NoProgress, match=r"max_revisits=3") as caught:
        await interpreter.run(ping_pong(), _turn_back, max_steps=50, max_revisits=3)

    assert caught.value.limit == 3
    assert [entry.state for entry in caught.value.revisits] == ["A", "B", "A"]
    assert "no progress" in str(caught.value)
    # Early means early: the run stopped well inside a budget it never spent.
    assert "exceeded" not in str(caught.value)


async def test_the_default_limit_halts_a_dithering_run_inside_the_default_budget() -> None:
    """With everything defaulted, dithering costs `DEFAULT_MAX_REVISITS` revisits, not 50 steps."""
    with pytest.raises(interpreter.NoProgress) as caught:
        await interpreter.run(ping_pong(), _turn_back)

    assert caught.value.limit == interpreter.DEFAULT_MAX_REVISITS
    assert len(caught.value.revisits) == interpreter.DEFAULT_MAX_REVISITS


async def test_disabling_the_halt_burns_the_budget_exactly_as_before() -> None:
    """`max_revisits=None` is the off switch: the old behavior, byte for byte.

    The same cycling run must raise the *budget* `ProcessError` with the old
    "exceeded N steps" message and must not be a `NoProgress`.
    """
    with pytest.raises(interpreter.ProcessError, match="exceeded 8 steps") as caught:
        await interpreter.run(ping_pong(), _turn_back, max_steps=8, max_revisits=None)

    assert not isinstance(caught.value, interpreter.NoProgress)


async def test_no_progress_is_distinguishable_from_deadlock_and_from_max_steps() -> None:
    """The three refusals stay three: a caller can branch on each without string-matching.

    `live.py` catches `ProcessError` and counts blocked — that accounting must keep
    working (`NoProgress` IS one) while a caller that wants the distinction gets it
    from the type alone.
    """
    assert issubclass(interpreter.NoProgress, interpreter.ProcessError)
    assert not issubclass(interpreter.NoProgress, interpreter.Deadlock)
    assert not issubclass(interpreter.Deadlock, interpreter.NoProgress)

    with pytest.raises(interpreter.NoProgress):
        await interpreter.run(ping_pong(), _turn_back, max_revisits=2)


async def test_a_detour_that_recovers_is_not_halted() -> None:
    """Consecutive means consecutive: a new state in between resets the count.

    The path A→B→A→B→C→B→T revisits three states in total — A, B, then B again after
    the detour through `C` — which meets a *total* limit of 3 but never a consecutive
    one, because reaching the never-seen `C` resets the count to zero. A run that
    keeps finding new ground must not be halted for the ground it retreads between
    finds.
    """
    process = Process(
        name="Detour",
        description="A cycle broken by new ground",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="Branch"),
            State(name="C", description="New ground"),
            State(name="T", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoA", source="B", target="A"),
            Transition(name="BtoC", source="B", target="C"),
            Transition(name="CtoB", source="C", target="B"),
            Transition(name="BtoT", source="B", target="T"),
        ],
    )
    script = iter(["BtoA", "BtoC", "BtoT"])

    async def wander(
        _state: str, enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        return next(script, enabled[0].name)

    run = await interpreter.run(process, wander, max_revisits=3)
    assert run.final_state == "T"
    # All three revisits were still recorded — halting and voicing are separate.
    assert [entry.state for entry in run.revisits] == ["A", "B", "B"]


# ── The typed revisit record ──


async def test_revisit_entries_carry_the_step_index_and_the_alternatives() -> None:
    """Each re-entry names where, when, and what else was on offer.

    Path A→B→A→B→T: step 1 re-enters `A` (the alternative was `BtoT`), step 2
    re-enters `B` (`AtoB` was the only edge, so no alternative existed — a forced
    revisit, which is data too).
    """
    calls = 0

    async def wander(
        _state: str, enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        nonlocal calls
        calls += 1
        return "BtoA" if calls == 1 else "BtoT"

    run = await interpreter.run(ping_pong(), wander)

    assert run.path == ["AtoB", "BtoA", "AtoB", "BtoT"]
    assert run.revisits == [
        interpreter.Revisit(state="A", step=1, alternatives=("BtoT",)),
        interpreter.Revisit(state="B", step=2, alternatives=()),
    ]
    assert [step.index for step in run.steps] == [0, 1, 2, 3]


async def test_a_run_without_revisits_records_none() -> None:
    async def straight(
        _state: str, _enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        return "BtoT"

    run = await interpreter.run(ping_pong(), straight)
    assert run.final_state == "T"
    assert run.revisits == []


async def test_revisits_are_visible_mid_run_through_the_accessor() -> None:
    """A decider standing at a choice can read the dead ends voiced so far.

    This is the seam `learning.decision_query` builds its retrieval query on, so it
    is pinned here in the library: at the first decision nothing is voiced, and by
    the second the accessor shows both re-entries — the chosen turn back into `A`
    *and* the forced step back into `B`, which no decider-maintained record could
    hold because no decider was consulted for it.
    """
    observed: list[list[interpreter.Revisit]] = []
    calls = 0

    async def watching(
        _state: str, _enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        nonlocal calls
        calls += 1
        observed.append(interpreter.revisits())
        return "BtoA" if calls == 1 else "BtoT"

    await interpreter.run(ping_pong(), watching)

    assert observed[0] == []
    assert [entry.state for entry in observed[1]] == ["A", "B"]


async def test_the_revisit_record_does_not_leak_past_the_run() -> None:
    """Every exit — completion, the halt itself, the budget — clears the ContextVar."""
    assert interpreter.revisits() == []

    with pytest.raises(interpreter.NoProgress):
        await interpreter.run(ping_pong(), _turn_back, max_revisits=2)
    assert interpreter.revisits() == []

    with pytest.raises(interpreter.ProcessError):
        await interpreter.run(ping_pong(), _turn_back, max_steps=4, max_revisits=None)
    assert interpreter.revisits() == []


async def test_the_halt_spends_no_work_on_the_visit_it_refuses() -> None:
    """`on_enter` is not called for the entry that trips the limit.

    The run has just declared that visit progress-free; running the state's work on
    it would contradict the verdict and spend a model call the halt exists to save.
    """
    entered: list[str] = []

    async def record(state: str) -> None:
        entered.append(state)

    with pytest.raises(interpreter.NoProgress):
        await interpreter.run(ping_pong(), _turn_back, max_revisits=2, on_enter=record)

    # A, B, then the re-entered A (revisit 1, under the limit) — the re-entered B
    # (revisit 2) trips the halt before its work runs.
    assert entered == ["A", "B", "A"]
