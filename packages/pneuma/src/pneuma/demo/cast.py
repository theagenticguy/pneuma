"""The agent cast: telemetry specialists, a hireable pool, and the incident lead.

Every class here is one `Agent` subclass, and every instance of a subclass is a
separate live thread. `Specialist` is the payoff of the object-oriented shape:
one class, four instances, each holding a different private telemetry plane in
`self.plane`. The decorator-first equivalent would be four near-identical
functions differing only in which constant their docstring interpolates.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field
from strands.tools.decorator import tool as strands_tool

from ..model import Effort
from . import incident
from .agent import Agent

# ── Result types ──


class Finding(BaseModel):
    """What one specialist concludes from its own plane, alone."""

    plane: str
    onset: str = Field(description="Earliest ISO-8601 timestamp where this plane shows trouble")
    observations: list[str] = Field(description="Concrete evidence, each citing a record id")
    suspects: list[str] = Field(description="Candidate mechanisms this plane cannot rule out")
    cannot_determine: str = Field(description="What this plane alone cannot establish")


class Verdict(BaseModel):
    """The lead's final call on the incident."""

    culprit_service: str
    culprit_change_id: str
    mechanism: str
    causal_chain: list[str] = Field(description="Ordered cause-to-symptom steps")
    ruled_out: list[str] = Field(description="Decoys considered and why each was dismissed")
    evidence_by_plane: dict[str, str] = Field(
        description="Plane name to the record ids that mattered"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class Critique(BaseModel):
    """An adversarial read of a draft verdict."""

    holds: bool = Field(description="Whether the verdict survives scrutiny")
    unsupported_claims: list[str]
    missing_evidence: list[str]
    alternative_worth_checking: str


class Timeline(BaseModel):
    """A causal ordering of events across planes."""

    ordered_events: list[str] = Field(description="Each entry: timestamp, plane, what happened")
    causal_breaks: list[str] = Field(
        description="Places where a suspected cause postdates its effect"
    )


# ── Telemetry specialists: one class, four private views ──


class Specialist(Agent):
    """Reads exactly one telemetry plane and reports what that plane alone supports."""

    role: ClassVar[str] = "specialist"
    purpose: ClassVar[str] = "Analyzes one telemetry plane in isolation"
    result_type: ClassVar[type] = Finding
    hireable: ClassVar[bool] = False
    effort: ClassVar[Effort] = "xhigh"

    def __init__(self, plane: str, *, effort: Effort | None = None) -> None:
        super().__init__(name=f"{plane}-analyst")
        self.plane = plane
        self.evidence = incident.render_plane(plane)
        if effort is not None:
            self.effort = effort

    def system_prompt(self) -> str:
        return (
            f"You are the {self.plane} specialist on an incident call. You can see the "
            f"{self.plane} plane and nothing else. Other specialists hold the other planes "
            "and you cannot read their data. Report only what your own evidence supports, "
            "and name explicitly what it cannot settle. Do not guess to sound decisive: "
            "an honest list of surviving candidates is more useful to the incident lead "
            "than a confident wrong answer."
        )

    @strands_tool
    def search_plane(self, substring: str) -> str:
        """Search your own telemetry plane for records containing a substring.

        Case-insensitive. Use it to check a specific service, change id, or
        timestamp without re-reading the whole plane.
        """
        needle = substring.strip().lower()
        hits = [line for line in self.evidence.splitlines() if needle in line.lower()]
        if not hits:
            return f"no {self.plane} record contains {substring!r}"
        return "\n".join(hits[:40])

    def brief(self, request: str) -> str:
        return (
            f"{request}\n\n"
            f"Your private {self.plane} evidence:\n"
            f"{self.evidence}\n\n"
            f"Allowed mechanism vocabulary: {', '.join(incident.MECHANISMS)}\n\n"
            "Cite record ids in every observation. In `suspects`, list every mechanism your "
            "plane alone leaves open. In `cannot_determine`, say what you would need from "
            "another plane."
        )


# ── The hireable pool: roles the lead can staff on demand ──


class Historian(Agent):
    """Reconstructs the causal order of events and finds ordering contradictions."""

    role: ClassVar[str] = "historian"
    purpose: ClassVar[str] = (
        "Builds a cross-plane timeline and flags effects that precede their supposed cause"
    )
    result_type: ClassVar[type] = Timeline
    effort: ClassVar[Effort] = "high"

    mandate: str = ""

    def system_prompt(self) -> str:
        return (
            "You order events in time and nothing else. You do not name a root cause. "
            "Your value is arithmetic on timestamps: if a proposed cause happens after the "
            "symptom it supposedly caused, say so plainly. That single check eliminates "
            "more wrong theories than any amount of reasoning about mechanisms."
        )

    def brief(self, request: str) -> str:
        return (
            f"Your mandate: {self.mandate}\n\n{request}\n\n"
            "Return events in strict chronological order. In `causal_breaks`, list every "
            "pair where a suspected cause postdates its claimed effect."
        )


class Skeptic(Agent):
    """Attacks a draft verdict and tries to find the claim that does not hold."""

    role: ClassVar[str] = "skeptic"
    purpose: ClassVar[str] = "Adversarially reviews a draft verdict for unsupported claims"
    result_type: ClassVar[type] = Critique
    effort: ClassVar[Effort] = "xhigh"

    mandate: str = ""

    def system_prompt(self) -> str:
        return (
            "You are the dissent. Assume the draft verdict is wrong and look for the claim "
            "that carries no evidence. Distinguish two failures: a claim with no supporting "
            "record, and a claim whose evidence is consistent with a different mechanism. "
            "If after real effort the verdict holds, say it holds. Manufactured doubt is as "
            "useless as manufactured confidence."
        )

    def brief(self, request: str) -> str:
        return (
            f"Your mandate: {self.mandate}\n\n{request}\n\n"
            f"Allowed mechanism vocabulary: {', '.join(incident.MECHANISMS)}\n\n"
            "Set `holds` to false only if you found a specific defect you can name."
        )


class Correlator(Agent):
    """Intersects findings from several planes to narrow the candidate set."""

    role: ClassVar[str] = "correlator"
    purpose: ClassVar[str] = (
        "Intersects multi-plane findings to eliminate candidates no single plane can"
    )
    result_type: ClassVar[type] = str
    effort: ClassVar[Effort] = "high"

    mandate: str = ""

    def system_prompt(self) -> str:
        return (
            "You perform set intersection on evidence. Given candidate mechanisms from "
            "several planes, report which survive in all of them and which each plane "
            "eliminates. Show the elimination step for each candidate you drop."
        )

    def brief(self, request: str) -> str:
        return f"Your mandate: {self.mandate}\n\n{request}"


# ── The lead: hires, delegates, and answers ──


class IncidentLead(Agent):
    """Owns the incident. Holds no telemetry; must obtain everything from others."""

    role: ClassVar[str] = "lead"
    purpose: ClassVar[str] = "Runs the incident and issues the final verdict"
    result_type: ClassVar[type] = Verdict
    hireable: ClassVar[bool] = False
    effort: ClassVar[Effort] = "xhigh"
    max_attempts: ClassVar[int] = 4

    def __init__(self, *, specialists: list[str], tool_list: list[Any] | None = None) -> None:
        super().__init__(name="incident-lead")
        self.specialists = specialists
        self._tools = tool_list or []

    def tools(self) -> list[Any]:
        return [*super().tools(), *self._tools]

    def system_prompt(self) -> str:
        peers = ", ".join(f"{p}-analyst" for p in self.specialists)
        return (
            "You are the incident lead. You hold no telemetry of your own: every fact in "
            "your verdict has to come from someone else.\n\n"
            f"Four specialists are already running as peer threads: {peers}. Each sees one "
            "plane and no other. Reach them with `list_threads` to get their thread ids, then "
            "`send_message` with a specific question. Vague questions get vague answers, so "
            "say which record ids or time window you care about.\n\n"
            "You can also build your own team. `hire(role, name, mandate)` creates a subagent "
            "that reports to you; `delegate(name, request)` gives it work and returns its "
            "answer; `dismiss(name)` ends it. Hire when a distinct skill would help, not by "
            "reflex, and give each hire a mandate narrow enough that its answer is checkable.\n\n"
            "The evidence is built so that no single plane identifies the cause: each plane "
            "alone is consistent with at least two mechanisms, and at least two innocent "
            "changes look guilty from one angle. Convergence across three or more planes is "
            "the only thing that settles it. Establish the timeline before naming a mechanism: "
            "a cause that postdates its effect is not a cause."
        )

    def brief(self, request: str) -> str:
        return (
            f"{request}\n\n"
            f"Allowed mechanism vocabulary (use one verbatim): {', '.join(incident.MECHANISMS)}\n\n"
            "Deliver a verdict with the causal chain in order, the decoys you dismissed and "
            "why, and per-plane record ids. Confidence should reflect how many independent "
            "planes agree, not how fluent your explanation reads."
        )
