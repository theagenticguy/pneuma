"""Per-state agents: the work inside each step, not just the choice between steps.

`State.agent_method` was declared from the start and never read. The interpreter
routed *between* states and nothing did the work *inside* one, so the whole pipeline
proved an agent could not walk off the map while leaving the map's contents empty.
This closes it, and the closing is now literal: `Caseworker` is a `ProcessAgent`, so
the object that does the work inside a state is the same object that walks between
them. `dispatch` used to be a bridge with nothing driving across it — no caller
anywhere ran it inside `interpreter.run` — and it is now the inherited hook the walk
itself calls at every state it enters.

`Caseworker` gives the municipality's real activities real methods. Each is an
`@ai_method` with its own typed result, so the schema the model must satisfy differs
per step: checking a confirmation returns findings and a verdict, drafting advice
returns text and a recommendation. That is the decorator paradigm doing the thing it
is for — one typed function per capability, not one chat box for everything.

`HANDLERS` is the one declaration behind all of it: `handler_for` reads it to resolve a
state, `Caseworker.handler_for` is the same lookup on the instance, and `coverage`
counts it to report how much of a mined model is actually wired. A state with no entry
is a control point and costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..method import ai_method
from ..process.agent import ProcessAgent
from ..process.ir import Process, State

Verdict = Literal["complete", "incomplete", "needs_clarification"]


class CheckResult(BaseModel):
    """What checking a submitted confirmation produced."""

    verdict: Verdict = Field(description="Whether the submission can proceed as filed")
    findings: list[str] = Field(description="Specific observations, one per issue or confirmation")
    missing: list[str] = Field(default_factory=list, description="Documents or fields absent")


class Determination(BaseModel):
    """A decision recorded against a case."""

    proceed: bool = Field(description="Whether the case advances on the current evidence")
    rationale: str = Field(description="One or two sentences a citizen could be shown")
    cites: list[str] = Field(description="What this determination relied on")


class AdviceDraft(BaseModel):
    """Internal advice drafted for a case."""

    body: str = Field(description="The advice text")
    recommendation: Literal["grant", "refuse", "request_more"] = Field(
        description="What the adviser recommends"
    )


@dataclass
class CaseFile:
    """State the handlers accumulate as a case progresses.

    Deliberately plain data. It is *not* the process state — the interpreter owns
    that and the verifier checks it. This is the paperwork.
    """

    reference: str
    facts: str
    findings: list[str] = field(default_factory=list)
    determinations: list[str] = field(default_factory=list)
    advice: str = ""

    def summary(self) -> str:
        parts = [f"Case {self.reference}: {self.facts}"]
        if self.findings:
            parts.append("Findings so far: " + "; ".join(self.findings))
        if self.determinations:
            parts.append("Determinations so far: " + "; ".join(self.determinations))
        if self.advice:
            parts.append(f"Internal advice: {self.advice}")
        return "\n".join(parts)


class Caseworker(ProcessAgent):
    """The municipality's activities, one typed AI function each — and the walker of them.

    Instance state is the case file, so two caseworkers handling two applications are
    two agents with private paperwork built from one class.

    Being a `ProcessAgent` is what lets one object both choose the next step and do the
    work inside it. The three hooks below are the whole of the wiring: `handler_for` says
    which activity a state means, `on_result` files what the activity produced, and
    `work()` is inherited — the walk that finally calls them, which nothing did before.
    """

    def __init__(
        self, case: CaseFile, *, process: Process | None = None, context: str = ""
    ) -> None:
        """The case is required; the process is not.

        A caseworker is useful without one. Its three activities are typed AI functions
        over the case file and nothing else — `check_confirmation` reads
        `self.case.summary()`, not a state — so compiling one, calling it, or handing it to
        a peer as a tool needs no process at all, which is how
        `tests/app/test_casestudy.py:370-373,384,416` uses it and how `dispatch` is called
        on a hand-built `State`. Requiring a process to construct one of those would
        demand a whole verified model as a parameter to a call that never looks at it.

        So the process is the walking equipment, taken keyword-only to keep it clearly
        distinct from the base's positional `process` — this constructor's first argument
        is the case, and a reader who sees `Caseworker(case, process=permits)` cannot
        confuse the two. Absent, every consumer of it fails through the `process` property
        below rather than each finding its own way to be confusing.
        """
        # The base builds a default name from `process.name`, so it cannot be handed the
        # None this constructor allows. Calling it whenever there *is* a process keeps the
        # base as the one place that knows the wiring; the other path assigns the same two
        # fields it would have.
        if process is None:
            self.process = None
            self.context = context
        else:
            super().__init__(process, context=context)
        # After `super()`, which sets `{process}-agent`. The published name is the case
        # reference: the paperwork is what distinguishes two caseworkers, not the model
        # they share, and it is also the compiled tool's prefix and every error message's
        # subject (`method._owner_name`).
        self.case = case
        self.name = f"caseworker-{case.reference}"

    @property
    def process(self) -> Process:
        """The process this caseworker walks, or a refusal naming what to pass.

        A property because the absence has exactly one honest response and several places
        would otherwise each invent their own. `work()` reads `state_map`, `decider` reads
        `name`, `choose`'s prompt template interpolates both — and an unset plain attribute
        would reach them as `AttributeError: 'Caseworker' object has no attribute
        'process'`, which reads like a bug in this class rather than a missing argument at
        the call site. One gate, one message, and it covers whatever needs the process
        next.

        The refusal is an `AttributeError` on purpose. `hasattr`, `getattr(..., default)`,
        and `inspect.getmembers` suppress only that type, so a capability probe or a
        debugger's variable pane gets its answer (no process) instead of this message in
        place of whatever it was actually reporting — the same trap `__repr__` below
        refuses to set. Direct calls still fail loudly, with the fix named.
        """
        if self._process is None:
            raise AttributeError(
                f"{self.name}: this caseworker was built without a process, so it can do its "
                f"activities but cannot walk one. Pass Caseworker(case, process=...) to use "
                f"work(), decider(), or choose()."
            )
        return self._process

    @process.setter
    def process(self, process: Process | None) -> None:
        """Accepts the None the base's `__init__` assigns, so the gate is the getter's."""
        self._process = process

    @classmethod
    def ai_methods(cls) -> list[str]:
        """This caseworker's activities, without the inherited decider.

        `ai_methods` answers "what capabilities does this agent publish" — it is what
        `agents()` turns into typed tools for a peer to call — and `choose` is not one of
        them. It is the interpreter's private adapter, compiled by `decider()` through
        `getattr` and never offered to anybody, and listing it would advertise "pick a
        transition in this process" as a service this desk provides.

        `tests/app/test_casestudy.py:371` pins the published set as the three activities,
        and it was written before this class had a decider to inherit. Filtering rather
        than restating the three names keeps the decorator the single source of truth: a
        fourth activity appears here by being declared, not by being listed twice.
        """
        return [name for name in super().ai_methods() if name != cls.CHOOSE]

    @ai_method(
        CheckResult,
        description="Check a submitted confirmation of receipt for completeness",
        max_attempts=2,
    )
    def check_confirmation(self, focus: str = "completeness") -> CheckResult:
        """Check the confirmation of receipt on this application, focusing on {focus}.

        {self.case.summary()}

        List what you can actually see in the case facts. Do not assume a document
        exists because it usually would: if the facts do not mention it, it is
        missing. An honest `incomplete` is more useful than an optimistic `complete`.
        """

    @ai_method(
        Determination,
        description="Determine whether the case may proceed on current evidence",
        max_attempts=2,
    )
    def determine(self, question: str) -> Determination:
        """Decide: {question}

        {self.case.summary()}

        Rely only on what the case file records. In `cites`, name the specific finding
        or determination you used. If the file does not support a decision, set
        `proceed` to false and say what is needed.
        """

    @ai_method(AdviceDraft, description="Draft internal advice on a case", max_attempts=2)
    def draft_advice(self, aspect: str) -> AdviceDraft:
        """Draft internal advice on the {aspect} aspect of this application.

        {self.case.summary()}

        Write for a colleague who will act on it. Be specific about what the file
        supports and what it does not.
        """

    # ── The two hooks the inherited walk calls ──

    def handler_for(self, state: State) -> tuple[str, dict[str, Any]] | None:
        """The activity `state` means, resolved from `HANDLERS`.

        The base resolves `agent_method` as a method name, which cannot work here: every
        mined state carries `agent_method='handle'` (`miner.py:125`), a placeholder that
        names no activity on anybody. So `agent_method` is the opt-in gate and the table
        keyed by the log's own activity label is the source of truth — which is the
        override `process/agent.py:175-180` was shaped for, and why the library needs to
        know nothing about tables.

        Delegates to the module-level function rather than reading `HANDLERS` again, so the
        walk and `coverage` cannot disagree about what is wired.
        """
        return handler_for(state)

    def on_result(self, state: State, result: Any) -> None:
        """File what an activity produced on the case file.

        Typed per result, because each activity produces a different kind of paperwork and
        a single `str` column would lose the distinction the typed returns exist to make: a
        check contributes findings, a determination contributes a recorded decision with
        its rationale, advice replaces the standing draft.

        `state` goes unread. The case file is a chronological record, not a per-state one —
        `summary()` renders it as prose for the next activity's prompt — and keying it by
        state would imply an activity happens at most once, which a mined process with a
        rework loop disproves. The parameter stays in the signature because it is the
        base's hook and a subclass that does want the state should not have to change it.
        """
        if isinstance(result, CheckResult):
            self.case.findings.extend(result.findings)
            if result.missing:
                self.case.findings.append("missing: " + ", ".join(result.missing))
        elif isinstance(result, Determination):
            self.case.determinations.append(
                f"{'proceed' if result.proceed else 'hold'} — {result.rationale}"
            )
        elif isinstance(result, AdviceDraft):
            self.case.advice = f"[{result.recommendation}] {result.body}"

    def __repr__(self) -> str:
        """Reads `_process` directly, because a repr that can raise is a trap.

        The base's repr interpolates `self.process.name`, which now goes through the gate
        above — so repring a process-less caseworker raised, and repr is exactly where that
        is worst. It is what a debugger calls on every frame and what pytest calls to
        explain a failed assertion, so the gate's message would arrive *in place of* the
        real failure, hiding it. Describing the absence is strictly more useful than
        refusing to describe the object.
        """
        state = "no process" if self._process is None else repr(self._process.name)
        return f"<Caseworker {self.name} over {state}>"


# Which mined activity maps to which handler. Written out rather than guessed: the
# mined state names come from the log's own activity labels, and a wrong mapping
# would have an agent doing the wrong job while every verifier still passed.
HANDLERS: dict[str, tuple[str, dict[str, Any]]] = {
    "T02CheckConfirmationOfReceipt": ("check_confirmation", {"focus": "completeness"}),
    "T04DetermineConfirmationOfReceipt": (
        "determine",
        {"question": "may the confirmation of receipt be determined as filed?"},
    ),
    "T06DetermineNecessityOfStopAdvice": (
        "determine",
        {"question": "is stop advice necessary for this application?"},
    ),
    "T10DetermineNecessityToStopIndication": (
        "determine",
        {"question": "is there a necessity to stop the indication?"},
    ),
    "T07-1DraftInternAdviceAspect1": ("draft_advice", {"aspect": "planning"}),
}


def handler_for(state: State) -> tuple[str, dict[str, Any]] | None:
    """The method and arguments for `state`, or None if it is a pure control point."""
    if state.agent_method is None:
        return None
    return HANDLERS.get(state.name)


async def dispatch(worker: Caseworker, state: State, **overrides: Any) -> Any | None:
    """Run the handler for `state` on `worker`, recording its output on the case file.

    Returns None when the state has no handler, which is the common case: most mined
    activities are recording steps rather than decisions, and calling a model for
    those would spend money to produce nothing.

    Now one line, and the body it lost is the point of this refactor. Dispatching used to
    live here as a free function no walk ever called, so the resolution and the filing were
    written beside the interpreter's blind spot rather than inside it. Both are
    `Caseworker`'s own hooks now and `work()` calls them at every state it enters; this
    stays as the way to dispatch one state in isolation, which is how a test drives a
    hand-built `State` without a whole process behind it
    (`tests/app/test_casestudy.py:404,417-418`). It also picks up what the method has that
    this never did: a fault in either hook arrives as `HandlerFailed` naming the state.
    """
    return await worker.dispatch(state, **overrides)


def coverage(process: Process) -> tuple[int, int]:
    """How many of the mined states have a real handler wired.

    Reported rather than assumed: a mined model can name 11 activities while only a
    handful have been implemented, and pretending otherwise would overstate the
    demo.
    """
    handled = sum(1 for state in process.states if handler_for(state) is not None)
    return handled, len(process.states)
