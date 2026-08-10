"""Offline tests for the `Briefing` and `Hiring` hooks on the hooks-first `Team` core.

Every delivery claim is asserted ON THE WIRE — from the contexts and `tool_specs` a recording
model actually received — never from the returned `TeamRun` alone (the render_brief lesson,
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
Budget claims count model calls with `Counting([])` as the load-bearing case: a guard that
raises before the model call leaves `contexts == []`.

`Counting` composes rather than subclasses (`ScriptedModel` is `@final`) and every fixture
output type is module level, because `compile_ai_method` resolves annotations against module
globals. This file is self-contained: `tests/library/` has no package.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Sequence
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Recruit, Team, TeamRun
from pneuma.team.hooks import Briefing, Hiring, Negotiation, Worklog

# ── Output types, module level ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


class Helped(BaseModel):
    note: str


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


class Helper(MethodAgent):
    def __init__(self, name: str, mandate: str = "") -> None:
        self.name = name
        self.mandate = mandate

    @ai_method(Helped, description="Do the one narrow thing you were hired for")
    def assist(self, request: str) -> Helped:
        """Your mandate: {self.mandate}

        {request}
        """


# ── Recording recruits ──


class Spy:
    """A `Recruit` that records its own lifecycle and never touches a model."""

    def __init__(self, name: str, *, answer: str = "ok", delay: float = 0.0) -> None:
        self.name = name
        self.answer = answer
        self.delay = delay
        self.events: list[str] = []
        self.requests: list[str] = []
        self.retirements = 0
        self.parent_id: Any = None

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.events.append("spawn")
        self.parent_id = parent_id
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.events.append("ask-start")
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append("ask-end")
        return self.answer

    async def retire(self) -> None:
        self.retirements += 1
        self.events.append("retire")


class FailingSpy(Spy):
    async def ask(self, request: str) -> Any:
        self.events.append("ask-start")
        raise KeyError("the source is unreadable")


class SlowSpawnSpy(Spy):
    """A spawn that genuinely suspends — the race window the reservation must close."""

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        await asyncio.sleep(0.01)
        return await super().spawn(coordinator, parent_id=parent_id)


class UnspawnableSpy(Spy):
    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.events.append("spawn-failed")
        raise ConnectionError("the coordinator refused the thread")


class FlakyRetireSpy(Spy):
    """First `retire` raises, later ones succeed; `alive` is the fact under test."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.alive = True

    async def retire(self) -> None:
        self.retirements += 1
        if self.retirements == 1:
            raise ConnectionError("transient: the coordinator hiccuped")
        self.alive = False


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


# ── The model ──


class Counting(Model):
    """A scripted model recording contexts, tool offers and tool results — the wire."""

    def __init__(
        self, turns: list[Turn], *, tag: str = "", journal: list[str] | None = None
    ) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self._tag = tag
        self._journal = journal
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
        if self._journal is not None:
            self._journal.append(f"model:{self._tag}")
        return self._inner.stream(messages, tool_specs, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]

    def tool_results(self, call: int) -> list[str]:
        return [
            inner["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "toolResult" in block
            for inner in block["toolResult"].get("content", [])
            if "text" in inner
        ]


def reading(source: str = "left", detail: str = "seen") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": detail}),))


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def helped(note: str = "done") -> Turn:
    return Turn(tool_calls=(("Helped", {"note": note}),))


def hire(role: str = "helper", name: str = "h", mandate: str = "m") -> Turn:
    return Turn(tool_calls=(("hire", {"role": role, "name": name, "mandate": mandate}),))


def scripted_lead(turns: list[Turn], **overrides: Any) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns, **overrides)
    return Chair().compiled("decide", model=model), model


# ── 1. Briefing: the barrier and the delivery ──


async def test_briefing_asks_every_member_and_the_folded_brief_reaches_the_lead_on_the_wire() -> (
    None
):
    """The whole phase, closed: each member's model was asked its question + the request,
    and the lead's first context carries the request AND each member's answer, attributed."""
    async with RuntimeHarness() as h:
        left_model = Counting([reading("left", "LEFT-EVIDENCE")])
        right_model = Counting([reading("right", "RIGHT-EVIDENCE")])
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, lead_model = scripted_lead([ruling()])
        team = Team(lead, members, hooks=[Briefing()])
        run = await team.run("THE-QUESTION", h.worker.coordinator)

    for model in (left_model, right_model):
        prompt = "\n".join(model.prompts(0))
        assert "THE-QUESTION" in prompt, "the request reaches every member"
        assert "Report what you alone know" in prompt, "and so does the default question"
    lead_prompt = "\n".join(lead_model.prompts(0))
    assert "THE-QUESTION" in lead_prompt
    assert "LEFT-EVIDENCE" in lead_prompt and "RIGHT-EVIDENCE" in lead_prompt, (
        "the briefings must actually appear in the lead's own model context — hooks_data "
        "recording them proves nothing about the wire"
    )
    assert "left-analyst.read" in lead_prompt, "each briefing is attributed to its member"
    assert set(run.hooks_data["briefing"]) == {"left-analyst.read", "right-analyst.read"}
    assert run.answer.admitted is True


async def test_every_briefing_finishes_before_the_leads_first_model_call() -> None:
    """The barrier is the design. Slowest member declared first, so a sequential loop still
    passes while an absent barrier does not: no `model:lead` before any `ask-end`."""
    journal: list[str] = []

    class Journaling(Spy):
        async def ask(self, request: str) -> Any:
            journal.append(f"ask-start:{self.name}")
            await asyncio.sleep(self.delay)
            journal.append(f"ask-end:{self.name}")
            return self.answer

    async with RuntimeHarness() as h:
        cast = [Journaling("slow", delay=0.05), Journaling("fast", delay=0.01)]
        lead, _ = scripted_lead([ruling()], tag="lead", journal=journal)
        await Team(lead, cast, hooks=[Briefing()]).run("go", h.worker.coordinator)

    assert journal.index("model:lead") > journal.index("ask-end:slow")
    assert journal.index("model:lead") > journal.index("ask-end:fast")
    assert journal.index("ask-end:fast") < journal.index("ask-end:slow"), (
        "and the briefings really ran concurrently"
    )


async def test_question_fn_briefs_each_member_its_own_question_and_can_hold_back_the_request() -> (
    None
):
    """The asymmetric-team seam: per-member questions, and `forward_request=False` keeps the
    run's request out of the members' prompts — the war-room shape, where a specialist that
    read the question would be reasoning about the answer."""
    async with RuntimeHarness() as h:
        spies = [Spy("a", answer="A-SAYS"), Spy("b", answer="B-SAYS")]
        lead, lead_model = scripted_lead([ruling()])
        hook = Briefing(lambda member: f"Only-for-{member.name}", forward_request=False)
        await Team(lead, spies, hooks=[hook]).run("SECRET-REQUEST", h.worker.coordinator)

    assert spies[0].requests == ["Only-for-a"]
    assert spies[1].requests == ["Only-for-b"]
    lead_prompt = "\n".join(lead_model.prompts(0))
    assert "SECRET-REQUEST" in lead_prompt and "A-SAYS" in lead_prompt


async def test_a_member_whose_briefing_raises_is_rendered_and_delivered_as_an_error() -> None:
    """One dead member is a fact the lead reads, not a run-ending fault."""
    async with RuntimeHarness() as h:
        good, bad = Spy("good", answer="ONLY-THE-GOOD-EVIDENCE"), FailingSpy("bad")
        lead, lead_model = scripted_lead([ruling()])
        run = await Team(lead, [good, bad], hooks=[Briefing()]).run("go", h.worker.coordinator)

    prompt = "\n".join(lead_model.prompts(0))
    assert "ONLY-THE-GOOD-EVIDENCE" in prompt
    assert "unreadable" in prompt, "the dead member's error is visible in the lead's prompt"
    assert run.hooks_data["briefing"]["bad"].startswith("error: ")
    assert bad.retirements == 1, "and the failed member was still retired"


async def test_a_cast_whose_every_member_failed_is_refused_before_the_lead_spends_anything() -> (
    None
):
    """The unrecoverable failure: `Counting([])` proves the refusal precedes the spend, and
    the members are still retired by the core's finally."""
    async with RuntimeHarness() as h:
        cast = [FailingSpy("a"), FailingSpy("b")]
        model = Counting([])
        lead = Chair().compiled("decide", model=model)
        with pytest.raises(RuntimeError) as raised:
            await Team(lead, cast, hooks=[Briefing()]).run("go", h.worker.coordinator)

    message = str(raised.value)
    assert "every one of the 2 member(s) failed" in message, "the guard's own wording"
    assert "a:" in message and "b:" in message and "unreadable" in message
    assert model.contexts == [], "the lead must never run against an empty evidence set"
    assert [spy.retirements for spy in cast] == [1, 1]


async def test_an_empty_cast_leaves_the_request_unchanged() -> None:
    """No members, no briefings heading — a stray empty section in every prompt is a thing
    no reader would think to check."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        await Team(lead, [], hooks=[Briefing()]).run("JUST-THE-QUESTION", h.worker.coordinator)

    prompt = "\n".join(lead_model.prompts(0))
    assert "JUST-THE-QUESTION" in prompt
    assert "reported" not in prompt


# ── 2. Hiring: the loop, the budget, the races, the unwind ──


def helper_factory(name: str) -> Recruit:
    return Member(Helper(name), "assist", model=Counting([helped()]))


async def test_a_lead_hires_delegates_and_dismisses_and_the_log_records_the_sequence() -> None:
    """The whole hiring loop in the order the model drove it, plus the wire: the three tools
    were offered to the lead's model, and the hire's real answer came back."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                hire(name="h1", mandate="order it"),
                Turn(tool_calls=(("delegate", {"name": "h1", "request": "when"}),)),
                Turn(tool_calls=(("dismiss", {"name": "h1"}),)),
                ruling(),
            ]
        )
        hiring = Hiring({"helper": helper_factory})
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    for tool in ("hire", "delegate", "dismiss"):
        assert tool in lead_model.tool_specs[0], f"{tool} missing from the lead's wire"
    assert "hire_dynamic" not in lead_model.tool_specs[0], "dynamic is off by default"
    log = run.hooks_data["hiring"]
    assert [e["action"] for e in log] == ["hire", "delegate", "dismiss"]
    assert "done" in log[1]["answer"], "the hire's real answer came back to the lead"
    assert len(lead_model.contexts) == 4


async def test_the_hiring_cap_refuses_in_text_and_spawns_nothing_past_it() -> None:
    built: list[str] = []

    def counting_factory(name: str) -> Recruit:
        built.append(name)
        return Spy(name)

    async with RuntimeHarness() as h:
        turns = [hire(name=f"h{i}") for i in range(3)]
        lead, lead_model = scripted_lead([*turns, ruling()])
        hiring = Hiring({"helper": counting_factory}, max_hires=2)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    hires = [e for e in run.hooks_data["hiring"] if e["action"] == "hire"]
    assert [e["name"] for e in hires] == ["h0", "h1"], "the third was refused"
    assert built == ["h0", "h1"], "and nothing was constructed for it"
    assert "hiring cap reached" in "\n".join(lead_model.tool_results(3))
    assert len(lead_model.contexts) == 4, "the refusal is text, so the cycle continued"


async def test_two_hires_in_one_assistant_turn_cannot_race_past_the_cap() -> None:
    """The concurrent tool executor is the runtime's default; `SlowSpawnSpy` makes the window
    real. The construction spy separates 'the roster refused it' from 'it was built and
    dropped' — the latter spends the thread the cap refused."""
    built: list[SlowSpawnSpy] = []

    def factory(name: str) -> Recruit:
        spy = SlowSpawnSpy(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(
                    tool_calls=(
                        ("hire", {"role": "helper", "name": "a", "mandate": "m"}),
                        ("hire", {"role": "helper", "name": "b", "mandate": "m"}),
                    )
                ),
                ruling(),
            ]
        )
        hiring = Hiring({"helper": factory}, max_hires=1)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    hires = [e["name"] for e in run.hooks_data["hiring"] if e["action"] == "hire"]
    assert len(hires) == 1, f"max_hires=1 and two hires in one turn: {hires}"
    assert len([spy for spy in built if spy.events]) == 1, (
        "a cap that spends what it refuses is not a cap"
    )


async def test_two_hires_sharing_one_name_in_one_turn_leak_no_live_thread() -> None:
    """The worse half of the race: without the reservation both spawn, the overwrite makes
    one unreachable, and no unwind path can find it. Per-object retirement is the assertion
    because the roster's final shape is identical in the correct and the leaking run."""
    built: list[SlowSpawnSpy] = []

    def factory(name: str) -> Recruit:
        spy = SlowSpawnSpy(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(
                    tool_calls=(
                        ("hire", {"role": "helper", "name": "dup", "mandate": "m"}),
                        ("hire", {"role": "helper", "name": "dup", "mandate": "m"}),
                    )
                ),
                ruling(),
            ]
        )
        run = await Team(lead, [], hooks=[Hiring({"helper": factory})]).run(
            "go", h.worker.coordinator
        )

    spawned = [spy for spy in built if spy.events]
    assert len(spawned) == 1, f"only one hire may reach a spawn under one name: {len(spawned)}"
    assert [e["name"] for e in run.hooks_data["hiring"] if e["action"] == "hire"] == ["dup"]
    assert all(spy.retirements == 1 for spy in spawned)


async def test_a_hire_whose_spawn_raises_holds_neither_its_name_nor_a_slot() -> None:
    """The reservation's rollback: the retry under the same name must succeed."""
    built: list[Spy] = []

    def factory(name: str) -> Recruit:
        spy: Spy = UnspawnableSpy(name) if not built else Spy(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([hire(), hire(), ruling()])
        hiring = Hiring({"helper": factory}, max_hires=1)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert [e["name"] for e in run.hooks_data["hiring"] if e["action"] == "hire"] == ["h"], (
        "the retry succeeded, so the failed spawn released the name and the slot"
    )
    assert built[1].retirements == 1, "the live recruit is the registered one"
    assert len(lead_model.contexts) == 3


async def test_unknown_role_duplicate_name_ghost_delegate_and_ghost_dismiss_are_all_text() -> None:
    """Every model-fixable mistake is a string, never a raise, and none corrupts the log."""
    built: list[str] = []

    def counting_factory(name: str) -> Recruit:
        built.append(name)
        return Spy(name)

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                hire(role="wizard", name="w"),
                hire(name="h"),
                hire(name="h"),
                Turn(tool_calls=(("delegate", {"name": "ghost", "request": "hi"}),)),
                Turn(tool_calls=(("dismiss", {"name": "ghost"}),)),
                ruling(),
            ]
        )
        hiring = Hiring({"helper": counting_factory})
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert [e["name"] for e in run.hooks_data["hiring"] if e["action"] == "hire"] == ["h"]
    assert built == ["h"], "no refusal built anything"
    assert "no such role" in "\n".join(lead_model.tool_results(1))
    assert "already have a subagent" in "\n".join(lead_model.tool_results(3))
    assert "not hired anyone named" in "\n".join(lead_model.tool_results(4))
    assert len(lead_model.contexts) == 6, "the model recovered from all four"


async def test_a_hire_that_raises_on_delegate_is_recorded_as_a_failure_not_a_crash() -> None:
    class Broken(Spy):
        async def ask(self, request: str) -> Any:
            raise ZeroDivisionError("the helper is wrong")

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                hire(),
                Turn(tool_calls=(("delegate", {"name": "h", "request": "go"}),)),
                ruling(),
            ]
        )
        hiring = Hiring({"helper": lambda name: Broken(name)})
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    log = run.hooks_data["hiring"]
    assert [e["action"] for e in log] == ["hire", "delegate_failed"]
    assert "ZeroDivisionError" in log[1]["error"]
    assert len(lead_model.contexts) == 3


async def test_a_dismissal_whose_retire_faults_leaves_the_recruit_reachable_by_the_unwind() -> None:
    """Retire first, unregister on success: a `pop` before the await drops the roster's only
    reference and the fault leaks a live thread. The hook's `on_teardown` retries."""
    async with RuntimeHarness() as h:
        recruit = FlakyRetireSpy("h")
        lead, lead_model = scripted_lead(
            [hire(), Turn(tool_calls=(("dismiss", {"name": "h"}),)), ruling()]
        )
        hiring = Hiring({"helper": lambda name: recruit})
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert not recruit.alive, (
        f"still live after the teardown; {recruit.retirements} retire call(s) reached it"
    )
    assert recruit.retirements >= 2, "the first raised and the teardown retried it"
    assert [e["action"] for e in run.hooks_data["hiring"]] == ["hire"], (
        "an incomplete dismissal is not in the audit as one"
    )
    assert len(lead_model.contexts) == 3, "and the fault did not end the cycle"


async def test_a_dismissal_that_succeeds_unregisters_and_frees_the_name_and_the_slot() -> None:
    async with RuntimeHarness() as h:
        first, second = Spy("h"), Spy("h")
        recruits = iter([first, second])
        lead, _ = scripted_lead(
            [hire(), Turn(tool_calls=(("dismiss", {"name": "h"}),)), hire(), ruling()]
        )
        hiring = Hiring({"helper": lambda name: next(recruits)}, max_hires=1)
        run = await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert [e["action"] for e in run.hooks_data["hiring"]] == ["hire", "dismiss", "hire"], (
        "the dismissal released both the name and the only slot under max_hires=1"
    )
    assert first.retirements == 1, "dismissed once, and not again by the teardown"
    assert second.retirements == 1, "and the replacement was retired by the teardown"


async def test_a_hire_is_spawned_as_a_child_of_the_lead() -> None:
    """`parent_id=ctx.thread_id` inside the hook: the hiring agent is the recorded parent,
    which is what makes cost attribution up the tree free. Members are the lead's children
    too, so both spies must report the same parent."""
    async with RuntimeHarness() as h:
        member, hired_spy = Spy("member"), Spy("h")
        lead, _ = scripted_lead([hire(), ruling()])
        hiring = Hiring({"helper": lambda name: hired_spy})
        await Team(lead, [member], hooks=[hiring]).run("go", h.worker.coordinator)

    assert member.parent_id is not None
    assert hired_spy.parent_id == member.parent_id, "hire and member share the lead as parent"


async def test_a_second_run_on_the_same_team_starts_from_an_empty_roster() -> None:
    """Per-run state is per run: run 2 opens with an empty log, run 1's names are free, the
    budget is whole again — while WITHIN run 2 a first-turn hire is still there for a
    third-turn delegate."""

    class Retired(Spy):
        async def ask(self, request: str) -> Any:
            if self.retirements:
                raise RuntimeError("I was retired in an earlier run and you delegated anyway")
            return await super().ask(request)

    built: list[Retired] = []

    def factory(name: str) -> Recruit:
        spy = Retired(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                hire(name="h1"),
                ruling(),
                hire(name="h1"),
                hire(name="h2"),
                Turn(tool_calls=(("delegate", {"name": "h1", "request": "again"}),)),
                ruling(),
            ]
        )
        hiring = Hiring({"helper": factory}, max_hires=2)
        team = Team(lead, [], hooks=[hiring])
        first = await team.run("one", h.worker.coordinator)
        second = await team.run("two", h.worker.coordinator)

    assert [(e["action"], e["name"]) for e in first.hooks_data["hiring"]] == [("hire", "h1")]
    assert [(e["action"], e["name"]) for e in second.hooks_data["hiring"]] == [
        ("hire", "h1"),
        ("hire", "h2"),
        ("delegate", "h1"),
    ], "run 2's report is run 2's: no inherited entry, and h1 was free to take again"
    assert len(built) == 3, "run 2 built its own h1"
    assert built[0].retirements == 1 and built[0] is not built[1]


async def test_a_negative_hiring_cap_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="max_hires=-1.*negative"):
        Hiring({}, max_hires=-1)


async def test_a_lead_that_faults_mid_run_still_has_its_hires_retired() -> None:
    """The lead's script exhausts after hiring; the hook's `on_teardown` runs on the fault
    path and the hire is retired — the unconditional-unwind claim at hook scale."""
    async with RuntimeHarness() as h:
        hired_spy = Spy("h")
        lead, _ = scripted_lead([hire()])  # ScriptExhausted on the second lead cycle
        hiring = Hiring({"helper": lambda name: hired_spy})
        with pytest.raises(Exception, match="ScriptExhausted|script has only"):
            await Team(lead, [], hooks=[hiring]).run("go", h.worker.coordinator)

    assert hired_spy.retirements == 1, "the fault path still released the hire"


# ── 3. Preset parity: the legacy five-phase behavior out of four hooks ──


async def test_the_full_preset_reproduces_the_legacy_five_phase_behavior_on_one_scenario() -> None:
    """Team(lead, members, hooks=[Briefing(), Negotiation(), Hiring(catalog), Worklog()]) on
    a scripted scenario, every claim on the wire: briefings reach the lead, the negotiation
    round is recorded (and the plan reached the members), the hire and the delegate landed,
    and the discovery fanned out to the other member, the lead AND the late hire (replay)."""
    async with RuntimeHarness() as h:
        left_model = Counting(
            [
                Turn(
                    tool_calls=(
                        ("post_discovery", {"kind": "obstacle", "body": "THE-SHARED-CLUE"}),
                    )
                ),
                reading("left", "LEFT-BRIEF"),
                reading("left", "looks right, APPROVED"),
            ]
        )
        right_model = Counting(
            [reading("right", "RIGHT-BRIEF"), reading("right", "fine by me, APPROVED")]
        )
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        hire_model = Counting([helped("HIRED-ANSWER")])
        lead, lead_model = scripted_lead(
            [
                hire(name="aide", mandate="check the clue"),
                Turn(tool_calls=(("delegate", {"name": "aide", "request": "verify it"}),)),
                ruling(cites=["THE-PLAN"]),
            ]
        )
        hiring = Hiring({"helper": lambda name: Member(Helper(name), "assist", model=hire_model)})
        team = Team(lead, members, hooks=[Briefing(), Negotiation(rounds=1), hiring, Worklog()])
        run = await team.run("who is right", h.worker.coordinator)

    # Briefings reached the lead (phase 2's delivery).
    lead_prompt = "\n".join(lead_model.prompts(0))
    assert "LEFT-BRIEF" in lead_prompt and "RIGHT-BRIEF" in lead_prompt

    # The discovery fanned out: to the other member's next context, to the lead's first
    # (register's replay — the channel opened after the post), and to the hire's first.
    right_review = "\n".join(right_model.prompts(1))
    assert "[team worklog]" in right_review and "THE-SHARED-CLUE" in right_review
    assert "THE-SHARED-CLUE" in lead_prompt, "the lead's channel replayed the briefing-time post"
    hire_first = "\n".join(hire_model.prompts(0))
    assert "THE-SHARED-CLUE" in hire_first, "a late hire joins knowing what the team flagged"
    entry = run.hooks_data["worklog"][0]
    assert entry["source"] == "left-analyst.read" and entry["failed"] == {}
    assert {"right-analyst.read", "lead"} <= set(entry["delivered"])

    # The hire and the delegate are on the log, the answer round-tripped.
    log = run.hooks_data["hiring"]
    assert [e["action"] for e in log] == ["hire", "delegate"]
    assert "HIRED-ANSWER" in log[1]["answer"]

    # The negotiation round ran, the plan reached both members, and it was unanimous.
    rounds = run.hooks_data["negotiation"]
    assert [e["outcome"] for e in rounds] == ["unanimous"]
    assert "THE-PLAN" in rounds[0]["plan"]
    for model in (left_model, right_model):
        review = "\n".join(model.prompts(len(model.contexts) - 1))
        assert "THE-PLAN" in review, "the plan text must appear in the member's own context"
    assert run.answer.cites == ["THE-PLAN"], "the approved draft is the answer"
    assert isinstance(run, TeamRun)
