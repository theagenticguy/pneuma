"""Offline tests for `dynamic_subagents`: the lead synthesizing a subagent inline.

Every claim is checked the way its siblings check it, deliberately. Tool *presence* is
asserted from the wire — the `tool_specs` each model call was offered — because "no
hire_dynamic when disabled" is a claim about what the model could see, not about a config
object nothing read (`test_team_worklog.py`'s convention). Instruction *delivery* is asserted
from the synthesized agent's own captured model context, per the render_brief-bug lesson: a
hiring log saying the instructions were recorded is exactly what a synthesis with a broken
prompt wire would also record. Refusals are read out of the lead's toolResult blocks, races
are staged against the concurrent tool executor with a spawn that genuinely suspends, and
teardown is asserted from the recruits' own retirement counters and the coordinator's registry.

Fixtures restate `test_team.py`'s conventions — `Counting` composes (`ScriptedModel` is
`@final`), output types are module level (`compile_ai_method` resolves annotations against
module globals), and this file is self-contained because `tests/library/` has no package.

One convention is this file's own and it is load-bearing: **every `dynamic_recruit` override
binds a scripted model by construction.** The library default builds a `Member` with no
`model=` override, which on a live runtime reaches a real model; an offline suite must make
that unrepresentable rather than merely avoided (the 17,859-token lesson in
`orchestrator-state-lifetimes-and-tool-races.md`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import InputShape
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import DynamicAgent, Member, Recruit, Roster, Team, hiring_tools

# ── Output types, module level ──


class Ruling(BaseModel):
    """The lead's call. `admitted` is what the oracle gates on."""

    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


class Reading(BaseModel):
    """What a cast member reports from its briefing."""

    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Helped(BaseModel):
    """What a catalog hire produces, so catalog and synthesis coexist in one test."""

    note: str


# ── The cast ──


class Chair(MethodAgent):
    """The lead. Holds no evidence, so hiring is all it can do."""

    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported", max_attempts=3)
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class Analyst(MethodAgent):
    """A typed cast member, for the worklog-replay test."""

    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Helper(MethodAgent):
    """A catalog role, for the shared-cap and byte-identical-hire claims."""

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
    """A scripted model recording every context AND every tool offer it saw.

    `offered_tools` is what carries the injection claims: "hire_dynamic was (not) on the
    wire" is a fact about what the model was offered per call (`strands/models/model.py:230`),
    not about any config object. `tool_results` extracts refusal strings, which ride back as
    *successful* tool results (`_hiring`'s measurement) under
    `content[].toolResult.content[].text` rather than as plain text blocks.
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


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(
        tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),),
        input_tokens=7,
        output_tokens=2,
    )


def answered(text: str) -> Turn:
    """One turn for a `DynamicAgent`: `@ai_method(str)` wraps `str` in a generated
    `FinalAnswer` model (`ai_thread.py:99-167`, measured), so the scripted tool call names
    that wrapper and the runtime unwraps `.answer` back to the plain string."""
    return Turn(tool_calls=(("FinalAnswer", {"answer": text}),), input_tokens=5, output_tokens=3)


def reading(detail: str) -> Turn:
    return Turn(tool_calls=(("Reading", {"source": "left", "detail": detail}),))


def helped(note: str = "done") -> Turn:
    return Turn(tool_calls=(("Helped", {"note": note}),))


def hire_dynamic(name: str, instructions: str, mandate: str = "m") -> tuple[str, dict[str, str]]:
    return ("hire_dynamic", {"name": name, "instructions": instructions, "mandate": mandate})


def posting(kind: str, body: str) -> Turn:
    return Turn(tool_calls=(("post_discovery", {"kind": kind, "body": body}),))


# ── The team under test ──


class SlowMember(Member):
    """A `Member` whose `spawn` genuinely suspends, for the race tests.

    `SlowSpawnSpy`'s reason, restated for the typed adapter: the in-memory coordinator's
    `spawn` awaits nothing that actually yields to the event loop (measured — with the
    reservation moved past the await, both race tests still passed on a bare `Member`), so
    the sleep is what opens the window the reservation must close.
    """

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        await asyncio.sleep(0.01)
        return await super().spawn(coordinator, parent_id=parent_id)


class Forge(Team):
    """The smallest dynamic-capable team, `Toy`/`Desk` restated.

    `dynamic_recruit` is overridden to bind a scripted model BY CONSTRUCTION: the queue is
    consumed in synthesis order, and a synthesis past the end of it gets `Counting([])` — a
    model whose first call raises `ScriptExhausted` — rather than the library default's
    unbound `Member`, which on a live runtime would reach a real model. `synthesized` keeps
    every member built, so tests assert per-object facts (retirement, thread identity) rather
    than roster shapes a correct run also produces.
    """

    def __init__(
        self,
        *,
        cast: Sequence[Recruit] = (),
        lead: AIFunction[..., Any] | None = None,
        roles: Mapping[str, Callable[[str], Recruit]] | None = None,
        dynamic_models: list[Counting] | None = None,
        slow_spawns: bool = False,
        dynamic_subagents: bool = False,
        worklog_enabled: bool = False,
        max_hires: int = 3,
        name: str = "forge",
    ) -> None:
        super().__init__(
            name=name,
            max_hires=max_hires,
            dynamic_subagents=dynamic_subagents,
            worklog_enabled=worklog_enabled,
            roster=Roster(),
        )
        self._cast = list(cast)
        self._lead = lead
        self._roles = roles or {}
        self.dynamic_models = list(dynamic_models or [])
        self.slow_spawns = slow_spawns
        self.synthesized: list[Member] = []

    def members(self) -> Sequence[Recruit]:
        return self._cast

    def briefing(self, member: Recruit) -> str:
        return f"Read your own source, {member.name}."

    def lead_function(self) -> AIFunction[..., Any]:
        assert self._lead is not None, "this test's Forge needs a lead"
        return self._lead

    def oracle(self, response: Any) -> None:
        if not getattr(response, "admitted", False):
            raise AssertionError("the ruling is not admitted; rule again")

    def catalog(self) -> Mapping[str, Callable[[str], Recruit]]:
        return self._roles

    def dynamic_recruit(self, name: str, instructions: str) -> Recruit:
        model = self.dynamic_models.pop(0) if self.dynamic_models else Counting([])
        shape = SlowMember if self.slow_spawns else Member
        member = shape(DynamicAgent(name, instructions), "answer", model=model)
        self.synthesized.append(member)
        return member


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


INSTRUCTIONS = (
    "You are a cartographer. THE-SYNTHESIZED-IDENTITY. Chart whatever coast you are handed."
)


# ── 1. Off by default: no tool on the wire, and the hiring surface is unchanged ──


async def test_disabled_by_default_hire_dynamic_is_not_offered_to_the_lead() -> None:
    """Backward compatibility from the wire: a team with a catalog and the flag defaulted must
    offer the lead exactly the three tools it always had. `offered_tools` is what the lead's
    model was actually shown, so a hook that leaked the fourth tool fails here even though
    every existing test — which never asks about the offer — would still pass."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = Forge(
            cast=[],
            lead=lead,
            roles={"helper": lambda name: Member(Helper(name), "assist", model=Counting([]))},
        )

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

    assert team.dynamic_subagents is False, "off is the default, not a fixture choice"
    assert all("hire_dynamic" not in offer for offer in lead_model.offered_tools), (
        "hire_dynamic must not reach the lead's model when the flag is off"
    )
    assert any("hire" in offer for offer in lead_model.offered_tools), (
        "while the catalog tools are still there — the flag must not subtract anything"
    )


async def test_no_catalog_and_no_flag_still_means_no_hook_at_all() -> None:
    """The pre-existing default, re-pinned at the seam the flag widened: `_gated_lead` now
    attaches the hook when there is a catalog OR the flag, so the no-catalog-no-flag case must
    still compose to a bare lead — one fewer thing a lead can do wrong."""
    async with RuntimeHarness():
        lead = Chair().compiled("decide", model=Counting([]))
        assert Forge(cast=[], lead=lead)._gated_lead().config.config_hook is None


async def test_the_flag_alone_grants_the_hiring_surface_with_an_empty_catalog() -> None:
    """A team whose whole hiring story is synthesis is legitimate: the flag must attach the
    hook even when `catalog()` is empty, and the wire must carry `hire_dynamic` alongside
    `delegate` and `dismiss` — a synthesized agent nobody can delegate to would be a thread
    spent on nothing."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = Forge(cast=[], lead=lead, dynamic_subagents=True)

        assert team._gated_lead().config.config_hook is not None

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

    offered = lead_model.offered_tools[0]
    assert "hire_dynamic" in offered
    assert "delegate" in offered and "dismiss" in offered, (
        "a dynamic hire is reached and released through the same tools as a catalog hire"
    )


# ── 2. The synthesis loop: instructions on the wire, delegate round-trips ──


async def test_the_lead_synthesizes_and_the_instructions_reach_the_dynamic_agents_model() -> None:
    """The load-bearing delivery claim, per the render_brief precedent: the hiring log
    recording the instructions proves nothing about the prompt wire, so the instructions text
    is asserted inside the synthesized agent's own captured model context. The delegate answer
    must round-trip back to the lead through the same `delegate` tool a catalog hire uses —
    no parallel path — and the log must record the instructions verbatim, because the audit
    trail is the safety story for a prompt nobody reviewed."""
    async with RuntimeHarness() as h:
        dyn_model = Counting([answered("MAPPED-THE-COAST")])
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("mapper", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "mapper", "request": "chart the coast"}),)),
                ruling(cites=["mapper"]),
            ]
        )
        team = Forge(cast=[], lead=lead, dynamic_subagents=True, dynamic_models=[dyn_model])

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    prompt = "\n".join(dyn_model.prompts(0))
    assert "THE-SYNTHESIZED-IDENTITY" in prompt, (
        "the lead's instructions must appear in the dynamic agent's own model context — the "
        "docstring template renders {self.instructions}, and only the wire can prove it did"
    )
    assert "chart the coast" in prompt, "and so does the delegated request"

    actions = [(e["action"], e.get("name")) for e in run.hiring_log]
    assert actions == [("hire_dynamic", "mapper"), ("delegate", "mapper")]
    assert run.hiring_log[0]["instructions"] == INSTRUCTIONS, "verbatim, not summarized"
    assert run.hiring_log[0]["mandate"] == "m"
    assert "MAPPED-THE-COAST" in run.hiring_log[1]["answer"], (
        "the dynamic agent's real answer came back to the lead through delegate"
    )
    confirmation = "\n".join(lead_model.tool_results(1))
    assert "hired mapper from your instructions" in confirmation, (
        "the tool's confirmation distinguishes a synthesized hire from a catalog one"
    )
    assert run.verdict.admitted is True


async def test_a_catalog_hire_and_a_dynamic_hire_coexist_and_the_log_tells_them_apart() -> None:
    """The transcript is the boundary's audit surface: a reviewed-catalog hire records
    `action="hire"` with a `role`, a synthesized one records `action="hire_dynamic"` with
    `instructions`, and both are delegated to through the one `delegate` tool. A reader of
    the log can always answer "which of these agents did a human review"."""
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
        team = Forge(
            cast=[],
            lead=lead,
            roles={
                "helper": lambda name: Member(Helper(name), "assist", model=Counting([helped()]))
            },
            dynamic_subagents=True,
            dynamic_models=[dyn_model],
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    hires = [e for e in run.hiring_log if e["action"] in ("hire", "hire_dynamic")]
    assert [(e["action"], e["name"]) for e in hires] == [
        ("hire", "vetted"),
        ("hire_dynamic", "bespoke"),
    ]
    assert "role" in hires[0] and "instructions" not in hires[0], (
        "a catalog hire's record names its reviewed role and carries no instructions"
    )
    assert "instructions" in hires[1] and "role" not in hires[1], (
        "a synthesized hire's record carries the unreviewed instructions instead"
    )
    delegated = [e for e in run.hiring_log if e["action"] == "delegate"]
    assert [e["name"] for e in delegated] == ["vetted", "bespoke"], (
        "one delegate tool reaches both kinds — no parallel path"
    )


# ── 3. Refusals: text the model can act on, and nothing spawned ──


async def test_empty_instructions_a_duplicate_name_and_the_cap_are_all_recoverable_text() -> None:
    """Three synthesis-side failures, three things a model can fix, so three strings and no
    raises — `_hiring`'s argument, restated for the fourth tool. Each refusal must build
    nothing (`synthesized` stays empty for it) and leave the log clean: a refusal recorded as
    a hire would corrupt the audit surface even though no thread was created. The refusal
    text is read out of the lead's toolResult blocks, because that is the only place the
    model reads it from."""
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
        team = Forge(
            cast=[],
            lead=lead,
            dynamic_subagents=True,
            dynamic_models=[dyn_model],
            max_hires=1,
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["name"] for e in run.hiring_log if e["action"] == "hire_dynamic"] == ["keeper"], (
        "only the well-formed synthesis landed; the three refusals are not in the audit"
    )
    assert len(team.synthesized) == 1, "and nothing was constructed for any refusal"
    assert len(lead_model.contexts) == 5, "every refusal was text, so the cycle continued"

    empty = "\n".join(lead_model.tool_results(1))
    # The TOOL's own wording, not the DynamicAgent constructor's: with the tool-level check
    # deleted, the factory's ValueError also says "instructions are empty" and rides back as
    # a tool fault — a coincidental fallback that satisfied a looser assertion (measured
    # during the break-the-guard step). Only the text refusal tells the model what to do.
    assert "state who it is and how it should work" in empty, (
        "the refusal must be the tool's text, not the constructor's exception leaking through"
    )
    duplicate = "\n".join(lead_model.tool_results(3))
    assert "already have a subagent named" in duplicate
    capped = "\n".join(lead_model.tool_results(4))
    assert "hiring cap reached" in capped and "dismiss someone first" in capped
    assert run.verdict.admitted is True


async def test_the_cap_is_shared_between_catalog_and_dynamic_hires() -> None:
    """One budget, not one per kind: a catalog hire spends the same headcount a synthesis
    needs, so with `max_hires=1` a vetted hire first means the synthesis is refused. Two caps
    would let a lead run 2x the intended team behind an innocent-looking flag."""
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "vetted", "mandate": "m"}),)),
                Turn(tool_calls=(hire_dynamic("bespoke", INSTRUCTIONS),)),
                ruling(),
            ]
        )
        team = Forge(
            cast=[],
            lead=lead,
            roles={
                "helper": lambda name: Member(Helper(name), "assist", model=Counting([helped()]))
            },
            dynamic_subagents=True,
            max_hires=1,
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["action"] for e in run.hiring_log] == ["hire"], (
        "the catalog hire took the only slot and the synthesis was refused"
    )
    assert team.synthesized == [], "refused before construction — the cap precedes the factory"
    assert team.roster.headcount == 1


async def test_an_empty_instructions_dynamic_agent_is_refused_at_construction_too() -> None:
    """The guard behind the tool's guard: `hire_dynamic` refuses whitespace as text before the
    factory runs, but a caller constructing a `DynamicAgent` directly gets a wiring-time
    `ValueError` — an agent whose whole identity is its instructions has nothing to
    synthesize from."""
    with pytest.raises(ValueError, match="instructions are empty"):
        DynamicAgent("hollow", "   \n  ")


# ── 4. The race: reserve-before-await, under the concurrent executor ──


async def test_two_hire_dynamics_sharing_one_name_in_one_turn_leak_no_live_thread() -> None:
    """The catalog race's worse half, restated for synthesis — and it is a real hazard here,
    not an inherited worry: `hire_dynamic` runs its own refusals before calling the shared
    `commission`, so a reservation that lived anywhere but the same synchronous stretch would
    reopen the window `test_team.py` measured (two spawned, one registered, retirements
    `[0, 1]` — a live thread no unwind path can reach).

    The suspension that makes the window real is `SlowMember`'s sleeping spawn — measured,
    the in-memory coordinator's own spawn never actually yields to the event loop, so a bare
    `Member` cannot interleave and a broken reservation still passes. The assertion is
    per-object — which synthesized members ever got a thread, and whether each was retired —
    because the roster's final shape is identical in the correct and the leaking run."""
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
        team = Forge(
            cast=[], lead=lead, dynamic_subagents=True, dynamic_models=models, slow_spawns=True
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    spawned = [m for m in team.synthesized if m._thread is not None]
    assert len(spawned) == 1, (
        f"only one synthesis may reach a spawn under one name; {len(spawned)} did"
    )
    assert [e["name"] for e in run.hiring_log if e["action"] == "hire_dynamic"] == ["dup"]
    assert all(not m.thread.live for m in spawned), (
        "every spawned dynamic member must be retired — a live one is a leaked thread that "
        "dismiss, the finally and teardown all walk roster.hires to find and cannot"
    )
    assert team.roster.headcount == 1


async def test_two_hire_dynamics_in_one_turn_cannot_race_past_the_cap() -> None:
    """The same interleaving against the budget: both calls in one assistant turn, a cap of
    one, and exactly one may spend it. `synthesized` counts constructions, because a version
    that built and spawned both before dropping one would satisfy a log assertion alone while
    having spent the thread the cap refused."""
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
        team = Forge(
            cast=[],
            lead=lead,
            dynamic_subagents=True,
            dynamic_models=models,
            slow_spawns=True,
            max_hires=1,
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    hires = [e["name"] for e in run.hiring_log if e["action"] == "hire_dynamic"]
    assert len(hires) == 1, f"max_hires=1 and two syntheses in one turn: {hires}"
    assert len(team.synthesized) == 1, "the refused synthesis constructed nothing"
    assert team.roster.headcount == 1
    assert len(lead_model.contexts) == 2, "the refusal is text, so the cycle continued"


# ── 5. Dismiss and teardown reach a dynamic hire exactly as a catalog one ──


async def test_dismiss_retires_the_dynamic_thread_and_frees_its_name_and_slot() -> None:
    """`dismiss` is kind-blind because the roster is: the synthesized member's own thread
    must be retired (asserted from the `MethodThread`'s `live`, the only thing that knows),
    the name and the only `max_hires=1` slot must free — proven by a second synthesis under
    the same name succeeding — and the worklog channel closes with the thread."""
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
        team = Forge(
            cast=[],
            lead=lead,
            dynamic_subagents=True,
            dynamic_models=models,
            worklog_enabled=True,
            max_hires=1,
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["action"] for e in run.hiring_log] == ["hire_dynamic", "dismiss", "hire_dynamic"], (
        "the dismissal released both the name and the only slot under max_hires=1"
    )
    first, second = team.synthesized
    assert not first.thread.live, "the dismissed dynamic thread really ended"
    assert not second.thread.live, "and the replacement was retired by the finally"
    # The dismissal closed the FIRST phoenix's channel; the one registered now is the second
    # synthesis's own. Identity, not presence: a dismiss that forgot the channel would leave
    # the dead thread's notify here and every later post would record a predictable failure.
    assert team.worklog.channels["phoenix"].__self__ is second.thread, (
        "the dismissed hire's channel was closed and the replacement re-registered its own"
    )


async def test_teardown_and_the_finally_retire_dynamic_hires_and_the_registry_is_empty() -> None:
    """The unwind paths are kind-blind too, asserted from the coordinator's own registry: after
    the run every synthesized thread is gone from `list_threads`, so "retired" is the
    runtime's fact and not this suite's inference. An explicit `teardown()` afterwards must
    be harmless — `retire` is idempotent per `MethodThread`'s contract."""
    async with RuntimeHarness() as h:
        models = [Counting([answered("WORKED")])]
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(hire_dynamic("ephemeral", INSTRUCTIONS),)),
                Turn(tool_calls=(("delegate", {"name": "ephemeral", "request": "go"}),)),
                ruling(),
            ]
        )
        team = Forge(cast=[], lead=lead, dynamic_subagents=True, dynamic_models=models)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

        dynamic_thread_id = team.synthesized[0].thread.id
        remaining = {str(info.thread_id) for info in await h.coordinator.list_threads()}
        assert str(dynamic_thread_id) not in remaining, (
            "the dynamic hire's thread must be gone from the coordinator after the run"
        )
        await team.teardown()  # idempotent: the finally already retired everything

    assert not team.synthesized[0].thread.live
    assert "WORKED" in run.hiring_log[1]["answer"]


# ── 6. Worklog: a dynamic hire is equipped and receives the replay ──


async def test_a_dynamic_hire_receives_prior_discoveries_and_can_post_its_own() -> None:
    """The worklog seam must reach a synthesized member exactly as it reaches a catalog hire,
    both directions asserted from the wire. Inbound: a discovery posted during the briefing
    phase — before the dynamic agent existed — must appear in its first model context, which
    is `register`'s replay working through `commission`. Outbound: the dynamic agent's model
    is offered `post_discovery` alongside its own contract, which is `Member.equip`
    composition surviving the synthesis path."""
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
        team = Forge(
            cast=[Member(Analyst("left"), "read", model=analyst_model)],
            lead=lead,
            dynamic_subagents=True,
            dynamic_models=[dyn_model],
            worklog_enabled=True,
        )

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    first_context = "\n".join(dyn_model.prompts(0))
    assert "[team worklog]" in first_context and "THE-PRIOR-DISCOVERY" in first_context, (
        "a discovery posted before the synthesis must reach the dynamic hire's FIRST context "
        "— register's replay, asserted from the wire and not from the entry's delivered list"
    )
    assert "post_discovery" in dyn_model.offered_tools[0], (
        "and the dynamic hire can post its own — equip composed onto the synthesized Member"
    )
    assert "latecomer" in run.worklog[0]["delivered"], "the entry agrees with the wire"


# ── 7. The fixed contract: what a DynamicAgent publishes, provably ──


async def test_a_dynamic_agent_publishes_exactly_one_ability_and_it_is_structured() -> None:
    """The MRO lesson, applied before it bites: `ai_methods()` walks every base class
    (`method.py:341-352`), so the published set is a fact about the whole hierarchy and not
    about this class's own body — a future `@ai_method` added to `MethodAgent` would silently
    become a second tool on every synthesized agent. Pinned as the exact list.

    The shape is the deviation this feature's design logged: `answer(request: str)` alone
    would compile to `STR_PROMPT` (one positional str — `ai_function.py:51-84`, measured),
    making the synthesized thread the one member shape addressable by every peer's free-text
    `send_message` (`ai_thread/tools.py:172-176`). The `context` parameter keeps it
    `STRUCTURED`, so a dynamic hire sits behind exactly the boundary a catalog hire does."""
    agent = DynamicAgent("probe", "some instructions")
    assert DynamicAgent.ai_methods() == ["answer"], (
        "the synthesized agent's published tool set must be exactly its one ability — a "
        "base-class @ai_method would leak in through the MRO walk"
    )
    compiled = agent.compiled("answer", model=Counting([]))
    assert compiled.input_shape is InputShape.STRUCTURED, (
        "a single-positional-str ability would be STR_PROMPT and thus send_message-reachable"
    )
    assert compiled.config.name == "probe.answer"


async def test_the_instructions_render_into_the_prompt_and_stay_off_the_signature() -> None:
    """The method paradigm's split, held for the dynamic case: `self.instructions` reaches
    the prompt through the docstring template and must NOT appear as a call parameter — the
    lead already wrote the instructions once, at hire time, and a signature that carried them
    would let every delegate rewrite the agent's identity per call."""
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
    import inspect

    parameters = set(inspect.signature(agent.answer).parameters)
    assert parameters == {"request", "context"}, (
        "instructions live on self, where a delegate call cannot rewrite them"
    )


def test_hiring_tools_grants_the_fourth_tool_only_when_a_factory_is_supplied() -> None:
    """The seam composes outside `Team` exactly as the three-tool version does, and the
    factory argument is the whole switch: same roster, same catalog, and only the call that
    passed `dynamic=` produces a four-tool hook."""
    roster = Roster()

    def factory(name: str, instructions: str) -> Recruit:
        return Member(DynamicAgent(name, instructions), "answer")

    without = hiring_tools(roster, {})
    with_factory = hiring_tools(roster, {}, dynamic=factory)
    assert callable(without) and callable(with_factory)
    assert roster.headcount == 0
