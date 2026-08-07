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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .ir import Invariant, Process, Transition

Decide = Callable[[str, list[Transition], dict[str, int | str]], Awaitable[str]]

# Called once per state the run occupies. The state *name* and nothing else: whoever
# installs the hook already holds the `Process` it came from, so handing over the
# `State` object, the variables, or the trace would grow this file's fixed surface to
# save the caller a dictionary lookup. See `run`'s docstring for why the hook exists.
OnEnter = Callable[[str], Awaitable[None]]

# The states this run has occupied, oldest first, current last. Owned by `run` and
# read by `offer`, because `run` is the only thing that knows the whole path: a state
# with one enabled transition is stepped through without consulting the decider, so a
# history a decider maintains for itself is missing exactly those steps. A ContextVar
# rather than an argument keeps the `Decide` signature unchanged, and gives each
# asyncio task its own copy so concurrent runs cannot see each other's paths.
_HISTORY: ContextVar[tuple[str, ...]] = ContextVar("pneuma_process_history", default=())


def history() -> list[str]:
    """The states the enclosing `run` has occupied so far, current state last."""
    return list(_HISTORY.get())


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
    on_enter: OnEnter | None = None,
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
        on_enter: Called once with the name of every state this run occupies, the
            initial state included, after that state's invariant check and before
            anything is decided from it. `None` leaves the walk exactly as it was.

    **Why the hook exists at all.** `decide` covers the decision *between* states and
    nothing here covered the work *within* one, which is the other half of the
    pipeline: `State.agent_method` has named a per-state `@ai_method` since the IR was
    written, and no caller could reach it during a run. The two are not the same
    callback and must not be collapsed into one. `decide` is consulted only where
    there is a choice — a state with a single enabled transition is stepped through
    without it (see `_elicit`) — so a run's work would silently skip every
    deterministic step. And the work has to happen *during* the walk rather than
    afterwards over `Run.steps`, because what a handler produces is what the next
    decision is made from.

    The hook takes the state name and nothing else, which is deliberate. Whoever
    installs it holds the `Process` this run is walking and can look the `State` up;
    passing the object, the variables, or the partial trace would widen the surface of
    the one file in this package that is meant to stay fixed, to save that lookup.

    Raises:
        Deadlock: Stuck in a non-terminal state.
        InvariantViolated: A safety property failed at runtime.
        ProcessError: Step budget exhausted, or the agent never proposed a legal move.
        Exception: Whatever `on_enter` raises, unchanged. A hook that fails is not a
            process failure, and softening it here would let a run report a completed
            case whose per-state work never happened.
    """
    variables = dict(start if start is not None else process.initial_assignments()[0])
    states = process.state_map
    current = process.initial_state
    trace = Run(process=process.name, variables=variables)

    _assert_invariants(process, current, variables)

    token = _HISTORY.set((current,))
    try:
        # Inside the history scope rather than beside the check above, so the hook sees
        # the same `history()` a decider standing in this state would.
        if on_enter is not None:
            await on_enter(current)

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
            _HISTORY.set((*_HISTORY.get(), current))

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
            # Every state the run occupies, terminal ones included, and once per
            # *visit* rather than once per name: a cycle that comes back to a state
            # comes back to its work too.
            if on_enter is not None:
                await on_enter(current)
        # A run whose final budgeted step LANDED on a terminal state has completed,
        # not exceeded anything — the loop's own terminal check sits at the top, so
        # without this re-check the step executes, the state's per-state work runs,
        # and then the run raises a message that is factually false. Downstream that
        # is not cosmetic: `live.py` counts ProcessError as `blocked`, so a case
        # completing in exactly `max_steps` transitions would corrupt the
        # completed/blocked split at precisely the budget the experiment uses.
        if states[current].terminal:
            trace.final_state = current
            return trace
    finally:
        _HISTORY.reset(token)

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


def offer(
    state: str,
    enabled: list[Transition],
    variables: dict[str, int | str],
    visited: list[str] | None = None,
) -> str:
    """Render the choice as prose for an agent's prompt.

    Each option carries the natural-language condition its guards came from, so the
    model sees why an option is available rather than only that it is.

    The visit history is what stops the agent going in circles. Without it every
    decision is made from scratch: the model re-derives the same "this moves the case
    forward" reasoning at a state it has already left, picks the same edge, and
    oscillates between two valid states until the step budget runs out. It is not a
    model failure; the prompt genuinely contained no evidence the state was a repeat.
    Marking already-visited targets converts an invisible cycle into a visible one.

    `visited` defaults to the enclosing `run`'s own history, which is the only
    complete one. Pass a list to override it; pass `[]` to suppress the history.
    """
    path = history() if visited is None else visited
    header = f"You are at `{state}`. Process variables: {variables}."
    lines = [header]
    if path:
        lines += [
            "",
            f"Steps taken so far ({len(path)}): " + " → ".join(path) + ".",
        ]
    lines += ["", "Legal moves:"]
    seen = set(path)
    for transition in enabled:
        reasons = [g.stated_as or str(g) for g in transition.guards]
        because = f" (available because {'; '.join(reasons)})" if reasons else ""
        repeat = " [REVISIT — you have already been here]" if transition.target in seen else ""
        lines.append(f"- `{transition.name}` → `{transition.target}`{because}{repeat}")
    lines += [
        "",
        "Prefer a move that advances the case toward completion. Answer with exactly "
        "one transition name from the list. Any other answer is rejected.",
    ]
    return "\n".join(lines)


def pick_first(_state: str, enabled: list[Transition], _variables: dict[str, Any]) -> str:
    """A deterministic stand-in for an agent, for tests and dry runs."""
    return enabled[0].name
