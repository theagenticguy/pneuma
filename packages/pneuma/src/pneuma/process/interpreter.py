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

from .ir import Invariant, Process, Transition

Decide = Callable[[str, list[Transition], dict[str, int | str]], Awaitable[str]]


@dataclass(frozen=True)
class Revisit:
    """One re-entry into a state this run had already occupied: a voiced dead end.

    The `[REVISIT]` marker in `offer` tells the *model* a target is a repeat, and
    nothing else could hear it — the fact lived in prompt prose and died with the
    prompt. This is the same fact as data, recorded on `Run.revisits` and readable
    mid-run through `revisits()`, so a retrieval query or a report can say which
    states the case circled and what else it could have done instead.

    Attributes:
        state: The state re-entered.
        step: `Step.index` of the transition that re-entered it.
        alternatives: Names of the *other* transitions that were enabled at the
            source — what the run could have chosen instead of circling. Empty means
            the revisit was forced: the interpreter steps through a lone enabled
            transition without consulting anyone.
    """

    state: str
    step: int
    alternatives: tuple[str, ...] = ()


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

# The dead ends the enclosing `run` has recorded so far, oldest first. A second
# ContextVar rather than a field a decider digs out of a `Run`, for the same reason
# `_HISTORY` is one: the decider's signature stays unchanged, and the entries are
# visible *during* the run — which is when a retrieval query wants to say "this case
# has already dead-ended twice" — not only after it ends.
_REVISITS: ContextVar[tuple[Revisit, ...]] = ContextVar("pneuma_process_revisits", default=())

DEFAULT_MAX_REVISITS = 5
"""Consecutive revisits tolerated before `run` halts with `NoProgress`.

Conservative on purpose: a legitimate detour re-enters a state once or twice, while
the dithering the live experiment measured re-enters them until the step budget
stops it. Five consecutive re-entries with no new state in between is the latter.
Pass `max_revisits=None` to disable the halt and burn the budget as before.
"""


def history() -> list[str]:
    """The states the enclosing `run` has occupied so far, current state last."""
    return list(_HISTORY.get())


def revisits() -> list[Revisit]:
    """The dead ends the enclosing `run` has recorded so far, oldest first.

    Empty outside a run, like `history()`. A decider building a retrieval query reads
    this to voice the run's dead ends at the moment a choice is being made.
    """
    return list(_REVISITS.get())


class ProcessError(RuntimeError):
    """The run could not continue."""


class Deadlock(ProcessError):
    """A non-terminal state with no enabled transition."""


class NoProgress(ProcessError):
    """The run halted early because consecutive revisits reached its limit.

    Modeled on `detect.discrimination`'s three-valued honesty: an outcome that stops
    short must *name the bound it hit*, or a reader cannot tell the finding from the
    harness's own cap. So this is not `Deadlock` — every transition here was enabled
    and legal — and not the `max_steps` `ProcessError` — the budget was not spent; the
    run declared it *would have been*, and says which limit made it stop. It still IS
    a `ProcessError`, so `casestudy/live.py:170-175`'s blocked accounting sees exactly
    what it saw before.

    Attributes:
        limit: The `max_revisits` value that was hit.
        revisits: The consecutive `Revisit` entries that hit it, oldest first.
    """

    def __init__(self, limit: int, revisits: tuple[Revisit, ...]) -> None:
        circled = " → ".join(entry.state for entry in revisits)
        super().__init__(
            f"no progress: {limit} consecutive revisits ({circled}) without reaching a new "
            f"state, halting at the max_revisits={limit} limit rather than burning the "
            f"remaining step budget"
        )
        self.limit = limit
        self.revisits = revisits


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
    revisits: list[Revisit] = field(default_factory=list)
    """Every re-entry into an already-visited state, in step order. See `Revisit`."""

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
    max_revisits: int | None = DEFAULT_MAX_REVISITS,
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
        max_revisits: Consecutive re-entries into already-visited states tolerated
            before the run halts with `NoProgress` — "consecutive" meaning no state
            this run had never seen was reached in between. `None` disables the halt
            and a dithering run burns the whole step budget, exactly as before this
            parameter existed. The default is conservative; see `DEFAULT_MAX_REVISITS`.
        on_enter: Called once with the name of every state this run occupies, the
            initial state included, after that state's invariant check and before
            anything is decided from it. `None` leaves the walk exactly as it was.
            Not called for the visit that trips `max_revisits`: the run has just
            declared that visit progress-free, and spending the state's work on it
            would contradict the verdict being raised.

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
        NoProgress: `max_revisits` consecutive revisits without a new state. Names
            the limit it hit, so it cannot be mistaken for the exhausted budget.
        ProcessError: Step budget exhausted, or the agent never proposed a legal move.
        Exception: Whatever `on_enter` raises, unchanged. A hook that fails is not a
            process failure, and softening it here would let a run report a completed
            case whose per-state work never happened.
    """
    variables = dict(start if start is not None else process.initial_assignments()[0])
    states = process.state_map
    current = process.initial_state
    trace = Run(process=process.name, variables=variables)
    seen = {current}
    consecutive: list[Revisit] = []

    _assert_invariants(process, current, variables)

    token = _HISTORY.set((current,))
    revisit_token = _REVISITS.set(())
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
            # The `[REVISIT]` marker in `offer` voices this fact to the model and to
            # nobody else. Recording it as data — on the trace for afterwards, in the
            # ContextVar for during — is what lets a retrieval query or a report see
            # the dead end instead of re-deriving it from the path.
            revisiting = current in seen
            if revisiting:
                entry = Revisit(
                    state=current,
                    step=index,
                    alternatives=tuple(t.name for t in enabled if t.name != chosen.name),
                )
                trace.revisits.append(entry)
                _REVISITS.set((*_REVISITS.get(), entry))
                consecutive.append(entry)
            else:
                seen.add(current)
                consecutive.clear()

            _assert_invariants(process, current, variables)
            # After the invariant check — a violation outranks dithering — and before
            # `on_enter`, so no work is spent on the visit the run just declared
            # progress-free. A revisited state is never terminal (a run returns the
            # moment it occupies a terminal state, so there is no second entry), so
            # this cannot pre-empt a completion.
            if revisiting and max_revisits is not None and len(consecutive) >= max_revisits:
                raise NoProgress(max_revisits, tuple(consecutive))
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
        _REVISITS.reset(revisit_token)

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

