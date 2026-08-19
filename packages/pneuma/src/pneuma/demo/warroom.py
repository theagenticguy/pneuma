"""The war room: one incident, run on the library's hooks-first `Team`.

`team.core.Team` owns the mechanics — spawn the specialists, run the lead with them as typed
tools, retire everybody — and the hooks own the phases: `Briefing` fans the opening out
behind a barrier and folds what came back into the lead's own prompt. What is left here is
the part that *is* about incidents: four telemetry planes, an `IncidentLead` that holds
nothing, the roles it may hire, and `incident.verify` as the standard.

Two demo-specific compositions worth naming. The *hiring* rides the lead's own
`config_hook` — `staffing_tools` over the demo's `Staff` roster — rather than the library's
`Hiring` hook, because the demo restores each role's `purpose` into the hire tool's
description and lands each mandate on the hired agent, both of which are `Staff`'s business;
the core recomposes a lead's own hook into the one hook it installs, so nothing is lost. The
*standard* rides the lead's own `post_conditions` — the runtime turns a refused verdict into
re-ask feedback the next attempt reads — so the library's team stays free of any grading
vocabulary.

The thread the model cannot see is still the point of the demo: the specialists are
`STR_PROMPT` `Agent`s, so they remain reachable through the runtime's `send_message` as chat
peers, exactly as before — and they join the lead as typed tools too, which is the library's
own path.
"""

from __future__ import annotations

import time
from typing import Any

from ai_functions.runtime.usage import subtree_usage
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ..team import Team, Workspace
from ..team.hooks import Briefing
from . import incident
from .cast import IncidentLead, Specialist, Verdict
from .staffing import Staff, staffing_tools

PLANES = ("deploys", "metrics", "logs", "traces")

OPENING = (
    "Incident: latency and error rates degraded across the fleet. Report what your "
    "plane alone shows, including every mechanism it cannot rule out."
)


class Investigation(BaseModel):
    """Everything one war-room run produced, under the demo's own names.

    The field set the demo already published: an `investigation.json` on disk and
    `docs/build_pdf.py:68-69` read `findings` and `staffing_log`, so serialisation writes
    those aliases, validation accepts either, and the properties keep `result.findings`
    working for `cli.py`. `verdict` is typed, so the artifact round-trips equal.
    """

    model_config = ConfigDict(serialize_by_alias=True)

    verdict: Verdict
    correct: bool
    oracle_failures: list[str]
    briefings: dict[str, str] = Field(
        serialization_alias="findings",
        validation_alias=AliasChoices("briefings", "findings"),
    )
    hiring_log: list[dict[str, Any]] = Field(
        serialization_alias="staffing_log",
        validation_alias=AliasChoices("hiring_log", "staffing_log"),
    )
    input_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float

    @property
    def findings(self) -> dict[str, str]:
        """What each plane reported, keyed by plane."""
        return self.briefings

    @property
    def staffing_log(self) -> list[dict[str, Any]]:
        """Every hire, delegation and dismissal the lead drove, in order."""
        return self.hiring_log


class _LeadWatch:
    """A hook that captures the lead's live thread id, for the usage rollup.

    The lead's thread is the root of the whole run — members spawn as its children, and
    hires as its children too (`parent_id=ctx.thread_id` in the hiring tools) — so
    `subtree_usage` walking from it reaches every model call the run made. The thread is
    fresh per run (never seeded), so its log is counted whole with no baseline.
    """

    def __init__(self) -> None:
        self.thread_id: Any = None

    def on_assemble(self, work: Workspace) -> None:
        self.thread_id = work.lead.id


class WarRoom:
    """The incident, composed onto the library's team: a cast, a briefing, a lead, a standard.

    Args:
        question: What the room investigates — the room's own, not the caller's, which is
            why `investigate` takes no request.
        max_hires: The lead's headcount budget for `staffing_tools`.
        planes: One specialist per entry, each holding that plane's private evidence.
    """

    name = "war-room"

    def __init__(
        self,
        *,
        question: str,
        max_hires: int = 3,
        planes: tuple[str, ...] = PLANES,
    ) -> None:
        self.question = question
        self.max_hires = max_hires
        self.planes = planes
        self.staff = Staff()
        """The roster the run used, replaced per `investigate` — read it after a run."""

    # ── The demo's own judgment ──

    def oracle(self, response: Verdict) -> None:
        """Post-condition on the lead's verdict: refuse until it satisfies the ground truth.

        Failures come back as assertion text, which the runtime feeds to the model as a new
        user turn so it can revise rather than restart. The message names the shortfall
        without leaking the answer.
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

    def grade(self, verdict: Verdict) -> tuple[bool, list[str]]:
        """`incident.verify` a second time, for the reader rather than for the model."""
        failures = incident.verify(
            verdict.culprit_service, verdict.culprit_change_id, verdict.mechanism
        )
        return not failures, failures

    # ── The run ──

    async def investigate(self, coordinator: Any) -> Investigation:
        """One full investigation: brief the planes, run the gated lead, grade, report.

        The staff roster is per run, replaced here — every promise attached to it (the
        headcount cap, the name reservation, the log the report publishes) is a promise
        about one run.
        """
        started = time.monotonic()
        self.staff = type(self.staff)()

        specialists = [Specialist(plane) for plane in self.planes]
        lead = IncidentLead(specialists=list(self.planes)).build()
        lead = lead.replace(
            config_hook=staffing_tools(self.staff, max_hires=self.max_hires),
            post_conditions=[self.oracle, *lead.config.post_conditions],
        )

        watch = _LeadWatch()
        briefing = Briefing(lambda member: OPENING, forward_request=False)
        team = Team(lead, specialists, hooks=[watch, briefing])

        run = await team.run(self.question, coordinator)

        verdict: Verdict = run.answer
        correct, failures = self.grade(verdict)
        usage, turns = await subtree_usage(coordinator, watch.thread_id)

        # Keyed by plane, because `findings` is a published field and a reader of
        # `investigation.json` knows `deploys`, not `deploys-analyst`. One specialist per
        # plane, in `self.planes` order — `strict` checks the assumption that makes the
        # re-key sound.
        raw = run.hooks_data.get("briefing", {})
        briefings = {
            plane: raw[member.name] for plane, member in zip(self.planes, specialists, strict=True)
        }

        return Investigation(
            verdict=verdict,
            correct=correct,
            oracle_failures=failures,
            briefings=briefings,
            hiring_log=self.staff.log,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            turns=turns,
            wall_seconds=round(time.monotonic() - started, 1),
        )
