"""The incident cast rewritten in the decorator paradigm.

`cast.py` gives every agent the same `(request: str)` door. Here each capability
declares its own typed parameters, so the lead calling an analyst is a typed
call — `analyze(window=..., focus="latency", max_records=8)` — and the schema the
model chooses from carries the enum, the defaults, and which fields are required.

The other half of the paradigm is that composition is just Python typing:
`AIFunction` is a `ToolProvider`, so `Analyst.compiled("analyze")` is a tool the
lead can hold. No message bus, no thread ids, no `send_message` — one agent calls
another the way one function calls another.

`Quant` covers the third case, which the rest of this project ignored: an AI
function whose body is *executed Python* rather than generated prose. With
`code_execution_mode="local"` the agent writes code, runs it in the sandbox, and
returns a typed result. Its `toolbox` parameter is annotated `Procedural`, so the
helpers it accumulates are defined in that sandbox on the next call and are a
learnable parameter a `TextGradOptimizer` step can rewrite — reusable code, not a
reusable prompt.
"""

from __future__ import annotations

from typing import Literal

from ai_functions import Procedural
from ai_functions.ai_thread.config import CodeExecutionMode
from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from . import incident

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


class Burst(BaseModel):
    """Arithmetic on one plane's numbers, computed rather than estimated."""

    peak_value: float = Field(description="Largest value observed in the window")
    peak_at: str = Field(description="ISO-8601 timestamp of the peak")
    baseline: float = Field(description="Median outside the burst")
    multiple: float = Field(description="peak_value / baseline")


class Toolbox(BaseModel):
    """Reusable helpers the quant accumulates across runs."""

    helpers: Procedural = Field(
        default=(
            "def parse_rows(text):\n"
            '    """Split a rendered telemetry plane into pipe-delimited fields."""\n'
            "    return [line.split('|') for line in text.splitlines() if '|' in line]\n"
        ),
        description="Python helpers available in the quant's execution environment.",
    )


class Quant(MethodAgent):
    """Answers numeric questions by writing and running Python, not by estimating.

    An LLM asked for a median over eighty rows will approximate one. This agent
    computes it: `code_execution_mode="local"` gives it a sandbox, and the
    `toolbox` parameter carries helpers it has already written, which the runtime
    defines in that sandbox and advertises by signature and docstring.
    """

    name = "quant"

    @ai_method(
        Burst,
        description="Compute peak, baseline, and multiple for one metric on one plane",
        code_execution_mode=CodeExecutionMode.LOCAL,
        code_executor_additional_imports=["statistics"],
        max_attempts=3,
    )
    def quantify(self, metric: str, rows: str, toolbox: Procedural) -> Burst:
        """Compute the burst profile of `{metric}`.

        Do the arithmetic in Python rather than reading values off by eye: take the
        baseline as the median outside the burst, and return the result by calling
        `final_answer` with a `Burst`. Prefer helpers that already exist over
        writing new logic.
        """

    def plane_text(self, plane: Plane) -> str:
        """The pipe-delimited text of one plane, to pass as `rows`."""
        return incident.render_plane(plane)
