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


def test_navigator_exposes_a_typed_contract() -> None:
    from pneuma.process.agent_driver import Navigator

    fn = Navigator(claims()).compiled("choose")
    assert fn.input_shape.value == "structured"
