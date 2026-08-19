"""Offline tests: no Bedrock calls, no network. Every model is scripted."""

from __future__ import annotations

from typing import ClassVar

import pytest
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import EventKind, InputShape
from pydantic import BaseModel
from rich.console import Console
from strands.tools.decorator import tool as strands_tool

from pneuma.demo import incident
from pneuma.demo.agent import ROSTER, Agent
from pneuma.demo.cast import Historian, IncidentLead, Specialist
from pneuma.demo.staffing import Staff, staffing_tools


class Answer(BaseModel):
    answer: str


class Echo(Agent):
    role: ClassVar[str] = "echo"
    purpose: ClassVar[str] = "repeats things"
    result_type: ClassVar[type] = str

    def __init__(self, *, prefix: str = "", name: str | None = None) -> None:
        super().__init__(name=name)
        self.prefix = prefix

    def brief(self, request: str) -> str:
        return f"{self.prefix}{request}"


# ── Compilation ──


def test_instance_state_reaches_the_prompt() -> None:
    """Two instances of one class produce two different prompts."""
    a = Echo(prefix="A: ", name="a").build()
    b = Echo(prefix="B: ", name="b").build()
    assert a.prompt_fn("hello") == "A: hello"
    assert b.prompt_fn("hello") == "B: hello"


def test_compiled_shape_is_addressable_by_peers() -> None:
    """STR_PROMPT is the only shape the runtime's send_message can reach."""
    for agent in (Echo(), Specialist("metrics"), IncidentLead(specialists=["metrics"])):
        assert agent.build().input_shape is InputShape.STR_PROMPT


def test_compiled_name_and_description_survive() -> None:
    fn = Echo(name="scribe").build()
    assert fn.name == "scribe"
    assert fn.config.description == "repeats things"
    assert fn.config.thread_name == "scribe"


def test_subclasses_register_only_when_hireable() -> None:
    assert {"historian", "skeptic", "correlator"} <= set(ROSTER)
    assert "specialist" not in ROSTER  # holds private evidence; the lead cannot conjure one
    assert "lead" not in ROSTER  # a lead hiring a lead is a loop we do not want


def test_specialists_carry_disjoint_evidence() -> None:
    planes = [Specialist(p) for p in ("deploys", "metrics", "logs", "traces")]
    bodies = [p.evidence for p in planes]
    assert len({*bodies}) == len(bodies)
    for p in planes:
        assert p.evidence and p.evidence in p.brief("go")
        for other in planes:
            if other.plane != p.plane:
                assert other.evidence not in p.brief("go")


def test_effort_defaults_to_xhigh_and_is_overridable() -> None:
    assert Specialist("logs").effort == "xhigh"
    assert Specialist("logs", effort="low").effort == "low"
    assert Historian().effort == "high"


def test_unspawned_agent_refuses_to_hand_out_a_handle() -> None:
    with pytest.raises(RuntimeError, match="has not been spawned"):
        _ = Echo().handle


# ── The staffing tools we added on top of the library ──


class ScriptedHelper(Agent):
    """A hireable role whose model is scripted, so `delegate` never hits Bedrock."""

    role: ClassVar[str] = "scripted-helper"
    purpose: ClassVar[str] = "test double"
    result_type: ClassVar[type] = str
    mandate: str = ""
    reply: ClassVar[str] = "onset was 14:20"

    def model(self) -> object:
        return ScriptedModel([Turn(text=self.reply)])

    def build(self):  # type: ignore[override]
        return super().build().replace(structured_output=False)

    def brief(self, request: str) -> str:
        return request


async def test_hire_delegate_dismiss_round_trip() -> None:
    """A scripted agent hires a subagent, delegates, and reads the answer back."""
    staff = Staff()
    fn = (
        Echo(name="boss")
        .build()
        .replace(
            model=ScriptedModel(
                [
                    Turn(
                        tool_calls=(
                            (
                                "hire",
                                {
                                    "role": "scripted-helper",
                                    "name": "h1",
                                    "mandate": "order events",
                                },
                            ),
                        )
                    ),
                    Turn(
                        tool_calls=(("delegate", {"name": "h1", "request": "when did it start"}),)
                    ),
                    Turn(tool_calls=(("dismiss", {"name": "h1"}),)),
                    Turn(text="done"),
                ]
            ),
            config_hook=staffing_tools(staff, allow=["scripted-helper"]),
            structured_output=False,
        )
    )
    async with RuntimeHarness() as h:
        handle = await h.spawn(fn, thread_name="boss")
        await handle.run("investigate")

    actions = [entry["action"] for entry in staff.log]
    assert actions == ["hire", "delegate", "dismiss"]
    delegated = next(e for e in staff.log if e["action"] == "delegate")
    assert ScriptedHelper.reply in delegated["answer"]
    assert staff.headcount == 0


async def test_hire_writes_a_parent_edge_into_the_hiring_agents_log() -> None:
    """parent_id is what makes token rollup work; assert the edge exists."""
    staff = Staff()
    fn = (
        Echo(name="boss")
        .build()
        .replace(
            model=ScriptedModel(
                [
                    Turn(
                        tool_calls=(
                            ("hire", {"role": "scripted-helper", "name": "h", "mandate": "m"}),
                        )
                    ),
                    Turn(text="ok"),
                ]
            ),
            config_hook=staffing_tools(staff),
            structured_output=False,
        )
    )
    async with RuntimeHarness() as h:
        handle = await h.spawn(fn, thread_name="boss")
        await handle.run("go")
        spawned = await h.events(handle.id, kinds=[EventKind.THREAD_SPAWNED])

    child_ids = {str(e.child_thread_id) for e in spawned}
    assert str(staff.thread_ids["h"]) in child_ids


async def test_hiring_cap_is_enforced() -> None:
    staff = Staff()
    turns = [
        Turn(tool_calls=(("hire", {"role": "scripted-helper", "name": f"h{i}", "mandate": "m"}),))
        for i in range(3)
    ]
    turns.append(Turn(text="ok"))
    fn = (
        Echo(name="boss")
        .build()
        .replace(
            model=ScriptedModel(turns),
            config_hook=staffing_tools(staff, max_hires=2),
            structured_output=False,
        )
    )
    async with RuntimeHarness() as h:
        handle = await h.spawn(fn, thread_name="boss")
        await handle.run("go")

    assert len([e for e in staff.log if e["action"] == "hire"]) == 2


async def test_unknown_role_is_rejected_without_raising() -> None:
    """A bad tool call must come back as text the model can recover from."""
    staff = Staff()
    fn = (
        Echo(name="boss")
        .build()
        .replace(
            model=ScriptedModel(
                [
                    Turn(tool_calls=(("hire", {"role": "wizard", "name": "w", "mandate": "m"}),)),
                    Turn(text="recovered"),
                ]
            ),
            config_hook=staffing_tools(staff),
            structured_output=False,
        )
    )
    async with RuntimeHarness() as h:
        handle = await h.spawn(fn, thread_name="boss")
        result = await handle.run("go")

    assert "recovered" in str(result)
    assert not staff.hires


async def test_allow_list_narrows_the_catalog() -> None:
    staff = Staff()
    fn = (
        Echo(name="boss")
        .build()
        .replace(
            model=ScriptedModel(
                [
                    Turn(tool_calls=(("hire", {"role": "skeptic", "name": "s", "mandate": "m"}),)),
                    Turn(text="ok"),
                ]
            ),
            config_hook=staffing_tools(staff, allow=["historian"]),
            structured_output=False,
        )
    )
    async with RuntimeHarness() as h:
        handle = await h.spawn(fn, thread_name="boss")
        await handle.run("go")

    assert not staff.hires


# ── The oracle ──


def test_ground_truth_passes_and_wrong_answers_fail() -> None:
    truth = incident.GROUND_TRUTH
    assert incident.verify(truth.culprit_service, truth.culprit_change_id, truth.mechanism) == []
    assert incident.verify("not-a-service", truth.culprit_change_id, truth.mechanism)
    assert incident.verify(truth.culprit_service, "chg-nonexistent", truth.mechanism)
    wrong = next(m for m in incident.MECHANISMS if m != truth.mechanism)
    assert incident.verify(truth.culprit_service, truth.culprit_change_id, wrong)


def test_every_plane_alone_is_ambiguous() -> None:
    """The demo is only interesting if no single plane can solve it."""
    ambiguity = incident.single_plane_ambiguity()
    assert len(ambiguity) == 4
    for plane, candidates in ambiguity.items():
        assert len(candidates) >= 2, f"{plane} alone identifies the cause; asymmetry broken"


def test_self_check_holds() -> None:
    incident.self_check()


# ── The CLI teardown path (a real bug lived here) ──


async def test_tape_subscription_uses_the_real_teardown_api() -> None:
    """`Subscription` exposes unsubscribe(), not close(). A wrong call here only
    surfaces after the whole investigation has finished, discarding the result."""
    from ai_functions import InMemoryCoordinator

    from pneuma.demo.live import Tape

    coordinator = InMemoryCoordinator()
    tape = Tape(console=Console(record=True))
    subscription = tape.watch(coordinator)
    subscription.unsubscribe()
    subscription.unsubscribe()  # idempotent by contract


# ── Method tools: @tool on a method, bound to instance state ──


class Toolful(Agent):
    """Base contributing one inherited tool."""

    role: ClassVar[str] = "toolful"
    result_type: ClassVar[type] = str
    hireable: ClassVar[bool] = False

    def brief(self, request: str) -> str:
        return request

    @strands_tool
    def inherited(self) -> str:
        """A tool defined on the base class."""
        return "base"


class ToolfulChild(Toolful):
    role: ClassVar[str] = "toolful-child"
    hireable: ClassVar[bool] = False

    def __init__(self, secret: str, *, name: str | None = None) -> None:
        super().__init__(name=name)
        self.secret = secret

    @strands_tool
    def reveal(self, count: int) -> str:
        """Return the first `count` characters of this instance's secret."""
        return self.secret[:count]


def test_method_tools_are_discovered_across_the_mro() -> None:
    """A subclass sees its own tools and its base's, not one or the other."""
    names = {t.tool_name for t in ToolfulChild("abcdef").tools()}
    assert names == {"inherited", "reveal"}


def test_method_tools_hide_self_from_the_model() -> None:
    """`self` must not appear in the schema, or the model would try to supply it."""
    reveal = next(t for t in ToolfulChild("abcdef").tools() if t.tool_name == "reveal")
    assert set(reveal.tool_spec["inputSchema"]["json"]["properties"]) == {"count"}


def test_method_tools_bind_to_their_own_instance() -> None:
    """Two instances get two tool objects, each closing over its own state."""
    a, b = ToolfulChild("aaa-1"), ToolfulChild("bbb-2")
    ta = next(t for t in a.tools() if t.tool_name == "reveal")
    tb = next(t for t in b.tools() if t.tool_name == "reveal")
    assert ta is not tb
    assert ta(5) == "aaa-1"
    assert tb(5) == "bbb-2"


def test_specialist_tool_reads_only_its_own_plane() -> None:
    """The information asymmetry has to survive being reachable through a tool."""
    metrics, logs = Specialist("metrics"), Specialist("logs")
    assert metrics.search_plane("checkout") != logs.search_plane("checkout")
    assert metrics.search_plane("no-such-record").startswith("no metrics record")


def test_lead_keeps_injected_tools_alongside_method_tools() -> None:
    """Overriding `tools()` must extend the method tools, not replace them."""
    lead = IncidentLead(specialists=["metrics"], tool_list=["injected-sentinel"])
    assert "injected-sentinel" in lead.tools()
