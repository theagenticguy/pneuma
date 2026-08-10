"""Offline tests for `Team.negotiate`: the optional bounded plan→objection→revision phase.

Every delivery claim here is asserted from a scripted model's own captured context, never from
the returned `TeamRun` alone. The precedent is the `render_brief` bug (`team.py`'s history, and
`docs/design/team.md`'s "Why the briefings reach the lead"): a phase once *recorded* briefings
that never reached the lead's prompt, and only reading the model's wire could have said so. So:

- the *plan* is asserted inside each member's model context, because a fan-out whose text never
  arrives is a transcript describing a delivery that did not happen;
- the *objections* are asserted inside the lead's revision context, for the same reason from the
  other direction;
- the *default* (rounds=0) is asserted as call-counts and phase events, because backward
  compatibility is a claim about what did NOT run, and a returned `TeamRun` with an empty list
  proves nothing about the calls that produced it.

`Counting` composes rather than subclasses (`ScriptedModel` is `@final`) and every fixture output
type is module level, because `compile_ai_method` resolves annotations against module globals —
both are `test_team.py`'s conventions, restated here because this file is deliberately
self-contained: `tests/library/` has no package, so test modules cannot import each other.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Sequence
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Recruit, Roster, Team, TeamRun

# ── Output types, module level ──


class Reading(BaseModel):
    """What one member reports — from a briefing or from a plan review."""

    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows, or the review verdict")


class Ruling(BaseModel):
    """The lead's plan. `cites` doubles as the distinguishable plan text in these tests."""

    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


# ── The cast ──


class Analyst(MethodAgent):
    """A typed member: the shape whose model context is the wire the plan must reach."""

    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Chair(MethodAgent):
    """The lead. Its scripted model's contexts are where the objections must appear."""

    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported", max_attempts=3)
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class Spy:
    """A model-free `Recruit` recording every request it was asked, for the order claims."""

    def __init__(self, name: str, *, answers: Sequence[str] = ("ok",)) -> None:
        self.name = name
        self.answers = list(answers)
        self.requests: list[str] = []
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.requests.append(request)
        return self.answers[min(len(self.requests), len(self.answers)) - 1]

    async def retire(self) -> None:
        self.retirements += 1


class RaisesOnReview(Spy):
    """Briefs fine, dies during the objection cycle — the non-fatal-error path's fixture."""

    async def ask(self, request: str) -> Any:
        if self.requests:  # the first ask is the briefing; every later one is a plan review
            self.requests.append(request)
            raise ConnectionError("the reviewer's thread is gone")
        return await super().ask(request)


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


# ── The model ──


class Counting(Model):
    """A scripted model that records every context it saw. Composition, as `test_team.py` does."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        return self._inner.stream(messages, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]

    def newest(self, call: int) -> str:
        """The text of the *last* message in the `call`-th context.

        A thread's history is cumulative — the runtime rebuilds every prior turn into each
        context — so a negative claim about what one round delivered ("the draft did NOT fan
        out again") must scope to that round's own request. Asserted over the whole context it
        would always fail, because round 1's plan legitimately sits in the history above.
        """
        return "\n".join(
            block["text"] for block in self.contexts[call][-1].get("content", []) if "text" in block
        )


def reading(detail: str, *, source: str = "left") -> Turn:
    return Turn(
        tool_calls=(("Reading", {"source": source, "detail": detail}),),
        input_tokens=5,
        output_tokens=3,
    )


def ruling(*cites: str, admitted: bool = True) -> Turn:
    return Turn(
        tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),),
        input_tokens=7,
        output_tokens=2,
    )


# ── The team under test ──


class Bench(Team):
    """The smallest negotiable team: injected cast and lead, `test_team.py`'s `Toy` restated."""

    def __init__(
        self,
        *,
        cast: Sequence[Recruit] = (),
        lead: AIFunction[..., Any] | None = None,
        negotiation_rounds: int = 0,
        name: str = "bench",
    ) -> None:
        super().__init__(name=name, negotiation_rounds=negotiation_rounds, roster=Roster())
        self._cast = list(cast)
        self._lead = lead

    def members(self) -> Sequence[Recruit]:
        return self._cast

    def briefing(self, member: Recruit) -> str:
        return f"Read your own source, {member.name}."

    def lead_function(self) -> AIFunction[..., Any]:
        assert self._lead is not None, "this test's Bench needs a lead"
        return self._lead

    def oracle(self, response: Any) -> None:
        if not getattr(response, "admitted", False):
            raise AssertionError("the ruling is not admitted; rule again")


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


APPROVING = f"looks right to me, {Team.APPROVAL}"


# ── 1. Off by default: rounds=0 is the pre-negotiation skeleton, provably ──


async def test_with_rounds_zero_the_phase_sequence_and_every_call_count_are_unchanged() -> None:
    """Backward compatibility as call-counts and events, not as a returned empty list.

    A `TeamRun` whose `negotiation` is `[]` is also what a *broken* default produces if the phase
    ran and recorded nothing, so the assertion is about what was called: each member's model saw
    exactly one context (its briefing), the lead's saw exactly one (the request+briefings), no
    `team.negotiated` event exists, and the event order is the pre-negotiation sequence exactly.
    The serialised artifact carries no `negotiation` key at all — the published shape
    (`demo/warroom.py` pins nine keys) must not grow one on teams that never negotiated.
    """
    async with RuntimeHarness() as h:
        member_model = Counting([reading("seen")])
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("left")])
        team = Bench(cast=members, lead=lead)  # negotiation_rounds defaulted, deliberately

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")
        kinds = [str(getattr(e, "kind", "")) for e in await h.events(handle.id)]

    assert team.negotiation_rounds == 0, "off is the default, not a fixture choice"
    assert len(member_model.contexts) == 1, "the member was briefed once and never re-asked"
    assert len(lead_model.contexts) == 1, "the lead ruled once and never revised"
    assert run.negotiation == []
    assert "team.negotiated" not in kinds
    assert [k for k in kinds if k.startswith("team.")] == [
        "team.assembled",
        "team.briefings_in",
        "team.lead_running",
        "team.graded",
    ], "the phase sequence is the pre-negotiation skeleton exactly"
    assert "negotiation" not in json.loads(team.serialize_result(run)), (
        "an empty transcript must not change the serialised artifact's key set"
    )


# ── 2. Unanimous approval stops the negotiation early ──


async def test_unanimous_approval_in_round_one_stops_early_and_the_plan_reached_every_member() -> (
    None
):
    """rounds=2, everyone approves in round 1: one transcript round, no revision cycle.

    The delivery half is the load-bearing one, per the `render_brief` precedent: the plan is
    asserted *inside each member's model context*, because a transcript entry saying the plan
    fanned out is exactly what a phase with a broken wire would also record. The draft ruling
    cites `DRAFT-PLAN-ALPHA`, so its rendered plan text is unmistakable in a prompt.
    """
    async with RuntimeHarness() as h:
        left_model = Counting([reading("left briefing"), reading(APPROVING)])
        right_model = Counting([reading("right briefing"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA")])
        team = Bench(cast=members, lead=lead, negotiation_rounds=2)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    for model in (left_model, right_model):
        assert len(model.contexts) == 2, "one briefing and one plan review each"
        review = "\n".join(model.prompts(1))
        assert "DRAFT-PLAN-ALPHA" in review, (
            "the plan text must actually appear in the member's own model context — a "
            "transcript claiming fan-out proves nothing about the wire"
        )
        assert Team.APPROVAL in review, "and the instruction names the token approves() checks"
    assert len(lead_model.contexts) == 1, "everyone approved, so no revision cycle was spent"
    assert [e["outcome"] for e in run.negotiation] == ["unanimous"]
    assert run.negotiation[0]["round"] == 1
    assert set(run.negotiation[0]["approved"]) == {"left-analyst.read", "right-analyst.read"}
    assert run.verdict.cites == ["DRAFT-PLAN-ALPHA"], "the approved draft is the verdict"


# ── 3. An objection reaches the lead, and the revision reaches the members ──


async def test_an_objection_reaches_the_leads_revision_prompt_and_the_revision_the_members() -> (
    None
):
    """The full loop, both wires pinned: objection → lead, revised plan → members.

    Round 1: the member objects with `THE-GAP-OBJECTION`. Round 2: it approves the revision.
    Four deliveries are asserted, each from a captured model context — the objection text and
    the objector's name in the lead's second context, the *revised* plan (cites
    `REVISED-PLAN-BETA`) in the member's third, and negatively: the draft plan is not what
    round 2 fanned out. The transcript shows both rounds, `revised` then `unanimous`, and the
    verdict is the revision — a phase that gathered objections and dropped them would fail the
    lead-context assertion while passing every transcript one, which is the render_brief bug's
    exact shape.
    """
    async with RuntimeHarness() as h:
        member_model = Counting(
            [
                reading("the briefing"),
                reading("this misses THE-GAP-OBJECTION and must change"),
                reading(APPROVING),
            ]
        )
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA"), ruling("REVISED-PLAN-BETA")])
        team = Bench(cast=members, lead=lead, negotiation_rounds=2)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    revision_prompt = "\n".join(lead_model.prompts(1))
    assert "THE-GAP-OBJECTION" in revision_prompt, (
        "the member's objection must actually appear in the lead's revision context — a "
        "transcript recording it proves nothing about what the lead was asked"
    )
    assert "left-analyst.read" in revision_prompt, "attributed to the member who raised it"

    round_two_review = member_model.newest(2)
    assert "REVISED-PLAN-BETA" in round_two_review, "the *revised* plan reached the member"
    assert "DRAFT-PLAN-ALPHA" not in round_two_review, (
        "and round 2 fanned out the revision, not the draft it replaced — scoped to round 2's "
        "own request, because the draft legitimately sits above it in the cumulative history"
    )

    assert [e["outcome"] for e in run.negotiation] == ["revised", "unanimous"]
    assert "THE-GAP-OBJECTION" in run.negotiation[0]["objections"]["left-analyst.read"]
    assert "REVISED-PLAN-BETA" in run.negotiation[0]["revision"]
    assert run.negotiation[0]["approved"] == []
    assert run.negotiation[1]["approved"] == ["left-analyst.read"]
    assert run.verdict.cites == ["REVISED-PLAN-BETA"], "the negotiated verdict is the revision"
    assert len(lead_model.contexts) == 2 and len(member_model.contexts) == 3


# ── 4. A member erroring mid-review is a briefing failure's twin ──


async def test_a_member_that_raises_during_review_is_stringified_and_the_run_completes() -> None:
    """The objection cycle inherits `brief`'s failure contract: rendered, never fatal.

    One member dies reviewing, one approves. The run must complete, the dead member's error must
    appear under the `BRIEFING_ERROR` rendering in the transcript AND in the lead's revision
    prompt (a reviewer that could not review blocks unanimity, so the lead revises knowing why),
    and the dead member must still be retired by the unconditional unwind.
    """
    async with RuntimeHarness() as h:
        broken = RaisesOnReview("fragile", answers=("briefed fine",))
        steady = Spy("steady", answers=("briefed fine", APPROVING, APPROVING))
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA"), ruling("REVISED-PLAN-BETA")])
        team = Bench(cast=[broken, steady], lead=lead, negotiation_rounds=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    entry = run.negotiation[0]
    assert entry["objections"]["fragile"].startswith(Team.BRIEFING_ERROR), (
        "a review fault is rendered exactly as a briefing fault is"
    )
    assert "the reviewer's thread is gone" in entry["objections"]["fragile"]
    assert entry["approved"] == ["steady"], "an errored member can never count as approving"
    revision_prompt = "\n".join(lead_model.prompts(1))
    assert "the reviewer's thread is gone" in revision_prompt, (
        "the lead revises knowing one reviewer died, not against a silently shrunken team"
    )
    assert run.verdict.cites == ["REVISED-PLAN-BETA"], "the run completed with the revision"
    assert broken.retirements == 1 and steady.retirements == 1, "and everybody was still retired"


# ── 5. The cap: bounded, and honest about not reaching unanimity ──


async def test_the_round_cap_is_reached_without_unanimity_and_the_run_proceeds_with_the_last_plan() -> (  # noqa: E501
    None
):
    """rounds=2 against a member that never approves: two rounds, then the run proceeds.

    The last transcript entry must say `cap_reached` — a transcript whose final round read
    `revised` would imply a further round existed — and the verdict must be the *last* revision,
    which every lead cycle already gated through the oracle. The lead's call count pins the
    bound: one draft plus exactly two revisions, however stubborn the member.
    """
    async with RuntimeHarness() as h:
        stubborn = Spy("stubborn", answers=("briefed", "no: FIRST-VETO", "no: SECOND-VETO"))
        lead, lead_model = scripted_lead(
            [ruling("DRAFT-PLAN-ALPHA"), ruling("REVISION-ONE"), ruling("REVISION-TWO")]
        )
        team = Bench(cast=[stubborn], lead=lead, negotiation_rounds=2)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["outcome"] for e in run.negotiation] == ["revised", "cap_reached"], (
        "the transcript must say the cap ended the negotiation, not imply unanimity"
    )
    assert len(lead_model.contexts) == 3, "one draft and exactly two revisions — the cap held"
    assert run.verdict.cites == ["REVISION-TWO"], "the run proceeds with the last gated revision"
    assert "FIRST-VETO" in "\n".join(lead_model.prompts(1))
    assert "SECOND-VETO" in "\n".join(lead_model.prompts(2)), (
        "each round's objections reached that round's revision, not a stale batch"
    )
    assert len(stubborn.requests) == 3, "briefing plus one review per round, no review at the cap"


# ── The edges the new invariants create ──


async def test_a_negative_round_budget_is_refused_at_construction() -> None:
    """`range(1, 0)` is empty, so a negative cap would silently behave as 0 — a wiring typo the
    caller must see now, with the other construction guards, rather than a negotiation that
    quietly never happens."""
    with pytest.raises(RuntimeError, match="negotiation_rounds=-1 is negative"):
        Bench(cast=[], lead=None, negotiation_rounds=-1)


async def test_an_empty_cast_never_negotiates_because_nobody_can_approve() -> None:
    """rounds>0 with no members is a no-op, not a vacuously unanimous round: `all([]) is True`,
    so without the guard the transcript would record a consensus no member ever gave.
    `Bench(cast=[])` is the hiring tests' shape and must stay negotiation-silent."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA")])
        team = Bench(cast=[], lead=lead, negotiation_rounds=3)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.negotiation == [], "no members, no rounds — not a fabricated unanimous one"
    assert len(lead_model.contexts) == 1


async def test_a_populated_transcript_survives_the_serialisation_round_trip() -> None:
    """The other half of the compat serializer: dropped when empty, kept when real. A serializer
    that always dropped the key would keep every artifact byte-compatible by silently deleting
    the negotiation's only durable record."""
    run = TeamRun(
        verdict={"admitted": True},
        correct=True,
        oracle_failures=[],
        briefings={"a": "read"},
        hiring_log=[],
        negotiation=[{"round": 1, "plan": "p", "objections": {}, "approved": [], "outcome": "x"}],
        input_tokens=1,
        output_tokens=1,
        turns=1,
        wall_seconds=0.1,
    )
    team = Bench(cast=[], lead=None)
    payload = json.loads(team.serialize_result(run))
    assert payload["negotiation"][0]["plan"] == "p", "a real transcript is in the artifact"
    assert team.deserialize_result(team.serialize_result(run)).negotiation == run.negotiation
