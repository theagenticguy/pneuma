"""Offline tests for `team.py`: the phases, the barrier, the oracle, the budget, the unwind.

Four kinds of claim, checked four different ways on purpose.

The *phases* are claims about order, so they are asserted from a recording the members and the
lead write as they run — never from the returned `TeamRun`, which a badly ordered run produces
just as happily. The sharpest one is the barrier: every member must have finished before the
lead's first model call, and only an interleaving record can say so.

The *budget* is a claim about what was spent, so every guard test counts model calls with a
`Counting` model rather than matching a message. `Counting([])` is the load-bearing case — a
guard that raises before the model call leaves `contexts == []`, and one that raises after it
raises `ScriptExhausted` instead of the error under test. A guard that raises after spending
what it protects is half a guard.

The *typed-members claim* is checked against `config.tools` and each member's `input_shape`,
because that is the kernel claim the whole module rests on: members compose by typing, not
through a message bus, and `send_message` cannot reach a `STRUCTURED` peer
(`ai_thread/tools.py:172-176`). A docstring saying so would prove nothing.

The *unwind* is checked from the recruits themselves, which count their own retirements, so
"everybody was retired" is a fact about the objects rather than about the code path a reader
believes ran.

`Counting` composes rather than subclasses — `ScriptedModel` is `@final` — and every fixture
output type is module level, because `compile_ai_method` resolves annotations with
`typing.get_type_hints` against module globals (`method.py:146`) and a function-local model
cannot be resolved at compile time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.protocols import Spawnable, Thread
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import EventKind, InputShape
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma._team_legacy import Member, Recruit, Roster, Team, TeamRun, hiring_tools
from pneuma.method import MethodAgent, ai_method

# ── The output types the cast returns, all module level ──


class Reading(BaseModel):
    """What one member reports from its own evidence."""

    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Ruling(BaseModel):
    """The lead's call. `admitted` is what the oracle gates on."""

    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


class Helped(BaseModel):
    """What a hired helper produces. A second type, because one per capability."""

    note: str


# ── The cast: MethodAgents with disjoint instance evidence ──


class Analyst(MethodAgent):
    """One agent, several instances, each holding evidence the others cannot see.

    The payoff of the method paradigm and the reason a team is worth orchestrating at all: if
    every member could see everything there would be nothing to convene.
    """

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
    """The lead. Holds no evidence, so it has to use the members it was given."""

    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported", max_attempts=3)
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour. Consult the members you hold as tools."""


class Helper(MethodAgent):
    """A hireable role. Constructed by a factory the team's catalog supplies."""

    def __init__(self, name: str, mandate: str = "") -> None:
        self.name = name
        self.mandate = mandate

    @ai_method(Helped, description="Do the one narrow thing you were hired for")
    def assist(self, request: str) -> Helped:
        """Your mandate: {self.mandate}

        {request}
        """


# ── Recording recruits: the cheapest way to assert order and unwind ──


class Spy:
    """A `Recruit` that records its own lifecycle and never touches a model.

    Used wherever the claim is about *order* or *unwind* rather than about a model call, because
    a recruit that costs nothing makes `Counting([])` a usable assertion in the same test.
    """

    def __init__(self, name: str, *, answer: str = "ok", delay: float = 0.0) -> None:
        self.name = name
        self.answer = answer
        self.delay = delay
        self.events: list[str] = []
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.events.append("spawn")
        self.parent_id = parent_id
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.events.append(f"ask-start:{request}")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append("ask-end")
        return self.answer

    async def retire(self) -> None:
        self.retirements += 1
        self.events.append("retire")


class FailingSpy(Spy):
    """A member whose briefing raises. The `return_exceptions` path."""

    async def ask(self, request: str) -> Any:
        self.events.append("ask-start")
        raise KeyError("the source is unreadable")


class SlowSpawnSpy(Spy):
    """A recruit whose `spawn` awaits, which every real recruit's does.

    The load-bearing fixture for the hiring-race tests: `coordinator.spawn` is a coroutine, so a
    `hire` that registers on the far side of that await yields to the other `hire` in the same
    turn. A `Spy` whose `spawn` never suspends cannot show the race at all — it is the suspension
    that makes two concurrent tool calls interleave.
    """

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        await asyncio.sleep(0.01)
        return await super().spawn(coordinator, parent_id=parent_id)


class UnspawnableSpy(Spy):
    """A recruit whose `spawn` raises, for the reservation's rollback."""

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self.events.append("spawn-failed")
        raise ConnectionError("the coordinator refused the thread")


class FlakyRetireSpy(Spy):
    """A recruit whose first `retire` raises and whose later ones succeed.

    `alive` is the fact the dismissal-ordering test is about: a transient fault on the way down
    must leave the recruit somewhere an unwind can still reach, and only the object knows whether
    anything ever did reach it.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.alive = True

    async def retire(self) -> None:
        self.retirements += 1
        self.events.append("retire")
        if self.retirements == 1:
            raise ConnectionError("transient: the coordinator hiccuped")
        self.alive = False


class _FakeHandle:
    """Just enough handle for `Roster.thread_ids`, which stores `handle.id` and nothing more."""

    def __init__(self, ident: str) -> None:
        self.id = ident


# ── The models ──


class Counting(Model):
    """A scripted model that reports how many times it was called and with what.

    `ScriptedModel` is `@final` and its `stream` ignores `messages`, so there is nothing to
    subclass — composition, exactly as `test_recall.py:244` and `test_process_agent.py:371` do
    it. `Counting([])` is the load-bearing case: a guard that raises before the model call leaves
    `contexts == []`, and one that raises after it raises `ScriptExhausted` instead of the error
    under test.
    """

    def __init__(
        self, turns: list[Turn], *, tag: str = "", journal: list[str] | None = None
    ) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self._tag = tag
        self._journal = journal
        self.contexts: list[list[Any]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        if self._journal is not None:
            self._journal.append(f"model:{self._tag}")
        return self._inner.stream(messages, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        """Every text block the model saw on its `call`-th invocation."""
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


def reading(source: str = "left") -> Turn:
    return Turn(
        tool_calls=(("Reading", {"source": source, "detail": "seen"}),),
        input_tokens=5,
        output_tokens=3,
    )


def ruling(*, admitted: bool = True, cites: Sequence[str] = ("left",)) -> Turn:
    return Turn(
        tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),),
        input_tokens=7,
        output_tokens=2,
    )


def helped(note: str = "done") -> Turn:
    return Turn(tool_calls=(("Helped", {"note": note}),))


# ── The team under test ──


class Toy(Team):
    """The smallest team that supplies all four required overrides.

    Members, lead and catalog are all injected rather than built in `__init__`, so one test can
    hand it recording spies and the next can hand it real `MethodAgent`s over scripted models
    without two fixtures existing.
    """

    def __init__(
        self,
        *,
        cast: Sequence[Recruit] = (),
        lead: AIFunction[..., Any] | None = None,
        roles: Mapping[str, Callable[[str], Recruit]] | None = None,
        max_hires: int = 3,
        name: str = "toy",
        journal: list[str] | None = None,
    ) -> None:
        super().__init__(name=name, max_hires=max_hires, roster=Roster())
        self._cast = list(cast)
        self._lead = lead
        self._roles = roles or {}
        self.journal = journal if journal is not None else []
        self.refusals: list[str] = []

    def members(self) -> Sequence[Recruit]:
        return self._cast

    def briefing(self, member: Recruit) -> str:
        return f"Read your own source, {member.name}."

    def lead_function(self) -> AIFunction[..., Any]:
        assert self._lead is not None, "this test's Toy needs a lead"
        return self._lead

    def oracle(self, response: Any) -> None:
        if not getattr(response, "admitted", False):
            self.refusals.append("not admitted")
            raise AssertionError(
                "the ruling is not admitted; say which members you relied on and rule again"
            )

    def catalog(self) -> Mapping[str, Callable[[str], Recruit]]:
        return self._roles


class Graded(Toy):
    """A team whose `grade` re-verifies after the oracle already passed."""

    def grade(self, verdict: Any) -> tuple[bool, list[str]]:
        failures = [] if getattr(verdict, "cites", None) else ["the ruling cites nobody"]
        return not failures, failures


class Bare(Team):
    """No overrides at all: the guard must name every one of the four."""


class Partial(Team):
    """Two of four supplied, so the refusal must list exactly the other two."""

    def members(self) -> Sequence[Recruit]:
        return []

    def oracle(self, response: Any) -> None:
        del response


def scripted_lead(turns: list[Turn], *, tools: Sequence[Any] = (), **overrides: Any) -> Any:
    """The `Chair`'s `decide` compiled over a scripted model, plus whatever tools a test wants."""
    model = Counting(turns, **overrides)
    return Chair().compiled("decide", model=model, tools=list(tools)), model


# ── 1. End to end ──


async def test_a_spawned_team_runs_every_phase_and_reports_what_it_spent() -> None:
    """The whole skeleton, closed, driven the way the runtime drives it.

    `coordinator.spawn(team)` with no special-casing is the claim `Spawnable` makes
    (`runtime/worker.py:488`), so the team is spawned through the harness and driven by one
    `handle.run`, exactly as `demo/cli.py:46-47` drives a war room. Usage is asserted as a
    positive number rather than an exact one: the scripted turns carry token counts, so a rollup
    that walked no `THREAD_SPAWNED` edges would report zero.
    """
    async with RuntimeHarness() as h:
        left = Analyst("left")
        right = Analyst("right")
        members = [
            Member(left, "read", model=Counting([reading("left")])),
            Member(right, "read", model=Counting([reading("right")])),
        ]
        lead, lead_model = scripted_lead([ruling(cites=["left", "right"])])
        team = Toy(cast=members, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("who is right")

    assert isinstance(run, TeamRun)
    assert run.verdict.admitted is True
    assert set(run.briefings) == {"left-analyst.read", "right-analyst.read"}
    assert "seen" in run.briefings["left-analyst.read"]
    assert run.correct is True and run.oracle_failures == []
    assert len(lead_model.contexts) == 1, "the lead ruled once and was admitted"
    assert run.turns == 3, "two briefings and one ruling, rolled up through the parent edges"
    assert run.input_tokens == 17 and run.output_tokens == 8
    assert run.wall_seconds >= 0.0


async def test_the_request_reaches_every_member_and_the_lead() -> None:
    """One string drives the whole team: it is appended to each briefing and is the lead's prompt.

    Asserted from the prompts rather than from the returned run, because the claim is that the
    text *arrived* at three separate models and not merely that a string was concatenated.
    """
    async with RuntimeHarness() as h:
        left_model = Counting([reading("left")])
        members = [Member(Analyst("left"), "read", model=left_model)]
        lead, lead_model = scripted_lead([ruling()])
        team = Toy(cast=members, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("THE-QUESTION")

    assert any("THE-QUESTION" in p for p in left_model.prompts(0))
    assert any("Read your own source" in p for p in left_model.prompts(0))
    assert any("THE-QUESTION" in p for p in lead_model.prompts(0))


async def test_a_team_is_both_a_spawnable_and_a_thread() -> None:
    """The protocol conformance, structurally. Both are `@runtime_checkable`, and a class holding
    only `to_thread` + `input_shape` satisfies `Spawnable` but not `Thread` — measured — so this
    assertion can fail."""
    team = Toy(cast=[], lead=None)
    assert isinstance(team, Spawnable)
    assert isinstance(team, Thread)
    assert team.to_thread() is team
    assert team.input_shape is InputShape.STR_PROMPT


# ── 2. The barrier ──


async def test_every_briefing_finishes_before_the_leads_first_model_call() -> None:
    """The barrier, which is the design and not an implementation detail.

    A lead that began interrogating while half the team was still reading would produce a
    verdict whose evidence depended on scheduling — the same team, the same data, a different
    answer. Asserted from one interleaved journal the members and the lead's model both write to,
    because a run without the barrier returns the same `TeamRun` and the same briefings.

    The delays are staggered and the slowest member is declared *first*, so a `gather` replaced
    by a sequential loop would still pass while an absent barrier would not: what this pins is
    that no `model:lead` entry precedes any `ask-end`.
    """
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
        team = Toy(cast=cast, lead=lead, journal=journal)

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

    assert journal.index("model:lead") > journal.index("ask-end:slow")
    assert journal.index("model:lead") > journal.index("ask-end:fast")
    # And they really did run concurrently: the fast member finished before the slow one, which a
    # sequential loop over a slow-first cast cannot produce.
    assert journal.index("ask-end:fast") < journal.index("ask-end:slow")
    assert journal.index("ask-start:fast") < journal.index("ask-end:slow")


async def test_the_cast_is_assembled_before_any_briefing_is_asked() -> None:
    """Phase 1 completes before phase 2 opens: a member cannot be briefed on a thread that does
    not exist yet, and a team that spawned lazily inside `ask` would have no parent edges to roll
    usage up through."""
    order: list[str] = []

    class Ordered(Spy):
        async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
            order.append(f"spawn:{self.name}")
            return await super().spawn(coordinator, parent_id=parent_id)

        async def ask(self, request: str) -> Any:
            order.append(f"ask:{self.name}")
            return await super().ask(request)

    async with RuntimeHarness() as h:
        cast = [Ordered("a"), Ordered("b")]
        lead, _ = scripted_lead([ruling()])
        handle = await h.spawn(Toy(cast=cast, lead=lead), thread_name="toy")
        await handle.run("go")

    assert order == ["spawn:a", "spawn:b", "ask:a", "ask:b"]


async def test_the_lead_is_composed_before_any_member_is_spawned_and_after_members_is_called() -> (
    None
):
    """Both orderings inside the wiring phase, pinned, because each buys something different.

    `members()` before `lead_function()`, so a subclass may build its cast there and hand it to
    the lead as tools — the shape a typed team has, since `agents()` is called on the same objects
    `members()` returns. And both before `assemble`, because `_gated_lead` is where the wiring
    guards live: a refusal that arrives after the barrier has already paid for a spawn and a real
    model call per member (measured — see the collision test).
    """
    order: list[str] = []

    class Ordered(Toy):
        def members(self) -> Sequence[Recruit]:
            order.append("members")
            return super().members()

        def lead_function(self) -> AIFunction[..., Any]:
            order.append("lead_function")
            return super().lead_function()

    class Watched(Spy):
        async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
            order.append("spawn")
            return await super().spawn(coordinator, parent_id=parent_id)

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling()])
        handle = await h.spawn(Ordered(cast=[Watched("a")], lead=lead), thread_name="toy")
        await handle.run("go")

    assert order == ["members", "lead_function", "spawn"]


async def test_members_are_spawned_as_children_of_the_teams_own_thread() -> None:
    """`parent_id=ctx.thread_id` is what writes the `THREAD_SPAWNED` edge `subtree_usage` walks
    (`runtime/usage.py:65-72`), so the rollup is a consequence of spawning correctly rather than
    bookkeeping the team has to do."""
    async with RuntimeHarness() as h:
        cast = [Spy("a"), Spy("b")]
        lead, _ = scripted_lead([ruling()])
        handle = await h.spawn(Toy(cast=cast, lead=lead), thread_name="toy")
        await handle.run("go")

    assert [spy.parent_id for spy in cast] == [handle.id, handle.id]


# ── 3. A member failure is rendered, not raised ──


async def test_a_member_whose_briefing_raises_becomes_an_error_string_and_the_run_continues() -> (
    None
):
    """A four-member team with one dead thread is still a team worth asking.

    `return_exceptions=True` is what makes that true, and the rendered string is what tells the
    lead — in its own briefing text — that one source is missing. A run that let the exception
    out would lose the three members that worked.
    """
    async with RuntimeHarness() as h:
        good, bad = Spy("good", answer="the good reading"), FailingSpy("bad")
        lead, lead_model = scripted_lead([ruling()])
        team = Toy(cast=[good, bad], lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.briefings["good"] == "the good reading"
    assert run.briefings["bad"].startswith("error: ")
    assert "unreadable" in run.briefings["bad"]
    assert len(lead_model.contexts) == 1, "the lead still ran"
    assert bad.retirements == 1, "and the failed member was still retired"


async def test_the_lead_is_asked_with_the_briefings_and_not_only_with_the_request() -> None:
    """The barrier is only worth holding if the evidence it waited for reaches the lead.

    It did not, once: `brief` put the answers into the returned `TeamRun` and `execute` handed the
    lead the bare `request`, so the module's own claim — that the lead "can see in its own briefing
    text that one plane is missing" — described a delivery that was not there. Asserted from the
    lead's *model context*, which is the only place that can say the text arrived; the returned
    `briefings` were always populated and prove nothing about what the lead read.

    Both kinds of briefing are checked, because the failure case is the one the claim was about: a
    surviving answer and a rendered error have to be equally visible, or a lead cannot tell a plane
    that reported nothing from a plane that was never there.
    """
    async with RuntimeHarness() as h:
        good, bad = Spy("good", answer="ONLY-THE-GOOD-EVIDENCE"), FailingSpy("bad")
        lead, lead_model = scripted_lead([ruling()])
        team = Toy(cast=[good, bad], lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("THE-QUESTION")

    prompt = "\n".join(lead_model.prompts(0))
    assert "THE-QUESTION" in prompt, "the request still reaches the lead"
    assert "ONLY-THE-GOOD-EVIDENCE" in prompt, "and so does what the surviving member reported"
    assert "good" in prompt and "bad" in prompt, "each briefing is attributed to its member"
    assert "unreadable" in prompt, "and the dead member's error is visible as an error"
    assert run.verdict.admitted is True


async def test_render_brief_is_the_composition_seam_and_an_override_owns_it() -> None:
    """Delivery is a template method, because composition is a judgment and not a mechanism.

    A lead that reaches its members another way wants them left out; a lead with a strict prompt
    format wants its own headings. Both are subclass business, so the base renders and the subclass
    overrides — and an override returning `request` unchanged restores the pre-delivery behaviour
    deliberately, which is the honest way to want it. Asserted in both directions from the lead's
    context, so "the override won" is a fact about what the model read.
    """

    class OwnFormat(Toy):
        def render_brief(self, request: str, briefings: Mapping[str, str]) -> str:
            return f"<<{request}>> [{len(briefings)} reports] {'|'.join(sorted(briefings))}"

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = OwnFormat(cast=[Spy("a", answer="AAA"), Spy("b", answer="BBB")], lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("Q")

    prompt = "\n".join(lead_model.prompts(0))
    assert "<<Q>> [2 reports] a|b" in prompt, "the override composed the lead's request"
    assert "AAA" not in prompt and "BBB" not in prompt, (
        "and the base's rendering did not also run — a delivery that appended both would make an "
        "override that wants the briefings *out* impossible"
    )


async def test_a_team_with_no_members_asks_its_lead_the_request_unchanged() -> None:
    """The empty-cast case, which the default rendering must leave alone.

    Half these tests drive a `Toy(cast=[])` to exercise the hiring seam by itself, and a team that
    declares no members has lost nothing — so there is no briefings section to add and the lead's
    prompt is the request. Pinned because the alternative is a stray empty heading in every prompt
    of every fixed-cast team, which is a thing no reader would think to check.
    """
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        handle = await h.spawn(Toy(cast=[], lead=lead), thread_name="toy")
        await handle.run("JUST-THE-QUESTION")

    prompt = "\n".join(lead_model.prompts(0))
    assert "JUST-THE-QUESTION" in prompt
    assert "reported" not in prompt, "no cast, so no briefings section at all"


async def test_a_run_whose_every_member_failed_refuses_before_the_lead_is_spawned() -> None:
    """The one member failure that is not recoverable, and the asymmetry is the argument.

    A four-member team missing one plane is still worth asking; a team with nothing is not. The
    lead holds no evidence of its own — that is why there is a team — so it would rule from the
    request alone and produce a verdict shaped exactly like a real one. Measured before the fix:
    the lead ran, its context mentioned no error at all, and `grade` returned `correct=True`.

    `Counting([])` is what proves the refusal precedes the spend: a run that reached the lead
    raises `ScriptExhausted` here instead of the error under test. And the type has to be a plain
    `RuntimeError` naming the members, because the caller is the only party who can act on a dead
    cast — there is no model in this failure to hand a string to.
    """
    async with RuntimeHarness() as h:
        cast = [FailingSpy("a"), FailingSpy("b")]
        model = Counting([])
        lead = Chair().compiled("decide", model=model)
        team = Toy(cast=cast, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(RuntimeError) as raised:
            await handle.run("go")

    message = str(raised.value)
    assert type(raised.value) is RuntimeError, f"a refusal, not a graded report: {message}"
    assert "every one of the 2 member(s) failed" in message, "the guard's own wording"
    assert "a:" in message and "b:" in message, "and it names who failed"
    assert "unreadable" in message, "with each member's own error, for the caller diagnosing it"
    assert model.contexts == [], "the lead must never be spawned against an empty evidence set"
    assert [spy.retirements for spy in cast] == [1, 1], "and the dead cast is still retired"


async def test_one_surviving_briefing_is_enough_to_run_the_lead() -> None:
    """The boundary of the refusal, from the other side: three of four dead is still a team.

    This is the case `return_exceptions=True` exists for, so the refusal must not swallow it. One
    survivor out of four, and the lead runs — with all four strings in its prompt, which is what
    makes the survivor's answer usable at all: a lead told only "here is one reading" cannot know
    it is reasoning from a quarter of the evidence.
    """
    async with RuntimeHarness() as h:
        cast = [
            FailingSpy("dead-1"),
            FailingSpy("dead-2"),
            Spy("alive", answer="THE-LAST-EVIDENCE"),
            FailingSpy("dead-3"),
        ]
        lead, lead_model = scripted_lead([ruling()])
        team = Toy(cast=cast, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    prompt = "\n".join(lead_model.prompts(0))
    assert len(lead_model.contexts) == 1, "one survivor is enough; the lead ran"
    assert "THE-LAST-EVIDENCE" in prompt
    assert prompt.count("unreadable") == 3, "and the three missing planes are visible as missing"
    assert run.correct is True
    assert sum(1 for text in run.briefings.values() if text.startswith("error: ")) == 3


async def test_two_members_under_one_name_are_refused_before_anything_is_spawned() -> None:
    """Briefings are keyed by `member.name`, so a repeated name silently drops a briefing.

    Measured on a cast of two answering `FIRST` and `SECOND`: the report carried
    `{'plane': 'SECOND'}` and the run was graded correct. The lost half is the one nothing can
    show — only a reader comparing `len(briefings)` against `len(members())` could notice, and no
    reader does.

    Refused with the other wiring guards rather than in `brief`, so it costs nothing: `Counting([])`
    and the spies' empty event lists together pin that the refusal precedes both the model call and
    the spawn — the placement lesson `_check_no_oracle_collision` was measured for.
    """
    async with RuntimeHarness() as h:
        cast = [Spy("plane", answer="FIRST"), Spy("plane", answer="SECOND"), Spy("other")]
        model = Counting([])
        lead = Chair().compiled("decide", model=model)
        team = Toy(cast=cast, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(RuntimeError) as raised:
            await handle.run("go")

    message = str(raised.value)
    assert type(raised.value) is RuntimeError, f"a wiring refusal, not a report: {message}"
    assert "more than one member named" in message, "the guard's own wording, not a fallback's"
    assert "['plane']" in message, "naming the duplicate and not the innocent third member"
    assert "'other'" not in message
    assert model.contexts == [], "the guard fires before any model call"
    assert [spy.events for spy in cast] == [[], [], []], "and before a single member was spawned"


# ── 4. The oracle gate ──


async def test_a_rejected_ruling_is_re_asked_with_the_oracles_own_words() -> None:
    """The gate is a post-condition, so refusal is the default and its text is the feedback.

    The runtime runs every validator before the cycle returns and turns any exception into the
    message of a `[VALIDATION ERROR]` user turn the next attempt reads. So exactly two lead calls,
    and the second one's context must contain the sentence the oracle raised — otherwise the
    model was re-asked with nothing to act on. This is `test_gated.py`'s measurement at team
    scale.
    """
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling(admitted=False), ruling(admitted=True)])
        team = Toy(cast=[Spy("a")], lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.verdict.admitted is True
    assert len(lead_model.contexts) == 2, "one refusal, one admitted ruling"
    assert team.refusals == ["not admitted"], "the oracle really did fire"
    assert any("say which members you relied on" in p for p in lead_model.prompts(1))


async def test_the_leads_own_post_conditions_survive_the_oracle_attach() -> None:
    """`replace` overwrites, so a naive attach would delete them — and nothing would say so.

    Measured: `AIFunction.replace` merges through `dataclasses.replace`
    (`ai_function.py:407`), so `replace(post_conditions=[oracle])` on a lead carrying its own
    yields `[oracle]` alone. The failure mode is the worst available: the subclass's checks are
    gone, no error is raised, and the run reports a gated verdict. Asserted on the composed
    function *and* by running it, so the ordering claim (oracle first) is not the only thing
    proved.
    """
    seen: list[str] = []

    def also_check(response: Ruling) -> None:
        seen.append("extra")
        if not response.cites:
            raise AssertionError("cite at least one member")

    async with RuntimeHarness() as h:
        model = Counting([ruling(cites=[]), ruling(cites=["a"])])
        lead = Chair().compiled("decide", model=model, post_conditions=[also_check])
        team = Toy(cast=[Spy("a")], lead=lead)

        composed = team._gated_lead()
        assert [f.__name__ for f in composed.config.post_conditions] == ["oracle", "also_check"]

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.verdict.cites == ["a"]
    assert len(model.contexts) == 2, "the lead's own condition refused the first ruling"
    assert seen == ["extra", "extra"], "and it ran on both attempts"
    assert team.refusals == [], "the oracle admitted both; only the extra condition refused"


async def test_the_leads_other_config_is_not_disturbed_by_the_attach() -> None:
    """Only the fields named in the `replace` may move. `max_attempts` is the one that matters:
    the oracle's retries are bounded by it, and an attach that reset it to the library default of
    10 would silently change how many times a refused ruling is re-asked — and how much a run
    costs when the oracle never admits."""
    async with RuntimeHarness():
        lead = Chair().compiled(
            "decide", model=Counting([]), max_attempts=2, system_prompt="THE-CHAIR", tools=["t"]
        )
        composed = Toy(cast=[], lead=lead)._gated_lead()

    assert composed.config.max_attempts == 2
    assert composed.config.system_prompt == "THE-CHAIR"
    assert composed.config.tools == ["t"], "the lead's own tools are not the hook's business"
    assert composed.config.name == "chair.decide"


# ── 5. Hiring: the budget, and every refusal recoverable ──


def helper_factory(mandate: str) -> Callable[[str], Recruit]:
    """A catalog factory closing over the mandate — the deliberate break from attribute injection.

    `demo/staffing.py:109` writes `sub.mandate = mandate` behind a `type: ignore`, which works
    only because every roster class happens to declare the attribute. The library cannot assume
    it, so the mandate reaches the factory as an argument and the factory decides. This is what
    the shape looks like from a caller's side, which is the point of testing it.
    """
    del mandate
    return lambda name: Member(Helper(name), "assist", model=Counting([helped()]))


async def test_a_lead_hires_delegates_and_dismisses_and_the_roster_records_the_sequence() -> None:
    """The whole hiring loop, in the order the model drove it.

    The log is the audit surface — the same one `demo/cli.py:102-106` prints — so the assertion
    is the action sequence and not just a headcount, and `headcount == 0` at the end proves the
    dismissal really unregistered rather than merely logging.
    """
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                Turn(
                    tool_calls=(("hire", {"role": "helper", "name": "h1", "mandate": "order it"}),)
                ),
                Turn(tool_calls=(("delegate", {"name": "h1", "request": "when"}),)),
                Turn(tool_calls=(("dismiss", {"name": "h1"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[Spy("a")], lead=lead, roles={"helper": helper_factory("order it")})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [entry["action"] for entry in run.hiring_log] == ["hire", "delegate", "dismiss"]
    delegated = next(e for e in run.hiring_log if e["action"] == "delegate")
    assert "done" in delegated["answer"], "the hire's real answer came back to the lead"
    assert team.roster.headcount == 0
    assert len(lead_model.contexts) == 4


async def test_a_second_run_on_the_same_handle_starts_from_an_empty_roster() -> None:
    """A `Team` outlives one `handle.run`, and every promise the roster carries is per run.

    One instance, one handle, two runs, and run 2 must be nobody's inheritor. All four
    consequences of the leak are asserted together because they share one cause and each is
    separately invisible: run 2's report opened with run 1's hiring log, run 1's names were still
    taken, `max_hires` was already spent down by run 1's headcount, and `delegate` reached a
    recruit run 1's `finally` had retired — the failure the `Retired` spy turns into a countable
    fact rather than a plausible answer.

    A fresh `Chair` over a fresh `Counting` per run, because a model is a script and two runs are
    two scripts; and the recruits are counted through the factory, so "run 2 got its own h1" is a
    statement about two objects rather than about one name.

    Run 2's script also carries the *within*-run persistence the reset must not break: it hires on
    its first turn and delegates on its third, so a roster reset any finer than per-run makes the
    `delegate` an error string here. That is the narrowest placement worth testing — measured, the
    `config_hook` fires exactly **once per cycle** and a `handle.run` is one cycle, so moving the
    reset into the hook is indistinguishable from leaving it in `execute`, and there is no finer
    seam for a wrong version to sit at.
    """

    class Retired(Spy):
        """Answers once, and refuses the moment it has been retired — a dead thread's behaviour."""

        async def ask(self, request: str) -> Any:
            if self.retirements:
                raise RuntimeError("I was retired in an earlier run and you delegated to me anyway")
            return await super().ask(request)

    built: list[Retired] = []

    def factory(name: str) -> Recruit:
        spy = Retired(name)
        built.append(spy)
        return spy

    scripts = iter(
        [
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h1", "mandate": "m"}),)),
                ruling(),
            ],
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h1", "mandate": "m"}),)),
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h2", "mandate": "m"}),)),
                Turn(tool_calls=(("delegate", {"name": "h1", "request": "again"}),)),
                ruling(),
            ],
        ]
    )

    class PerRunLead(Toy):
        def lead_function(self) -> AIFunction[..., Any]:
            return Chair().compiled("decide", model=Counting(next(scripts)))

    async with RuntimeHarness() as h:
        team = PerRunLead(cast=[], lead=None, roles={"helper": factory}, max_hires=2)
        handle = await h.spawn(team, thread_name=team.name)

        first = await handle.run("the first question")
        roster_after_first = team.roster
        second = await handle.run("the second question")

    assert [(e["action"], e["name"]) for e in first.hiring_log] == [("hire", "h1")]
    assert [(e["action"], e["name"]) for e in second.hiring_log] == [
        ("hire", "h1"),
        ("hire", "h2"),
        ("delegate", "h1"),
    ], "run 2's report is run 2's: no inherited entry, and the name h1 was free to take again"

    assert team.roster is not roster_after_first, "a fresh roster, not a cleared one"
    assert len(built) == 3, "run 2 built its own h1 rather than finding run 1's name taken"
    assert built[0] is not built[2] and built[0].retirements == 1
    assert [e["action"] for e in second.hiring_log if e["action"] == "delegate_failed"] == [], (
        "and the delegate reached run 2's own live h1, not run 1's retired one"
    )
    assert (first.verdict.admitted, second.verdict.admitted) == (True, True)


async def test_the_per_run_roster_keeps_the_subclasss_own_roster_class() -> None:
    """The reset is `type(self.roster)()` and not `Roster()`, which is not a stylistic choice.

    `WarRoom` narrows the field to a `Staff` whose `record` is what lands a hire's mandate on the
    agent it hired, so a base resetting to the library's own class would leave run 1 correct and
    every later run briefing its hires on nothing — with no exception anywhere. Asserted with a
    roster subclass that records what it saw, so the claim is that the *subclass's* method ran and
    not merely that an `isinstance` held.
    """

    @dataclass
    class Narrower(Roster):
        seen: list[str] = field(default_factory=list)

        def record(self, action: str, **fields: Any) -> None:
            self.seen.append(action)
            super().record(action, **fields)

    class Narrowed(Toy):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.roster = Narrower()

    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Narrowed(cast=[], lead=lead, roles={"helper": lambda name: Spy(name)})
        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

    assert type(team.roster) is Narrower, "the reset must not demote a narrowed roster to Roster"
    assert team.roster.seen == ["hire"], "and the subclass's own record() is what ran"


async def test_the_hiring_cap_refuses_in_text_and_spawns_nothing_past_it() -> None:
    """A cap that raised would end a cycle the model was going to finish; a cap checked after the
    spawn would be a cap that still spent the thread it refused.

    Measured on the way in: a tool returning `"error: ..."` reaches the model as a *successful*
    tool result whose content is that string, and the cycle continues. So the third hire is
    refused, the run completes, and the spy's count proves nothing was constructed for it.
    """
    built: list[str] = []

    def counting_factory(name: str) -> Recruit:
        built.append(name)
        return Spy(name)

    async with RuntimeHarness() as h:
        turns = [
            Turn(tool_calls=(("hire", {"role": "helper", "name": f"h{i}", "mandate": "m"}),))
            for i in range(3)
        ]
        lead, lead_model = scripted_lead([*turns, ruling()])
        team = Toy(cast=[], lead=lead, roles={"helper": counting_factory}, max_hires=2)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    hires = [e for e in run.hiring_log if e["action"] == "hire"]
    assert [e["name"] for e in hires] == ["h0", "h1"], "the third was refused"
    assert built == ["h0", "h1"], "and nothing was constructed for it"
    assert len(lead_model.contexts) == 4, "the cycle continued past the refusal"
    assert run.verdict.admitted is True


async def test_two_hires_in_one_assistant_turn_cannot_race_past_the_cap() -> None:
    """The cap has to hold against the *concurrent* tool executor, which is the runtime's default.

    `ConcurrentToolExecutor` (`strands/agent/agent.py:462`) runs every tool call in one assistant
    turn as its own task, and `recruit.spawn` awaits — so with the registration on the far side of
    that await, both `hire`s read the same pre-hire roster, both pass a cap of one, and the cap is a
    number nobody enforced. `SlowSpawnSpy` is what makes the window real: a spawn that never
    suspends cannot interleave, so a fixture `Spy` would let a broken cap pass.

    Both facts are asserted because they separate the two failures. The *log* says what the roster
    accepted; the *construction spy* says what was built, and a version that spawned both and then
    dropped one would satisfy a log assertion alone while having spent the thread the cap refused.
    """
    built: list[SlowSpawnSpy] = []

    def factory(name: str) -> Recruit:
        spy = SlowSpawnSpy(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
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
        team = Toy(cast=[], lead=lead, roles={"helper": factory}, max_hires=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    hires = [e["name"] for e in run.hiring_log if e["action"] == "hire"]
    assert len(hires) == 1, f"max_hires=1 and two hires in one turn: {hires}"
    assert team.roster.headcount == 1
    assert len([spy for spy in built if spy.events]) == 1, (
        "and the refused hire spawned nothing — a cap that spends what it refuses is not a cap"
    )
    assert len(lead_model.contexts) == 2, "the refusal is text, so the cycle continued"
    assert run.verdict.admitted is True


async def test_two_hires_sharing_one_name_in_one_turn_leak_no_live_thread() -> None:
    """The same race under one name, which is the worse half: it leaks a thread nothing can reach.

    Without the reservation both calls pass the duplicate-name check, both spawn, and the second
    write to `roster.hires[name]` overwrites the first — so the first recruit is live and
    unregistered, and `dismiss`, `execute`'s `finally` and `teardown` all walk `roster.hires` to
    find who to retire. Measured before the fix: two recruits spawned, one in the roster,
    retirement counts `[0, 1]`.

    The assertion is per-object retirement rather than a headcount, because a headcount cannot see
    a leak: one entry in the roster is exactly what the *correct* run produces too.
    """
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
        team = Toy(cast=[], lead=lead, roles={"helper": factory})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    spawned = [spy for spy in built if spy.events]
    assert len(spawned) == 1, f"only one hire may reach a spawn under one name: {len(spawned)}"
    assert [e["name"] for e in run.hiring_log if e["action"] == "hire"] == ["dup"]
    assert all(spy.retirements == 1 for spy in spawned), (
        f"every spawned recruit must be retired; {[s.retirements for s in spawned]} — a 0 is a "
        f"live thread the finally, teardown and dismiss all walk roster.hires to find and cannot"
    )


async def test_a_hire_whose_spawn_raises_holds_neither_its_name_nor_a_slot() -> None:
    """The reservation's rollback: reserving before the await means undoing it when the await fails.

    Otherwise the fix trades one bug for another — a spawn that raises would leave a phantom entry
    holding a name the model can never use and a slot against a cap it never filled. So the second
    hire, under the same name and after the first one's spawn failed, has to succeed.

    The exception is deliberately not caught by `hire`: a spawn fault is not one of the five
    model-fixable failures, so it surfaces as a tool fault and the cycle continues past it — which
    is why the model gets a second turn to try again at all.
    """
    built: list[Spy] = []

    def factory(name: str) -> Recruit:
        spy: Spy = UnspawnableSpy(name) if not built else Spy(name)
        built.append(spy)
        return spy

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": factory}, max_hires=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["name"] for e in run.hiring_log if e["action"] == "hire"] == ["h"], (
        "the retry succeeded, so the failed spawn released the name"
    )
    assert team.roster.headcount == 1, "and the slot: max_hires=1 and the retry still fit"
    assert team.roster.hires["h"] is built[1], "the live recruit is registered, not the dead one"
    assert len(lead_model.contexts) == 3


async def test_an_unknown_role_and_a_duplicate_name_are_both_recoverable_text() -> None:
    """Three hire-side failures, three things a model can fix, so three strings and no raises.

    Both refusals are asserted to have spawned nothing *and* to have left the log clean: a
    refusal recorded as a hire would corrupt the audit surface even though no thread was created.
    """
    built: list[str] = []

    def counting_factory(name: str) -> Recruit:
        built.append(name)
        return Spy(name)

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "wizard", "name": "w", "mandate": "m"}),)),
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": counting_factory})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["name"] for e in run.hiring_log if e["action"] == "hire"] == ["h"]
    assert built == ["h"], "neither the unknown role nor the duplicate name built anything"
    assert len(lead_model.contexts) == 4, "the model recovered from both"
    assert run.verdict.admitted is True


async def test_a_dismissal_whose_retire_faults_leaves_the_recruit_reachable_by_the_unwind() -> None:
    """`dismiss` retires first and unregisters only on success, which is what keeps a fault
    recoverable.

    A `pop` before the await drops the roster's only reference to the recruit into a local, so a
    `retire` that raises — a coordinator hiccup, a `ThreadNotFoundError` from something else's
    teardown — leaves a live thread that `execute`'s `finally`, `teardown` and a second `dismiss`
    all walk `roster.hires` to find and none of them can. Measured before the fix: one retire call
    total, and the recruit still alive after both the `finally` and an explicit `teardown`.

    Asserted from the recruit's own `alive`, because that is the only thing that knows: the log
    shows no dismissal either way, and a headcount of zero is what the *correct* run reaches too
    — by way of the `finally` retrying the retire the fault interrupted.
    """
    async with RuntimeHarness() as h:
        recruit = FlakyRetireSpy("h")
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("dismiss", {"name": "h"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": lambda name: recruit})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")
        await team.teardown()

    assert not recruit.alive, (
        f"the recruit is still live after the finally and an explicit teardown; "
        f"{recruit.retirements} retire call(s) reached it"
    )
    assert recruit.retirements >= 2, "the first raised, and something retried it — that is the fix"
    assert [e["action"] for e in run.hiring_log] == ["hire"], (
        "the dismissal did not complete, so it is not in the audit as one"
    )
    assert len(lead_model.contexts) == 3, "and the fault did not end the cycle"


async def test_a_dismissal_that_succeeds_unregisters_and_is_recorded() -> None:
    """The positive control on the reordering: a clean `dismiss` still ends the thread *and* takes
    the recruit off the roster, which is what frees the name and the cap slot.

    Worth its own test because the fix moved the `pop` after the await, and a version that retired
    and then forgot to unregister would pass every leak assertion above while making `max_hires`
    monotonic — a lead could dismiss all day and never hire again.
    """
    async with RuntimeHarness() as h:
        first, second = Spy("h"), Spy("h")
        recruits = iter([first, second])
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("dismiss", {"name": "h"}),)),
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": lambda name: next(recruits)}, max_hires=1)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert [e["action"] for e in run.hiring_log] == ["hire", "dismiss", "hire"], (
        "the dismissal released both the name and the only slot under max_hires=1"
    )
    assert first.retirements == 1, "dismissed once by the tool, and not again by the finally"
    assert second.retirements == 1, "and the replacement was retired by the finally"
    assert "h" not in {e["name"] for e in run.hiring_log if e["action"] == "delegate_failed"}
    assert len(lead_model.contexts) == 4


async def test_delegating_to_someone_unhired_and_dismissing_a_stranger_are_text_too() -> None:
    """The other two model-fixable mistakes. Together with the three above, every failure in the
    hiring seam is recoverable — which is the property that lets a lead use these tools at all."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("delegate", {"name": "ghost", "request": "hi"}),)),
                Turn(tool_calls=(("dismiss", {"name": "ghost"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": lambda name: Spy(name)})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.hiring_log == [], "neither mistake is an action worth recording"
    assert len(lead_model.contexts) == 3


async def test_a_hire_that_raises_on_delegate_is_reported_as_a_failure_not_a_crash() -> None:
    """A subagent's fault is the lead's problem to re-scope, so it comes back as text — and it is
    logged as `delegate_failed`, because a delegation that produced nothing must not look in the
    audit like one that produced an answer."""

    class Broken(Spy):
        async def ask(self, request: str) -> Any:
            raise ZeroDivisionError("the helper is wrong")

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("delegate", {"name": "h", "request": "go"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": lambda name: Broken(name)})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    actions = [e["action"] for e in run.hiring_log]
    assert actions == ["hire", "delegate_failed"]
    assert "ZeroDivisionError" in run.hiring_log[1]["error"]
    assert len(lead_model.contexts) == 3


async def test_no_catalog_means_no_hiring_tools_at_all() -> None:
    """An empty catalog is a meaningful configuration and the default: a team whose cast is fixed
    grants no hiring tools, which is one fewer thing a lead can do wrong. Asserted from the
    composed function, because the absence of a hook is what makes the absence of the tools
    true."""
    async with RuntimeHarness():
        lead = Chair().compiled("decide", model=Counting([]))
        assert Toy(cast=[], lead=lead)._gated_lead().config.config_hook is None
        with_roles = Toy(cast=[], lead=lead, roles={"helper": lambda name: Spy(name)})
        assert with_roles._gated_lead().config.config_hook is not None


# ── 6. The parent edge a hire writes ──


async def test_a_hire_is_spawned_as_a_child_of_the_lead_not_of_the_team() -> None:
    """`parent_id=ctx.thread_id` inside the hook means the *hiring* agent is the recorded parent,
    which is what makes cost attribution up the tree a free consequence of hiring correctly.

    Asserted from the runtime's own `THREAD_SPAWNED` events on the lead's thread — the demo's
    `test_demo.py:154-181` claim, restated for the library seam — and negatively against the
    team's own log, so a hook that closed over the wrong thread id fails here.
    """
    async with RuntimeHarness() as h:
        real_hires: list[Member] = []

        def factory(name: str) -> Recruit:
            member = Member(Helper(name), "assist", model=Counting([helped()]))
            real_hires.append(member)
            return member

        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": factory})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

        lead_thread_id = run.hiring_log[0]["thread_id"]
        team_children = await h.events(handle.id, kinds=[EventKind.THREAD_SPAWNED])
        child_ids = {str(e.child_thread_id) for e in team_children}
        lead_id = next(tid for tid in child_ids)
        lead_children = await h.events(lead_id, kinds=[EventKind.THREAD_SPAWNED])

    assert {str(e.child_thread_id) for e in lead_children} == {lead_thread_id}
    assert lead_thread_id not in child_ids, "a hire is the lead's child, not the team's"


async def test_a_hires_tokens_reach_the_teams_rollup_through_that_edge() -> None:
    """The consequence the edge exists for, measured end to end: a hire two levels down bills up.

    Without the parent edge the rollup would stop at the lead and the team would under-report
    every subagent its lead created — which is the failure `runtime/usage.py:65-72` describes
    from the other side.
    """
    async with RuntimeHarness() as h:

        def factory(name: str) -> Recruit:
            return Member(
                Helper(name),
                "assist",
                model=Counting(
                    [
                        Turn(
                            tool_calls=(("Helped", {"note": "x"}),),
                            input_tokens=100,
                            output_tokens=50,
                        )
                    ]
                ),
            )

        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                Turn(tool_calls=(("delegate", {"name": "h", "request": "go"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[], lead=lead, roles={"helper": factory})

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.input_tokens >= 100 and run.output_tokens >= 50, (
        f"the hire's spend is missing from the rollup: {run.input_tokens}/{run.output_tokens}"
    )
    assert run.turns == 4, "three lead cycles and the hire's one"


# ── 7. Teardown ──


async def test_a_lead_that_never_satisfies_the_oracle_still_retires_everybody() -> None:
    """The `finally` is unconditional, and it has to be: a mid-run fault must not leave a cast of
    live threads on the coordinator.

    Measured on the way in: an exception out of `execute` propagates to the caller of
    `handle.run` and the worker does *not* call `teardown()` on that path — so this `finally` is
    the only thing that unwinds, which is why the claim is asserted from the recruits' own
    counters rather than from the absence of a complaint.

    The fault is a lead that exhausts its attempts against the oracle, which is the realistic
    one: `max_attempts=1` means one initial try plus one retry, both refused, then
    `AIFunctionError`. The lead hired before it started failing, so the hire is on the hook too —
    a run that unwound only the declared cast would leave that thread alive.
    """
    async with RuntimeHarness() as h:
        cast = [Spy("a"), Spy("b")]
        hired = Spy("h")
        model = Counting(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(admitted=False),
                ruling(admitted=False),
                ruling(admitted=False),
            ]
        )
        lead = Chair().compiled("decide", model=model, max_attempts=1)
        team = Toy(cast=cast, lead=lead, roles={"helper": lambda name: hired})

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(Exception) as raised:
            await handle.run("go")

    assert "not satisfied" in str(raised.value), f"the oracle exhausted the attempts: {raised}"
    assert team.refusals == ["not admitted", "not admitted"], "it really was the oracle refusing"
    assert [spy.retirements for spy in cast] == [1, 1], "every member was retired"
    assert hired.retirements == 1, "and so was the hire the lead made before it started failing"


async def test_an_oracle_that_is_itself_broken_burns_the_retries_and_the_run_still_unwinds() -> (
    None
):
    """A bug in an oracle is indistinguishable from a refusal, and the library cannot fix that.

    Measured: a validator raising `AttributeError` under `max_attempts=2` is called **three
    times** and the cycle raises `AIFunctionError: Result not satisfied after 3 attempt(s)` — the
    original type is gone, because the runtime turns every validator exception into feedback for
    the model. That is `gated.py`'s central trap, and `Team` deliberately does not fault-wrap
    `oracle` (see `docs/design/team.md`). What this pins is the part the library *does* owe: the
    cast is still retired, so a broken oracle costs turns and not leaked threads.
    """

    class Typoed(Toy):
        def oracle(self, response: Any) -> None:
            raise AttributeError("typo in the oracle")

    async with RuntimeHarness() as h:
        cast = [Spy("a")]
        model = Counting([ruling(), ruling(), ruling(), ruling()])
        lead = Chair().compiled("decide", model=model, max_attempts=2)
        team = Typoed(cast=cast, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(Exception) as raised:
            await handle.run("go")

    assert "not satisfied after 3 attempt" in str(raised.value)
    assert len(model.contexts) == 3, "one initial try plus two retries, all burnt"
    assert cast[0].retirements == 1, "and the cast was still retired"


async def test_teardown_covers_the_hires_when_something_outside_the_run_terminates_the_team() -> (
    None
):
    """The third path. `execute`'s `finally` covers the normal and the faulting run; a supervisor
    calling `terminate_now` reaches neither, and the worker awaits `teardown()` there — measured.
    Idempotence is asserted in the same test because an unwind that ran twice must not crash."""
    async with RuntimeHarness():
        hired = Spy("h")
        team = Toy(cast=[], lead=None)
        team.roster.hires["h"] = hired
        await team.teardown()
        await team.teardown()

    assert hired.retirements == 2, "retire is called each time; idempotence is the recruit's job"


async def test_an_already_retired_member_does_not_abort_the_unwind_of_the_others() -> None:
    """`return_exceptions=True` on the unwind, and the reason: a recruit something else already
    tore down raises `ThreadNotFoundError`, which is a `KeyError` and sails past the handlers
    callers write. Without it the first failure leaves every later member alive — the exact
    failure an unwind loop exists to prevent."""

    class Stubborn(Spy):
        async def retire(self) -> None:
            self.retirements += 1
            raise KeyError("already gone")

    async with RuntimeHarness() as h:
        first, second = Stubborn("first"), Spy("second")
        lead, _ = scripted_lead([ruling()])
        handle = await h.spawn(Toy(cast=[first, second], lead=lead), thread_name="toy")
        run = await handle.run("go")

    assert run.verdict.admitted is True
    assert first.retirements == 1 and second.retirements == 1


# ── 8. The required-overrides guard ──


async def test_a_team_missing_every_required_override_is_refused_at_construction() -> None:
    """Before any spawn and before any model call, naming all four at once.

    `Counting([])` is not usable here because the refusal precedes construction of anything at
    all — which is the stronger property: there is no coordinator, no thread, and no cycle for a
    late guard to have spent. The message must name each missing override, because a team with
    three of them missing would otherwise cost three failed wirings to diagnose.
    """
    with pytest.raises(RuntimeError) as raised:
        Bare()

    message = str(raised.value)
    assert type(raised.value) is RuntimeError, f"a wiring refusal, not a report: {message}"
    assert "must be overridden" in message, "the guard's own wording, not a fallback's"
    for name in ("members", "briefing", "lead_function", "oracle"):
        assert repr(name) in message, f"{name} is missing from the refusal"


async def test_the_refusal_names_only_what_is_actually_missing() -> None:
    """A guard that listed all four regardless would be a constant, not a check. `Partial`
    supplies `members` and `oracle`, so exactly the other two may appear."""
    with pytest.raises(RuntimeError) as raised:
        Partial()

    message = str(raised.value)
    assert "'briefing'" in message and "'lead_function'" in message
    assert "'members'" not in message and "'oracle'" not in message


async def test_the_guard_is_satisfied_by_an_override_and_nothing_else() -> None:
    """The negative control: a fully wired team constructs, and the base's own methods raise
    `NotImplementedError` when reached directly — so the guard is what makes the refusal a
    wiring-time event rather than a mid-run one."""
    Toy(cast=[], lead=None)  # constructs
    assert Team.REQUIRED == ("members", "briefing", "lead_function", "oracle")


# ── 9. fork ──


async def test_fork_is_refused_because_a_team_run_is_not_forkable() -> None:
    """A fork copies the event log, which is the whole of an `AIThread`'s state and nowhere near
    the whole of a team's: two branches sharing one live cast would retire each other's members
    and hire into one dict. `protocols.py:235` names `NotImplementedError` as the honest answer."""
    team = Toy(cast=[], lead=None)
    with pytest.raises(NotImplementedError, match="not forkable"):
        await team.fork()


async def test_notify_is_accepted_and_starts_nothing() -> None:
    """The sanctioned no-op for a thread that ignores injections: the phases are fixed, so there
    is no boundary at which a team could observe one."""
    team = Toy(cast=[], lead=None)
    assert await team.notify("anything") is None


# ── 10. The typed-members claim ──


async def test_the_lead_holds_each_member_as_a_typed_tool_and_no_member_is_reachable_by_chat() -> (
    None
):
    """The kernel claim the module rests on, as a test rather than a docstring.

    Two halves. Positively: every member's capability is in the lead's `config.tools` under its
    qualified name, so composition happened by *typing* — `test_method.py:216-224`'s claim inside
    the skeleton. Negatively: every member's `input_shape` is `STRUCTURED`, and `send_message`
    refuses any peer that is not `STR_PROMPT` (`ai_thread/tools.py:172-176`), so no member is
    addressable by the message bus at all. A team that quietly compiled its members down to one
    `str` to make them chattable would fail the second half.
    """
    async with RuntimeHarness():
        left, right = Analyst("left"), Analyst("right")
        tools = [*left.agents(), *right.agents()]
        lead = Chair().compiled("decide", model=Counting([]), tools=tools)
        team = Toy(cast=[Member(left, "read"), Member(right, "read")], lead=lead)

        composed = team._gated_lead()
        published = {t.config.name for t in composed.config.tools}

    assert published == {"left-analyst.read", "right-analyst.read"}
    for agent in (left, right):
        assert agent.compiled("read").input_shape is InputShape.STRUCTURED
    assert Chair().compiled("decide").input_shape is InputShape.STRUCTURED


async def test_a_typed_member_is_briefed_through_its_real_keyword() -> None:
    """`Member` names which keyword the briefing lands in, defaulting to the first parameter, so
    the typed contract survives into the team: `read(focus=...)` and never `read(some_string)`."""
    async with RuntimeHarness() as h:
        model = Counting([reading()])
        member = Member(Analyst("left"), "read", model=model)
        assert member.name == "left-analyst.read"

        await member.spawn(h.coordinator)
        try:
            result = await member.ask("THE-BRIEFING")
        finally:
            await member.retire()

    assert isinstance(result, Reading)
    assert any("with THE-BRIEFING in mind" in p for p in model.prompts(0)), (
        "the briefing reached the typed parameter and the template rendered it"
    )


async def test_a_capability_with_no_parameter_cannot_be_briefed_and_says_so_at_wiring() -> None:
    """Refused when the `Member` is built, not from the middle of the barrier. Without it the
    failure is a `TypeError` from the signature bind after the team is already assembled."""

    class Mute(MethodAgent):
        name = "mute"

        @ai_method(Reading, description="Takes nothing")
        def observe(self) -> Reading:
            """Observe."""

    with pytest.raises(RuntimeError, match="takes no positional parameter"):
        Member(Mute(), "observe")


# ── 11. Serialization ──


class NarrowRun(TeamRun):
    """A `TeamRun` whose verdict type is declared, which is what makes the round-trip hold."""

    verdict: Ruling


class Narrow(Toy):
    def run_type(self) -> type[TeamRun]:
        return NarrowRun


def test_a_narrowed_teamrun_round_trips_through_the_protocols_pair() -> None:
    """`protocols.py` states `deserialize_result(serialize_result(x)) == x` as an `Ensures`, and
    the base cannot deliver it: a pydantic field typed `Any` validates a serialised `BaseModel`
    back as a plain `dict` — measured — so the base round-trips *shape* and not equality. That is
    why `run_type()` exists, and this asserts both halves so the seam is not decorative."""
    run = NarrowRun(
        verdict=Ruling(admitted=True, cites=["a"]),
        correct=True,
        oracle_failures=[],
        briefings={"a": "read"},
        hiring_log=[{"action": "hire", "name": "h"}],
        input_tokens=11,
        output_tokens=7,
        turns=2,
        wall_seconds=0.3,
    )
    team = Narrow(cast=[], lead=None)
    back = team.deserialize_result(team.serialize_result(run))

    assert back == run
    assert isinstance(back.verdict, Ruling)
    assert back.hiring_log == [{"action": "hire", "name": "h"}]

    # And the base, honestly: same fields, verdict widened to a dict rather than equal.
    base = Toy(cast=[], lead=None)
    widened = base.deserialize_result(base.serialize_result(run))
    assert widened.verdict == {"admitted": True, "cites": ["a"]}
    assert widened.turns == 2


# ── grade: the post-run judgment ──


async def test_grade_runs_after_the_oracle_and_its_failures_reach_the_report() -> None:
    """The two hooks answer different questions, so a team may hold a standard it would be wrong
    to re-ask against. Here the oracle admits a ruling that cites nobody and `grade` refuses it,
    which is only observable because `correct` and `oracle_failures` are separate fields."""
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling(cites=[])])
        team = Graded(cast=[], lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        run = await handle.run("go")

    assert run.verdict.admitted is True, "the oracle admitted it"
    assert run.correct is False
    assert run.oracle_failures == ["the ruling cites nobody"]


async def test_the_default_grade_reports_correct_because_the_oracle_already_gated() -> None:
    """The defaulted half of the asymmetry, stated as a test so the default is a decision rather
    than an omission: a verdict that reached `grade` satisfied the oracle or the cycle raised."""
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling()])
        handle = await h.spawn(Toy(cast=[], lead=lead), thread_name="toy")
        run = await handle.run("go")

    assert (run.correct, run.oracle_failures) == (True, [])


# ── The progress markers a live tape subscribes to ──


async def test_every_phase_writes_a_namespaced_progress_marker() -> None:
    """`CustomEvent`s are what a live tape renders (`demo/live.py:55-63`), and they are the only
    observation of a phase a reader outside the process has. Kinds stay under `team.*` so a
    subscriber can filter one team's progress out of a shared log."""
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead(
            [
                Turn(tool_calls=(("hire", {"role": "helper", "name": "h", "mandate": "m"}),)),
                ruling(),
            ]
        )
        team = Toy(cast=[Spy("a")], lead=lead, roles={"helper": lambda name: Spy(name)})
        handle = await h.spawn(team, thread_name=team.name)
        await handle.run("go")

        kinds = [str(getattr(e, "kind", "")) for e in await h.events(handle.id)]

    assert "team.assembled" in kinds
    assert "team.briefings_in" in kinds
    assert "team.lead_running" in kinds
    assert "team.graded" in kinds
    assert kinds.index("team.assembled") < kinds.index("team.briefings_in")
    assert kinds.index("team.briefings_in") < kinds.index("team.lead_running")


# ── The config_hook conflict, refused rather than resolved ──


async def test_a_lead_with_its_own_hook_and_a_catalog_is_refused_before_any_spend() -> None:
    """The runtime calls exactly one `config_hook` per cycle (`ai_thread.py:548-553`), so a lead
    that brought its own and a team with a catalog is a real conflict.

    Refused loudly rather than resolved by precedence, because either silent outcome is invisible:
    the lead loses its tools, or the team loses its hiring. `Counting([])` proves the refusal
    precedes the spend — a resolution that picked a winner and ran would raise `ScriptExhausted`
    here instead.
    """
    async with RuntimeHarness() as h:
        model = Counting([])
        lead = Chair().compiled("decide", model=model, config_hook=lambda ctx: {"tools": []})
        team = Toy(cast=[], lead=lead, roles={"helper": lambda name: Spy(name)})

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(RuntimeError) as raised:
            await handle.run("go")

    message = str(raised.value)
    assert "already carries a config_hook" in message, "the guard's own wording"
    assert model.contexts == [], "the refusal must precede any model call"
    assert team.roster.log == [], "and nothing was hired against a hook that was never attached"


async def test_a_lead_with_its_own_hook_and_no_catalog_keeps_it() -> None:
    """The negative control: the conflict is between a hook and a catalog, not a hook and a team.
    A fixed-cast team must not be forbidden from having a lead with cycle-local tools."""
    async with RuntimeHarness():
        hook = lambda ctx: {"tools": []}  # noqa: E731 — the identity matters, not the name
        lead = Chair().compiled("decide", model=Counting([]), config_hook=hook)
        assert Toy(cast=[], lead=lead)._gated_lead().config.config_hook is hook


# ── The oracle/lead parameter collision, refused rather than reported by a model ──


class Colliding(Toy):
    """An oracle whose result parameter is named after one of the lead's parameters.

    The silent shape, which is the only one worth guarding. The runtime fills the first slot with
    the verdict positionally and then injects `question=` by keyword, so the call raises
    `TypeError: got multiple values for argument 'question'` — and the runtime catches that and
    reports it to the *model* as a validation failure. Every verdict appears refused, for a reason
    no model can act on.
    """

    def oracle(self, question: Any) -> None:
        del question


async def test_an_oracle_named_after_a_lead_parameter_is_refused_before_any_spend() -> None:
    """`gated._check_no_collision` at team scale, and reachable now that a lead is typed.

    Two assertions carry the weight. `Counting([])` means any model call raises `ScriptExhausted`
    instead of this error, so the refusal provably precedes the spend; and the exception must be a
    plain `RuntimeError` about wiring rather than the `AIFunctionError` an exhausted retry loop
    produces — which is exactly what happens with the guard removed, after burning every attempt.
    """
    async with RuntimeHarness() as h:
        model = Counting([])
        cast = [Spy("a"), Spy("b")]
        lead = Chair().compiled("decide", model=model)
        team = Colliding(cast=cast, lead=lead)

        handle = await h.spawn(team, thread_name=team.name)
        with pytest.raises(RuntimeError) as raised:
            await handle.run("go")

    message = str(raised.value)
    assert type(raised.value) is RuntimeError, f"a wiring refusal, not a report: {message}"
    assert "result parameter is named" in message, "the guard's own wording, not a fallback's"
    assert "'question'" in message, "the message must name the colliding parameter"
    assert model.contexts == [], "the guard must fire before any model call"
    assert [spy.events for spy in cast] == [[], []], (
        "and before a single member was spawned — measured with the composition left after the "
        "barrier, this refusal cost two spawns and two real briefing cycles to reach"
    )


async def test_the_collision_guard_covers_the_leads_own_conditions_too() -> None:
    """The trap is a property of the runtime's kwarg injection, so an extra condition hits it the
    same way the oracle would. A guard that only checked `self.oracle` would let a subclass add
    the same mistake one line later."""

    async with RuntimeHarness():
        # `rigour` is the lead's second parameter, and this condition's FIRST — the fatal slot.
        def colliding(rigour: Any) -> None:
            del rigour

        lead = Chair().compiled("decide", model=Counting([]), post_conditions=[colliding])
        with pytest.raises(RuntimeError, match="'rigour'"):
            Toy(cast=[], lead=lead)._gated_lead()


async def test_a_condition_naming_a_non_first_lead_parameter_is_allowed() -> None:
    """Narrow on purpose, in `gated._check_no_collision`'s spirit: the runtime *documents* the
    keyword injection, so a validator whose second parameter names a lead parameter is legitimate
    and must not be forbidden."""

    def reads_the_question(response: Any, question: str = "") -> None:
        del response, question

    async with RuntimeHarness():
        lead = Chair().compiled("decide", model=Counting([]), post_conditions=[reads_the_question])
        composed = Toy(cast=[], lead=lead)._gated_lead()

    assert [f.__name__ for f in composed.config.post_conditions] == [
        "oracle",
        "reads_the_question",
    ]


def test_repr_names_the_team_and_its_budget() -> None:
    assert repr(Toy(cast=[], lead=None, name="bench", max_hires=2)) == ("<Toy 'bench' max_hires=2>")


def test_hiring_tools_is_usable_without_a_team_at_all() -> None:
    """The seam is a function over a roster and a mapping, so it composes outside this class —
    which is what lets Wave 2 bind the demo's `ROSTER` without subclassing anything."""
    roster = Roster()
    hook = hiring_tools(roster, {"helper": lambda name: Spy(name)}, max_hires=1)
    assert callable(hook)
    assert roster.headcount == 0
