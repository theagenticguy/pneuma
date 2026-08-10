"""Offline tests for the `Worklog` hook: typed discoveries fanned back at step boundaries.

Every delivery claim is asserted from a scripted model's own captured context, never from the
returned `TeamRun` alone — the render_brief precedent: a worklog entry saying a discovery was
`delivered` is exactly what a hook with a broken wire would also record. So the rendered
discovery is asserted *inside* the other members' and the lead's model contexts, and the
poster's exclusion is asserted as the marker's absence from the poster's own contexts.

Tool-injection claims are asserted from the wire too: `Counting` records the tool offer each
model call carried, because "no tool when the hook is absent" is a claim about what the model
could see. Fixtures restate `test_team.py`'s conventions; self-contained file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model
from strands.tools.decorator import tool as strands_tool

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Team, Workspace
from pneuma.team.hooks import DISCOVERY_KINDS, Negotiation, Worklog

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

# ── Output types, module level ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows, or the review verdict")


class Ruling(BaseModel):
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


class DeadReceiver:
    """A `Recruit` whose handle's `notify` always raises — a retired teammate's channel.

    `ask` still answers (and approves any plan), because the claim under test is that a dead
    *channel* is recorded and skipped, not that a dead member takes the run down.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.retirements = 0
        self.thread = _DeadHandle(f"tid-{name}")

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        return self.thread

    async def ask(self, request: str) -> Any:
        return f"nothing to report, {Negotiation.APPROVAL}"

    async def retire(self) -> None:
        self.retirements += 1


class _DeadHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident

    async def notify(self, text: str) -> None:
        raise RuntimeError("the receiver's thread is retired")


# ── The model ──


class Counting(Model):
    """Records contexts, tool offers and tool results — the whole wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []
        self.offered_tools: list[list[str]] = []

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
        self.offered_tools.append([spec["name"] for spec in tool_specs or []])
        return self._inner.stream(messages, tool_specs, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]

    def all_text(self) -> str:
        return "\n".join(text for call in range(len(self.contexts)) for text in self.prompts(call))

    def tool_results(self, call: int) -> list[str]:
        return [
            inner["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "toolResult" in block
            for inner in block["toolResult"].get("content", [])
            if "text" in inner
        ]


def reading(detail: str, *, source: str = "left") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": detail}),))


def ruling(*cites: str, admitted: bool = True) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def posting(kind: str, body: str) -> Turn:
    return Turn(tool_calls=(("post_discovery", {"kind": kind, "body": body}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


APPROVING = f"looks right to me, {Negotiation.APPROVAL}"
MARKER = "[team worklog]"


# ── 1. Absent hook: no tool on the wire, no key in the run ──


async def test_without_the_hook_no_tool_is_offered_and_the_run_carries_no_key() -> None:
    """The off state is the hook's absence, asserted from the wire: `offered_tools` is what
    the member's model was actually shown."""
    async with RuntimeHarness() as h:
        member_model = Counting([reading(APPROVING)])
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("left")])
        # Negotiation alone, so the member gets one cycle for the tool-absence check.
        run = await Team(lead, members, hooks=[Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    assert all("post_discovery" not in offer for offer in member_model.offered_tools), (
        "the discovery tool must not reach a member's model without the hook"
    )
    assert all("post_discovery" not in offer for offer in lead_model.offered_tools)
    assert "worklog" not in run.hooks_data


# ── 2. A discovery reaches every OTHER member's next context, the lead, and the log ──


async def test_a_discovery_reaches_the_other_members_next_context_and_the_lead_by_replay() -> None:
    """Left posts during its briefing cycle — before the worklog's channels even open, so
    every delivery here is `register`'s replay. Right's *next* context (its negotiation
    review) must carry the rendered discovery — `notify` buffers into the thread's history
    and drains at the next model call. The lead's first context must carry it too: its
    channel opened before its thread ever cycled, so the pending notify drains into the
    lead's FIRST context."""
    from pneuma.team.hooks import Briefing

    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                posting("obstacle", "THE-SHARED-CLUE"),
                reading("left brief"),
                reading(APPROVING),
            ]
        )
        right_model = Counting([reading("right brief"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN")])
        team = Team(lead, members, hooks=[Briefing(), Worklog(), Negotiation(rounds=1)])
        run = await team.run("go", h.worker.coordinator)

    review = "\n".join(right_model.prompts(len(right_model.contexts) - 1))
    assert MARKER in review and "THE-SHARED-CLUE" in review, (
        "the discovery must actually appear in the other member's own model context — a "
        "worklog entry claiming delivery proves nothing about the wire"
    )
    assert "left-analyst.read" in review, "attributed to the member who posted it"
    lead_first = "\n".join(lead_model.prompts(0))
    assert MARKER in lead_first and "THE-SHARED-CLUE" in lead_first, (
        "the pending notify drained into the lead's FIRST context"
    )
    entries = run.hooks_data["worklog"]
    assert [e["kind"] for e in entries] == ["obstacle"]
    assert entries[0]["body"] == "THE-SHARED-CLUE"
    assert entries[0]["source"] == "left-analyst.read"
    assert set(entries[0]["delivered"]) == {"right-analyst.read", "lead"}
    assert entries[0]["failed"] == {}


# ── 3. The poster never receives its own discovery ──


async def test_the_poster_is_excluded_from_its_own_fan_out() -> None:
    """Absence asserted where presence had every chance: the poster's thread reaches two more
    step boundaries (its reading and its round-2 approval), so an inclusive fan-out would
    drain the marker into one of those contexts."""
    async with RuntimeHarness() as h:
        left_model = Counting(
            [posting("dead-end", "MY-OWN-FINDING"), reading("no"), reading(APPROVING)]
        )
        right_model = Counting([reading("no"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN"), ruling("REVISED-PLAN")])
        team = Team(lead, members, hooks=[Worklog(), Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    assert MARKER not in left_model.all_text(), (
        "the poster already knows; an echo would spend its next context restating it"
    )
    assert "left-analyst.read" not in run.hooks_data["worklog"][0]["delivered"]
    assert MARKER in right_model.all_text(), "while the other member really was reached"


# ── 4. One dead channel: recorded, and the rest still reached ──


async def test_a_failing_notify_is_recorded_on_the_entry_and_the_rest_are_still_reached() -> None:
    """`DeadReceiver` sits *first* in the cast so its failure precedes the healthy
    deliveries — a fan-out that stopped at the first fault would leave the later channels
    unreached. The failure lands on the entry, the run completes, nothing raises."""
    async with RuntimeHarness() as h:
        dead = DeadReceiver("fragile")
        left_model = Counting(
            [posting("contradicts-plan", "THE-WARNING"), reading("no"), reading(APPROVING)]
        )
        right_model = Counting([reading("no"), reading(APPROVING)])
        members = [
            dead,
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN"), ruling("REVISED-PLAN")])
        team = Team(lead, members, hooks=[Worklog(), Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    entry = run.hooks_data["worklog"][0]
    assert "fragile" in entry["failed"], "the dead channel's failure is on the record"
    assert "retired" in entry["failed"]["fragile"], "with the error itself, for a reader"
    assert "fragile" not in entry["delivered"]
    assert "right-analyst.read" in entry["delivered"], (
        "the fan-out continued past the fault — one dead teammate must not stop the rest"
    )
    assert "lead" in entry["delivered"]
    assert MARKER in right_model.all_text()
    assert run.answer.admitted is True, "a fan-out fault is never fatal"
    assert dead.retirements == 1, "the dead-channelled member was still retired"


# ── 5. Two concurrent posts both land: reserve-before-await ──


async def test_two_posts_in_one_assistant_turn_both_land_with_no_lost_update() -> None:
    """The concurrent tool executor runs both `post_discovery` calls as interleaved tasks
    and every fan-out awaits; the entry is appended in the same synchronous stretch that
    builds it, so neither post may drop the other."""
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                Turn(
                    tool_calls=(
                        ("post_discovery", {"kind": "obstacle", "body": "FIRST-CONCURRENT"}),
                        ("post_discovery", {"kind": "dead-end", "body": "SECOND-CONCURRENT"}),
                    )
                ),
                reading("no"),
                reading(APPROVING),
            ]
        )
        right_model = Counting([reading("no"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN"), ruling("REVISED-PLAN")])
        team = Team(lead, members, hooks=[Worklog(), Negotiation(rounds=2)])
        run = await team.run("go", h.worker.coordinator)

    bodies = {e["body"] for e in run.hooks_data["worklog"]}
    assert bodies == {"FIRST-CONCURRENT", "SECOND-CONCURRENT"}, (
        f"both concurrent posts must land; the log holds {bodies}"
    )
    delivered = right_model.all_text()
    assert "FIRST-CONCURRENT" in delivered and "SECOND-CONCURRENT" in delivered, (
        "both texts reached the other member's model — an append on the far side of an "
        "await could drop one with nothing raised"
    )


# ── The edges ──


async def test_an_invented_kind_is_refused_as_text_and_nothing_is_logged_or_fanned() -> None:
    """A wrong kind is a mistake the model can fix: the refusal rides back as a successful
    tool result naming the real kinds, no entry lands, and nothing fans out."""
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                posting("epiphany", "NOT-A-KIND"),
                posting("obstacle", "NOW-A-REAL-ONE"),
                reading(APPROVING),
            ]
        )
        right_model = Counting([reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Team(lead, members, hooks=[Worklog(), Negotiation(rounds=1)])
        run = await team.run("go", h.worker.coordinator)

    assert [e["kind"] for e in run.hooks_data["worklog"]] == ["obstacle"], "only the real kind"
    assert "NOT-A-KIND" not in right_model.all_text(), "the refused post fanned out to nobody"
    refusal = "\n".join(left_model.tool_results(1))
    assert "no such kind" in refusal and "obstacle" in refusal, (
        "the refusal names the real kinds in the poster's own next context"
    )


async def test_every_declared_kind_is_accepted() -> None:
    """The vocabulary is closed but every word in it must work."""

    class _Team:
        hooks: list[Any] = []

    log = Worklog()
    work = Workspace(team=_Team(), request="r", coordinator=None, members=[])  # type: ignore[arg-type]
    for kind in DISCOVERY_KINDS:
        await log.post(work, kind, "body", "someone")
    assert [e["kind"] for e in work.data["worklog"]] == list(DISCOVERY_KINDS)
    assert DISCOVERY_KINDS == ("bears-on-teammate", "contradicts-plan", "obstacle", "dead-end")


async def test_a_member_with_its_own_config_hook_is_refused_when_the_worklog_needs_the_slot() -> (
    None
):
    """The one-hook-per-cycle conflict, at member scale: the runtime calls exactly one hook,
    so a member carrying its own and a team whose Worklog contributes member tools is a real
    conflict, refused before any spawn."""
    async with RuntimeHarness() as h:
        member = Member(
            Analyst("left"), "read", model=Counting([]), config_hook=lambda ctx: {"tools": []}
        )
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN")])
        with pytest.raises(RuntimeError, match="already carries a config_hook"):
            await Team(lead, [member], hooks=[Worklog()]).run("go", h.worker.coordinator)
    assert lead_model.contexts == [], "refused before the lead spent anything"


async def test_an_equipped_members_own_tools_survive_the_worklog_hook() -> None:
    """The hook's `tools` patch REPLACES compiled tools, so the member's own `tools=` must
    be recomposed in. Asserted from the wire: the member's model is offered both."""

    @strands_tool(name="own_tool", description="A tool the member brought itself.")
    async def own_tool() -> str:
        return "own"

    async with RuntimeHarness() as h:
        left_model = Counting([reading(APPROVING)])
        member = Member(Analyst("left"), "read", model=left_model, tools=[own_tool])
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        await Team(lead, [member], hooks=[Worklog(), Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    offered = left_model.offered_tools[0]
    assert "post_discovery" in offered, "the worklog tool arrived"
    assert "own_tool" in offered, "and the member's own tool was not replaced away"


async def test_a_second_run_on_the_same_hook_instance_starts_from_an_empty_worklog() -> None:
    """One hook instance, two runs: run 2's log opens empty and run 1's channels are gone —
    a log carried across runs would replay run 1's discoveries into run 2's freshly spawned
    threads."""
    async with RuntimeHarness() as h:
        worklog = Worklog()

        def fresh_members(other: str) -> tuple[list[Member], Counting]:
            # Left posts during round 1 and objects, so a round 2 exists — the step
            # boundary at which the peer's buffered notify drains into an observable
            # context. The peer's NAME differs between runs, deliberately: run 2 reusing
            # run 1's names would overwrite any stale channel under the same key and hide
            # a broken per-run reset (measured — the identical-cast variant passed with
            # the reset disabled).
            left_model = Counting(
                [posting("obstacle", "RUN-SPECIFIC"), reading("not yet"), reading(APPROVING)]
            )
            peer_model = Counting([reading(APPROVING), reading(APPROVING)])
            return [
                Member(Analyst("left"), "read", model=left_model),
                Member(Analyst(other), "read", model=peer_model),
            ], peer_model

        members1, _ = fresh_members("right")
        lead1, _ = scripted_lead([ruling("R1"), ruling("R1-REVISED")])
        hooks1 = [worklog, Negotiation(rounds=1)]
        first = await Team(lead1, members1, hooks=hooks1).run("one", h.worker.coordinator)

        members2, peer2 = fresh_members("center")
        lead2, lead2_model = scripted_lead([ruling("R2"), ruling("R2-REVISED")])
        hooks2 = [worklog, Negotiation(rounds=1)]
        second = await Team(lead2, members2, hooks=hooks2).run("two", h.worker.coordinator)

    assert len(first.hooks_data["worklog"]) == 1 and len(second.hooks_data["worklog"]) == 1, (
        "run 2's report is run 2's: one discovery each, no inherited entry"
    )
    assert first.hooks_data["worklog"] is not second.hooks_data["worklog"]
    entry2 = second.hooks_data["worklog"][0]
    assert set(entry2["delivered"]) == {"center-analyst.read", "lead"}, (
        f"run 2's fan-out must reach exactly run 2's cast — {entry2['delivered']} means a "
        f"stale run-1 channel survived the reset"
    )
    assert "right-analyst.read" not in entry2["failed"], (
        "a failure recorded against run 1's retired member IS the leaked channel"
    )
    # Run 2's post fanned into run 2's threads, and exactly once — no replayed duplicate.
    # The post lands mid-negotiation, so it drains at the next step boundary: the peer's
    # round-2 review and the lead's revision cycle.
    assert peer2.all_text().count("RUN-SPECIFIC") == 1
    assert "\n".join(lead2_model.prompts(1)).count("RUN-SPECIFIC") == 1
