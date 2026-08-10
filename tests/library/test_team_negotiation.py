"""Offline tests for the `Negotiation` hook: bounded plan→objection→revision on the core loop.

Every delivery claim is asserted from a scripted model's own captured context, never from the
returned `TeamRun` alone — the render_brief precedent: a phase once *recorded* briefings that
never reached the lead's prompt, and only the wire could say so. The *plan* is asserted inside
each member's model context; the *objections* inside the lead's revision context; the default
(no hook) as call counts, because "nothing ran" is a claim about calls, not about a dict.

Fixtures restate `test_team.py`'s conventions; this file is self-contained because
`tests/library/` has no package.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Sequence
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Team
from pneuma.team.hooks import Negotiation

# ── Output types, module level ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows, or the review verdict")


class Ruling(BaseModel):
    """`cites` doubles as the distinguishable plan text in these tests."""

    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


# ── The cast ──


class Analyst(MethodAgent):
    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class Spy:
    """A model-free `Recruit` recording every request it was asked."""

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
    """Every ask raises — with no briefing hook on the team, every ask IS a review.

    The error message deliberately QUOTES the approval token: a rendered error embeds the
    exception's own text, so a containment-only `approves()` — one that forgot the
    error-prefix check — would count this dead reviewer as approving. The message makes
    that break detectable instead of coincidentally missed.
    """

    async def ask(self, request: str) -> Any:
        self.requests.append(request)
        raise ConnectionError("the reviewer's thread is gone before it could decide on APPROVED")


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


# ── The model ──


class Counting(Model):
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
        """The text of the *last* message in the `call`-th context — a thread's history is
        cumulative, so negative claims about one round must scope to that round's request."""
        return "\n".join(
            block["text"] for block in self.contexts[call][-1].get("content", []) if "text" in block
        )


def reading(detail: str, *, source: str = "left") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": detail}),))


def ruling(*cites: str, admitted: bool = True) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


APPROVING = f"looks right to me, {Negotiation.APPROVAL}"


# ── 1. Unanimous approval accepts in round one ──


async def test_unanimous_approval_in_round_one_accepts_and_the_plan_reached_every_member() -> None:
    """rounds=2, everyone approves in round 1: one recorded round, no revision cycle, and
    the plan text — unmistakable via its cites — inside each member's own model context."""
    async with RuntimeHarness() as h:
        left_model = Counting([reading(APPROVING)])
        right_model = Counting([reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA")])
        team = Team(lead, members, hooks=[Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    for model in (left_model, right_model):
        assert len(model.contexts) == 1, "one plan review each — no briefing hook on this team"
        review = "\n".join(model.prompts(0))
        assert "DRAFT-PLAN-ALPHA" in review, (
            "the plan text must actually appear in the member's own model context — a "
            "hooks_data entry claiming fan-out proves nothing about the wire"
        )
        assert Negotiation.APPROVAL in review, "the instruction names the token approves() checks"
    assert len(lead_model.contexts) == 1, "everyone approved, so no revision cycle was spent"
    rounds = run.hooks_data["negotiation"]
    assert [e["outcome"] for e in rounds] == ["unanimous"]
    assert rounds[0]["round"] == 1
    assert set(rounds[0]["approved"]) == {"left-analyst.read", "right-analyst.read"}
    assert run.answer.cites == ["DRAFT-PLAN-ALPHA"], "the approved draft is the answer"


# ── 2. An objection reaches the lead, and the revision reaches the members ──


async def test_an_objection_reaches_the_leads_revision_prompt_and_the_revision_the_members() -> (
    None
):
    """The full loop, both wires pinned: objection → lead's second context, revised plan →
    member's second review; negatively, round 2 fanned out the revision and not the draft."""
    async with RuntimeHarness() as h:
        member_model = Counting(
            [
                reading("this misses THE-GAP-OBJECTION and must change"),
                reading(APPROVING),
            ]
        )
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA"), ruling("REVISED-PLAN-BETA")])
        team = Team(lead, members, hooks=[Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    revision_prompt = "\n".join(lead_model.prompts(1))
    assert "THE-GAP-OBJECTION" in revision_prompt, (
        "the member's objection must actually appear in the lead's revision context"
    )
    assert "left-analyst.read" in revision_prompt, "attributed to the member who raised it"

    round_two_review = member_model.newest(1)
    assert "REVISED-PLAN-BETA" in round_two_review, "the *revised* plan reached the member"
    assert "DRAFT-PLAN-ALPHA" not in round_two_review, (
        "round 2 fanned out the revision, not the draft — scoped to round 2's own request"
    )

    rounds = run.hooks_data["negotiation"]
    assert [e["outcome"] for e in rounds] == ["revised", "unanimous"]
    assert "THE-GAP-OBJECTION" in rounds[0]["objections"]["left-analyst.read"]
    assert rounds[0]["approved"] == []
    assert rounds[1]["approved"] == ["left-analyst.read"]
    assert run.answer.cites == ["REVISED-PLAN-BETA"], "the negotiated answer is the revision"
    assert len(lead_model.contexts) == 2 and len(member_model.contexts) == 2
    # And the core's transcript shows the revise round the hook drove.
    assert [e["kind"] for e in run.transcript] == ["revise"]


# ── 3. A member erroring mid-review is stringified and blocks unanimity ──


async def test_a_member_that_raises_during_review_is_stringified_and_the_run_completes() -> None:
    """One member dies reviewing, one approves. The error is rendered in the recorded round
    AND in the lead's revision prompt, it never counts as approval, and everybody is still
    retired by the core's unwind."""
    async with RuntimeHarness() as h:
        broken = RaisesOnReview("fragile")
        steady = Spy("steady", answers=(APPROVING,))
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA"), ruling("REVISED-PLAN-BETA")])
        team = Team(lead, [broken, steady], hooks=[Negotiation(rounds=1)])
        run = await team.run("go", h.worker.coordinator)

    entry = run.hooks_data["negotiation"][0]
    assert entry["objections"]["fragile"].startswith("error: "), (
        "a review fault is rendered exactly as a briefing fault is"
    )
    assert "the reviewer's thread is gone" in entry["objections"]["fragile"]
    assert entry["approved"] == ["steady"], "an errored member can never count as approving"
    revision_prompt = "\n".join(lead_model.prompts(1))
    assert "the reviewer's thread is gone" in revision_prompt, (
        "the lead revises knowing one reviewer died, not against a silently shrunken team"
    )
    assert run.answer.cites == ["REVISED-PLAN-BETA"], "the run completed with the revision"
    assert broken.retirements == 1 and steady.retirements == 1


# ── 4. The cap: bounded, honest about not reaching unanimity ──


async def test_the_round_cap_is_reached_without_unanimity_and_the_run_proceeds() -> None:
    """rounds=2 against a member that never approves: exactly two revision cycles (three
    lead calls), the last recorded round says `cap_reached`, the answer is the last revision,
    and the core's transcript carries its own `revise_cap` marker."""
    async with RuntimeHarness() as h:
        stubborn = Spy("stubborn", answers=("no: FIRST-VETO", "no: SECOND-VETO", "no: THIRD-VETO"))
        lead, lead_model = scripted_lead(
            [ruling("DRAFT-PLAN-ALPHA"), ruling("REVISION-ONE"), ruling("REVISION-TWO")]
        )
        team = Team(lead, [stubborn], hooks=[Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    rounds = run.hooks_data["negotiation"]
    assert [e["outcome"] for e in rounds] == ["revised", "revised", "cap_reached"], (
        "the record must say the cap ended the negotiation, not imply unanimity"
    )
    assert len(lead_model.contexts) == 3, "one draft and exactly two revisions — the cap held"
    assert run.answer.cites == ["REVISION-TWO"], "the run proceeds with the last revision"
    assert "FIRST-VETO" in "\n".join(lead_model.prompts(1))
    assert "SECOND-VETO" in "\n".join(lead_model.prompts(2)), (
        "each round's objections reached that round's revision, not a stale batch"
    )
    assert run.transcript[-1]["kind"] == "revise_cap", "the core recorded the cap itself"
    assert len(stubborn.requests) == 3, "one review per round including the capped one"


# ── The edges ──


async def test_a_negative_round_budget_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="rounds=-1.*negative"):
        Negotiation(rounds=-1)


async def test_an_empty_cast_never_negotiates_because_nobody_can_approve() -> None:
    """No members is a no-op `Accept`, not a vacuously unanimous round: `all([]) is True`,
    so without the guard the record would show a consensus no member ever gave."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN-ALPHA")])
        team = Team(lead, [], hooks=[Negotiation(rounds=3)])
        run = await team.run("go", h.worker.coordinator)

    assert "negotiation" not in run.hooks_data, "no members, no rounds — not a fabricated one"
    assert len(lead_model.contexts) == 1


async def test_no_negotiation_hook_means_no_extra_calls_and_no_key() -> None:
    """The off state is the hook's absence: zero member cycles, one lead cycle, no key."""
    async with RuntimeHarness() as h:
        spy = Spy("watcher")
        lead, lead_model = scripted_lead([ruling("left")])
        run = await Team(lead, [spy]).run("go", h.worker.coordinator)

    assert spy.requests == [], "nobody fanned anything out"
    assert len(lead_model.contexts) == 1
    assert run.hooks_data == {}
    assert set(run.model_dump()) == {"answer"}, "empty keys serialise away"
