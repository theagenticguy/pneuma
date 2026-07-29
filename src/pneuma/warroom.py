"""The war room: a deterministic orchestrator that runs as a thread itself.

`WarRoom` implements the library's `Spawnable` protocol, which asks for exactly
two members — `to_thread()` and `input_shape`. Satisfying it means a plain-Python
workflow with no LLM in its control flow gets the same runtime treatment as an
AI agent: a handle, a lifecycle, its own event log, and token rollup from every
child it spawns. The fan-out order and the barrier are ordinary `asyncio`, so
they are reproducible in a way a prompt-driven orchestrator is not.

The thread the model cannot see is the point of the demo. Four specialists hold
disjoint evidence and can only reach each other through the runtime's
`send_message`; the lead holds nothing and has to both interrogate them and hire
its own helpers. Whether that converges on the planted root cause is checked by
a post-condition against `incident.verify`, so the run either satisfies the
oracle or fails loudly.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Self

from ai_functions.runtime.usage import last_event_id, subtree_usage
from ai_functions.types import CustomEvent, InputShape, ThreadContext
from pydantic import BaseModel

from . import incident
from .cast import IncidentLead, Specialist, Verdict
from .staffing import Staff, staffing_tools

PLANES = ("deploys", "metrics", "logs", "traces")


class Investigation(BaseModel):
    """Everything one war-room run produced."""

    verdict: Verdict
    correct: bool
    oracle_failures: list[str]
    findings: dict[str, str]
    staffing_log: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float


@dataclass
class WarRoom:
    """A `Spawnable` that stands up the team, runs the incident, and grades itself."""

    question: str
    planes: tuple[str, ...] = PLANES
    max_hires: int = 3
    name: str = "war-room"
    input_shape: InputShape = InputShape.STR_PROMPT
    staff: Staff = field(default_factory=Staff)

    def to_thread(self) -> Self:
        return self

    async def execute(self, ctx: ThreadContext, request: str) -> Investigation:
        started = time.monotonic()
        baseline = await last_event_id(ctx.coordinator, ctx.thread_id)

        specialists = [Specialist(plane) for plane in self.planes]
        for s in specialists:
            await s.spawn(ctx.coordinator, parent_id=ctx.thread_id)

        ctx.on_event(
            CustomEvent(
                kind="warroom.team_ready",
                payload={"specialists": [s.name for s in specialists]},
            )
        )

        # Phase 1: every specialist reads its own plane, in parallel. A barrier here
        # is deliberate — the lead should not start interrogating a half-formed team.
        opening = (
            "Incident: latency and error rates degraded across the fleet. Report what your "
            "plane alone shows, including every mechanism it cannot rule out."
        )
        findings = await asyncio.gather(
            *(s.ask(opening) for s in specialists), return_exceptions=True
        )
        rendered: dict[str, str] = {}
        for s, f in zip(specialists, findings, strict=True):
            rendered[s.plane] = f"error: {f!r}" if isinstance(f, BaseException) else str(f)

        ctx.on_event(CustomEvent(kind="warroom.findings_in", payload={"count": len(rendered)}))

        # Phase 2: the lead works the room. It holds no evidence, so it must use
        # send_message to interrogate peers and hire to build whatever it lacks.
        hook = staffing_tools(self.staff, max_hires=self.max_hires)
        lead = IncidentLead(specialists=list(self.planes))
        lead_fn = lead.build().replace(
            config_hook=hook,
            post_conditions=[_oracle],
        )
        lead_handle = await ctx.coordinator.spawn(
            lead_fn, thread_name=lead.name, parent_id=ctx.thread_id
        )

        try:
            verdict: Verdict = await lead_handle.run(f"{self.question}\n\n{request}".strip())
        finally:
            await asyncio.gather(
                *(s.retire() for s in specialists),
                *(sub.retire() for sub in list(self.staff.hires.values())),
                lead_handle.terminate_now(),
                return_exceptions=True,
            )

        failures = incident.verify(
            verdict.culprit_service, verdict.culprit_change_id, verdict.mechanism
        )
        usage, turns = await subtree_usage(ctx.coordinator, ctx.thread_id, since_id=baseline)

        return Investigation(
            verdict=verdict,
            correct=not failures,
            oracle_failures=failures,
            findings=rendered,
            staffing_log=self.staff.log,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            turns=turns,
            wall_seconds=round(time.monotonic() - started, 1),
        )

    # ── Remaining Thread protocol surface ──

    async def notify(self, text: str) -> None:
        del text

    async def fork(self) -> Self:
        raise NotImplementedError("a war room is not forkable")

    async def teardown(self) -> None:
        await asyncio.gather(
            *(sub.retire() for sub in list(self.staff.hires.values())), return_exceptions=True
        )

    def serialize_result(self, result: Investigation) -> str:
        return result.model_dump_json(indent=2)

    def deserialize_result(self, payload: str) -> Investigation:
        return Investigation.model_validate_json(payload)


def _oracle(response: Verdict) -> None:
    """Post-condition: the lead's verdict must satisfy the planted ground truth.

    Failures come back as assertion text, which the library feeds to the model as
    a new user turn so it can revise rather than restart. The message names the
    shortfall without leaking the answer.
    """
    if response.mechanism not in incident.MECHANISMS:
        raise AssertionError(
            f"mechanism {response.mechanism!r} is not in the allowed vocabulary: "
            f"{', '.join(incident.MECHANISMS)}"
        )
    if len(response.evidence_by_plane) < 3:
        raise AssertionError(
            f"evidence_by_plane cites {len(response.evidence_by_plane)} plane(s); no single "
            "plane can identify this cause, so a verdict needs corroboration from at least 3"
        )
    if not response.ruled_out:
        raise AssertionError(
            "ruled_out is empty; at least two innocent changes look guilty here, so name "
            "the ones you dismissed and why"
        )
    problems = incident.verify(
        response.culprit_service, response.culprit_change_id, response.mechanism
    )
    if problems:
        raise AssertionError(
            "the verdict does not match the evidence: "
            + "; ".join(problems)
            + ". Re-interrogate the specialists whose planes you leaned on least."
        )
