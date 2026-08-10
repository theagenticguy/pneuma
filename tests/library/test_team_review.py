"""Offline tests for `team/hooks/review.py`: the `Critic` and the `Council`.

Every delivery claim is checked ON THE WIRE — from the contexts a recording model actually
received — never from the returned `TeamRun` alone (the render_brief lesson,
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
The load-bearing claims here: a critic's findings appear in the lead's *second* context; a
clean review costs zero extra lead cycles; and an errored or absent reviewer never settles
`Accept` (`.erpaval/solutions/verification/truncation-must-dominate-positive-evidence.md`)
— the silent-accept fallback is the defect these hooks exist to refuse, so its tests assert
the record's `outcome`, not just the verdict a coincidental `Revise` could also produce.

`Counting` composes a `ScriptedModel` (which is `@final`); output types are module level
because `compile_ai_method` resolves annotations against module globals (`method.py:146`).
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
from pneuma.team.hooks import Council, Critic

# ── Output types, module level for get_type_hints ──


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="What it relies on")


# ── The cast ──


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class RedTeam(MethodAgent):
    """A reviewer as a typed member: `@ai_method(str)` answers via `FinalAnswer`."""

    name = "red-team"

    @ai_method(str, description="Refute one answer, or say you cannot")
    def review(self, brief: str, style: str = "harsh") -> str:
        """Review this brief with {style} rigour: {brief}"""


class Counting(Model):
    """Composes a `ScriptedModel` and records what each call carried on the wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []
        self.tool_specs: list[list[str]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(
        self, messages: Any, tool_specs: Any = None, *args: Any, **kwargs: Any
    ) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        self.tool_specs.append([spec["name"] for spec in (tool_specs or [])])
        return self._inner.stream(messages, tool_specs, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


class Voice:
    """A model-less `Recruit` panelist: fixed answers, a lifecycle journal, optional death.

    `asked` records what each review request carried — the boundary the hook writes to;
    delivery from `ask` down to a real member's model is the adapter's job and is proven
    separately with a `Counting`-backed `Member` (and was proven for the adapter itself in
    test_team_core).
    """

    def __init__(self, name: str, *answers: str, fail: Exception | None = None) -> None:
        self.name = name
        self.answers = list(answers) or ["APPROVED"]
        self.fail = fail
        self.asked: list[str] = []
        self.spawns = 0
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.spawns += 1
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.asked.append(request)
        if self.fail is not None:
            raise self.fail
        return self.answers[min(len(self.asked), len(self.answers)) - 1]

    async def retire(self) -> None:
        self.retirements += 1


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def review_says(text: str) -> Turn:
    """One reviewer turn: `@ai_method(str)` wraps `str` in a generated `FinalAnswer`."""
    return Turn(tool_calls=(("FinalAnswer", {"answer": text}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


def scripted_reviewer(turns: list[Turn]) -> tuple[Member, Counting]:
    model = Counting(turns)
    return Member(RedTeam(), "review", model=model), model


def entries(run: Any) -> list[dict[str, Any]]:
    return run.hooks_data["review"]


# ── 1. Critic: findings, clean review, caps ──


async def test_critic_findings_reach_the_leads_revise_context_on_the_wire() -> None:
    """The whole loop, closed: the reviewer's model receives the request AND the answer;
    its findings land in the lead's SECOND context; the revised answer is re-reviewed and a
    clean second review accepts it. Every leg asserted from a recording model's wire."""
    async with RuntimeHarness() as h:
        reviewer, reviewer_model = scripted_reviewer(
            [review_says("the citation DRAFT-1 is fabricated"), review_says("NO-FINDINGS")]
        )
        lead, lead_model = scripted_lead([ruling(cites=["DRAFT-1"]), ruling(cites=["FIXED-2"])])
        run = await Team(lead, [], hooks=[Critic(reviewer)]).run(
            "who is right", h.worker.coordinator
        )

    assert len(reviewer_model.contexts) == 2, "one review per lead answer"
    first_review = "\n".join(reviewer_model.prompts(0))
    assert "who is right" in first_review, "the reviewer sees the run's request"
    assert "DRAFT-1" in first_review, "the reviewer sees the answer under review"
    assert len(lead_model.contexts) == 2, "one draft, one revision"
    revise_prompt = "\n".join(lead_model.prompts(1))
    assert "the citation DRAFT-1 is fabricated" in revise_prompt, "findings on the lead's wire"
    assert "red-team.review" in revise_prompt, "the feedback names its reviewer"
    assert "FIXED-2" in "\n".join(reviewer_model.prompts(1)), "the REVISED answer is re-reviewed"
    assert run.answer.cites == ["FIXED-2"]
    assert [e["outcome"] for e in entries(run)] == ["findings", "clean"]


async def test_a_clean_review_accepts_with_zero_extra_lead_cycles() -> None:
    async with RuntimeHarness() as h:
        reviewer, reviewer_model = scripted_reviewer([review_says("NO-FINDINGS")])
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Critic(reviewer)]).run("go", h.worker.coordinator)

    assert len(lead_model.contexts) == 1, "a clean review costs the lead nothing"
    assert len(reviewer_model.contexts) == 1
    assert run.transcript == []
    assert [e["outcome"] for e in entries(run)] == ["clean"]


async def test_critic_cap_exhaustion_passes_the_last_answer_and_records_every_round() -> None:
    """A never-satisfied critic is bounded by `rounds`: rounds=1 spends one revision, the
    cap (not a clean review) ends the loop, and the LAST answer is the one returned."""
    async with RuntimeHarness() as h:
        reviewer, _ = scripted_reviewer(
            [review_says("still fabricated"), review_says("STILL fabricated")]
        )
        lead, lead_model = scripted_lead([ruling(cites=["a"]), ruling(cites=["b"])])
        run = await Team(lead, [], hooks=[Critic(reviewer, rounds=1)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 2, "draft + exactly the cap's one revision"
    assert run.answer.cites == ["b"], "cap exhaustion passes the last answer on"
    assert [e["kind"] for e in run.transcript] == ["revise", "revise_cap"]
    assert [e["outcome"] for e in entries(run)] == ["findings", "findings"]
    assert [e["round"] for e in entries(run)] == [1, 2]


async def test_critic_rounds_zero_records_findings_but_never_reruns_the_lead() -> None:
    """`rounds=0` is review-without-teeth by cap, not by silence: the findings are on the
    record and the transcript says the cap ended the loop after zero revisions."""
    async with RuntimeHarness() as h:
        reviewer, _ = scripted_reviewer([review_says("wrong, section 2")])
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Critic(reviewer, rounds=0)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 1
    assert [e["kind"] for e in run.transcript] == ["revise_cap"]
    assert [e["outcome"] for e in entries(run)] == ["findings"]


# ── 2. Critic: review integrity — an errored/empty review never settles Accept ──


async def test_an_errored_reviewer_never_settles_accept_and_the_error_reaches_the_lead() -> None:
    """The integrity rule at its sharpest: the reviewer's thread dies on every ask, and the
    verdict is Revise — outcome `error` on the record, the stringified error in the lead's
    revise context, and the loop ends by CAP, never by a clean review."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", fail=RuntimeError("reviewer thread died"))
        lead, lead_model = scripted_lead([ruling(cites=["a"]), ruling(cites=["b"])])
        run = await Team(lead, [], hooks=[Critic(reviewer, rounds=1)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 2, "the error revised; it did not wave the answer through"
    assert "reviewer thread died" in "\n".join(lead_model.prompts(1))
    assert [e["outcome"] for e in entries(run)] == ["error", "error"]
    assert [e["kind"] for e in run.transcript] == ["revise", "revise_cap"]


async def test_an_empty_review_is_an_error_on_the_record_not_a_finding_and_not_clean() -> None:
    """A reviewer that answers whitespace reviewed nothing. It must not read as clean (the
    silent accept) and must not read as findings either — the record says `error`, naming
    the reviewer, so the audit shows a review that failed rather than one that happened."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", "   ")
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Critic(reviewer, rounds=0)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 1
    assert [e["kind"] for e in run.transcript] == ["revise_cap"], "Revise, bounded — not Accept"
    (entry,) = entries(run)
    assert entry["outcome"] == "error"
    assert "red-team" in entry["review"] and "empty" in entry["review"]


async def test_an_error_that_quotes_no_findings_is_still_an_error() -> None:
    """The precedence guard: error detection runs before the clean-token check, so a dead
    reviewer whose repr happens to carry the token cannot read as a clean review."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", fail=RuntimeError("gave NO-FINDINGS then crashed"))
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Critic(reviewer, rounds=0)]).run(
            "go", h.worker.coordinator
        )

    assert [e["outcome"] for e in entries(run)] == ["error"]
    assert [e["kind"] for e in run.transcript] == ["revise_cap"]


# ── 3. Critic: reviewer lifecycle ──


async def test_a_standalone_reviewer_is_spawned_and_retired_by_the_hook_and_is_no_tool() -> None:
    """A reviewer outside the cast: the hook owns its lifecycle (one spawn, one retire) and
    it never appears on the lead's tool wire — the lead cannot consult its own reviewer."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", "NO-FINDINGS")
        lead, lead_model = scripted_lead([ruling()])
        await Team(lead, [Voice("colleague")], hooks=[Critic(reviewer)]).run(
            "go", h.worker.coordinator
        )

    assert reviewer.spawns == 1 and reviewer.retirements == 1
    assert not any("red-team" in spec for spec in lead_model.tool_specs[0]), (
        f"the reviewer leaked onto the lead's wire: {lead_model.tool_specs[0]}"
    )


async def test_a_reviewer_already_in_the_cast_is_not_spawned_or_retired_twice() -> None:
    """When the reviewer object IS a member, the core owns its lifecycle: exactly one spawn
    and one retire happen in total, and the review still runs over the live thread."""
    async with RuntimeHarness() as h:
        insider = Voice("insider", "NO-FINDINGS")
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, [insider], hooks=[Critic(insider)]).run(
            "the question", h.worker.coordinator
        )

    assert insider.spawns == 1, "the hook must not double-spawn a cast member"
    assert insider.retirements == 1, "the hook must not retire what the core owns"
    assert len(insider.asked) == 1 and "the question" in insider.asked[0]
    assert [e["outcome"] for e in entries(run)] == ["clean"]


# ── 4. Council: votes, the threshold boundary, objections on the wire ──


async def test_council_below_threshold_revises_with_the_objections_on_the_leads_wire() -> None:
    """1/3 approvals under threshold 0.5: the two objections travel to the lead attributed
    by name, the approver is named as already-approving, and every panelist saw the request
    and the answer. The second vote is unanimous and accepts the revised answer."""
    async with RuntimeHarness() as h:
        yes = Voice("optimist", "APPROVED", "APPROVED")
        no1 = Voice("skeptic", "objection: DRAFT-1 is uncited", "APPROVED")
        no2 = Voice("pedant", "objection: wrong section", "APPROVED")
        lead, lead_model = scripted_lead([ruling(cites=["DRAFT-1"]), ruling(cites=["FIXED-2"])])
        run = await Team(lead, [], hooks=[Council([yes, no1, no2])]).run(
            "who is right", h.worker.coordinator
        )

    for panelist in (yes, no1, no2):
        assert len(panelist.asked) == 2, f"{panelist.name} voted on both answers"
        assert "who is right" in panelist.asked[0], "panelists see the run's request"
        assert "DRAFT-1" in panelist.asked[0], "panelists see the answer under review"
        assert "FIXED-2" in panelist.asked[1], "the second vote is over the REVISED answer"
    assert len(lead_model.contexts) == 2
    revise_prompt = "\n".join(lead_model.prompts(1))
    assert "skeptic: objection: DRAFT-1 is uncited" in revise_prompt
    assert "pedant: objection: wrong section" in revise_prompt
    assert "Already approved by: optimist" in revise_prompt
    assert "optimist:" not in revise_prompt.split("Already approved")[0], (
        "an approval is not an objection"
    )
    assert run.answer.cites == ["FIXED-2"]
    first, second = entries(run)
    assert first == {
        "hook": "Council",
        "round": 1,
        "approved": ["optimist"],
        "reviews": first["reviews"],
        "accepted": False,
    }
    assert second["accepted"] is True and second["approved"] == ["optimist", "skeptic", "pedant"]


async def test_council_exactly_at_threshold_accepts() -> None:
    """The boundary is `>=`: 2/4 at threshold 0.5 accepts, with zero extra lead cycles."""
    async with RuntimeHarness() as h:
        panel = [
            Voice("a", "APPROVED"),
            Voice("b", "APPROVED"),
            Voice("c", "objection: no"),
            Voice("d", "objection: also no"),
        ]
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Council(panel, threshold=0.5)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 1, "exactly-at-threshold is acceptance, not revision"
    assert entries(run)[0]["accepted"] is True
    assert entries(run)[0]["approved"] == ["a", "b"]


async def test_council_just_under_threshold_revises() -> None:
    """The other side of the boundary: 1/3 at threshold 0.5 is under, and revises."""
    async with RuntimeHarness() as h:
        panel = [Voice("a", "APPROVED", "APPROVED")] + [
            Voice(n, "objection", "APPROVED") for n in ("b", "c")
        ]
        lead, lead_model = scripted_lead([ruling(), ruling()])
        run = await Team(lead, [], hooks=[Council(panel, threshold=0.5)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 2, "1/3 < 0.5 must revise"
    assert entries(run)[0]["accepted"] is False


async def test_an_errored_panelist_counts_as_an_objection_never_as_absence() -> None:
    """The integrity rule, Council shape: 1 approval + 1 error at threshold 1.0 must revise
    — if the error left the denominator, the survivor's 1/1 would be unanimous and accept.
    The stringified error travels to the lead as that panelist's objection, and it can never
    read as approval even though the repr quotes the token."""
    async with RuntimeHarness() as h:
        yes = Voice("optimist", "APPROVED", "APPROVED")
        dead = Voice("casualty", fail=RuntimeError("panelist died APPROVED-ly"))
        lead, lead_model = scripted_lead([ruling(cites=["a"]), ruling(cites=["b"])])
        run = await Team(lead, [], hooks=[Council([yes, dead], threshold=1.0, rounds=1)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 2, "the errored panelist must not wave the answer through"
    revise_prompt = "\n".join(lead_model.prompts(1))
    assert "casualty: error: " in revise_prompt and "panelist died" in revise_prompt
    first = entries(run)[0]
    assert first["approved"] == ["optimist"] and first["accepted"] is False
    assert [e["kind"] for e in run.transcript] == ["revise", "revise_cap"]


async def test_a_typed_panelists_approval_is_read_out_of_its_structured_answer() -> None:
    """The containment tradeoff, wire-verified: a real `Member` panelist answers through a
    scripted model as `FinalAnswer(answer='APPROVED')`, `str()` embeds the token, and the
    vote counts — an equality check would silently veto every typed panelist."""
    async with RuntimeHarness() as h:
        panelist_model = Counting([review_says("APPROVED")])
        panelist = Member(RedTeam(), "review", model=panelist_model)
        lead, lead_model = scripted_lead([ruling(cites=["DRAFT-1"])])
        run = await Team(lead, [], hooks=[Council([panelist], threshold=1.0)]).run(
            "the question", h.worker.coordinator
        )

    assert len(panelist_model.contexts) == 1, "the vote reached the panelist's model"
    vote_prompt = "\n".join(panelist_model.prompts(0))
    assert "the question" in vote_prompt and "DRAFT-1" in vote_prompt
    assert len(lead_model.contexts) == 1
    assert entries(run)[0]["approved"] == ["red-team.review"]


async def test_council_spawns_only_the_panelists_the_cast_does_not_carry() -> None:
    """A mixed panel: the cast member is asked over the core-owned thread (one spawn, one
    retire in total); the outsider is hook-owned (one spawn, one retire by the hook); and
    neither panelist's vote is lost to the split."""
    async with RuntimeHarness() as h:
        insider = Voice("insider", "APPROVED")
        outsider = Voice("outsider", "APPROVED")
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, [insider], hooks=[Council([insider, outsider])]).run(
            "go", h.worker.coordinator
        )

    assert insider.spawns == 1 and insider.retirements == 1, "cast panelist stays core-owned"
    assert outsider.spawns == 1 and outsider.retirements == 1, "outsider is hook-owned"
    assert entries(run)[0]["approved"] == ["insider", "outsider"]


# ── 5. Advisory mode: annotation, never a gate ──


async def test_advisory_critic_records_findings_but_never_revises() -> None:
    """`advisory=True` changes what the verdict does, never what the record says: findings
    are on the record as findings, and the lead's model is called exactly once."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", "objection: section 2 is wrong")
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Critic(reviewer, advisory=True)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 1, "advisory review must not cost a revision"
    assert run.transcript == [], "no revise, no revise_cap — Accept, not a spent cap"
    assert [e["outcome"] for e in entries(run)] == ["findings"], "the record still says findings"


async def test_advisory_council_records_a_failed_vote_but_never_revises() -> None:
    async with RuntimeHarness() as h:
        panel = [Voice("a", "objection: no"), Voice("b", fail=RuntimeError("died"))]
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Council(panel, advisory=True)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 1
    assert run.transcript == []
    (entry,) = entries(run)
    assert entry["accepted"] is False, "the record says the vote failed, even though no gate"
    assert entry["approved"] == []


# ── 6. Guards, and both hooks on one team ──


def test_an_empty_council_is_refused_at_construction() -> None:
    """0/0 >= threshold is vacuously true for any reachable threshold — a review by nobody
    settling Accept is the silent-accept defect verbatim, refused where the wirer looks."""
    with pytest.raises(ValueError, match="nobody to vote"):
        Council([])


def test_a_threshold_outside_zero_one_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="threshold=0.*outside"):
        Council([Voice("a")], threshold=0)
    with pytest.raises(ValueError, match="threshold=1.5.*outside"):
        Council([Voice("a")], threshold=1.5)
    assert Council([Voice("a")], threshold=1.0).threshold == 1.0, "1.0 (unanimity) is legal"


def test_negative_rounds_are_refused_at_construction_for_both_hooks() -> None:
    with pytest.raises(ValueError, match="rounds=-1.*negative"):
        Critic(Voice("r"), rounds=-1)
    with pytest.raises(ValueError, match="rounds=-2.*negative"):
        Council([Voice("a")], rounds=-2)


async def test_a_critic_and_a_council_share_the_review_record_in_run_order() -> None:
    """Both hooks on one team: the critic reviews first (hook order), the council votes on
    what the critic accepted, and `hooks_data["review"]` interleaves their entries in the
    order they actually ran, each attributed to its hook."""
    async with RuntimeHarness() as h:
        reviewer = Voice("red-team", "objection: cite something", "NO-FINDINGS")
        panel = [Voice("a", "APPROVED"), Voice("b", "APPROVED")]
        lead, lead_model = scripted_lead([ruling(cites=[]), ruling(cites=["FIXED"])])
        run = await Team(lead, [], hooks=[Critic(reviewer), Council(panel)]).run(
            "go", h.worker.coordinator
        )

    assert len(lead_model.contexts) == 2, "one critic revision, then a clean vote"
    assert [(e["hook"], e.get("outcome", e.get("accepted"))) for e in entries(run)] == [
        ("Critic", "findings"),
        ("Critic", "clean"),
        ("Council", True),
    ]
    assert "FIXED" in panel[0].asked[0], "the council votes on what the critic accepted"
    assert run.answer.cites == ["FIXED"]
