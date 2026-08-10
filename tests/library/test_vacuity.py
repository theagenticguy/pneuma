"""Does the detector actually detect the defect it was built for?

The defect class is one where a wrong result looks exactly like a right one: a rule
that verifies green over a state space in which it can never fire. TLC cannot tell you
about it, because TLC's answer to "did anything break this rule" is genuinely "no".

So the tests that matter here are the ones where a bare `tla.check` says `verified` and
the gated one says `vacuous` over the same process. Everything else is scaffolding for
that. Every process here is hand-built and small enough to count by hand, so each
asserted number is derivable from the process constructed beside it, and the TLC-backed
test asserts the checker's verdict alongside the detector's so a disagreement fails here
rather than being reconciled by a reviewer.

Nothing in this module reaches into `pneuma.casestudy`, so it collects and runs with the
application's dependencies absent from the environment. The same claims over processes
mined from real logs are in `tests/app/test_vacuity_on_real_logs.py`.
"""

from __future__ import annotations

import pytest

from pneuma import detect
from pneuma.detect import vacuity
from pneuma.process import tla
from pneuma.process.ir import Effect, Guard, Invariant, Process, State, Transition, Variable

needs_tlc = pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")


# ── The claim: a pinned nondeterministic variable makes TLC verify an unchecked rule ──


def claim_process(*, pinned: bool) -> Process:
    """A claim whose size is decided by the claimant, not by the workflow.

    The project's original confessed bug in miniature. `size` comes from outside the
    process, so pinning it to `"small"` makes the `Large` branch unreachable and the
    two-approval rule is about a case the model never visits. Nothing is malformed:
    every state is reachable topologically, the invariant names a real state, and its
    guards reference real variables in their declared domains.
    """
    return Process(
        name="Claim",
        states=[State(name="Intake"), State(name="Approve"), State(name="Pay", terminal=True)],
        initial_state="Intake",
        variables=[
            Variable(
                name="size",
                values=["small", "large"],
                initial="small" if pinned else None,
                description="decided by the claimant",
            ),
            Variable(name="approvals", low=0, high=2, initial=0),
        ],
        transitions=[
            Transition(
                name="Small",
                source="Intake",
                target="Pay",
                guards=[Guard(variable="size", op="eq", value="small")],
            ),
            Transition(
                name="Large",
                source="Intake",
                target="Approve",
                guards=[Guard(variable="size", op="eq", value="large")],
            ),
            Transition(
                name="ApproveOnce",
                source="Approve",
                target="Pay",
                effects=[Effect(variable="approvals", increment=1)],
            ),
        ],
        invariants=[
            Invariant(
                name="LargeNeedsTwoApprovals",
                stated_as="a large claim may not be paid with fewer than two approvals",
                forbidden_state="Pay",
                forbidden_when=[
                    Guard(variable="size", op="eq", value="large"),
                    Guard(variable="approvals", op="lt", value=2),
                ],
            )
        ],
    )


def test_a_pinned_nondeterministic_variable_is_caught_and_named() -> None:
    """The case the project confessed to and had no detector-level test for.

    Structural checks all pass: `unreachable_states` is empty, so a topological reader
    sees a model where every state is reachable. Only the assignment-level sweep sees
    that `Approve` is reachable and `Pay`-with-a-large-claim is not, and only the
    relaxation says *why*: freeing the pinned initial value makes the rule fire.
    """
    pinned = claim_process(pinned=True)
    assert pinned.unreachable_states() == set(), "topologically every state is reachable"
    assert pinned.variable_map["size"].nondeterministic is False, "the bug: it was pinned"

    verdict = detect.verdict_for(pinned, "LargeNeedsTwoApprovals")
    assert verdict.live is False
    assert verdict.antecedent_states == 1, "Pay is reached, but only by a small claim"
    assert verdict.violating_states == 0
    assert verdict.cause == "pinned_variable"
    assert verdict.vacuous
    assert verdict.witnesses == 0, "so a checker's pass must be withdrawn"

    # The counterfactual carries a trace, which is the part a reviewer can act on: it
    # names the path the model would have to allow for the rule to matter.
    assert verdict.relaxed_trace is not None
    assert verdict.relaxed_trace.locations == ("Intake", "Approve", "Pay")
    assert dict(verdict.relaxed_trace.start)["size"] == "large"


def test_freeing_the_same_variable_makes_the_same_rule_live() -> None:
    """The control. One field changes; the rule goes from unfirable to violated.

    This is what makes the previous test a detection rather than a coincidence. Same
    states, same transitions, same invariant. `initial=None` on one variable, and the
    rule acquires a real counterexample with a shortest trace.
    """
    freed = claim_process(pinned=False)
    assert freed.variable_map["size"].nondeterministic

    verdict = detect.verdict_for(freed, "LargeNeedsTwoApprovals")
    assert verdict.live is True
    assert verdict.violating_states == 1
    assert verdict.cause is None
    assert not verdict.vacuous
    assert verdict.witnesses == 1

    assert verdict.trace is not None
    assert verdict.trace.locations == ("Intake", "Approve", "Pay")
    assert verdict.trace.edges == ("Large", "ApproveOnce")
    assert verdict.trace.depth == 2
    assert dict(verdict.trace.end) == {"size": "large", "approvals": 1}

    # Freeing the variable is what grows the state space, and the count is small enough
    # to check by hand: 2 sizes x (Intake, then either Pay or Approve-then-Pay) = 5.
    assert verdict.reachable_states == 5
    assert (
        detect.verdict_for(claim_process(pinned=True), "LargeNeedsTwoApprovals").reachable_states
        == 2
    )


@needs_tlc
def test_tlc_verifies_the_pinned_model_and_the_gate_withdraws_the_pass() -> None:
    """The whole point of the module, in one function.

    Same rule, two models. On the pinned one TLC explores the state space, finds no
    error, and reports `verified` — a green verdict on a rule about a case it never
    visited. On the freed one it finds the counterexample. Nothing but the detector
    distinguishes the first from a real pass, and `with_witnesses` turns that into a
    withdrawn verdict rather than a footnote.
    """
    pinned = claim_process(pinned=True)
    bare = tla.check(pinned, timeout=200)
    assert bare.outcome == "verified", bare.raw[-1200:]
    assert bare.ok, "TLC's own verdict on a rule it never tested"
    assert bare.distinct_states == 2
    assert bare.witnesses is None, "not yet measured, which is distinct from measured-zero"

    gated = bare.with_witnesses(detect.witness_counts(pinned))
    assert gated.outcome == "vacuous"
    assert not gated.ok, "the pass is withdrawn"
    assert gated.vacuous_invariants == ("LargeNeedsTwoApprovals",)
    assert "no witness state" in gated.summary

    freed = claim_process(pinned=False)
    violated = tla.check(freed, timeout=200)
    assert violated.outcome == "violated"
    assert violated.violated == "LargeNeedsTwoApprovals"
    # A real violation is not something the gate can turn green.
    assert violated.with_witnesses(detect.witness_counts(freed)).outcome == "violated"


# ── Whole-process sweep: the structural rules the checker adds ──


def test_the_structural_rules_do_not_gate_a_pass_they_cannot_fail() -> None:
    """`TypeOK` cannot fail on a sound model, and that is not a vacuous verdict.

    If the structural rules gated, every correct process would report a withdrawn pass
    and the gate would carry no information at all. They are still swept and reported,
    because a *failing* one is a real finding; they just do not vote.
    """
    freed = claim_process(pinned=False)
    audited = detect.audit_process(freed)

    for name in (detect.DEADLOCK_RULE, detect.TYPE_RULE):
        verdict = audited.verdicts[name]
        assert verdict.live is False, "a sound model cannot break either"
        assert not verdict.gates
        assert not verdict.vacuous, "unfirable wellformedness is soundness, not vacuity"
    assert set(audited.witness_counts()) == {"LargeNeedsTwoApprovals"}


def test_a_real_deadlock_is_reported_as_a_live_violation() -> None:
    """The structural rules are swept, not decorative: a stuck state is found.

    `Trap` is non-terminal with no outgoing transition, which is what `NoDeadlock`
    exists to catch. Written as a predicate over the *successors* rather than the
    assignment, because out-degree is not a property of the variables.
    """
    stuck = Process(
        name="Stuck",
        states=[State(name="Begin"), State(name="Trap"), State(name="End", terminal=True)],
        initial_state="Begin",
        transitions=[
            Transition(name="ToTrap", source="Begin", target="Trap"),
            Transition(name="ToEnd", source="Begin", target="End"),
        ],
    )
    verdict = detect.audit_process(stuck).verdicts[detect.DEADLOCK_RULE]
    assert verdict.live is True
    assert verdict.violating_states == 1
    assert verdict.trace is not None
    assert verdict.trace.locations == ("Begin", "Trap")


# ── Honest bounds: a truncated sweep is not a finding ──


def test_seeding_is_budgeted_too_so_the_limit_cannot_lie() -> None:
    """A cap applied only to expansion would be a cap that lies.

    Three free variables over 10 values each is 1,000 starting states before a single
    transition fires, so a sweep bounded at 10 must report truncation from seeding
    alone rather than silently enumerating them all.
    """
    wide = Process(
        name="Wide",
        states=[State(name="Begin"), State(name="End", terminal=True)],
        initial_state="Begin",
        variables=[Variable(name=f"v{i}", low=0, high=9) for i in range(3)],
        transitions=[Transition(name="Go", source="Begin", target="End")],
    )
    assert len(wide.initial_assignments()) == 1000

    bounded = detect.audit_process(wide, limit=10)
    assert bounded.truncated
    assert bounded.sweeps["exact"].start_states == 10
    assert bounded.sweeps["exact"].reachable_states == 10
    assert bounded.unknown, "every rule is unknown, not safe"


def test_a_zero_or_negative_limit_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        detect.audit_process(claim_process(pinned=False), limit=0)


# ── The walk agrees with the interpreter, by construction ──


def test_the_walk_applies_effects_the_way_the_interpreter_does() -> None:
    """Two effects on one variable must accumulate, not both read the pre-state.

    `interpreter.run` assigns into the live variables dict as it goes, so the second
    effect sees the first. A walk that applied each against the pre-state would compute
    a different successor and could then miss or invent a violating state. It belongs
    here rather than in `test_rules.py`, because it is a test of the walk.
    """
    invariant = Invariant(
        name="NeverTwo",
        forbidden_state="End",
        forbidden_when=[Guard(variable="n", op="eq", value=2)],
    )
    process = Process(
        name="Accumulate",
        states=[State(name="Begin"), State(name="End", terminal=True)],
        initial_state="Begin",
        variables=[Variable(name="n", low=0, high=3, initial=0)],
        transitions=[
            Transition(
                name="Twice",
                source="Begin",
                target="End",
                effects=[Effect(variable="n", increment=1), Effect(variable="n", increment=1)],
            )
        ],
        invariants=[invariant],
    )

    verdict = detect.verdict_for(process, "NeverTwo")
    assert verdict.violating_states == 1, "n must reach 2, not 1"
    assert verdict.live is True
    assert dict(verdict.trace.end)["n"] == 2  # type: ignore[union-attr]


def test_an_unsatisfiable_condition_the_ir_accepts_is_caught_by_reachability() -> None:
    """`Invariant._not_vacuous` rejects only an invariant with no condition at all, so
    a self-contradictory conjunction is constructed without complaint and can never
    fire. It belongs here for the same reason as the test above."""
    contradiction = Invariant(
        name="NeverFires",
        forbidden_state="Middle",
        forbidden_when=[
            Guard(variable="f", op="eq", value=0),
            Guard(variable="f", op="eq", value=1),
        ],
    )
    process = Process(
        name="Contradiction",
        states=[State(name="Begin"), State(name="Middle"), State(name="End", terminal=True)],
        initial_state="Begin",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[
            Transition(name="T1", source="Begin", target="Middle"),
            Transition(name="T2", source="Middle", target="End"),
        ],
        invariants=[contradiction],
    )

    verdict = detect.verdict_for(process, "NeverFires")
    assert verdict.antecedent_states > 0, "the forbidden state is reachable"
    assert verdict.violating_states == 0
    assert verdict.live is False
    assert verdict.vacuous


def test_a_scope_that_is_never_reached_is_a_different_finding_from_an_unmet_condition() -> None:
    """Two ways to forbid nothing, told apart.

    A rule whose state is never reached and a rule whose state is reached with the
    condition never holding both report zero violations, and they need different fixes:
    the first is a rule about the wrong state, the second a rule the graph already
    enforces. Splitting scope from breach is what makes them separable.

    `NeverReachedWhenUnset` is the shape the real permit log produces at
    `min_edge_cases=200`: the only edge into `Reached` runs through `Gate`, which sets
    the flag, so the flag is never 0 on arrival. No relaxation rescues it, because
    freeing the initial value to 1 still does not satisfy a condition asking for 0.
    """
    process = Process(
        name="Islands",
        states=[
            State(name="Begin"),
            State(name="Gate"),
            State(name="Reached"),
            State(name="Marooned"),
            State(name="End", terminal=True),
        ],
        initial_state="Begin",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[
            Transition(
                name="T1",
                source="Begin",
                target="Gate",
                effects=[Effect(variable="f", value=1)],
            ),
            Transition(name="T2", source="Gate", target="Reached"),
            Transition(name="T3", source="Reached", target="End"),
            Transition(name="T4", source="Marooned", target="End"),
        ],
        invariants=[
            Invariant(
                name="NeverReachedWhenUnset",
                forbidden_state="Reached",
                forbidden_when=[Guard(variable="f", op="eq", value=0)],
            ),
            Invariant(name="NeverMarooned", forbidden_state="Marooned"),
        ],
    )
    assert process.unreachable_states() == {"Marooned"}, "the structural check sees this one"

    audited = detect.audit_process(process)
    unmet = audited.verdicts["NeverReachedWhenUnset"]
    assert (unmet.antecedent_states, unmet.violating_states) == (1, 0)
    assert unmet.cause == "unsatisfiable", "the effect on the way in, not a pinned start"
    assert unmet.vacuous

    absent = audited.verdicts["NeverMarooned"]
    assert (absent.antecedent_states, absent.violating_states) == (0, 0)
    assert absent.cause == "unreachable_scope"
    assert absent.vacuous


def test_a_guard_that_is_load_bearing_keeps_its_pass() -> None:
    """The false-positive the relaxation exists to avoid.

    `AB` is the only edge into `B` and its guard is what keeps `f = 0` out, so the rule
    is doing real work even though nothing violates it. Withdrawing that pass would
    make the detector fire on every correctly-guarded control, which is a worse failure
    than the one it is fixing: a gate nobody can leave on is a gate nobody uses.
    """
    process = Process(
        name="Guarded",
        states=[State(name="A"), State(name="B"), State(name="C", terminal=True)],
        initial_state="A",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[
            Transition(
                name="AB",
                source="A",
                target="B",
                guards=[Guard(variable="f", op="eq", value=1)],
            ),
            Transition(name="AC", source="A", target="C"),
            Transition(name="BC", source="B", target="C"),
        ],
        invariants=[
            Invariant(
                name="NeverBWhenUnset",
                forbidden_state="B",
                forbidden_when=[Guard(variable="f", op="eq", value=0)],
            )
        ],
    )

    verdict = detect.verdict_for(process, "NeverBWhenUnset")
    assert verdict.live is False, "nothing violates it"
    assert verdict.cause == "guarded"
    assert not verdict.vacuous, "but a guard earned the pass, so it stands"
    assert verdict.witnesses == 1
    # The counterfactual names the edge whose guard is carrying the property.
    assert verdict.relaxed_trace is not None
    assert verdict.relaxed_trace.edges == ("AB",)


# ── Guard satisfiability, before any enumeration ──


@pytest.mark.parametrize(
    ("clauses", "fragment"),
    [
        ([("f", "eq", 0), ("f", "eq", 1)], "equal 0, 1 at once"),
        ([("f", "eq", 0), ("f", "ne", 0)], "equal and not equal"),
        ([("n", "ge", 3), ("n", "le", 1)], ">= 3 and <= 1"),
        ([("n", "eq", 5), ("n", "lt", 3)], "contradicts"),
        ([("s", "eq", "a"), ("s", "eq", "b")], "equal 'a', 'b' at once"),
    ],
)
def test_an_unsatisfiable_conjunction_is_named_without_enumerating(
    clauses: list[tuple[str, str, int | str]], fragment: str
) -> None:
    """Cheaper and sharper than discovering zero hits.

    A sweep reports that a condition never held over the states it happened to reach;
    this reports that no assignment could satisfy it and names the variable, without
    walking a state space for nothing.
    """
    found = vacuity.contradictory(clauses)
    assert found is not None
    assert fragment in found


@pytest.mark.parametrize(
    "clauses",
    [
        [],
        [("f", "eq", 0)],
        [("n", "gt", 2), ("n", "lt", 9)],
        [("f", "eq", 0), ("g", "eq", 1)],
        [("f", "ne", 0), ("f", "ne", 1)],
    ],
)
def test_a_satisfiable_or_undecided_conjunction_returns_none(
    clauses: list[tuple[str, str, int | str]],
) -> None:
    """None means "not provably empty here", never "satisfiable".

    The last case is the honest limit: `f != 0 /\\ f != 1` is unsatisfiable over the
    domain `0..1`, and this helper cannot see domains. Claiming a contradiction it
    cannot prove would be the same overreach as claiming safety it cannot prove.
    """
    assert vacuity.contradictory(clauses) is None


def test_the_contradiction_note_reaches_the_verdict() -> None:
    """Found before enumeration, reported on the object a caller reads afterwards."""
    process = Process(
        name="Impossible",
        states=[State(name="Begin"), State(name="End", terminal=True)],
        initial_state="Begin",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[Transition(name="Go", source="Begin", target="End")],
        invariants=[
            Invariant(
                name="Never",
                forbidden_state="End",
                forbidden_when=[
                    Guard(variable="f", op="eq", value=0),
                    Guard(variable="f", op="eq", value=1),
                ],
            )
        ],
    )
    assert detect.contradictions_in(process)
    verdict = detect.verdict_for(process, "Never")
    assert verdict.contradiction is not None
    assert verdict.contradiction in str(verdict)


# ── The general mechanism, with no process IR in sight ──


def test_the_core_takes_any_transition_system_not_only_a_process() -> None:
    """The liftability claim, tested rather than asserted.

    `vacuity` imports nothing from pneuma, so a consumer supplies `starts`/`successors`
    and a list of rules and gets the same counts, causes and traces. If this test ever
    needs a `Process` to pass, the module has stopped being liftable.
    """

    class Counter:
        """A counter that stops at 3, defined without reference to any IR."""

        def starts(self):
            yield ("count", {"n": 0})

        def successors(self, location, assignment):
            if assignment["n"] < 3:
                yield ("tick", "count", {"n": assignment["n"] + 1})
            else:
                yield ("stop", "done", dict(assignment))

    assert isinstance(Counter(), vacuity.System)

    reaches_three = vacuity.Rule(
        name="ReachesThree",
        broken=lambda visit: visit.assignment.get("n") == 3,
    )
    reaches_nine = vacuity.Rule(
        name="ReachesNine",
        broken=lambda visit: visit.assignment.get("n") == 9,
        scope=lambda visit: visit.location == "nowhere",
    )

    audited = vacuity.audit(lambda _level: Counter(), [reaches_three, reaches_nine])
    assert audited.reachable_states == 5, "n = 0..3, then done"
    assert audited.verdicts["ReachesThree"].live is True
    assert audited.verdicts["ReachesThree"].trace.edges == ("tick", "tick", "tick")  # type: ignore[union-attr]
    assert audited.verdicts["ReachesNine"].live is False
    assert audited.verdicts["ReachesNine"].cause == "unreachable_scope"
    assert audited.verdicts["ReachesNine"].vacuous
    # n = 3 in two of the five states: `count` on arrival, and `done` after stopping.
    assert audited.witness_counts() == {"ReachesThree": 2, "ReachesNine": 0}


def test_the_trace_is_a_shortest_path_not_merely_a_path() -> None:
    """Breadth-first, so the first breach found sits at minimum depth.

    A long way round and a short way to the same violating state exist here. A
    depth-first walk would plausibly report the four-step path, and a reviewer handed
    that trace would be reading a worse bug report than the one-step one.
    """
    process = Process(
        name="TwoWays",
        states=[
            State(name="Start"),
            State(name="Detour1"),
            State(name="Detour2"),
            State(name="Detour3"),
            State(name="Bad", terminal=True),
        ],
        initial_state="Start",
        transitions=[
            Transition(name="Short", source="Start", target="Bad"),
            Transition(name="Long1", source="Start", target="Detour1"),
            Transition(name="Long2", source="Detour1", target="Detour2"),
            Transition(name="Long3", source="Detour2", target="Detour3"),
            Transition(name="Long4", source="Detour3", target="Bad"),
        ],
        invariants=[Invariant(name="NeverBad", forbidden_state="Bad")],
    )
    trace = detect.verdict_for(process, "NeverBad").trace
    assert trace is not None
    assert trace.depth == 1
    assert trace.edges == ("Short",)


def test_a_rule_that_cannot_be_evaluated_raises_with_the_state_named() -> None:
    """A rule that throws is a modelling error, and swallowing it would shrink the
    state space silently — the same failure this module detects."""

    class Trivial:
        def starts(self):
            yield ("only", {})

        def successors(self, location, assignment):
            return iter(())

    exploding = vacuity.Rule(
        name="Explodes",
        broken=lambda _visit: 1 / 0 > 0,  # noqa: B018
    )
    with pytest.raises(vacuity.SweepError, match="Explodes at only"):
        vacuity.sweep(Trivial(), [exploding])


def test_relaxations_beyond_exact_are_optional_for_a_checker_equivalent_count() -> None:
    """`("exact",)` gives one sweep and no counterfactual.

    The cheap path for a caller that only wants the checker's own numbers. It must
    still report the counts; what it loses is the ability to name a cause, and it says
    so by reporting `unsatisfiable` rather than guessing at `pinned_variable`.
    """
    pinned = claim_process(pinned=True)
    cheap = detect.audit_process(pinned, relaxations=("exact",))
    full = detect.audit_process(pinned)

    assert len(cheap.sweeps) == 1
    assert cheap.reachable_states == full.reachable_states
    assert cheap.verdicts["LargeNeedsTwoApprovals"].live is False
    assert cheap.verdicts["LargeNeedsTwoApprovals"].cause == "unsatisfiable"
    assert full.verdicts["LargeNeedsTwoApprovals"].cause == "pinned_variable"
    # Both still withdraw the pass, which is the part that must not depend on depth.
    assert cheap.witness_counts() == full.witness_counts() == {"LargeNeedsTwoApprovals": 0}


def test_relaxations_must_include_exact() -> None:
    with pytest.raises(ValueError, match="must include 'exact'"):
        detect.audit_process(claim_process(pinned=True), relaxations=("free_guards",))
