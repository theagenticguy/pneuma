"""Tests for the verified-process pipeline: IR, TLA+/TLC, interpreter, Hypothesis.

The central claim is that one IR feeds two independent verifiers, and that each
catches a bug the other could miss. `bugged_claims` is the fixture that proves it:
one extra transition, plausible enough that a person would accept it in review, and
it pays a large claim on a single approval.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import run_state_machine_as_test
from pydantic import ValidationError

from pneuma.process import interpreter, properties, tla
from pneuma.process.ir import Effect, Guard, Invariant, Process, State, Transition, Variable


def checked(**overrides: object) -> settings:
    """Fresh settings per test.

    A single shared `settings` object reused by both a `@given` test and
    `run_state_machine_as_test` made the stateful test stop finding its bug: the
    two calls share Hypothesis state through the object, and the earlier one left
    it in a state where the later search never reached the faulty branch. Building
    a new instance per call keeps the searches independent.
    """
    defaults: dict[str, object] = {
        "max_examples": 150,
        "stateful_step_count": 12,
        "deadline": None,
        "suppress_health_check": [HealthCheck.filter_too_much],
    }
    return settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def claims(*, expedite: bool = False) -> Process:
    """A claims process. `expedite=True` adds the shortcut that breaks the rule."""
    transitions = [
        Transition(name="Extract", source="Intake", target="Triage"),
        Transition(
            name="RouteSmall",
            source="Triage",
            target="Review",
            guards=[
                Guard(
                    variable="amount_band",
                    op="eq",
                    value="small",
                    stated_as="small claims go to a single adjuster",
                )
            ],
        ),
        Transition(
            name="RouteLarge",
            source="Triage",
            target="Escalated",
            guards=[
                Guard(
                    variable="amount_band",
                    op="eq",
                    value="large",
                    stated_as="large claims need senior review",
                )
            ],
        ),
        Transition(
            name="Approve",
            source="Review",
            target="Paid",
            effects=[Effect(variable="approvals", increment=1)],
        ),
        Transition(name="Reject", source="Review", target="Denied"),
        Transition(
            name="SeniorApprove",
            source="Escalated",
            target="Paid",
            effects=[Effect(variable="approvals", increment=2)],
        ),
        Transition(name="SeniorReject", source="Escalated", target="Denied"),
    ]
    if expedite:
        transitions.append(
            Transition(
                name="Expedite",
                source="Escalated",
                target="Paid",
                guards=[Guard(variable="amount_band", op="eq", value="large")],
                effects=[Effect(variable="approvals", increment=1)],
            )
        )
    return Process(
        name="ClaimsIntake",
        description="Insurance claim from intake to settlement",
        initial_state="Intake",
        states=[
            State(name="Intake", description="Read the claim", agent_method="extract"),
            State(name="Triage", description="Route by size", agent_method="triage"),
            State(name="Review", description="Adjuster reviews", agent_method="review"),
            State(name="Escalated", description="Senior reviews", agent_method="review"),
            State(name="Paid", terminal=True),
            State(name="Denied", terminal=True),
        ],
        variables=[
            Variable(name="approvals", low=0, high=3, initial=0),
            # No `initial`: claim size comes from the claimant, so every branch is explored.
            Variable(name="amount_band", values=["small", "large"]),
        ],
        transitions=transitions,
        invariants=[
            Invariant(
                name="LargeNeedsTwoApprovals",
                stated_as="a large claim is never paid on fewer than two approvals",
                forbidden_state="Paid",
                forbidden_when=[
                    Guard(variable="amount_band", op="eq", value="large"),
                    Guard(variable="approvals", op="lt", value=2),
                ],
            )
        ],
    )


# ── The IR rejects malformed processes before any verifier runs ──


def test_dangling_transition_target_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown state"):
        Process(
            name="P",
            initial_state="A",
            states=[State(name="A"), State(name="B", terminal=True)],
            transitions=[Transition(name="T", source="A", target="Nowhere")],
        )


def test_guard_on_unknown_variable_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown variable"):
        Process(
            name="P",
            initial_state="A",
            states=[State(name="A"), State(name="B", terminal=True)],
            transitions=[
                Transition(
                    name="T",
                    source="A",
                    target="B",
                    guards=[Guard(variable="ghost", op="eq", value=1)],
                )
            ],
        )


def test_guard_value_outside_the_domain_is_rejected() -> None:
    """A guard that can never hold silently disables a branch, so reject it."""
    with pytest.raises(ValidationError, match="outside the domain"):
        Process(
            name="P",
            initial_state="A",
            states=[State(name="A"), State(name="B", terminal=True)],
            variables=[Variable(name="n", low=0, high=2, initial=0)],
            transitions=[
                Transition(
                    name="T",
                    source="A",
                    target="B",
                    guards=[Guard(variable="n", op="eq", value=9)],
                )
            ],
        )


def test_process_with_no_terminal_state_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no terminal state"):
        Process(
            name="P",
            initial_state="A",
            states=[State(name="A")],
            transitions=[Transition(name="Loop", source="A", target="A")],
        )


def test_unreachable_states_are_reported() -> None:
    process = Process(
        name="P",
        initial_state="A",
        states=[State(name="A"), State(name="B", terminal=True), State(name="Orphan")],
        transitions=[Transition(name="T", source="A", target="B")],
    )
    assert process.unreachable_states() == {"Orphan"}


def test_free_variable_yields_every_starting_assignment() -> None:
    """The fix for vacuous verification: a free variable multiplies the start states."""
    starts = claims().initial_assignments()
    assert {s["amount_band"] for s in starts} == {"small", "large"}


# ── Consumer 1: TLA+ and TLC ──


def test_rendered_spec_declares_every_transition_as_an_action() -> None:
    spec = tla.render(claims())
    for transition in claims().transitions:
        assert f"{transition.name} ==" in spec
    assert "INVARIANT LargeNeedsTwoApprovals" in tla.render_config(claims())


def test_free_variable_renders_as_a_set_membership() -> None:
    """`\\in` over the domain, not `=`, or TLC never visits the other branch."""
    spec = tla.render(claims())
    assert 'amount_band \\in {"small", "large"}' in spec
    assert "approvals = 0" in spec


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_verifies_the_sound_process() -> None:
    result = tla.check(claims())
    assert result.ok, result.raw[-2000:]
    # Both branches reachable, so the invariant is not passing vacuously.
    assert result.distinct_states >= 10


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_catches_the_shortcut_that_skips_senior_approval() -> None:
    result = tla.check(claims(expedite=True))
    assert not result.ok
    assert result.violated == "LargeNeedsTwoApprovals"
    assert any("Expedite" in line for line in result.trace)


# ── Liveness is a second question, asked only when asked for ──


def test_the_default_spec_says_nothing_about_termination() -> None:
    """Safety-only is the default, so nothing about fairness may leak into it.

    Two mined models in the case study have real rework loops and are asserted `ok`
    today. They stay `ok` because the flag is off, not because they terminate.
    """
    spec = tla.render(claims())
    config = tla.render_config(claims())
    assert "Termination" not in spec
    assert "WF_" not in spec
    assert "Spec == Init /\\ [][Next]_vars\n" in spec
    assert "Termination" not in config
    assert "PROPERTY" not in config


def test_the_liveness_spec_asks_whether_a_terminal_state_is_always_reached() -> None:
    spec = tla.render(claims(), liveness=True)
    assert 'Termination == <>(pc \\in {"Paid", "Denied"})' in spec
    assert "Spec == Init /\\ [][Next]_vars /\\ WF_vars(Next)" in spec
    assert "Spec == Init /\\ [][Next]_vars\n" not in spec


def test_the_liveness_config_names_exactly_one_property() -> None:
    """TLC never says *which* temporal property failed, so a second one would make
    the violation unattributable."""
    config = tla.render_config(claims(), liveness=True)
    assert config.splitlines().count("PROPERTY Termination") == 1
    assert len([line for line in config.splitlines() if line.startswith("PROPERTY")]) == 1
    # The invariants are still checked; liveness is an addition, not a replacement.
    assert "INVARIANT LargeNeedsTwoApprovals" in config


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_proves_the_acyclic_process_always_terminates() -> None:
    result = tla.check(claims(), liveness=True, timeout=200)
    assert result.ok, result.raw[-2000:]
    assert result.outcome == "verified"


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_a_cycle_the_safety_check_accepts_is_caught_by_the_liveness_check() -> None:
    """The gate must be able to fail. `revisiting()` has a real `B → C → B` loop, so
    a behavior can go around it forever: safety holds, termination does not.

    `WF_vars(Next)` forbids only stalling while a move is enabled. Looping is moving,
    so the loop survives fairness and refutes `Termination` — the correct verdict.
    """
    safe = tla.check(revisiting(), timeout=200)
    assert safe.ok, "the cycle breaks no invariant, so safety alone cannot see it"

    live = tla.check(revisiting(), liveness=True, timeout=200)
    assert not live.ok
    assert live.outcome == "violated"
    assert live.violated == "TemporalProperty"
    assert any(line.startswith("Back to state") for line in live.trace), live.trace


# ── The verdict parser: a broken run must never look like a clean one ──
#
# Every fixture below is real TLC 2.19 output, trimmed, captured with the
# `returncode` java actually exited with. The whole safety argument rests on this
# verdict, so the parser is tested against the checker's own vocabulary rather
# than against a paraphrase of it.


def _tlc(body: str) -> str:
    """Prepend the banner every real TLC run prints."""
    return "TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)\n" + body


_CLEAN = _tlc("""Computing initial states...
Finished computing initial states: 1 distinct state generated at 2026-07-30 17:29:31.
Model checking completed. No error has been found.
3 states generated, 2 distinct states found, 0 states left on queue.
Finished in 00s at (2026-07-30 17:29:31)
""")

# Init is unsatisfiable: TLC explores nothing, finds no error, and exits 0.
_NO_STATES = _tlc("""Computing initial states...
Finished computing initial states: 0 distinct states generated at 2026-07-30 17:29:11.
Model checking completed. No error has been found.
0 states generated, 0 distinct states found, 0 states left on queue.
Finished in 00s at (2026-07-30 17:29:11)
""")

_VIOLATED = _tlc("""Finished computing initial states: 1 distinct state generated.
Error: Invariant Inv is violated.
Error: The behavior up to this point is:
State 1: <Initial predicate>
pc = 0

State 2: <Next line 6, col 13 to line 6, col 29 of module Bad>
pc = 1

2 states generated, 2 distinct states found, 0 states left on queue.
""")

_PARSE_FAILED = _tlc("""Lexical error at line 4, column 17.  Encountered: "?"
Fatal errors while parsing TLA+ spec in file Broken
Error: Parsing or semantic analysis failed.
""")

_EVAL_FAILED = _tlc("""Finished computing initial states: 1 distinct state generated.
Error: Evaluating invariant Inv failed.
The first argument of < should be an integer, but instead it is:
"x"
Error: The behavior up to this point is:
State 1: <Initial predicate>
pc = 0

2 states generated, 2 distinct states found, 0 states left on queue.
""")

_DEADLOCK = _tlc("""Finished computing initial states: 1 distinct state generated.
Error: Deadlock reached.
Error: The behavior up to this point is:
State 1: <Initial predicate>
pc = 0

2 states generated, 2 distinct states found, 0 states left on queue.
""")

_PROGRESS = (
    "Progress(2) at 2026-07-30 17:30:40: 4 states generated,"
    " 2 distinct states found, 0 states left on queue."
)

_TEMPORAL = _tlc(f"""{_PROGRESS}
Error: Temporal properties were violated.

Error: The following behavior constitutes a counter-example:

State 1: <Initial predicate>
pc = 0

State 2: Stuttering
4 states generated, 2 distinct states found, 0 states left on queue.
""")

# A lasso: the counterexample is a finite prefix plus a cycle, and the cycle is named
# only by the closing `Back to state` line. Captured from a real `liveness=True` run
# over `revisiting()`, which exits 13. The two interpolated lines are verbatim too;
# they are only assembled here because they exceed the line limit as literals.
_LASSO_PROGRESS = (
    "Progress(3) at 2026-08-07 02:06:16: 6 states generated,"
    " 4 distinct states found, 0 states left on queue."
)
_LASSO_CHECKING = (
    "Checking temporal properties for the complete state space with"
    " 4 total distinct states at (2026-08-07 02:06:16)"
)

_LASSO = _tlc(f"""Implied-temporal checking--satisfiability problem has 1 branches.
Computing initial states...
Finished computing initial states: 1 distinct state generated at 2026-08-07 02:06:16.
{_LASSO_PROGRESS}
{_LASSO_CHECKING}
Error: Temporal properties were violated.

Error: The following behavior constitutes a counter-example:

State 1: <Initial predicate>
pc = "A"

State 2: <AtoB line 17, col 3 to line 18, col 14 of module Revisit>
pc = "B"

State 3: <BtoC line 21, col 3 to line 22, col 14 of module Revisit>
pc = "C"

Back to state 2: <CtoB line 29, col 3 to line 30, col 14 of module Revisit>

Finished checking temporal properties in 00s at 2026-08-07 02:06:16
6 states generated, 4 distinct states found, 0 states left on queue.
Finished in 00s at (2026-08-07 02:06:16)
""")

# A temporal property TLC could not evaluate. Real output, and the surprise is the
# exit status: TLC returns **0** here, having checked nothing, with no success line.
# Only the `Error:` line separates this from a pass.
_LIVENESS_EVAL_FAILED = _tlc("""Implied-temporal checking--satisfiability problem has 1 branches.
Computing initial states...
Error: The second argument of > should be an integer, but instead it is:
"x"
1 states generated, 1 distinct states found, 1 states left on queue.
Finished in 00s at (2026-08-07 02:08:48)
""")

_HEAP_AFTER_SUCCESS = _tlc("""Model checking completed. No error has been found.
14 states generated, 8 distinct states found, 0 states left on queue.
Error: Java heap space
""")


def test_a_clean_tlc_run_still_reads_as_verified() -> None:
    """The negative control: tightening the parser must not fail a good run."""
    result = tla._parse(_CLEAN, returncode=0)
    assert result.ok
    assert result.outcome == "verified"
    assert (result.states_found, result.distinct_states) == (3, 2)
    assert result.violated is None
    assert result.failure is None


def test_zero_distinct_states_is_never_a_pass() -> None:
    """TLC finds no error because it checked nothing. That is not verification."""
    result = tla._parse(_NO_STATES, returncode=0)
    assert not result.ok
    assert result.outcome == "vacuous"
    assert result.distinct_states == 0
    assert "verified" not in result.summary


def test_a_nonzero_exit_is_never_a_pass() -> None:
    """java crashing after printing the success line must not read as verified."""
    result = tla._parse(_CLEAN, returncode=1)
    assert not result.ok
    assert result.outcome == "failed"
    assert result.returncode == 1
    assert "verified" not in result.summary


def test_an_error_line_after_the_success_line_is_never_a_pass() -> None:
    """The success line is printed before the run can still die. Order proves nothing."""
    result = tla._parse(_HEAP_AFTER_SUCCESS, returncode=0)
    assert not result.ok
    assert result.outcome == "failed"
    assert result.failure is not None
    assert "heap" in result.failure.lower()


def test_a_spec_failure_is_distinguishable_from_a_property_violation() -> None:
    """A malformed spec is a broken checker, not a broken process."""
    result = tla._parse(_PARSE_FAILED, returncode=150)
    assert not result.ok
    assert result.outcome == "failed"
    assert result.violated is None
    assert "VIOLATED" not in result.summary
    assert "None" not in result.summary
    assert result.failure is not None
    assert "Parsing or semantic analysis failed" in result.failure


def test_an_invariant_failing_to_evaluate_is_a_checker_failure() -> None:
    """TLC could not decide the invariant, so it neither held nor was violated."""
    result = tla._parse(_EVAL_FAILED, returncode=76)
    assert not result.ok
    assert result.outcome == "failed"
    assert result.violated is None


def test_an_invariant_violation_is_reported_as_a_violation() -> None:
    result = tla._parse(_VIOLATED, returncode=12)
    assert not result.ok
    assert result.outcome == "violated"
    assert result.violated == "Inv"
    assert len(result.trace) == 2
    assert "VIOLATED Inv" in result.summary


def test_tlcs_own_deadlock_report_is_a_violation_not_a_pass() -> None:
    result = tla._parse(_DEADLOCK, returncode=11)
    assert not result.ok
    assert result.outcome == "violated"
    assert result.violated == "Deadlock"


def test_a_temporal_violation_is_not_a_pass() -> None:
    """Also exercises a `Progress(...)` line, which the old parser crashed on."""
    result = tla._parse(_TEMPORAL, returncode=13)
    assert not result.ok
    assert result.outcome == "violated"
    assert result.distinct_states == 2


def test_a_temporal_property_that_cannot_be_evaluated_is_a_checker_failure() -> None:
    """TLC exits 0 on this one, having decided nothing. If exit status alone were
    trusted, an unevaluable liveness property would read as termination verified."""
    result = tla._parse(_LIVENESS_EVAL_FAILED, returncode=0)
    assert not result.ok
    assert result.outcome == "failed"
    assert result.violated is None
    assert "VIOLATED" not in result.summary


def test_a_lasso_counterexample_keeps_the_line_that_closes_the_loop() -> None:
    """Dropping `Back to state N` would leave the trace reading as a finite prefix,
    which is the one thing a liveness counterexample is not."""
    result = tla._parse(_LASSO, returncode=13)
    assert not result.ok
    assert result.outcome == "violated"
    assert result.violated == "TemporalProperty"
    assert result.trace[-1].startswith("Back to state 2:")
    assert "CtoB" in result.trace[-1], "the loop-closing action has to be nameable"
    assert result.distinct_states == 4


def test_progress_lines_do_not_break_the_state_counts() -> None:
    """`Progress(2) at ...: 4 states generated` starts with a non-numeric token."""
    noisy = _CLEAN.replace("3 states generated", f"{_PROGRESS}\n3 states generated")
    result = tla._parse(noisy, returncode=0)
    assert result.ok
    # The final totals line wins over any interim progress report.
    assert (result.states_found, result.distinct_states) == (3, 2)


def test_witness_counts_gate_the_pass_for_the_vacuity_detector() -> None:
    """The seam: an invariant with no witness state cannot be reported as verified."""
    verified = tla._parse(_CLEAN, returncode=0)
    assert verified.with_witnesses({"Inv": 4}).ok
    vacuous = verified.with_witnesses({"Inv": 4, "Unreached": 0})
    assert not vacuous.ok
    assert vacuous.vacuous_invariants == ("Unreached",)
    assert "Unreached" in vacuous.summary


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_run_with_an_unsatisfiable_init_is_not_a_pass() -> None:
    """End to end through real TLC: `values=[]` renders `e \\in {}`, so `Init` has no
    solution. TLC exits 0 saying no error was found, having explored nothing."""
    process = Process(
        name="Vacuous",
        initial_state="A",
        states=[State(name="A"), State(name="B", terminal=True)],
        variables=[Variable(name="e", values=[])],
        transitions=[Transition(name="T", source="A", target="B")],
    )
    assert "e \\in {}" in tla.render(process)
    result = tla.check(process, timeout=120)
    assert "Model checking completed. No error has been found." in result.raw
    assert result.returncode == 0
    assert result.distinct_states == 0
    assert not result.ok
    assert result.outcome == "vacuous"


# ── Consumer 2: the interpreter ──


async def test_interpreter_runs_each_branch_to_a_terminal_state() -> None:
    process = claims()
    for start in process.initial_assignments():
        run = await interpreter.run(process, _always_first, start=start)
        assert process.state_map[run.final_state].terminal
        if start["amount_band"] == "large":
            assert "RouteLarge" in run.path
            assert run.variables["approvals"] >= 2


async def test_illegal_proposals_are_rejected_not_obeyed() -> None:
    """The agent is an untrusted oracle: it proposes, the interpreter decides."""
    attempts: list[str] = []

    async def rogue(_state: str, enabled: list[Transition], _v: dict[str, int | str]) -> str:
        attempts.append("call")
        # `SeniorApprove` does not leave `Review`; taking it would break the invariant.
        return "SeniorApprove" if len(attempts) < 3 else enabled[0].name

    run = await interpreter.run(claims(), rogue, start={"approvals": 0, "amount_band": "small"})
    assert run.rejections == 2
    assert "SeniorApprove" not in run.path


async def test_an_agent_that_never_proposes_legally_fails_loudly() -> None:
    async def stubborn(_s: str, _e: list[Transition], _v: dict[str, int | str]) -> str:
        return "NoSuchTransition"

    with pytest.raises(interpreter.ProcessError, match="no legal transition"):
        await interpreter.run(claims(), stubborn, start={"approvals": 0, "amount_band": "small"})


async def test_runtime_invariant_check_catches_the_bugged_process() -> None:
    """Defence in depth: even unverified, the bad path is stopped as it happens."""
    with pytest.raises(interpreter.InvariantViolated, match="LargeNeedsTwoApprovals"):
        await interpreter.run(
            claims(expedite=True),
            _prefer("Expedite"),
            start={"approvals": 0, "amount_band": "large"},
        )


async def test_deadlock_is_reported_with_the_stuck_state() -> None:
    process = Process(
        name="Stuck",
        initial_state="A",
        states=[State(name="A"), State(name="B", terminal=True)],
        variables=[Variable(name="flag", low=0, high=1, initial=0)],
        transitions=[
            Transition(
                name="OnlyWhenSet",
                source="A",
                target="B",
                guards=[Guard(variable="flag", op="eq", value=1)],
            )
        ],
    )
    with pytest.raises(interpreter.Deadlock, match="A has no enabled transition"):
        await interpreter.run(process, _always_first)


def test_the_prompt_carries_the_natural_language_conditions() -> None:
    """The NL a guard came from reaches the agent; only the formal part reaches TLC."""
    process = claims()
    offered = interpreter.offer("Triage", process.outgoing("Triage"), {"amount_band": "large"})
    assert "large claims need senior review" in offered
    assert "`RouteLarge`" in offered


# ── Consumer 3: Hypothesis over the same IR ──


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(proposals=st.lists(st.sampled_from([t.name for t in claims().transitions]), max_size=8))
def test_no_proposal_sequence_can_break_a_verified_process(proposals: list[str]) -> None:
    """Whatever the agent says, the run either completes legally or fails cleanly.

    This is the property that matters for an untrusted decision-maker: the failure
    set is closed. Nothing an agent returns produces a bad terminal state.
    """
    process = claims()
    for start in process.initial_assignments():
        try:
            run = properties.run_sync(process, proposals, start=start)
        except interpreter.ProcessError:
            continue  # refusing to proceed is a legal outcome
        assert process.state_map[run.final_state].terminal
        for spec in process.invariants:
            assert not spec.violated_by(run.final_state, run.variables)


def test_stateful_machine_finds_no_violation_in_the_sound_process() -> None:
    run_state_machine_as_test(properties.machine_for(claims()), settings=checked())


def test_stateful_machine_shrinks_a_counterexample_in_the_bugged_process() -> None:
    """Hypothesis reaches the same conclusion as TLC, by sampling instead of proof.

    Sampling is why the example budget is high here. Only about 5% of random walks
    reach the violating end state, because `SeniorApprove` usually fires before
    `Expedite` and satisfies the rule legitimately. At 150 examples this test found
    the bug perhaps two runs in three — the exact asymmetry that makes TLC worth
    running alongside: it *exhausts* the space, so a rare path is not a matter of
    luck, and PBT confirms the finding against code TLC never executes.
    """
    with pytest.raises(AssertionError, match="LargeNeedsTwoApprovals"):
        run_state_machine_as_test(
            properties.machine_for(claims(expedite=True)),
            settings=checked(max_examples=1500, stateful_step_count=16),
        )


# ── Helpers ──


async def _always_first(
    _state: str, enabled: list[Transition], _variables: dict[str, int | str]
) -> str:
    return enabled[0].name


def _prefer(name: str) -> interpreter.Decide:
    async def decide(
        _state: str, enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        for transition in enabled:
            if transition.name == name:
                return name
        return enabled[0].name

    return decide


# ── The agent as the untrusted decision-maker ──


async def test_agent_choice_is_validated_against_the_verified_skeleton() -> None:
    """The model proposes, the interpreter decides. Illegal answers cost a turn, nothing more."""
    from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn

    from pneuma.process.agent_driver import Navigator

    async with RuntimeHarness():
        process = claims()
        navigator = Navigator(process, context="claims desk")
        model = ScriptedModel(
            [
                # `Approve` does not leave `Escalated`; obeying it would pay a large
                # claim on one approval, which is the invariant TLC checks.
                Turn(tool_calls=(("Choice", {"transition": "Approve", "reason": "looks fine"}),)),
                Turn(
                    tool_calls=(
                        ("Choice", {"transition": "SeniorApprove", "reason": "senior signs off"}),
                    )
                ),
            ]
        )
        compiled = navigator.compiled("choose", model=model)

        async def decide(
            state: str, enabled: list[Transition], variables: dict[str, int | str]
        ) -> str:
            choice = await compiled(
                state, interpreter.offer(state, enabled, variables), "claim of $80,000"
            )
            return choice.transition

        run = await interpreter.run(process, decide, start={"approvals": 0, "amount_band": "large"})

    assert run.final_state == "Paid"
    assert run.variables["approvals"] == 2
    assert "Approve" in [r for step in run.steps for r in step.rejected]


async def test_single_option_states_never_consult_the_model() -> None:
    """Cost control: asking an agent to choose from one option buys nothing."""
    consulted: list[str] = []

    async def counting(state: str, enabled: list[Transition], _v: dict[str, int | str]) -> str:
        consulted.append(state)
        return enabled[0].name

    process = claims()
    await interpreter.run(process, counting, start={"approvals": 0, "amount_band": "large"})
    # Intake and Triage each have exactly one enabled move; only Escalated branches.
    assert consulted == ["Escalated"]


# ── The history the agent is shown is the path that actually ran ──


def revisiting() -> Process:
    """A process with a real cycle: `A → B → C → B → D`.

    `A` has one enabled move, so the step into `B` is taken without consulting the
    agent. That deterministic step is the one a caller-maintained history loses.
    """
    return Process(
        name="Revisit",
        description="A cycle the agent can be talked out of",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="Branch"),
            State(name="C", description="Detour"),
            State(name="D", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoC", source="B", target="C"),
            Transition(name="BtoD", source="B", target="D"),
            Transition(name="CtoB", source="C", target="B"),
        ],
    )


async def _detour_once(offers: list[str]) -> tuple[interpreter.Run, list[list[str]]]:
    """Drive `revisiting()` around the cycle exactly once, recording every offer."""
    histories: list[list[str]] = []
    calls = 0

    async def wander(state: str, enabled: list[Transition], variables: dict[str, int | str]) -> str:
        nonlocal calls
        calls += 1
        histories.append(interpreter.history())
        offers.append(interpreter.offer(state, enabled, variables))
        return "BtoC" if calls == 1 else "BtoD"

    run = await interpreter.run(revisiting(), wander, max_steps=10)
    return run, histories


async def test_the_history_includes_steps_taken_without_the_model() -> None:
    """`_elicit` skips the agent at a single-option state; the history must not skip it."""
    offers: list[str] = []
    run, _ = await _detour_once(offers)

    assert run.path == ["AtoB", "BtoC", "CtoB", "BtoD"]
    # `AtoB` was deterministic, so `A` never reached a decider that could record it.
    assert "Steps taken so far (2): A → B." in offers[0]
    assert "Steps taken so far (4): A → B → C → B." in offers[1]


async def test_a_state_the_run_passed_through_is_marked_as_a_revisit() -> None:
    offers: list[str] = []
    await _detour_once(offers)

    assert "[REVISIT" not in offers[0]
    assert "`C` [REVISIT" in offers[1], "C was visited via a detour and is offered again"
    assert "`D` [REVISIT" not in offers[1]


async def test_the_history_equals_the_states_the_run_actually_occupied() -> None:
    offers: list[str] = []
    run, histories = await _detour_once(offers)

    occupied = [run.steps[0].state] + [step.target for step in run.steps]
    assert occupied == ["A", "B", "C", "B", "D"]
    # One history per decision, each a prefix of the real path ending at the state
    # the agent was standing in.
    assert histories == [occupied[:2], occupied[:4]]


async def test_a_decider_that_tracks_nothing_still_gets_the_full_history() -> None:
    """The three-argument `Decide` contract is what `learning.py` and `Navigator` use.

    Both call `offer(state, enabled, variables)` with no history of their own, so the
    history has to come from the interpreter or those two call sites see a prompt the
    live experiment does not.
    """
    offers: list[str] = []

    async def watching(
        state: str, enabled: list[Transition], variables: dict[str, int | str]
    ) -> str:
        offers.append(interpreter.offer(state, enabled, variables))
        return enabled[0].name

    await interpreter.run(claims(), watching, start={"approvals": 0, "amount_band": "large"})

    assert len(offers) == 1, "only Escalated branches"
    assert "Steps taken so far (3): Intake → Triage → Escalated." in offers[0]


def test_the_history_is_empty_outside_a_run() -> None:
    """`offer` is also called standalone, where there is no path to report."""
    assert interpreter.history() == []
    offered = interpreter.offer("Triage", claims().outgoing("Triage"), {"amount_band": "large"})
    assert "Steps taken so far" not in offered


async def test_the_run_history_does_not_leak_past_the_run() -> None:
    """Every exit — completion, step budget, deadlock — has to clear the history."""
    assert interpreter.history() == []

    await interpreter.run(claims(), _always_first, start={"approvals": 0, "amount_band": "large"})
    assert interpreter.history() == []

    with pytest.raises(interpreter.ProcessError):
        await interpreter.run(revisiting(), _prefer("BtoC"), max_steps=4)
    assert interpreter.history() == []


async def test_an_explicit_visited_argument_still_overrides_the_run_history() -> None:
    """`offer` stays usable standalone, so a caller can supply or suppress the path."""
    captured: list[str] = []

    async def watching(
        state: str, enabled: list[Transition], variables: dict[str, int | str]
    ) -> str:
        captured.append(interpreter.offer(state, enabled, variables, visited=[]))
        captured.append(interpreter.offer(state, enabled, variables, visited=["Escalated"]))
        return enabled[0].name

    await interpreter.run(claims(), watching, start={"approvals": 0, "amount_band": "large"})

    assert "Steps taken so far" not in captured[0]
    assert "Steps taken so far (1): Escalated." in captured[1]


def test_navigator_exposes_a_typed_contract() -> None:
    from pneuma.process.agent_driver import Navigator

    fn = Navigator(claims()).compiled("choose")
    assert fn.input_shape.value == "structured"
