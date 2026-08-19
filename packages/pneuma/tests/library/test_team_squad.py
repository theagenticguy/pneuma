"""Offline tests for `team/squad.py`: a whole `Team` as one `Recruit`, so teams nest.

Every delivery claim is checked ON THE WIRE — from the contexts and `tool_specs` a recording
model actually received — never from the returned `TeamRun` alone (the render_brief lesson,
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
The nesting claim in particular: the inner lead's model must actually run, and the inner
run's threads must land as a subtree under the outer lead in the one coordinator registry.

`Counting` composes rather than subclasses — `ScriptedModel` is `@final` — and every fixture
output type is module level, because `compile_ai_method` resolves annotations with
`typing.get_type_hints` against module globals (`method.py:146`).
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
from pneuma.team import Member, Recruit, Team
from pneuma.team.squad import Squad

# ── Output types, module level for get_type_hints ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


# ── The cast: an outer commander, an inner chair, one analyst ──


class Analyst(MethodAgent):
    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source
        self.evidence = f"only the {source} record: {source.upper()}-1"

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}.

        Your private evidence: {self.evidence}
        """


class Chair(MethodAgent):
    """The inner team's lead; its thread is named `decide-lead` by the core."""

    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour. Consult the members you hold as tools."""


class Commander(MethodAgent):
    """The outer team's lead; a distinct method name so its thread (`command-lead`) is
    distinguishable from the inner lead's in the coordinator registry."""

    name = "commander"

    @ai_method(Ruling, description="Command the squads and rule")
    def command(self, question: str) -> Ruling:
        """Rule on {question}. Consult the squads you hold as tools."""


# ── Recording model: contexts AND tool_specs, both wire facts ──


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


def reading(source: str = "left") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": "seen"}),))


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def call_member(name: str, request: str = "report your evidence") -> Turn:
    return Turn(tool_calls=((name, {"request": request}),))


def scripted_chair(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


def scripted_commander(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Commander().compiled("command", model=model), model


# ── 1. The protocol ──


def test_a_squad_satisfies_the_recruit_protocol() -> None:
    """`Recruit` is runtime_checkable, so this is the same check the core's duck-typing
    relies on: name, spawn, ask, retire — all present on a bare `Squad`."""
    lead, _ = scripted_chair([ruling()])
    squad = Squad(Team(lead, []), "west")
    assert isinstance(squad, Recruit)
    assert squad.name == "west"


# ── 2. Nesting end to end ──


async def test_outer_lead_reaches_the_inner_teams_lead_and_member_through_one_squad_tool() -> None:
    """The whole nesting story, closed on the wire: the squad is a tool on the outer lead's
    wire under its own name, the outer lead's call runs the INNER lead's model, the inner
    lead consults its own member, and the answers chain back up. Every hop asserted from the
    model that carried it, not from `TeamRun` alone."""
    async with RuntimeHarness() as h:
        inner_member_model = Counting([reading("left")])
        inner_lead, inner_lead_model = scripted_chair(
            [call_member("left-analyst_read", "what does left show"), ruling(cites=["left"])]
        )
        inner = Team(inner_lead, [Member(Analyst("left"), "read", model=inner_member_model)])
        squad = Squad(inner, "west")

        outer_lead, outer_lead_model = scripted_commander(
            [call_member("west", "assess the west"), ruling(cites=["west"])]
        )
        run = await Team(outer_lead, [squad]).run("who holds the west", h.worker.coordinator)

    # A squad name has no dots, so the wire name IS the name (`_tool_name` maps, not renames).
    assert "west" in outer_lead_model.tool_specs[0], "the squad is a tool on the outer wire"
    # Two inner-lead cycles: the tool-calling turn, then the ruling after the tool result.
    assert len(inner_lead_model.contexts) == 2, "the outer lead's call ran the inner lead"
    assert any("assess the west" in p for p in inner_lead_model.prompts(0))
    assert len(inner_member_model.contexts) == 1, "the inner lead consulted its own member"
    assert run.answer.cites == ["west"], "the outer run's answer is the scripted chain's end"
    member_calls = [e for e in run.transcript if e["kind"] == "member"]
    assert [e["member"] for e in member_calls] == ["west"]
    assert "left" in member_calls[0]["answer"], "the inner ruling came back through the tool"
    # The run survives on the adapter for callers that want the inner audit trail.
    assert squad.last_run is not None and squad.last_run.answer.cites == ["left"]


# ── 3. Statelessness across asks ──


async def test_each_ask_is_a_fresh_run_and_the_second_carries_no_residue_of_the_first() -> None:
    """Two asks on one spawned squad are two full `Team.run`s. The inner lead's Counting
    instance is shared across both (each run compiles fresh from the same `AIFunction`, so
    contexts accumulate on the one model) — which is exactly what lets us assert the fresh
    thread: the second run's first context carries the second question and NOT the first
    run's answer marker. A `Member`'s thread would have carried both."""
    async with RuntimeHarness() as h:
        inner_lead, inner_lead_model = scripted_chair(
            [ruling(cites=["FIRST-RUN-MARKER"]), ruling(cites=["second"])]
        )
        squad = Squad(Team(inner_lead, []), "west")
        await squad.spawn(h.worker.coordinator)

        first = await squad.ask("first question")
        second = await squad.ask("second question")

    assert first.cites == ["FIRST-RUN-MARKER"] and second.cites == ["second"]
    assert len(inner_lead_model.contexts) == 2, "two asks, two fresh lead cycles"
    assert any("second question" in p for p in inner_lead_model.prompts(1))
    assert not any("FIRST-RUN-MARKER" in p for p in inner_lead_model.prompts(1)), (
        "the second run's first context carries residue of the first — the thread was reused"
    )
    assert squad.last_run is not None and squad.last_run.answer.cites == ["second"], (
        "last_run is the most recent ask's run"
    )


# ── 4. The unspawned refusal ──


async def test_ask_before_spawn_raises_naming_the_squad() -> None:
    lead, lead_model = scripted_chair([ruling()])
    squad = Squad(Team(lead, []), "west")
    with pytest.raises(RuntimeError, match="west.*not spawned"):
        await squad.ask("anything")
    assert lead_model.contexts == [], "refused before the inner team spent anything"


# ── 5. Retirement ──


async def test_retire_is_idempotent_and_a_retired_squad_refuses_asks() -> None:
    lead, _ = scripted_chair([ruling()])
    squad = Squad(Team(lead, []), "west")
    await squad.spawn(object())  # spawn only stores wiring; no thread exists yet
    await squad.retire()
    await squad.retire()  # the unwind loop calls retire unconditionally; twice must not raise
    with pytest.raises(RuntimeError, match="west.*not spawned"):
        await squad.ask("anything")


# ── 6. One subtree in one event log ──


async def test_the_nested_run_is_one_subtree_under_the_outer_lead() -> None:
    """`parent_id` chains through: the squad stores the outer lead's handle id at spawn and
    passes it to `Team.run`, so the inner lead spawns as a CHILD of the outer lead and the
    inner member as a child of the inner lead. Asserted from the coordinator's own
    `list_threads()` registry (`ThreadInfo.parent_id`, the public discovery surface) —
    snapshotted MID-RUN by an inner-team `on_assemble` hook, because every thread is
    deregistered by the unwind and the registry is empty after the run (the core's teardown
    test pins exactly that)."""
    snapshot: list[Any] = []

    class Census:
        async def on_assemble(self, work: Any) -> None:
            snapshot.extend(await work.coordinator.list_threads())

    async with RuntimeHarness() as h:
        inner_lead, _ = scripted_chair([call_member("left-analyst_read"), ruling(cites=["left"])])
        inner = Team(
            inner_lead,
            [Member(Analyst("left"), "read", model=Counting([reading()]))],
            hooks=[Census()],
        )
        outer_lead, _ = scripted_commander([call_member("west"), ruling(cites=["west"])])
        await Team(outer_lead, [Squad(inner, "west")]).run("go", h.worker.coordinator)

    by_name = {i.thread_name: i for i in snapshot}
    outer = by_name["command-lead"]
    inner_info = by_name["decide-lead"]
    assert outer.parent_id is None, "the outer lead is the subtree's root"
    assert inner_info.parent_id == outer.thread_id, "the inner lead is the outer lead's child"
    grandchildren = [i for i in snapshot if i.parent_id == inner_info.thread_id]
    assert grandchildren, "the inner member sits under the inner lead — three generations deep"
