"""Offline tests for `team/core.py`: the bare team, the composer, the answer loop, the unwind.

Every claim about delivery is checked ON THE WIRE — from the contexts and `tool_specs` a
recording model actually received — never from the returned `TeamRun`, which a mis-wired run
produces just as happily (the render_brief lesson,
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
Budget claims count model calls with `Counting([])` as the load-bearing case: a guard that
raises before the model call leaves `contexts == []`, and one that raises after it raises
`ScriptExhausted` instead of the error under test.

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
from strands.tools.decorator import tool as strands_tool

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Accept, Member, Recruit, Revise, Team, TeamRun, Workspace

# ── Output types, module level for get_type_hints ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


# ── The cast ──


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
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour. Consult the members you hold as tools."""


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


class Spy:
    """A `Recruit` that records its own lifecycle and never touches a model."""

    def __init__(self, name: str, *, answer: str = "ok") -> None:
        self.name = name
        self.answer = answer
        self.events: list[str] = []
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.events.append("spawn")
        self.parent_id = parent_id
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.events.append(f"ask:{request}")
        return self.answer

    async def retire(self) -> None:
        self.retirements += 1
        self.events.append("retire")


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


def reading(source: str = "left") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": "seen"}),))


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def call_member(name: str, request: str = "report your evidence") -> Turn:
    return Turn(tool_calls=((name, {"request": request}),))


def scripted_lead(turns: list[Turn], **overrides: Any) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model, **overrides), model


# ── 1. The bare team ──


async def test_bare_team_lead_reaches_a_member_as_a_typed_tool_and_the_answer_is_ungraded() -> None:
    """The whole core, closed: the member is on the lead's wire under its own name, the lead
    calls it, the member's model runs, and the answer comes back with NO grading anywhere —
    `admitted=False` returns as-is, which no oracle-bearing skeleton would allow."""
    async with RuntimeHarness() as h:
        left_model = Counting([reading("left")])
        member = Member(Analyst("left"), "read", model=left_model)
        lead, lead_model = scripted_lead(
            [call_member("left-analyst_read", "what does left show"), ruling(admitted=False)]
        )
        team = Team(lead, [member])
        run = await team.run("who is right", h.worker.coordinator)

    assert isinstance(run, TeamRun)
    assert run.answer.admitted is False, "returned ungraded: no oracle refused it"
    # The wire name is dot-mapped (strands drops dotted names from the registry with only a
    # warning — measured); the transcript keeps the member's real name.
    assert "left-analyst_read" in lead_model.tool_specs[0], "the member is a tool on the wire"
    assert len(left_model.contexts) == 1, "the lead's tool call reached the member's model"
    assert any("what does left show" in p for p in left_model.prompts(0))
    member_calls = [e for e in run.transcript if e["kind"] == "member"]
    assert member_calls == [
        {
            "kind": "member",
            "member": "left-analyst.read",
            "request": "what does left show",
            "answer": member_calls[0]["answer"],
        }
    ]
    assert "seen" in member_calls[0]["answer"]


async def test_bare_team_with_no_hooks_costs_exactly_one_lead_cycle_and_no_member_cycles() -> None:
    """hooks=() adds zero model cycles: a lead that answers without consulting anybody spends
    one model call, and every member model spends none. Counted, not inferred."""
    async with RuntimeHarness() as h:
        left_model = Counting([])
        member = Member(Analyst("left"), "read", model=left_model)
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [member]).run("go", h.worker.coordinator)

    assert run.answer.admitted is True
    assert len(lead_model.contexts) == 1, "one lead cycle, nothing else"
    assert left_model.contexts == [], "an unconsulted member never touched its model"
    assert run.transcript == [] and run.hooks_data == {}
    assert set(run.model_dump()) == {"answer"}, "empty keys serialise away"


async def test_the_convenience_path_spawns_and_closes_its_own_coordinator() -> None:
    """`run(request)` with no coordinator works end to end — the ~10-line call site."""
    lead, lead_model = scripted_lead([ruling()])
    run = await Team(lead, [Spy("watcher")]).run("go")
    assert run.answer.admitted is True
    assert len(lead_model.contexts) == 1


# ── 2. Hooks: request folding and assembly order ──


async def test_on_request_folds_in_hook_order_and_the_result_reaches_the_lead_on_the_wire() -> None:
    """Two rewriting hooks compose left to right, and the *folded* text is what the lead's
    model was actually asked — asserted from its context, not from any return value."""

    class Prefixer:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def on_request(self, work: Workspace, request: str) -> str:
            return f"[{self.tag}] {request}"

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = Team(lead, [], hooks=[Prefixer("first"), Prefixer("second")])
        await team.run("THE-QUESTION", h.worker.coordinator)

    assert any("[second] [first] THE-QUESTION" in p for p in lead_model.prompts(0))


async def test_on_assemble_sees_live_members_and_runs_before_the_lead() -> None:
    """The ordering contract hooks build briefings on: members are spawned when `on_assemble`
    fires, and the lead's model has not yet been called. Asserted from an interleaving journal
    the spy and the hook both write."""
    journal: list[str] = []

    class Observer:
        async def on_assemble(self, work: Workspace) -> None:
            journal.append(f"assemble:{work.members[0].events[-1]}")
            journal.append(f"asked:{await work.members[0].ask('probe')}")

        def on_request(self, work: Workspace, request: str) -> str:
            journal.append("request")
            return request

    async with RuntimeHarness() as h:
        spy = Spy("watcher", answer="here")
        lead, lead_model = scripted_lead([ruling()])
        await Team(lead, [spy], hooks=[Observer()]).run("go", h.worker.coordinator)

    assert journal == ["assemble:spawn", "asked:here", "request"]
    assert spy.parent_id is not None, "members are children of the lead's thread"
    assert spy.retirements == 1


# ── 3. The composer: one hook, everything folded ──


async def test_composer_folds_lead_tools_member_tools_and_two_hooks_on_the_wire() -> None:
    """The single-config-hook constraint, closed: the lead carries its own `tools=` override
    AND its own `config_hook`, two hooks each contribute a tool, and one member joins — and
    every one of them is in the `tool_specs` the lead's model received on one cycle. The
    runtime calls exactly one hook (`ai_thread.py:548-553`) and its patch REPLACES tools
    (`config.py:166-185`), so this passing means the core's fold really is the one hook."""

    @strands_tool(name="own_tool", description="the lead's own compiled tool")
    def own_tool(x: str) -> str:
        return x

    @strands_tool(name="hooked_tool", description="from the lead's own config_hook")
    def hooked_tool(x: str) -> str:
        return x

    def own_hook(ctx: Any) -> dict[str, Any]:
        return {"tools": [own_tool, hooked_tool]}

    class Contributor:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def tools_for_lead(self, work: Workspace, ctx: Any) -> list[Any]:
            @strands_tool(name=f"{self.tag}_tool", description=f"from hook {self.tag}")
            def contributed(x: str) -> str:
                return x

            return [contributed]

    async with RuntimeHarness() as h:
        member = Member(Analyst("left"), "read", model=Counting([]))
        lead, lead_model = scripted_lead([ruling()], config_hook=own_hook)
        team = Team(lead, [member], hooks=[Contributor("alpha"), Contributor("beta")])
        await team.run("go", h.worker.coordinator)

    specs = lead_model.tool_specs[0]
    for expected in ("own_tool", "hooked_tool", "left-analyst_read", "alpha_tool", "beta_tool"):
        assert expected in specs, f"{expected} missing from the lead's wire: {specs}"


async def test_composer_recomposes_a_members_own_tools_with_hook_contributions() -> None:
    """The T2 lesson on the member side: a hook's `tools` patch REPLACES the member's compiled
    tools, so the member's own `tools=` must be recomposed in ahead of what hooks add. Both
    are asserted from the member model's own wire."""

    @strands_tool(name="members_own", description="the member carried this itself")
    def members_own(x: str) -> str:
        return x

    class MemberContributor:
        def tools_for_member(self, work: Workspace, member: Recruit, ctx: Any) -> list[Any]:
            @strands_tool(name="hook_gave", description="a hook contributed this")
            def hook_gave(x: str) -> str:
                return x

            return [hook_gave]

    async with RuntimeHarness() as h:
        member_model = Counting([reading("left")])
        member = Member(Analyst("left"), "read", model=member_model, tools=[members_own])
        lead, _ = scripted_lead([call_member("left-analyst_read"), ruling()])
        await Team(lead, [member], hooks=[MemberContributor()]).run("go", h.worker.coordinator)

    specs = member_model.tool_specs[0]
    assert "members_own" in specs, f"the member's own tool was lost to the hook: {specs}"
    assert "hook_gave" in specs, f"the hook's tool never reached the member: {specs}"


# ── 4. The answer loop ──


class OneRevision:
    """Revises once with fixed feedback, then accepts whatever comes back."""

    def __init__(self, feedback: str = "cite somebody") -> None:
        self.feedback = feedback
        self.seen: list[Any] = []

    def on_answer(self, work: Workspace, answer: Any) -> Accept | Revise:
        self.seen.append(answer)
        if len(self.seen) == 1:
            return Revise(self.feedback)
        return Accept()


async def test_revise_feedback_reaches_the_leads_next_context_on_the_wire() -> None:
    """`Revise(feedback)` re-runs the lead and the feedback text is IN the second call's
    context — the delivery claim checked where delivery happens."""
    async with RuntimeHarness() as h:
        hook = OneRevision("cite somebody, anybody")
        lead, lead_model = scripted_lead([ruling(cites=[]), ruling(cites=["left"])])
        run = await Team(lead, [], hooks=[hook]).run("go", h.worker.coordinator)

    assert len(lead_model.contexts) == 2, "one draft, one revision"
    assert any("cite somebody, anybody" in p for p in lead_model.prompts(1))
    assert run.answer.cites == ["left"], "the revised answer is the one returned"
    assert [e["kind"] for e in run.transcript] == ["revise"]
    assert hook.seen[0].cites == [] and hook.seen[1].cites == ["left"]


async def test_accept_passes_straight_through_with_no_extra_lead_cycle() -> None:
    class AlwaysAccept:
        def on_answer(self, work: Workspace, answer: Any) -> Accept:
            return Accept()

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[AlwaysAccept()]).run("go", h.worker.coordinator)

    assert len(lead_model.contexts) == 1
    assert run.transcript == []


async def test_cap_exhaustion_passes_the_last_answer_and_records_it() -> None:
    """A hook that revises forever is bounded by its own cap: cap=2 spends exactly two
    revision cycles (three lead calls), the transcript says the cap ended the loop, and the
    LAST answer is the one returned."""

    class NeverSatisfied:
        def on_answer(self, work: Workspace, answer: Any) -> Revise:
            return Revise("still wrong", cap=2)

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["a"]), ruling(cites=["b"]), ruling(cites=["c"])]
        )
        run = await Team(lead, [], hooks=[NeverSatisfied()]).run("go", h.worker.coordinator)

    assert len(lead_model.contexts) == 3, "draft + exactly cap revisions"
    assert run.answer.cites == ["c"], "cap exhaustion passes the last answer on"
    kinds = [e["kind"] for e in run.transcript]
    assert kinds == ["revise", "revise", "revise_cap"]
    assert run.transcript[-1] == {"kind": "revise_cap", "hook": "NeverSatisfied", "rounds": 2}


async def test_two_answer_hooks_each_get_their_own_budget_in_order() -> None:
    """The loop is per hook, in hook order: hook A revises once and accepts, then hook B sees
    A's accepted answer and revises once itself."""
    a, b = OneRevision("A says revise"), OneRevision("B says revise")
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["draft"]), ruling(cites=["afterA"]), ruling(cites=["afterB"])]
        )
        run = await Team(lead, [], hooks=[a, b]).run("go", h.worker.coordinator)

    assert len(lead_model.contexts) == 3
    assert any("A says revise" in p for p in lead_model.prompts(1))
    assert any("B says revise" in p for p in lead_model.prompts(2))
    assert b.seen[0].cites == ["afterA"], "B reviews what A accepted"
    assert run.answer.cites == ["afterB"]


async def test_a_verdict_that_is_neither_accept_nor_revise_raises_naming_the_hook() -> None:
    class Confused:
        def on_answer(self, work: Workspace, answer: Any) -> None:
            return None

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling()])
        with pytest.raises(RuntimeError, match="Confused.on_answer returned None"):
            await Team(lead, [], hooks=[Confused()]).run("go", h.worker.coordinator)


# ── 5. Teardown ──


async def test_everything_is_retired_even_when_a_hook_raises_and_the_error_survives() -> None:
    """A raising `on_teardown` neither leaks a thread nor eats the fault: members and the lead
    are retired (the coordinator registry holds nothing of the run's), and the hook's error is
    the one the caller sees."""

    class Bomb:
        def on_teardown(self, work: Workspace) -> None:
            raise ConnectionError("teardown hiccup")

    async with RuntimeHarness() as h:
        spies = [Spy("one"), Spy("two")]
        lead, _ = scripted_lead([ruling()])
        with pytest.raises(ConnectionError, match="teardown hiccup"):
            await Team(lead, spies, hooks=[Bomb()]).run("go", h.worker.coordinator)

        assert all(s.retirements == 1 for s in spies), "the raise did not stop the unwind"
        infos = await h.worker.coordinator.list_threads()
        live = [i for i in infos if i.status.name not in ("TERMINATED", "DONE", "FAILED")]
        assert live == [], f"threads left live after teardown: {live}"


async def test_a_mid_run_fault_still_retires_everybody_and_runs_teardown_hooks() -> None:
    """The lead's model exhausts its script mid-run; the members are retired anyway and
    `on_teardown` fired — the unconditional-finally claim, from the objects that know."""

    class Recorder:
        def __init__(self) -> None:
            self.torn_down = 0

        def on_teardown(self, work: Workspace) -> None:
            self.torn_down += 1

    recorder = Recorder()
    async with RuntimeHarness() as h:
        spies = [Spy("one"), Spy("two")]
        lead, _ = scripted_lead([])  # ScriptExhausted on the first lead cycle
        with pytest.raises(Exception, match="ScriptExhausted|script has only"):
            await Team(lead, spies, hooks=[recorder]).run("go", h.worker.coordinator)

    assert all(s.retirements == 1 for s in spies)
    assert recorder.torn_down == 1


# ── 6. Guards ──


def test_two_members_colliding_on_the_wire_are_refused_at_construction() -> None:
    """Both the same-name case and the sneaky one — `a.b` vs `a_b` map to one wire name."""
    lead, _ = scripted_lead([ruling()])
    with pytest.raises(RuntimeError, match="collide on the lead's wire"):
        Team(lead, [Spy("twin"), Spy("twin")])
    with pytest.raises(RuntimeError, match="collide on the lead's wire"):
        Team(lead, [Spy("a.b"), Spy("a_b")])


def test_a_negative_revise_cap_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="cap=-1.*negative"):
        Revise("feedback", cap=-1)


async def test_member_own_config_hook_refused_only_when_a_hook_needs_the_slot() -> None:
    """The one-hook-per-cycle conflict is refused loudly when real (a hook contributes member
    tools onto a member already carrying its own hook), and NOT refused when no hook needs the
    slot — the bare path must not police what it does not use."""

    def own(ctx: Any) -> dict[str, Any]:
        return {}

    class MemberContributor:
        def tools_for_member(self, work: Workspace, member: Recruit, ctx: Any) -> list[Any]:
            return []

    async with RuntimeHarness() as h:
        # Bare path: the member's own hook is fine.
        member = Member(Analyst("left"), "read", model=Counting([]), config_hook=own)
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, [member]).run("go", h.worker.coordinator)
        assert run.answer.admitted is True

        # Hooked path: the slot is genuinely contested, so it is refused before any spawn.
        member2 = Member(Analyst("right"), "read", model=Counting([]), config_hook=own)
        lead2, lead2_model = scripted_lead([ruling()])
        with pytest.raises(RuntimeError, match="already carries a config_hook"):
            await Team(lead2, [member2], hooks=[MemberContributor()]).run(
                "go", h.worker.coordinator
            )
        assert lead2_model.contexts == [], "refused before the lead spent anything"
