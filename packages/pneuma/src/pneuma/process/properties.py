"""Hypothesis over the same IR: property-based testing of the real interpreter.

TLC and Hypothesis check different things, and the pair is the point.

TLC exhausts a bounded *abstraction*. It proves the skeleton admits no bad state,
and it never runs your code.

Hypothesis samples the *implementation*. It drives `interpreter.run` with an
adversarial decision function, and when something breaks it shrinks the failing
run to the shortest sequence that still breaks it. That covers the gap TLC leaves:
whether this interpreter agrees with the model TLC verified.

`machine_for` builds a `RuleBasedStateMachine` from the IR, so one transition
becomes one rule, guards become the precondition, and every invariant becomes an
`@invariant` — the same three concepts the TLA+ renderer consumes, aimed at
running code instead.

Hypothesis cannot run inside the `code_execution_mode=LOCAL` sandbox: that
interpreter allows pure-computation stdlib only, and Hypothesis needs its own
tracing and decorator machinery. It belongs out here, around the AI function.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from . import interpreter
from .ir import Process, Transition


def adversarial_decider(
    proposals: list[str],
) -> Callable[[str, list[Transition], dict[str, int | str]], Any]:
    """A decision function that replays a Hypothesis-generated list of proposals.

    Once the list runs out it falls back to a legal move, so a short proposal list
    still produces a complete run rather than an artificial failure.
    """
    remaining = list(proposals)

    async def decide(
        _state: str, enabled: list[Transition], _variables: dict[str, int | str]
    ) -> str:
        if remaining:
            return remaining.pop(0)
        return enabled[0].name

    return decide


def run_sync(
    process: Process,
    proposals: list[str],
    start: dict[str, int | str] | None = None,
    **kwargs: Any,
) -> interpreter.Run:
    """Drive one run from a list of proposals, synchronously.

    Hypothesis rules are synchronous, so the async interpreter is driven through
    `asyncio.run` here rather than leaking async into every property.
    """
    return asyncio.run(
        interpreter.run(process, adversarial_decider(proposals), start=start, **kwargs)
    )


def machine_for(process: Process) -> type[RuleBasedStateMachine]:
    """Build a stateful test machine mirroring `process`.

    Rules are generated per transition so Hypothesis can search *sequences* of
    steps, which is what finds ordering bugs a single-step property misses.
    """
    initial = process.initial_assignments()
    states = process.state_map

    class ProcessMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.variables: dict[str, int | str] = dict(initial[0])
            self.current = process.initial_state
            self.history: list[str] = []

        @initialize(start=st.sampled_from(range(len(initial))))
        def choose_start(self, start: int) -> None:
            """Let Hypothesis pick the starting assignment.

            Without this the machine always begins from one assignment, and every
            branch guarded on a nondeterministic variable becomes unreachable — the
            same vacuity trap that makes a TLC run pass while never visiting the
            case the invariant is about. Here it silently reported *no violation*
            on a process that had one.
            """
            self.variables = dict(initial[start])

        @invariant()
        def state_is_declared(self) -> None:
            assert self.current in states, f"walked into undeclared state {self.current!r}"

        @invariant()
        def variables_stay_in_domain(self) -> None:
            for variable in process.variables:
                value = self.variables[variable.name]
                assert value in variable.domain, (
                    f"{variable.name} left its domain: {value!r} not in {variable.domain}"
                )

        @invariant()
        def process_invariants_hold(self) -> None:
            for spec in process.invariants:
                assert not spec.violated_by(self.current, self.variables), (
                    f"{spec.name} violated in {self.current} with {self.variables}"
                    f" after {self.history}"
                )

    for transition in process.transitions:

        def make(transition: Transition = transition) -> Callable[..., None]:
            def take(self: ProcessMachine) -> None:
                if self.current != transition.source or not transition.enabled(self.variables):
                    return
                for effect in transition.effects:
                    self.variables[effect.variable] = effect.apply(self.variables)
                self.current = transition.target
                self.history.append(transition.name)

            take.__name__ = f"take_{transition.name}"
            return take

        setattr(ProcessMachine, f"take_{transition.name}", rule()(make()))

    # No `precondition` on the rules above. Hypothesis raises InvalidDefinition when
    # *every* rule is disabled, which is exactly what a terminal state means — so
    # gating on "not terminal" turns a completed process into a spurious error.
    # Rules stay always-enabled and no-op when they do not apply, which also keeps
    # illegal steps in the search space where the invariants can judge them.

    ProcessMachine.__name__ = f"{process.name}Machine"
    return ProcessMachine
