"""The war room: one incident, run on the library's `Team` skeleton.

`Team` owns the shape this file used to spell out — fan out to the cast, hold a barrier, run a
lead against a post-condition oracle with a budgeted hiring seam, roll the subtree's tokens up,
and retire everybody whatever happened. None of that is about incidents, so none of it is here
any more. What is left is the part that *is* about incidents: four telemetry planes, an
`IncidentLead` that holds nothing, the roles it may hire, and `incident.verify` as the standard.

The thread the model cannot see is still the point of the demo. The four specialists hold disjoint
evidence and can only reach each other through the runtime's `send_message`, which is the one
place the demo deliberately parts company with the library: `send_message` refuses any target
whose `input_shape` is not `STR_PROMPT` (`ai_thread/tools.py:172-176`), so a room of chat peers is
a room of agents compiled down to one `str`. `team.py` therefore mentions `send_message` nowhere
and joins its members as typed tools; this file keeps the bus, because a room of peers is the
demo's subject. Subclassing is what makes both true at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_functions import AIFunction
from ai_functions.types import ThreadContext
from pydantic import AliasChoices, ConfigDict, Field

from .._team_legacy import Recruit, Team, TeamRun
from . import incident
from .cast import IncidentLead, Specialist, Verdict
from .staffing import Staff, staffing_tools

PLANES = ("deploys", "metrics", "logs", "traces")

OPENING = (
    "Incident: latency and error rates degraded across the fleet. Report what your "
    "plane alone shows, including every mechanism it cannot rule out."
)


class Investigation(TeamRun):
    """Everything one war-room run produced: a `TeamRun` under the demo's own names.

    Two things this class buys, and both are named in `team.py`. It narrows `verdict` to
    `Verdict`, which is what makes `deserialize_result(serialize_result(run)) == run` true — the
    base's `verdict: Any` validates a serialised `BaseModel` back as a plain `dict`, so the
    protocol's `Ensures` holds for a subclass and not for `TeamRun` itself (`team.py:204-224`).

    And it keeps the field set the demo already published. `TeamRun` calls them `briefings` and
    `hiring_log`; an `investigation.json` on disk and `docs/build_pdf.py:68-69` call them
    `findings` and `staffing_log`. Renaming a field of a shipped artifact to match a base class
    would be the library dictating the application's vocabulary, so the fields keep the library's
    names inside the model and the demo's names everywhere a reader meets them: serialisation
    writes the aliases, validation accepts either, and the properties keep `result.findings`
    working for `cli.py:102-106`. Measured against `artifacts/run3/investigation.json`: same nine
    keys, same order.
    """

    model_config = ConfigDict(serialize_by_alias=True)

    verdict: Verdict
    briefings: dict[str, str] = Field(
        serialization_alias="findings",
        validation_alias=AliasChoices("briefings", "findings"),
    )
    hiring_log: list[dict[str, Any]] = Field(
        serialization_alias="staffing_log",
        validation_alias=AliasChoices("hiring_log", "staffing_log"),
    )

    @property
    def findings(self) -> dict[str, str]:
        """What each plane reported, keyed by plane."""
        return self.briefings

    @property
    def staffing_log(self) -> list[dict[str, Any]]:
        """Every hire, delegation and dismissal the lead drove, in order."""
        return self.hiring_log


@dataclass
class WarRoom(Team):
    """The incident, supplied to the skeleton: a cast, a briefing, a lead, and a standard.

    Six methods and no phases. `question` is keyword-only because `Team`'s fields are all
    defaulted and a required positional cannot follow them — the constructor `cli.py:46` already
    writes is unchanged, and a `WarRoom()` with nothing to investigate is still refused.
    """

    question: str = field(kw_only=True)
    planes: tuple[str, ...] = PLANES
    name: str = "war-room"
    roster: Staff = field(default_factory=Staff)
    """Narrowed from `Roster`: a `Staff` is what lands each hire's mandate on the hire itself."""

    # ── What the skeleton asks for ──

    def members(self) -> Sequence[Recruit]:
        """One specialist per plane, built per run so a test can bind a model after construction."""
        return [Specialist(plane) for plane in self.planes]

    def briefing(self, member: Recruit) -> str:
        """One opening for everybody: each specialist's own evidence is already in its prompt."""
        del member
        return OPENING

    def lead_function(self) -> AIFunction[..., Any]:
        """The lead, carrying the demo's own hiring hook. `Team` attaches only the oracle.

        This is one of the two exits `_gated_lead` names for a lead that arrives with a hook: keep
        the hook and return an empty `catalog()`, or compose the team's hiring into it
        (`team.py:763-769`). The runtime calls exactly one `config_hook` per cycle, so a lead with
        a hook and a team with a catalog is a real conflict the library refuses rather than
        resolves.

        The choice between them is measured, not stylistic. `hiring_tools` renders its catalog as
        role *names*, because a `Mapping[str, Callable]` has nothing else, while the demo's `hire`
        description has always carried each role's `purpose` — and a lead choosing between three
        roles it is told nothing about is choosing on a word. `staffing_tools` restores it, so
        routing the war room through that one binding also means the hook the offline tests
        exercise is the hook a live run uses. The oracle composes the ordinary way regardless:
        `_gated_lead` prepends it, and a lead with a hook and no catalog keeps its hook untouched.
        """
        lead = IncidentLead(specialists=list(self.planes)).build()
        return lead.replace(config_hook=staffing_tools(self.roster, max_hires=self.max_hires))

    def oracle(self, response: Verdict) -> None:
        """Post-condition: the lead's verdict must satisfy the planted ground truth.

        Failures come back as assertion text, which the library feeds to the model as a new user
        turn so it can revise rather than restart. The message names the shortfall without leaking
        the answer.
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

    def grade(self, verdict: Any) -> tuple[bool, list[str]]:
        """`incident.verify` a second time, for the reader rather than for the model.

        The oracle has already gated by the time this runs, so this is not a second chance to
        refuse — it is the same standard reported as a result, which is what `correct` and
        `oracle_failures` are for.
        """
        failures = incident.verify(
            verdict.culprit_service, verdict.culprit_change_id, verdict.mechanism
        )
        return not failures, failures

    def run_type(self) -> type[TeamRun]:
        return Investigation

    # ── The two places the demo's own behaviour differs from the default ──

    async def execute(self, ctx: ThreadContext, request: str) -> TeamRun:
        """Run the skeleton with the standing question leading whatever the run was driven with.

        `cli.py:53` drives the room with `handle.run("")`, because the question is the room's and
        not the caller's; the skeleton hands its `request` to the lead verbatim, so without this
        the lead would be asked nothing at all.
        """
        return await super().execute(ctx, f"{self.question}\n\n{request}".strip())

    async def brief(
        self, ctx: ThreadContext, cast: Sequence[Recruit], request: str
    ) -> dict[str, str]:
        """Barrier, error rendering and event from the base; keys and audience from the demo.

        Keyed by plane rather than by member name, because `findings` is a published field and a
        reader of `investigation.json` knows `deploys`, not `deploys-analyst`. `strict=True` checks
        the assumption that makes the re-key sound — one specialist per plane, in `self.planes`
        order.

        The request is deliberately not forwarded. A specialist answers for its own plane and is
        not told what the lead was asked; one that read the question would be reasoning about the
        verdict, which is the lead's job and the asymmetry the demo exists to test.
        """
        del request
        answers = await super().brief(ctx, cast, "")
        return {
            plane: answers[member.name] for plane, member in zip(self.planes, cast, strict=True)
        }
