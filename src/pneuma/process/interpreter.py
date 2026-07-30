"""Execute a verified `Process`, dispatching each state to an `@ai_method`.

This is the fixed interpreter: hand-written, reviewed once, and reused for every
process. Nothing here is generated, which is the whole point — the model supplies
data, and the code that acts on that data is code you read.

The agent is treated as an untrusted oracle. It proposes which transition to take;
the interpreter decides whether that transition is legal. A proposal naming an
unknown transition, one leaving a different state, or one whose guards do not hold
is rejected and re-offered, so the model cannot walk the process off its verified
skeleton no matter what it returns.

Invariants are re-checked after every step. TLC already proved they hold for the
*model*, so a runtime violation means the model and the interpreter disagree — a
bug in this file or in the IR, caught immediately rather than in production.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .ir import Invariant, Process, Transition

Decide = Callable[[str, list[Transition], dict[str, int | str]], Awaitable[str]]


class ProcessError(RuntimeError):
    """The run could not continue."""


class Deadlock(ProcessError):
    """A non-terminal state with no enabled transition."""


class InvariantViolated(ProcessError):
    """A safety property failed at runtime, which TLC said was impossible."""

    def __init__(self, invariant: Invariant, state: str, variables: dict[str, int | str]) -> None:
        super().__init__(
            f"{invariant.name} violated in {state} with {variables}"
            + (f" — stated as: {invariant.stated_as}" if invariant.stated_as else "")
        )
        self.invariant = invariant


@dataclass
class Step:
    """One executed transition, and what the agent said."""

    index: int
    state: str
    transition: str
    target: str
    variables: dict[str, int | str]
    rejected: list[str] = field(default_factory=list)


@dataclass
class Run:
    """The whole trace, replayable and inspectable."""

    process: str
    steps: list[Step] = field(default_factory=list)
    final_state: str = ""
    variables: dict[str, int | str] = field(default_factory=dict)

    @property
    def path(self) -> list[str]:
        return [step.transition for step in self.steps]

    @property
    def rejections(self) -> int:
        return sum(len(step.rejected) for step in self.steps)


async def run(
    process: Process,
    decide: Decide,
    *,
    start: dict[str, int | str] | None = None,
    max_steps: int = 50,
    max_rejections: int = 3,
) -> Run:
    """Drive `process` to a terminal state.

    Args:
        process: A validated IR, ideally one TLC has already checked.
        decide: Chooses among the enabled transitions. This is where an
            `@ai_method` goes; a deterministic function stands in for tests.
        start: Initial variable assignment, defaulting to the first the IR allows.
        max_steps: Cap on executed transitions, so a cycle cannot run forever.
        max_rejections: How many illegal proposals to tolerate per step before
            giving up on the agent and refusing to guess on its behalf.

    Raises:
        Deadlock: Stuck in a non-terminal state.
        InvariantViolated: A safety property failed at runtime.
        ProcessError: Step budget exhausted, or the agent never proposed a legal move.
    """
    variables = dict(start if start is not None else process.initial_assignments()[0])
    states = process.state_map
    current = process.initial_state
    trace = Run(process=process.name, variables=variables)

    _assert_invariants(process, current, variables)

    for index in range(max_steps):
        if states[current].terminal:
            trace.final_state = current
            return trace

        enabled = [t for t in process.outgoing(current) if t.enabled(variables)]
        if not enabled:
            raise Deadlock(f"{current} has no enabled transition with {variables}")

        chosen, rejected = await _elicit(decide, current, enabled, variables, max_rejections)

        for effect in chosen.effects:
            variables[effect.variable] = effect.apply(variables)
        current = chosen.target

        trace.steps.append(
            Step(
                index=index,
                state=chosen.source,
                transition=chosen.name,
                target=current,
                variables=dict(variables),
                rejected=rejected,
            )
        )
        _assert_invariants(process, current, variables)

    raise ProcessError(f"exceeded {max_steps} steps without reaching a terminal state")


async def _elicit(
    decide: Decide,
    current: str,
    enabled: list[Transition],
    variables: dict[str, int | str],
    max_rejections: int,
) -> tuple[Transition, list[str]]:
    """Ask for a legal transition, rejecting proposals that are not.

    A single enabled transition is taken without consulting the agent: there is
    nothing to decide, and asking would spend a model call to be told the only
    answer.
    """
    if len(enabled) == 1:
        return enabled[0], []

    legal = {t.name: t for t in enabled}
    rejected: list[str] = []
    for _ in range(max_rejections + 1):
        proposed = await decide(current, enabled, dict(variables))
        if proposed in legal:
            return legal[proposed], rejected
        rejected.append(proposed)
    raise ProcessError(
        f"{current}: no legal transition after {len(rejected)} rejected proposals "
        f"({rejected}); legal were {sorted(legal)}"
    )


def _assert_invariants(process: Process, current: str, variables: dict[str, int | str]) -> None:
    for invariant in process.invariants:
        if invariant.violated_by(current, variables):
            raise InvariantViolated(invariant, current, dict(variables))


def offer(state: str, enabled: list[Transition], variables: dict[str, int | str]) -> str:
    """Render the choice as prose for an agent's prompt.

    Each option carries the natural-language condition its guards came from, so the
    model sees why an option is available rather than only that it is.
    """
    lines = [f"You are at `{state}`. Process variables: {variables}.", "", "Legal moves:"]
    for transition in enabled:
        reasons = [g.stated_as or str(g) for g in transition.guards]
        because = f" (available because {'; '.join(reasons)})" if reasons else ""
        lines.append(f"- `{transition.name}` → `{transition.target}`{because}")
    lines += [
        "",
        "Answer with exactly one transition name from the list. Any other answer is rejected.",
    ]
    return "\n".join(lines)


def pick_first(_state: str, enabled: list[Transition], _variables: dict[str, Any]) -> str:
    """A deterministic stand-in for an agent, for tests and dry runs."""
    return enabled[0].name
