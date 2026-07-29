"""The incident cast rewritten in the decorator paradigm.

`cast.py` gives every agent the same `(request: str)` door. Here each capability
declares its own typed parameters, so the lead calling an analyst is a typed
call — `analyze(window=..., focus="latency", max_records=8)` — and the schema the
model chooses from carries the enum, the defaults, and which fields are required.

The other half of the paradigm is that composition is just Python typing:
`AIFunction` is a `ToolProvider`, so `Analyst.compiled("analyze")` is a tool the
lead can hold. No message bus, no thread ids, no `send_message` — one agent calls
another the way one function calls another.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from . import incident
from .method import MethodAgent, ai_method

Plane = Literal["deploys", "metrics", "logs", "traces"]
Focus = Literal["latency", "errors", "saturation", "ordering"]


class PlaneFinding(BaseModel):
    """What one plane supports on its own."""

    plane: str
    onset: str = Field(description="Earliest ISO-8601 timestamp showing trouble")
    observations: list[str] = Field(description="Evidence, each citing a record id")
    suspects: list[str] = Field(description="Mechanisms this plane cannot rule out")
    cannot_determine: str = Field(description="What this plane alone cannot establish")


class OrderingCheck(BaseModel):
    """Whether a proposed cause actually precedes its effect."""

    holds: bool = Field(description="False when the cause postdates the effect")
    explanation: str


class Analyst(MethodAgent):
    """Holds one telemetry plane and answers typed questions about it."""

    def __init__(self, plane: Plane) -> None:
        self.plane = plane
        self.evidence = incident.render_plane(plane)
        self.name = f"{plane}-analyst"

    @ai_method(
        PlaneFinding,
        description="Analyze this analyst's own telemetry plane over a time window",
        max_attempts=3,
    )
    def analyze(
        self,
        window: str,
        focus: Focus = "errors",
        max_records: int = 12,
    ) -> PlaneFinding:
        """Analyze the {self.plane} plane over {window}, focusing on {focus}.

        You can see the {self.plane} plane and nothing else. Other analysts hold
        the other planes and you cannot read their data.

        Your private evidence:
        {self.evidence}

        Allowed mechanism vocabulary: {", ".join(incident.MECHANISMS)}

        Cite record ids in every observation, at most {max_records} of them. In
        `suspects`, list every mechanism your plane alone leaves open. Report only
        what your own evidence supports: an honest list of surviving candidates is
        worth more than a confident wrong answer.
        """

    @ai_method(
        OrderingCheck,
        description="Check on this plane whether a suspected cause precedes a symptom",
    )
    def check_ordering(self, cause_at: str, symptom_at: str) -> OrderingCheck:
        """Decide whether a cause at {cause_at} can explain a symptom at {symptom_at}.

        Your private {self.plane} evidence:
        {self.evidence}

        Do arithmetic on the timestamps, nothing more. A cause that postdates its
        effect is not a cause, so set `holds` to false when {cause_at} is later
        than {symptom_at}.
        """
