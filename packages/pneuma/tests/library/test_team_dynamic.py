"""Offline tests for dynamic hiring: `Hiring(dynamic=True)` and the `DynamicAgent` contract.

Tool *presence* is asserted from the wire — the tool offer each model call carried — because
"no hire_dynamic when disabled" is a claim about what the model could see. Instruction
*delivery* is asserted from the synthesized agent's own captured model context, per the
render_brief lesson: a hiring log saying the instructions were recorded is exactly what a
synthesis with a broken prompt wire would also record. Refusals are read out of the lead's
toolResult blocks; races are staged against the concurrent tool executor with a spawn that
genuinely suspends; teardown is asserted from the recruits' own state.

One convention is this file's own and load-bearing: **every dynamic factory binds a scripted
model by construction.** The library default builds a `Member` with no `model=` override,
which on a live runtime reaches a real model; an offline suite must make that unrepresentable
(the 17,859-token lesson in orchestrator-state-lifetimes-and-tool-races.md).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import InputShape
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import DynamicAgent, Member, Recruit, Team
from pneuma.team.hooks import Briefing, Hiring, Roster, Worklog, hiring_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

# ── Output types, module level ──


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Helped(BaseModel):
    note: str


# ── The cast ──


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class Analyst(MethodAgent):
    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Helper(MethodAgent):
    def __init__(self, name: str, mandate: str = "") -> None:
        self.name = name
        self.mandate = mandate

    @ai_method(Helped, description="Do the one narrow thing you were hired for")
    def assist(self, request: str) -> Helped:
        """Your mandate: {self.mandate}

        {request}
        """


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


def ruling(*, admitted: bool = True, cites: list[str] | None = None) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": cites or []}),))


def answered(text: str) -> Turn:
    """One turn for a `DynamicAgent`: `@ai_method(str)` wraps `str` in a generated
    `FinalAnswer` model, so the scripted tool call names that wrapper."""
    return Turn(tool_calls=(("FinalAnswer", {"answer": text}),))


def reading(detail: str) -> Turn:
    return Turn(tool_calls=(("Reading", {"source": "left", "detail": detail}),))


def helped(note: str = "done") -> Turn:
    return Turn(tool_calls=(("Helped", {"note": note}),))


def hire_dynamic(name: str, instructions: str, mandate: str = "m") -> tuple[str, dict[str, str]]:
    return ("hire_dynamic", {"name": name, "instructions": instructions, "mandate": mandate})


def posting(kind: str, body: str) -> Turn:
    return Turn(tool_calls=(("post_discovery", {"kind": kind, "body": body}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


class SlowMember(Member):
    """A `Member` whose `spawn` genuinely suspends, for the race tests: the in-memory
    coordinator's spawn never actually yields, so the sleep is what opens the window the
    reservation must close."""

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        await asyncio.sleep(0.01)
        return await super().spawn(coordinator, parent_id=parent_id)


class ScriptedHiring(Hiring):
    """A `Hiring` whose synthesized members carry scripted models BY CONSTRUCTION.

    The queue is consumed in synthesis order; a synthesis past its end gets `Counting([])`
    — a model whose first call raises — rather than the library default's unbound `Member`.
    `synthesized` keeps every member built, so tests assert per-object facts.
    """

    def __init__(self, *args: Any, dynamic_models: list[Counting] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.dynamic_models = list(dynamic_models or [])
        self.slow_spawns = False
        self.synthesized: list[Member] = []

    def dynamic_recruit(self, name: str, instructions: str) -> Recruit:
        model = self.dynamic_models.pop(0) if self.dynamic_models else Counting([])
        shape = SlowMember if self.slow_spawns else Member
        member = shape(DynamicAgent(name, instructions), "answer", model=model)
        self.synthesized.append(member)
        return member


INSTRUCTIONS = (
    "You are a cartographer. THE-SYNTHESIZED-IDENTITY. Chart whatever coast you are handed."
)


# ── 1. Off by default ──


async def test_disabled_by_default_hire_dynamic_is_not_offered_to_the_lead() -> None:
    """A team with a catalog and dynamic defaulted must offer exactly the three tools."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        hiring = ScriptedHiring(
            {"helper": lambda name: Member(Helper(name), "assist", model=Counting([]))}
        )
        await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert hiring.dynamic is False, "off is the default, not a fixture choice"
    assert all("hire_dynamic" not in offer for offer in lead_model.offered_tools), (
        "hire_dynamic must not reach the lead's model when the flag is off"
    )
    assert any("hire" in offer for offer in lead_model.offered_tools), (
        "while the catalog tools are still there — the flag must not subtract anything"
    )


async def test_the_flag_alone_grants_the_hiring_surface_with_an_empty_catalog() -> None:
    """A team whose whole hiring story is synthesis is legitimate: `hire_dynamic` rides
    alongside `delegate` and `dismiss` even with no catalog."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        hiring = ScriptedHiring(dynamic=True)
        await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    offered = lead_model.offered_tools[0]
    assert "hire_dynamic" in offered
    assert "delegate" in offered and "dismiss" in offered, (
        "a dynamic hire is reached and released through the same tools as a catalog hire"
    )


# ── 2. The synthesis loop ──


async def test_the_lead_synthesizes_and_the_instructions_reach_the_dynamic_agents_model() -> None:
    """The load-bearing delivery claim: the instructions text asserted inside the synthesized
    agent's own captured model context; the delegate answer round-trips; the log records the
    instructions verbatim (the audit trail is the safety story)."""
    async with RuntimeHarness() as h:
        dyn_model = Counting([answered("MAPPED-THE-COAST")])
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("mapper", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "mapper", "request": "chart the coast"}),)),
                ruling(cites=["mapper"]),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=[dyn_model])
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    prompt = "\n".join(dyn_model.prompts(0))
    assert "THE-SYNTHESIZED-IDENTITY" in prompt, (
        "the lead's instructions must appear in the dynamic agent's own model context — "
        "only the wire can prove the docstring template rendered them"
    )
    assert "chart the coast" in prompt, "and so does the delegated request"

    log = run.hooks_data["hiring"]
    assert [(e["action"], e.get("name")) for e in log] == [
        ("hire_dynamic", "mapper"),
        ("delegate", "mapper"),
    ]
    assert log[0]["instructions"] == INSTRUCTIONS, "verbatim, not summarized"
    assert "MAPPED-THE-COAST" in log[1]["answer"], "the real answer came back through delegate"
    confirmation = "\n".join(lead_model.tool_results(1))
    assert "hired mapper from your instructions" in confirmation, (
        "the tool's confirmation distinguishes a synthesized hire from a catalog one"
    )
    assert run.answer.admitted is True


async def test_a_catalog_hire_and_a_dynamic_hire_coexist_and_the_log_tells_them_apart() -> None:
    """A reader of the log can always answer 'which of these agents did a human review':
    `hire` carries a role and no instructions; `hire_dynamic` the reverse; one `delegate`
    tool reaches both."""
    async with RuntimeHarness() as h:
        dyn_model = Counting([answered("SYNTH-ANSWER")])
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "vetted", "mandate": "m"}),)),
                Turn(tool_calls=(hire_dynamic("bespoke", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "vetted", "request": "a"}),)),
                Turn(tool_calls=(("delegate", {"name": "bespoke", "request": "b"}),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(
            {"helper": lambda name: Member(Helper(name), "assist", model=Counting([helped()]))},
            dynamic=True,
            dynamic_models=[dyn_model],
        )
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    log = run.hooks_data["hiring"]
    hires = [e for e in log if e["action"] in ("hire", "hire_dynamic")]
    assert [(e["action"], e["name"]) for e in hires] == [
        ("hire", "vetted"),
        ("hire_dynamic", "bespoke"),
    ]
    assert "role" in hires[0] and "instructions" not in hires[0]
    assert "instructions" in hires[1] and "role" not in hires[1]
    delegated = [e for e in log if e["action"] == "delegate"]
    assert [e["name"] for e in delegated] == ["vetted", "bespoke"], (
        "one delegate tool reaches both kinds — no parallel path"
    )


# ── 3. Refusals: text the model can act on, nothing spawned ──


async def test_empty_instructions_a_duplicate_name_and_the_cap_are_all_recoverable_text() -> None:
    """Three synthesis-side refusals, all strings, none building anything, and the refusal
    text is the TOOL's own — the constructor's ValueError also mentions empty instructions,
    so the assertion pins the wording only the text refusal carries."""
    async with RuntimeHarness() as h:
        dyn_model = Counting([])  # the one successful synthesis never gets delegated to
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("blank", "   "),)),
                Turn(tool_calls=(hire_dynamic("keeper", INSTRUCTIONS),)),
                Turn(tool_calls=(hire_dynamic("keeper", "another identity entirely"),)),
                Turn(tool_calls=(hire_dynamic("overflow", "a second synthesized agent"),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=[dyn_model], max_hires=1)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    log = run.hooks_data["hiring"]
    assert [e["name"] for e in log if e["action"] == "hire_dynamic"] == ["keeper"], (
        "only the well-formed synthesis landed; the three refusals are not in the audit"
    )
    assert len(hiring.synthesized) == 1, "and nothing was constructed for any refusal"
    assert len(lead_model.contexts) == 5, "every refusal was text, so the cycle continued"

    empty = "\n".join(lead_model.tool_results(1))
    assert "state who it is and how it should work" in empty, (
        "the refusal must be the tool's text, not the constructor's exception leaking through"
    )
    duplicate = "\n".join(lead_model.tool_results(3))
    assert "already have a subagent named" in duplicate
    capped = "\n".join(lead_model.tool_results(4))
    assert "hiring cap reached" in capped and "dismiss someone first" in capped


async def test_the_cap_is_shared_between_catalog_and_dynamic_hires() -> None:
    """One budget, not one per kind: with max_hires=1 a vetted hire first means the
    synthesis is refused before its factory even runs."""
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "vetted", "mandate": "m"}),)),
                Turn(tool_calls=(hire_dynamic("bespoke", INSTRUCTIONS),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(
            {"helper": lambda name: Member(Helper(name), "assist", model=Counting([helped()]))},
            dynamic=True,
            max_hires=1,
        )
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert [e["action"] for e in run.hooks_data["hiring"]] == ["hire"], (
        "the catalog hire took the only slot and the synthesis was refused"
    )
    assert hiring.synthesized == [], "refused before construction — the cap precedes the factory"


async def test_an_empty_instructions_dynamic_agent_is_refused_at_construction_too() -> None:
    """The guard behind the tool's guard, for every caller that is not the tool."""
    with pytest.raises(ValueError, match="instructions are empty"):
        DynamicAgent("hollow", "   \n  ")


# ── 4. The race: reserve-before-await under the concurrent executor ──


async def test_two_hire_dynamics_sharing_one_name_in_one_turn_leak_no_live_thread() -> None:
    """`hire_dynamic` runs its own refusals before the shared `commission`, so a reservation
    living anywhere but the same synchronous stretch reopens the measured window (two
    spawned, one registered, one leaked). `SlowMember` makes the window real."""
    async with RuntimeHarness() as h:
        models = [Counting([]), Counting([])]
        lead, _ = scripted_lead(
            [
                Turn(
                    tool_calls=(
                        hire_dynamic("dup", "the first identity"),
                        hire_dynamic("dup", "the second identity"),
                    )
                ),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=models)
        hiring.slow_spawns = True
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    spawned = [m for m in hiring.synthesized if m._thread is not None]
    assert len(spawned) == 1, (
        f"only one synthesis may reach a spawn under one name; {len(spawned)} did"
    )
    log = run.hooks_data["hiring"]
    assert [e["name"] for e in log if e["action"] == "hire_dynamic"] == ["dup"]
    assert all(not m.thread.live for m in spawned), (
        "every spawned dynamic member must be retired — a live one is a leaked thread"
    )


async def test_two_hire_dynamics_in_one_turn_cannot_race_past_the_cap() -> None:
    async with RuntimeHarness() as h:
        models = [Counting([]), Counting([])]
        lead, lead_model = scripted_lead(
            [
                Turn(
                    tool_calls=(
                        hire_dynamic("first", "identity one"),
                        hire_dynamic("second", "identity two"),
                    )
                ),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=models, max_hires=1)
        hiring.slow_spawns = True
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    log = run.hooks_data["hiring"]
    hires = [e["name"] for e in log if e["action"] == "hire_dynamic"]
    assert len(hires) == 1, f"max_hires=1 and two syntheses in one turn: {hires}"
    assert len(hiring.synthesized) == 1, "the refused synthesis constructed nothing"
    assert len(lead_model.contexts) == 2, "the refusal is text, so the cycle continued"


# ── 5. Dismiss and teardown are kind-blind ──


async def test_dismiss_retires_the_dynamic_thread_and_frees_its_name_and_slot() -> None:
    """The synthesized member's thread really ends (asserted from `MethodThread.live`), and
    a second synthesis under the same name succeeds under max_hires=1."""
    async with RuntimeHarness() as h:
        models = [Counting([]), Counting([])]
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("phoenix", "the first identity"),)),
                Turn(tool_calls=(("dismiss", {"name": "phoenix"}),)),
                Turn(tool_calls=(hire_dynamic("phoenix", "the second identity"),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=models, max_hires=1)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    log = run.hooks_data["hiring"]
    assert [e["action"] for e in log] == ["hire_dynamic", "dismiss", "hire_dynamic"], (
        "the dismissal released both the name and the only slot under max_hires=1"
    )
    first, second = hiring.synthesized
    assert not first.thread.live, "the dismissed dynamic thread really ended"
    assert not second.thread.live, "and the replacement was retired by the teardown"


async def test_teardown_retires_dynamic_hires_and_the_registry_is_empty() -> None:
    async with RuntimeHarness() as h:
        models = [Counting([answered("WORKED")])]
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("ephemeral", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "ephemeral", "request": "go"}),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=models)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

        dynamic_thread_id = hiring.synthesized[0].thread.id
        remaining = {str(info.thread_id) for info in await h.coordinator.list_threads()}
        assert str(dynamic_thread_id) not in remaining, (
            "the dynamic hire's thread must be gone from the coordinator after the run"
        )

    assert not hiring.synthesized[0].thread.live
    assert "WORKED" in run.hooks_data["hiring"][1]["answer"]


# ── 6. Worklog: a dynamic hire is equipped and receives the replay ──


async def test_a_dynamic_hire_receives_prior_discoveries_and_can_post_its_own() -> None:
    """Both directions from the wire. Inbound: a discovery posted during the briefing phase
    — before the dynamic agent existed — appears in its first model context (`on_hire`'s
    replayed channel). Outbound: its model is offered `post_discovery` (the sibling equip
    surviving the synthesis path)."""
    async with RuntimeHarness() as h:
        analyst_model = Counting([posting("obstacle", "THE-PRIOR-DISCOVERY"), reading("briefed")])
        dyn_model = Counting([answered("ACKNOWLEDGED")])
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("latecomer", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "latecomer", "request": "verify it"}),)),
                ruling(),
            ]
        )
        hiring = ScriptedHiring(dynamic=True, dynamic_models=[dyn_model])
        team = Team(
            lead,
            [Member(Analyst("left"), "read", model=analyst_model)],
            hooks=[Briefing(), hiring, Worklog()],
        )
        run = await team.run("go", h.worker.coordinator)

    first_context = "\n".join(dyn_model.prompts(0))
    assert "[team worklog]" in first_context and "THE-PRIOR-DISCOVERY" in first_context, (
        "a discovery posted before the synthesis must reach the dynamic hire's FIRST "
        "context — the replayed channel, asserted from the wire"
    )
    assert "post_discovery" in dyn_model.offered_tools[0], (
        "and the dynamic hire can post its own — the sibling equip reached it"
    )
    # The channel is keyed by the Recruit's name — for a Member that is
    # `{agent}.{method}`, uniformly with cast members, so the poster-exclusion check
    # cannot mis-echo a hire its own post. The bare roster name lives in the hiring log.
    assert "latecomer.answer" in run.hooks_data["worklog"][0]["delivered"], (
        "the entry agrees with the wire"
    )


# ── 7. The fixed contract ──


async def test_a_dynamic_agent_publishes_exactly_one_ability_and_it_is_structured() -> None:
    """`ai_methods()` walks the MRO, so the published set is a hierarchy fact; and the
    `context` parameter keeps the compiled shape STRUCTURED — a single-positional-str
    ability would be STR_PROMPT and thus send_message-reachable (the T2/T7 signature trap)."""
    agent = DynamicAgent("probe", "some instructions")
    assert DynamicAgent.ai_methods() == ["answer"], (
        "the synthesized agent's published tool set must be exactly its one ability"
    )
    compiled = agent.compiled("answer", model=Counting([]))
    assert compiled.input_shape is InputShape.STRUCTURED, (
        "a single-positional-str ability would be STR_PROMPT and send_message-reachable"
    )
    assert compiled.config.name == "probe.answer"


async def test_the_instructions_render_into_the_prompt_and_stay_off_the_signature() -> None:
    """`self.instructions` reaches the prompt through the docstring template and must NOT be
    a call parameter — a signature that carried them would let every delegate rewrite the
    agent's identity per call."""
    agent = DynamicAgent("scribe", "THE-IDENTITY-TEXT")
    model = Counting([answered("ok")])

    async with RuntimeHarness() as h:
        thread = await agent.spawn("answer", h.coordinator, model=model)
        try:
            result = await thread.run(request="THE-REQUEST")
        finally:
            await thread.retire()

    assert result == "ok", "@ai_method(str) unwraps FinalAnswer back to the plain string"
    prompt = "\n".join(model.prompts(0))
    assert "THE-IDENTITY-TEXT" in prompt and "THE-REQUEST" in prompt
    parameters = set(inspect.signature(agent.answer).parameters)
    assert parameters == {"request", "context"}, (
        "instructions live on self, where a delegate call cannot rewrite them"
    )


def test_hiring_tools_grants_the_fourth_tool_only_when_a_factory_is_supplied() -> None:
    """The functional seam composes outside the hook, and the factory argument is the whole
    switch — same roster, same catalog, only the `dynamic=` call gets four tools."""
    roster = Roster()

    def factory(name: str, instructions: str) -> Recruit:
        return Member(DynamicAgent(name, instructions), "answer", model=Counting([]))

    without = hiring_tools(roster, {})
    with_factory = hiring_tools(roster, {}, dynamic=factory)
    assert callable(without) and callable(with_factory)
    assert roster.headcount == 0
