"""Per-state agents: the work inside each step, not just the choice between steps.

`State.agent_method` was declared from the start and never read. The interpreter
routed *between* states and nothing did the work *inside* one, so the whole pipeline
proved an agent could not walk off the map while leaving the map's contents empty.
This closes it.

`Caseworker` gives the municipality's real activities real methods. Each is an
`@ai_method` with its own typed result, so the schema the model must satisfy differs
per step: checking a confirmation returns findings and a verdict, drafting advice
returns text and a recommendation. That is the decorator paradigm doing the thing it
is for — one typed function per capability, not one chat box for everything.

`dispatch` is the bridge: it reads `agent_method` off the state the interpreter just
entered and calls the matching method. A state with no handler is a control point and
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
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


class Caseworker(MethodAgent):
    """The municipality's activities, one typed AI function each.

    Instance state is the case file, so two caseworkers handling two applications are
    two agents with private paperwork built from one class.
    """

    def __init__(self, case: CaseFile) -> None:
        self.case = case
        self.name = f"caseworker-{case.reference}"

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
    """Run the handler for `state`, recording its output on the case file.

    Returns None when the state has no handler, which is the common case: most mined
    activities are recording steps rather than decisions, and calling a model for
    those would spend money to produce nothing.
    """
    bound = handler_for(state)
    if bound is None:
        return None

    method, arguments = bound
    result = await worker.compiled(method, **overrides)(**arguments)

    if isinstance(result, CheckResult):
        worker.case.findings.extend(result.findings)
        if result.missing:
            worker.case.findings.append("missing: " + ", ".join(result.missing))
    elif isinstance(result, Determination):
        worker.case.determinations.append(
            f"{'proceed' if result.proceed else 'hold'} — {result.rationale}"
        )
    elif isinstance(result, AdviceDraft):
        worker.case.advice = f"[{result.recommendation}] {result.body}"

    return result


def coverage(process: Process) -> tuple[int, int]:
    """How many of the mined states have a real handler wired.

    Reported rather than assumed: a mined model can name 11 activities while only a
    handful have been implemented, and pretending otherwise would overstate the
    demo.
    """
    handled = sum(1 for state in process.states if handler_for(state) is not None)
    return handled, len(process.states)
