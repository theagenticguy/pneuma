"""An agent bound to one verified `Process`: it chooses transitions and does the work.

`interpreter.py` walks a model-checked skeleton and validates every proposal against it,
and `agent_driver.Navigator` supplied the proposals. Between them they covered the
decision *between* states and nothing at all inside one — `State.agent_method` has named
a per-state `@ai_method` since `ir.py:178` was written and the only reader it ever had
was `casestudy/handlers.handler_for`, which treats it as a boolean gate and looks the
real handler up by state name. So the pipeline proved an agent could not walk off the map
while leaving the map's contents empty. Rationale: `docs/design/process_agent.md`.

This class closes that loop, and the two halves stay two halves.

**Choosing is not working, and one callback cannot be both.** The decider runs only where
there is a choice: `_elicit` steps through a state with a single enabled transition
without consulting anybody (`interpreter.py:171-172`), which is deliberate cost control
and is pinned by `tests/library/test_process.py:803-814`. Dispatching per-state work from
inside the decider would therefore skip every deterministic step in the corridor — the
majority of a mined process. Doing it afterwards over `Run.steps` would run the work in
the right places and the wrong order, after the choices it was supposed to inform. So
`interpreter.run` grew one hook, `on_enter`, called once per state occupied, and this
class installs a dispatcher into it.

**Handlers are resolved by name, at last.** `handler_for` reads `agent_method` off the
entered state and dispatches to the `@ai_method` it names. A state naming nothing, or
naming a method this agent does not have, is a pure control point that costs nothing —
which is not a fallback but the *common* case, and it has to stay free: mined states
carry `agent_method='handle'` (`casestudy/miner.py:125`), a name that resolves to no
method on anybody, and `tests/app/test_casestudy.py:418` pins that as correctly `None`.

**Overrides reach everything.** `work(..., model=scripted)` is forwarded to every
`compiled` call the run makes — the `choose` compilation and each handler's — so a whole
process, decisions and work together, is scriptable offline without monkeypatching
anything. Compilation happens per `work()` call and always through `self.compiled`, so
the instance-rebinding pattern (`tests/app/test_casestudy.py:542-570`) reaches it too.

    class Clerk(ProcessAgent):
        @ai_method(CheckResult, description="Check a filing for completeness")
        def check(self, focus: str) -> CheckResult:
            '''Check the filing, focusing on {focus}. Case facts: {self.context}'''

        def arguments_for(self, state):
            return {"focus": state.description or "completeness"}

`await Clerk(process).work(facts="claim of $80,000")` drives the process to a terminal
state, asking the model to choose only at real branches and running `check` at every
state whose `agent_method` says `"check"`.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any

from pydantic import BaseModel, Field

from ..method import MethodAgent, _owner_name, ai_method
from . import interpreter
from .ir import Process, State, Transition

__all__ = ["Choice", "HandlerFailed", "ProcessAgent"]


class Choice(BaseModel):
    """One transition, and why."""

    transition: str = Field(description="Exactly one transition name from the offered list")
    reason: str = Field(description="One sentence, citing the condition that applies")


class HandlerFailed(RuntimeError):
    """A state's handler raised, so the run stopped where it was.

    Deliberately *not* a `ProcessError`. The interpreter's three failures all mean the
    process refused to continue, and callers branch on that: `casestudy/live.py:172-175`
    catches `ProcessError` and counts the case as `blocked`, which is an experimental
    result. A bug in a handler is not a result about the process, and inheriting from
    `ProcessError` would launder one into the other — a code fault arriving in a report as
    evidence about a guardrail.
    """


class ProcessAgent(MethodAgent):
    """An agent bound to one verified `Process`: it chooses transitions and works inside them.

    The subclass supplies handler `@ai_method`s and, if it needs them, the two hooks that
    say which one to run and what to do with the result. This base owns the skeleton: the
    `choose` capability, the decider adapter, the `on_enter` dispatcher, the wiring guard,
    and the fault wrapping.

    **Why the process is on `self` and the facts are a call argument.** The process is what
    the agent *is* bound to — it decides which transitions exist, so it cannot be something
    the model supplies — while the facts of the case change per run and belong in the
    argument list, where `collect_nodes` can see them if a caller ever wants to optimize the
    prompt against them (`method.py`'s header; `docs/design/method.md`). That is the same
    split `GatedProposer` makes between its gate and its candidate, stated once here so
    subclasses inherit it rather than rediscover it.

    **What this class deliberately does not do.** No memory, no optimizer, no `Team`
    orchestration, and no `Run`-level trace capture. See `docs/design/process_agent.md`.
    """

    CHOOSE = "choose"
    """The name of the decision capability, in one place.

    The decorator below, the compilation in `decider`, and the collision guard all have to
    agree about it; three literals would not. A class attribute rather than a module
    constant for the reason `GatedProposer.REASK` is one — a subclass that renames its
    decider renames it once.
    """

    def __init__(self, process: Process, *, context: str = "") -> None:
        self.process = process
        self.context = context
        self.name = f"{process.name.lower()}-agent"

    # ── The decision between states ──

    @ai_method(
        Choice, description="Choose the next transition in a business process", max_attempts=2
    )
    def choose(self, state: str, options: str, facts: str) -> Choice:
        """You are executing the `{self.process.name}` process.

        {self.process.description}

        Case facts:
        {facts}

        {options}

        Pick the transition the process rules require for these facts. Name the
        condition you relied on. Choosing an option not on the list wastes a turn:
        the runtime rejects it and asks again.
        """

    def decider(self, facts: str, **overrides: Any) -> interpreter.Decide:
        """Adapt `choose` into the callable `interpreter.run` expects.

        `offer` reads the run's visit history itself, so nothing here has to track
        where the case has already been.

        Compiled once per decider rather than once per decision, and always through
        `self.compiled` — the instance binding is how a test scripts the model
        (`method.py:407-411` makes the same argument for `spawn`), and a decider built at
        wiring time instead of inside `work` would have captured the real model before any
        binding happened.
        """
        compiled = self.compiled(self.CHOOSE, **overrides)

        async def decide(
            state: str, enabled: list[Transition], variables: dict[str, int | str]
        ) -> str:
            choice = await compiled(state, interpreter.offer(state, enabled, variables), facts)
            return choice.transition

        return decide

    # ── The work inside a state ──

    def handler_for(self, state: State) -> tuple[str, dict[str, Any]] | None:
        """The method and arguments for `state`, or None if it is a pure control point.

        The default honours `agent_method` literally: it names an `@ai_method` on this
        agent, or it names nothing this agent can do and the state is free. That is the
        promise `ir.py:178` has carried unkept, and honouring it is what lets a process and
        an agent be wired together by the IR alone.

        Returning None for an unrecognised name rather than raising is the load-bearing
        half. Mined states all carry `agent_method='handle'` (`casestudy/miner.py:125`,
        `casestudy/aimine.py:372`), a placeholder no agent implements, and
        `tests/app/test_casestudy.py:418` pins it as correctly `None`. A base that raised
        would make every mined process unrunnable by the class built to run it.

        This is the override point, and the signature is `casestudy.handlers.handler_for`'s
        (`handlers.py:155`) on purpose: an agent whose real mapping is a table keyed by
        state name — the mined-activity case, where `agent_method` is only the opt-in gate —
        overrides this and returns its own pair. The library then needs to know nothing
        about tables, which is what keeps it on the library side of
        `tests/library/test_boundary.py`.
        """
        method = state.agent_method
        if method is None or method not in self.ai_methods():
            return None
        return method, self.arguments_for(state)

    def arguments_for(self, state: State) -> dict[str, Any]:
        """The keyword arguments the handler for `state` is called with. Default: none.

        Separate from `handler_for` rather than folded into it because the two overrides
        answer different questions and are wanted separately. An agent whose states name
        their methods correctly and only needs per-state arguments overrides this one and
        inherits the resolution; an agent whose mapping does not come from `agent_method` at
        all overrides `handler_for` and can ignore this. Folding them together would force
        the first case to restate the resolution rule it agreed with.

        Not validated against the handler's signature, deliberately. A missing required
        argument raises `TypeError` from the signature bind at dispatch — loud, immediate,
        and naming the method — so a wiring-time check would buy a slightly better message
        for a failure that is already impossible to miss. The one collision worth refusing
        early is `choose`, because that one is *not* loud; see `_check_no_decider_handler`.
        """
        return {}

    def on_result(self, state: State, result: Any) -> Awaitable[None] | None:
        """What to do with a handler's output. Default: nothing.

        The base cannot know: a check produces findings to file, a determination produces a
        record, and an agent running a corridor of pure observations may want none of it.
        `casestudy.handlers.dispatch` (`handlers.py:176-186`) is the shape this hook
        generalises — dispatch on the result type, append to the case file.

        May be `async`, and the return value is awaited when it is awaitable. A sync-only
        hook would turn `async def on_result` into a silent no-op: the coroutine would never
        be awaited, the paperwork would never be written, the run would still report a
        completed case, and the only trace would be a `RuntimeWarning` at garbage
        collection. That is the fail-soft class this package's guards exist to remove, and
        it costs one `isawaitable` check to make unrepresentable.
        """
        return None

    async def dispatch(self, state: State, **overrides: Any) -> Any:
        """Run `state`'s handler, hand the result to `on_result`, and return it.

        Returns None when the state has no handler, which is the common case and the whole
        reason a mined process is affordable: most activities in a real log are recording
        steps, and spending a model call on each would buy nothing.

        **Why a fault here stops the run.** The three hooks this calls are the caller's
        code, and any of them can be wrong. A dispatcher that swallowed the exception and
        kept walking would produce a `Run` that reached a terminal state with the work
        inside it missing — a report of a completed case that did not happen, which is worse
        than a crash by exactly the amount a reader trusts the report. So every fault is
        re-raised as `HandlerFailed` naming the state, the method, and which part broke, with
        the original attached.

        Wrapped for context and *not* re-dressed as a verdict, which is the difference from
        `gated.py`. Every hook a `GatedProposer` calls runs inside a post-condition
        validator, where the runtime turns any exception into `[VALIDATION ERROR]` feedback
        the next attempt reads — so a bug there must be re-raised as something that says it
        is a bug, or it burns every retry masquerading as a refusal. Nothing here is on that
        path: a handler runs its own cycle to completion and there is no model waiting to be
        told anything, so the exception's job is to reach the caller with a traceback.
        """
        bound = self._resolve(state)
        if bound is None:
            return None

        method, arguments = bound
        try:
            result = await self.compiled(method, **overrides)(**arguments)
        except Exception as error:
            raise HandlerFailed(self._fault_text(state, method, error)) from error

        try:
            recorded = self.on_result(state, result)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception as error:
            raise HandlerFailed(
                self._fault_text(state, method, error, part="on_result hook")
            ) from error
        return result

    # ── The driver ──

    async def work(
        self,
        facts: str,
        *,
        start: dict[str, int | str] | None = None,
        max_steps: int = 50,
        max_rejections: int = 3,
        max_revisits: int | None = interpreter.DEFAULT_MAX_REVISITS,
        **overrides: Any,
    ) -> interpreter.Run:
        """Drive the bound process to a terminal state, working inside every state entered.

        Named `work` rather than `run` because `run` already means two different things in
        this call chain — `MethodThread.run` is one cycle of one capability and
        `interpreter.run` is a whole process — and a third would be the worst of the three.

        Args:
            facts: The case, as prose. Reaches `choose`'s prompt and nothing else; handlers
                get theirs from `arguments_for` and from whatever the subclass put on
                `self`.
            start: Initial variable assignment, forwarded to `interpreter.run`.
            max_steps: Cap on executed transitions, forwarded.
            max_rejections: Illegal proposals tolerated per decision, forwarded.
            max_revisits: Consecutive no-progress revisits tolerated before the run
                halts with `NoProgress`, forwarded. `None` disables the halt.
            overrides: `ThreadConfig` overrides applied to *every* compilation this run
                makes — the decider's and each handler's. `model=` is the one that matters:
                it makes a whole process, decisions and work together, scriptable offline
                through one keyword, which is the seam the case study's
                `_scripted_choose` monkeypatch had to work around.

        Returns:
            The `interpreter.Run`, unchanged and unwrapped. Handler results are the
            subclass's to keep via `on_result`; putting them on the `Run` would mean
            editing the fixed interpreter's own data structure to carry this class's
            payload.

        Raises:
            RuntimeError: A state names the decider as its handler. Refused before
                anything is compiled or spent.
            HandlerFailed: A handler or `on_result` raised.
            Deadlock, InvariantViolated, NoProgress, ProcessError: Straight through
                from `interpreter.run`. The verified skeleton's refusals are not this
                class's to soften, and a caller that catches `ProcessError` to count
                blocked cases has to keep seeing exactly what it saw before —
                `NoProgress` IS a `ProcessError`, so that accounting is undisturbed.
        """
        self._check_no_decider_handler()
        # Resolved once rather than per state entry: `state_map` rebuilds the dict on every
        # access (`ir.py:277-279`), and the hook runs at every step.
        states = self.process.state_map

        async def on_enter(state: str) -> None:
            await self.dispatch(states[state], **overrides)

        return await interpreter.run(
            self.process,
            self.decider(facts, **overrides),
            start=start,
            max_steps=max_steps,
            max_rejections=max_rejections,
            max_revisits=max_revisits,
            on_enter=on_enter,
        )

    # ── Guards ──

    def _check_no_decider_handler(self) -> None:
        """Refuse a process whose state names the decider as its per-state handler.

        `choose` is an `@ai_method` on this agent, so the default `handler_for` resolves
        `agent_method="choose"` to it and the state would dispatch the decision-maker as if
        it were work. Both outcomes are bad and neither is legible. With the default
        `arguments_for` the call arrives with no arguments and dies on the signature bind —
        a `TypeError` about `choose` from a state that never mentioned choosing. With an
        `arguments_for` that happens to supply `state`/`options`/`facts` it is worse: a real
        model call, spent, returning a transition name that `on_result` receives and the
        interpreter never sees, at a state that then also gets a genuine decision. The run
        completes and the wasted turn is invisible.

        Checked once at `work()` entry, before anything is compiled or spent, which is the
        `gated._check_no_collision` and `Recall.bound` precedent: a mistake whose failure is
        silent or misattributed becomes a wiring-time refusal that names the fix.

        Only the *declared* `agent_method` is scanned, and the narrowness is deliberate. An
        override of `handler_for` that returns the decider's name is not reachable from
        here — the override is arbitrary code — and the boundary matches the one
        `arguments_for` draws: this class refuses what it can see at wiring time and lets
        runtime binding fail loudly on its own.
        """
        offenders = [
            state.name for state in self.process.states if state.agent_method == self.CHOOSE
        ]
        if offenders:
            raise RuntimeError(
                f"{_owner_name(self)}: state(s) {offenders!r} name {self.CHOOSE!r} as their "
                f"agent_method, which is this agent's decider and not a unit of per-state work. "
                f"Dispatching it would either die on the signature bind or spend a turn "
                f"producing a transition name the interpreter never sees — at a state that "
                f"then gets a real decision anyway. Rename the state's agent_method, or "
                f"override handler_for to return None for it."
            )

    # ── Internals ──

    def _resolve(self, state: State) -> tuple[str, dict[str, Any]] | None:
        """`handler_for`, fault-wrapped, because it and `arguments_for` are overrides too.

        The lesson `gated.py` learned the hard way: the gate was wrapped and the hook added
        beside it was not, so a typoed override surfaced as a raw `AttributeError` three
        frames from the state that caused it. Resolution runs before the model call, so
        there is nothing to burn — the wrap buys the state's name on the traceback, which is
        the only thing that makes a mapping bug findable in a run of a dozen states.
        """
        try:
            return self.handler_for(state)
        except Exception as error:
            raise HandlerFailed(
                f"{_owner_name(self)}: resolving a handler for state {state.name!r} failed, "
                f"which is a fault in handler_for or arguments_for rather than anything the "
                f"process did: {type(error).__name__}: {error}"
            ) from error

    def _fault_text(
        self, state: State, method: str, error: Exception, *, part: str = "handler"
    ) -> str:
        """One wording for "the work inside a state broke", naming state, method, and part."""
        return (
            f"{_owner_name(self)}: the {part} {method!r} for state {state.name!r} raised, so the "
            f"run stopped there rather than reporting a case whose work did not happen: "
            f"{type(error).__name__}: {error}"
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {_owner_name(self)} over {self.process.name!r}>"
