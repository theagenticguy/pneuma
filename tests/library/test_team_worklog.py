"""Offline tests for the Team worklog: typed discoveries fanned back at step boundaries.

Every delivery claim is asserted from a scripted model's own captured context, never from the
returned `TeamRun` alone — the `render_brief` precedent, restated by `test_team_negotiation.py`
and load-bearing here too: a worklog entry saying a discovery was `delivered` is exactly what a
phase with a broken wire would also record. So the rendered discovery text is asserted *inside*
the other members' and the lead's model contexts, and the poster's exclusion is asserted as the
marker's absence from the poster's own contexts.

The tool-injection claims are asserted from the wire as well: `Counting` here records the
`tool_specs` each model call was offered, because "no tool injected when disabled" is a claim
about what the model could see, not about a config object nothing read.

`Counting` composes rather than subclasses (`ScriptedModel` is `@final`) and every fixture
output type is module level, because `compile_ai_method` resolves annotations against module
globals — both are `test_team.py`'s conventions, restated because this file is deliberately
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
from strands.tools.decorator import tool as strands_tool

from pneuma._team_legacy import DISCOVERY_KINDS, Member, Recruit, Roster, Team, TeamRun, Worklog
from pneuma.method import MethodAgent, ai_method

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
    """A typed member: the shape whose model context is the wire a discovery must reach."""

    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Chair(MethodAgent):
    """The lead. Its scripted model's contexts are where a replayed discovery must appear."""

    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported", max_attempts=3)
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class DeadReceiver:
    """A `Recruit` whose spawn handle exposes a `notify` that always raises.

    The retired-teammate fixture: `_open_channel` duck-types `notify` off the spawn handle, so
    this recruit gets a channel, and every fan-out to it fails the way a torn-down thread's
    would — `handle.notify` raises once the thread is gone. `ask` still answers, because the
    claim under test is that a dead *channel* is recorded and skipped, not that a dead member
    takes the run down (that path is `brief`'s, already pinned in `test_team.py`).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        return _DeadHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        # Approves any plan, so a negotiation round in the fixture ends unanimously — the
        # claim under test is the dead *channel*, not a stubborn reviewer.
        return f"nothing to report, {Team.APPROVAL}"

    async def retire(self) -> None:
        self.retirements += 1


class _DeadHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident

    async def notify(self, text: str) -> None:
        raise RuntimeError("the receiver's thread is retired")


# ── The model ──


class Counting(Model):
    """A scripted model recording every context AND every tool offer it saw.

    `tool_specs` is the addition over the sibling files' `Counting`: "the discovery tool was
    (not) injected" is a claim about what the model was offered on the wire, and only the
    model's own `stream` signature carries that (`strands/models/model.py:230`).
    """

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
        """Every text block from every context, for absence claims scoped to a whole thread."""
        return "\n".join(text for call in range(len(self.contexts)) for text in self.prompts(call))

    def tool_results(self, call: int) -> list[str]:
        """The text inside every toolResult block the model saw on its `call`-th invocation.

        A tool's refusal string travels back as a *successful tool result* (`_hiring`'s
        measurement), which lives under `content[].toolResult.content[].text` rather than as a
        plain text block — so a claim about what refusal the model read needs this extraction.
        """
        return [
            inner["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "toolResult" in block
            for inner in block["toolResult"].get("content", [])
            if "text" in inner
        ]


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


def posting(kind: str, body: str) -> Turn:
    """One assistant turn that posts one discovery. The cycle continues to the next turn."""
    return Turn(tool_calls=(("post_discovery", {"kind": kind, "body": body}),))


# ── The team under test ──


class Desk(Team):
    """The smallest worklog-capable team: injected cast and lead, `Bench`/`Toy` restated."""

    def __init__(
        self,
        *,
        cast: Sequence[Recruit] = (),
        lead: AIFunction[..., Any] | None = None,
        worklog_enabled: bool = False,
        negotiation_rounds: int = 0,
        name: str = "desk",
    ) -> None:
        super().__init__(
            name=name,
            worklog_enabled=worklog_enabled,
            negotiation_rounds=negotiation_rounds,
            roster=Roster(),
        )
        self._cast = list(cast)
        self._lead = lead

    def members(self) -> Sequence[Recruit]:
        return self._cast

    def briefing(self, member: Recruit) -> str:
        return f"Read your own source, {member.name}."

    def lead_function(self) -> AIFunction[..., Any]:
        assert self._lead is not None, "this test's Desk needs a lead"
        return self._lead

    def oracle(self, response: Any) -> None:
        if not getattr(response, "admitted", False):
            raise AssertionError("the ruling is not admitted; rule again")


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


APPROVING = f"looks right to me, {Team.APPROVAL}"
MARKER = "[team worklog]"

# ── 1. Off by default: no tool on the wire, no key in the artifact ──


async def test_disabled_by_default_no_tool_is_offered_and_the_artifact_shape_is_unchanged() -> None:
    """Backward compatibility asserted from the wire and the artifact, not from a config object.

    `offered_tools` is what the member's model was actually shown, so "no tool injected" is a
    claim the model itself witnesses; a config-level assertion would pass even if a hook leaked
    the tool onto every cycle. The serialised artifact must carry no `worklog` key at all — the
    published shape (`demo/warroom.py` pins nine keys) must not grow one on teams that never
    enabled the log.
    """
    async with RuntimeHarness() as h:
        member_model = Counting([reading("seen")])
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("left")])
        team = Desk(cast=members, lead=lead)  # worklog_enabled defaulted, deliberately

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")
        kinds = [str(getattr(e, "kind", "")) for e in await h.events(handle.id)]

    assert team.worklog_enabled is False, "off is the default, not a fixture choice"
    assert all("post_discovery" not in offer for offer in member_model.offered_tools), (
        "the discovery tool must not reach a member's model when the worklog is disabled"
    )
    assert all("post_discovery" not in offer for offer in lead_model.offered_tools)
    assert run.worklog == []
    assert team.worklog.channels == {}, "no channel was opened either"
    assert "team.discovery" not in kinds
    assert "worklog" not in json.loads(team.serialize_result(run)), (
        "an empty worklog must not change the serialised artifact's key set"
    )


# ── 2. A posted discovery reaches every OTHER member's next context, and the log ──


async def test_a_discovery_reaches_the_other_members_next_context_and_the_teamrun_worklog() -> None:
    """The delivery half is the load-bearing one, per the `render_brief` precedent.

    Left posts `THE-SHARED-CLUE` during its briefing cycle. Right's *next* model context — its
    negotiation review, the next step boundary its thread reaches — must carry the rendered
    discovery, because `notify` buffers into the thread's history and drains at the next model
    call (`ai_thread.py:465-476`). Asserted from right's captured context, because a `worklog`
    entry claiming `delivered` is exactly what a phase with a broken wire would also record.
    The lead's first context must carry it too: the lead's channel opens *after* the briefing
    phase, so this pins `register`'s replay of prior entries.
    """
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                posting("obstacle", "THE-SHARED-CLUE"),
                reading("left briefing"),
                reading(APPROVING),
            ]
        )
        right_model = Counting([reading("right briefing"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, lead_model = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=members, lead=lead, worklog_enabled=True, negotiation_rounds=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")
        # The progress marker is written by the *poster's* cycle context, so it lands on the
        # posting member's thread — the same attribution `team.hired` has on the lead's.
        poster_events = [str(getattr(e, "kind", "")) for e in await h.events(members[0].thread.id)]

    review = "\n".join(right_model.prompts(len(right_model.contexts) - 1))
    assert MARKER in review and "THE-SHARED-CLUE" in review, (
        "the discovery must actually appear in the other member's own model context — a "
        "worklog entry claiming delivery proves nothing about the wire"
    )
    assert "left-analyst.read" in review, "attributed to the member who posted it"
    lead_first = "\n".join(lead_model.prompts(0))
    assert MARKER in lead_first and "THE-SHARED-CLUE" in lead_first, (
        "the lead's channel opened after the post, so this is register's replay on the wire"
    )
    assert [e["kind"] for e in run.worklog] == ["obstacle"]
    assert run.worklog[0]["body"] == "THE-SHARED-CLUE"
    assert run.worklog[0]["source"] == "left-analyst.read"
    assert set(run.worklog[0]["delivered"]) == {"right-analyst.read", "lead"}
    assert run.worklog[0]["failed"] == {}
    assert "team.discovery" in poster_events, "the progress marker a live tape subscribes to"


# ── 3. The poster never receives its own discovery ──


async def test_the_poster_is_excluded_from_its_own_fan_out() -> None:
    """Asserted as absence from the poster's whole thread, which its review cycle makes real.

    The poster's thread reaches another step boundary (its negotiation review), so an
    inclusive fan-out would drain the marker into that context — the absence is checked where
    presence had every chance to appear. `delivered` must not name the poster either, and the
    channel map must still hold it: exclusion is per-post, not an unregistration.
    """
    async with RuntimeHarness() as h:
        left_model = Counting(
            [posting("dead-end", "MY-OWN-FINDING"), reading("briefed"), reading(APPROVING)]
        )
        right_model = Counting([reading("briefed"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=members, lead=lead, worklog_enabled=True, negotiation_rounds=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert MARKER not in left_model.all_text(), (
        "the poster already knows; an echo would spend its next context restating it"
    )
    assert "left-analyst.read" not in run.worklog[0]["delivered"]
    assert "left-analyst.read" in team.worklog.channels, (
        "excluded from its own post, not unregistered from the log"
    )
    assert MARKER in right_model.all_text(), "while the other member really was reached"


# ── 4. One dead channel: the failure is recorded and the fan-out continues ──


async def test_a_failing_notify_is_recorded_on_the_entry_and_the_rest_are_still_reached() -> None:
    """One member's channel raises mid-fan-out; the discovery must still reach everyone else.

    `DeadReceiver`'s handle raises from `notify` the way a retired thread's does, and it sits
    *first* in the cast so its failure precedes the healthy deliveries — a fan-out that stopped
    at the first fault would leave the later channels unreached, which is exactly the ordering
    this pins. The failure lands on the entry (`failed`), the run completes, the tool's own
    answer reaches the poster's model as a successful result, and nothing raises anywhere.
    """
    async with RuntimeHarness() as h:
        dead = DeadReceiver("fragile")
        left_model = Counting(
            [posting("contradicts-plan", "THE-WARNING"), reading("briefed"), reading(APPROVING)]
        )
        right_model = Counting([reading("briefed"), reading(APPROVING)])
        members = [
            dead,
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=members, lead=lead, worklog_enabled=True, negotiation_rounds=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    entry = run.worklog[0]
    assert "fragile" in entry["failed"], "the dead channel's failure is on the record"
    assert "retired" in entry["failed"]["fragile"], "with the error itself, for a reader"
    assert "fragile" not in entry["delivered"]
    assert "right-analyst.read" in entry["delivered"], (
        "the fan-out continued past the fault — one dead teammate must not stop the rest"
    )
    assert "lead" in entry["delivered"]
    assert MARKER in right_model.all_text(), "and the healthy member really was reached"
    assert run.verdict.admitted is True, "the run completed; a fan-out fault is never fatal"
    assert dead.retirements == 1, "the dead-channelled member was still retired by the unwind"


# ── 5. Two concurrent posts both land: reserve-before-await ──


async def test_two_posts_in_one_assistant_turn_both_land_with_no_lost_update() -> None:
    """The concurrent tool executor is the runtime's default, so two `post_discovery` calls in
    ONE assistant turn run as two interleaved tasks (`strands/agent/agent.py:462`), and every
    fan-out awaits. The entry is appended in the same synchronous stretch that builds it —
    reserve-before-await, the hiring seam's lesson — so both posts must be in the log, both
    texts must reach the other member, and neither may overwrite or drop the other.
    """
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                Turn(
                    tool_calls=(
                        ("post_discovery", {"kind": "obstacle", "body": "FIRST-CONCURRENT"}),
                        ("post_discovery", {"kind": "dead-end", "body": "SECOND-CONCURRENT"}),
                    )
                ),
                reading("briefed"),
                reading(APPROVING),
            ]
        )
        right_model = Counting([reading("briefed"), reading(APPROVING)])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=members, lead=lead, worklog_enabled=True, negotiation_rounds=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    bodies = {e["body"] for e in run.worklog}
    assert bodies == {"FIRST-CONCURRENT", "SECOND-CONCURRENT"}, (
        f"both concurrent posts must land; the log holds {bodies}"
    )
    assert all("right-analyst.read" in e["delivered"] for e in run.worklog)
    delivered = right_model.all_text()
    assert "FIRST-CONCURRENT" in delivered and "SECOND-CONCURRENT" in delivered, (
        "both texts reached the other member's model — a dict-keyed aggregation or an append "
        "on the far side of an await could drop one with nothing raised"
    )


# ── The edges the new seam creates ──


async def test_an_invented_kind_is_refused_as_text_and_nothing_is_logged_or_fanned() -> None:
    """A wrong kind is a mistake the model can fix, so it is text, for `_hiring`'s reason —
    and the refusal must be total: no entry, no fan-out, no event. The model reads the list of
    real kinds and posts again, which the second turn proves."""
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                posting("epiphany", "NOT-A-KIND"),
                posting("obstacle", "NOW-A-REAL-ONE"),
                reading("briefed"),
            ]
        )
        right_model = Counting([reading("briefed")])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=members, lead=lead, worklog_enabled=True)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["kind"] for e in run.worklog] == ["obstacle"], "only the real kind landed"
    assert "NOT-A-KIND" not in right_model.all_text(), "the refused post fanned out to nobody"
    assert "NOW-A-REAL-ONE" in run.worklog[0]["body"], "and the model recovered in-cycle"
    refusal = "\n".join(left_model.tool_results(1))
    assert "no such kind" in refusal and "obstacle" in refusal, (
        "the refusal names the real kinds in the poster's own next context — it rides back as "
        "a successful tool result, so the model reads it and can fix it"
    )


async def test_every_declared_kind_is_accepted() -> None:
    """The vocabulary is closed but every word in it must work — a kind the tool's check
    misspelled would refuse a legitimate post forever, invisibly, on every team."""
    log = Worklog()
    for kind in DISCOVERY_KINDS:
        await log.post(kind, "body", "someone")
    assert [e["kind"] for e in log.entries] == list(DISCOVERY_KINDS)
    assert DISCOVERY_KINDS == ("bears-on-teammate", "contradicts-plan", "obstacle", "dead-end")


async def test_a_member_with_its_own_config_hook_is_refused_by_equip() -> None:
    """The runtime calls exactly one hook per cycle (`ai_thread.py:548-553`), so a member
    constructed with its own `config_hook=` override and a team that equips the discovery tool
    is `_gated_lead`'s conflict at member scale — refused loudly, because either silent
    precedence costs tools invisibly."""
    member = Member(Analyst("left"), "read", config_hook=lambda ctx: {"tools": []})
    with pytest.raises(RuntimeError, match="already carries a config_hook"):
        member.equip(lambda ctx: {"tools": []})


async def test_an_equipped_members_own_tools_survive_the_worklog_hook() -> None:
    """The hook's `tools` patch REPLACES the compiled tools for the cycle (`config.py:166-185`,
    measured for hiring_tools), so `Member.spawn` must compose the member's own `tools=` back
    in — a member that carried tools must not lose them to a worklog it never asked about.
    Asserted from the wire: the member's model is offered both."""

    @strands_tool(name="own_tool", description="A tool the member brought itself.")
    async def own_tool() -> str:
        return "own"

    async with RuntimeHarness() as h:
        left_model = Counting([reading("briefed")])
        member = Member(Analyst("left"), "read", model=left_model, tools=[own_tool])
        lead, _ = scripted_lead([ruling("DRAFT-PLAN")])
        team = Desk(cast=[member], lead=lead, worklog_enabled=True)

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

    offered = left_model.offered_tools[0]
    assert "post_discovery" in offered, "the worklog tool arrived"
    assert "own_tool" in offered, "and the member's own tool was not replaced away"


async def test_a_second_run_on_the_same_handle_starts_from_an_empty_worklog() -> None:
    """The roster's lifetime argument, restated: every promise on the worklog is per run, and a
    log carried into run 2 would open run 2's report with run 1's discoveries and replay them
    into run 2's freshly spawned threads. One instance, one handle, two runs."""

    class PerRunLead(Desk):
        def __init__(self, scripts: Any, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._scripts = scripts

        def lead_function(self) -> AIFunction[..., Any]:
            return Chair().compiled("decide", model=Counting(next(self._scripts)))

    def fresh_cast() -> list[Recruit]:
        return [
            Member(
                Analyst("left"),
                "read",
                model=Counting([posting("obstacle", "RUN-SPECIFIC"), reading("briefed")]),
            )
        ]

    class PerRunCast(PerRunLead):
        def members(self) -> Sequence[Recruit]:
            return fresh_cast()

    async with RuntimeHarness() as h:
        scripts = iter([[ruling("R1")], [ruling("R2")]])
        team = PerRunCast(scripts, cast=[], lead=None, worklog_enabled=True)
        handle = await h.spawn(team, thread_name=team.name)

        first = await handle.run("one")
        log_after_first = team.worklog
        second = await handle.run("two")

    assert len(first.worklog) == 1 and len(second.worklog) == 1, (
        "run 2's report is run 2's: one discovery each, no inherited entry"
    )
    assert team.worklog is not log_after_first, "a fresh worklog, not a cleared one"


async def test_a_populated_worklog_survives_the_serialisation_round_trip() -> None:
    """The other half of the compat serializer: dropped when empty, kept when real — a
    serializer that always dropped the key would keep every artifact byte-compatible by
    deleting the worklog's only durable record."""
    run = TeamRun(
        verdict={"admitted": True},
        correct=True,
        oracle_failures=[],
        briefings={"a": "read"},
        hiring_log=[],
        worklog=[
            {
                "kind": "obstacle",
                "body": "b",
                "source": "a",
                "delivered": ["lead"],
                "failed": {},
            }
        ],
        input_tokens=1,
        output_tokens=1,
        turns=1,
        wall_seconds=0.1,
    )
    team = Desk(cast=[], lead=None)
    payload = json.loads(team.serialize_result(run))
    assert payload["worklog"][0]["body"] == "b", "a real worklog is in the artifact"
    assert team.deserialize_result(team.serialize_result(run)).worklog == run.worklog
