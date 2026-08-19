"""Live Bedrock tests for the five kernel classes.

Every kernel behavior is already pinned offline with scripted models. These tests answer a
different question: does each piece work against the real model? They call Bedrock with real
credentials, so they are skipped by default. Set `PNEUMA_LIVE_KERNEL=1` to run them. On an
EC2 box with an instance profile that has Bedrock access, no other setup is needed.

Run them all:

    PNEUMA_LIVE_KERNEL=1 uv run pytest tests/app/test_kernel_live.py -p no:randomly -v

Or run one:

    PNEUMA_LIVE_KERNEL=1 uv run pytest tests/app/test_kernel_live.py -k thread -v

Each test uses `effort="low"` and small token budgets to keep cost and latency down. A full
run makes roughly seven model calls. The assertions check plumbing, not model quality: typed
results arrive, history carries across calls, a rejected answer comes back corrected, the
recalled value lands in the optimizer's graph, the walk stays legal, and the team run
returns a graded result with its threads cleaned up.
"""

from __future__ import annotations

import os
import tempfile
from typing import Annotated, Any

import pytest
from ai_functions import Coordinator, JSONMemoryBackend
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness
from pydantic import BaseModel, Field

from pneuma.method import MethodAgent, ai_method
from pneuma.model import opus5
from pneuma.recall import Recall, Recalled

live = pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE_KERNEL") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE_KERNEL=1 to run the kernel against the real model",
)


def small_model() -> Any:
    """The cheapest sensible live configuration: low effort, tight ceiling."""
    return opus5("low", max_tokens=4_000, show_thinking=False)


# ── Fixtures (module level: compile resolves annotations against module globals) ──


class Answer(BaseModel):
    """A short factual answer."""

    text: str = Field(description="The answer, one sentence.")
    topic: str = Field(description="The topic word the question was about.")


class Tutor(MethodAgent):
    """One typed ability over private context, for the MethodThread round trip."""

    name = "tutor"

    def __init__(self, style: str) -> None:
        self.style = style

    @ai_method(Answer, description="Answer a question in one sentence")
    def answer(self, question: str) -> Answer:
        """Answer in a {self.style} style, one sentence only.

        Question: {question}"""


class Pick(BaseModel):
    """A number proposal, for the gated retry test."""

    value: int = Field(description="The proposed number.")
    reason: str = Field(description="One short sentence.")


class Advice(BaseModel):
    """The memory schema for the recall test."""

    guidance: list[str] = Field(
        default_factory=lambda: [
            "when the question mentions water, answer with the boiling point",
            "when the question mentions light, answer with the speed of light",
        ],
        description="Stored advice entries.",
    )


class Navigator(MethodAgent):
    """One ability with a memory-declared parameter, for the recall test."""

    name = "live-navigator"

    @ai_method(Answer, description="Answer using retrieved guidance")
    def answer(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=1)],
        question: str,
    ) -> Answer:
        """Guidance retrieved for this question: {advice}

        Question: {question}
        Answer in one sentence."""


@live
async def test_live_a_method_thread_carries_history_across_two_real_calls() -> None:
    """MethodThread: two calls, one conversation, against the real model.

    The second call asks the model to repeat the topic of the first question without
    restating it. The model can only do that if the first turn is really in its context.
    """
    async with RuntimeHarness() as h:
        thread = await Tutor("terse").spawn("answer", h.coordinator, model=small_model())
        first = await thread.run(question="What temperature does water boil at, at sea level?")
        second = await thread.run(
            question="What was the topic word of my previous question? Answer with just it."
        )
        await thread.retire()

    assert isinstance(first, Answer) and isinstance(second, Answer)
    assert "water" in (second.text + second.topic).lower()


@live
async def test_live_a_gated_proposer_corrects_a_rejected_answer() -> None:
    """GatedProposer: the model's first answer is refused and its retry must comply.

    The gate rejects any value below 50. The prompt nudges the model toward a small value,
    so the first proposal is very likely rejected, and the runtime feeds the rejection back.
    Whatever path the model takes, the accepted answer must satisfy the gate.
    """
    from dataclasses import dataclass

    from pneuma.gated import GatedProposer

    @dataclass
    class Floor:
        ok: bool
        message: str

        def report_text(self) -> str:
            return self.message

    class Picker(GatedProposer):
        name = "live-picker"

        def __init__(self) -> None:
            super().__init__(gate=lambda pick: Floor(pick.value >= 50, "value must be 50 or more"))

        def candidate_of(self, response: Any) -> Any:
            return response

        @ai_method(Pick, description="Propose a number", max_attempts=3)
        def propose(self, hint: str) -> Pick:
            """Propose a single integer. {hint}"""

    async with RuntimeHarness():
        picker = Picker()
        fn = picker.gated("propose", model=small_model())
        result = await fn(hint="A small number like 3 would be nice.")

    assert isinstance(result, Pick)
    assert result.value >= 50, "the gate admitted a value it should have rejected"


@live
async def test_live_recall_injects_memory_and_the_optimizer_sees_it() -> None:
    """Recall: a real call gets its advice from memory, and the gradient graph carries it.

    The claim under test is the whole point of the Recall design. The retrieved value must
    arrive as a call argument, and after the traced call the optimizer's graph must contain
    the `guidance` parameter with the retrieved entry ids attached.
    """
    async with RuntimeHarness():
        memory = JSONMemoryBackend(
            Advice, "live-navigator", path=os.path.join(tempfile.mkdtemp(), "advice.json")
        )
        try:
            navigator = Navigator()
            result = await Recall(navigator, memory).trace(
                "answer",
                question="What temperature does water boil at, at sea level?",
                queries={"advice": "a question about water"},
                overrides={"model": small_model()},
            )
            graph = await build_graph_from_result(result, [memory])
        finally:
            memory.close()

    assert isinstance(result.value, Answer)
    assert "100" in result.value.text or "212" in result.value.text
    assert [p.name for p in graph.parameters] == ["guidance"]
    assert graph.parameters[0].meta["results"], "no retrieved entry ids reached the graph"


@live
async def test_live_a_process_agent_walks_a_small_process_legally() -> None:
    """ProcessAgent: the real model chooses transitions and the interpreter keeps it legal.

    A three-state process with one real branch. Whatever the model proposes, the run must
    end at a terminal state and every executed transition must be one the process defines.
    """
    from pneuma.process.agent import ProcessAgent
    from pneuma.process.ir import Process, State, Transition

    process = Process(
        name="Errand",
        description="Buy milk: go to the store, pay, done.",
        initial_state="Home",
        states=[
            State(name="Home", description="Starting point"),
            State(name="Store", description="At the store, ready to pay"),
            State(name="Done", terminal=True),
        ],
        transitions=[
            Transition(name="WalkToStore", source="Home", target="Store"),
            Transition(name="Pay", source="Store", target="Done"),
            Transition(name="GoHomeEmptyHanded", source="Store", target="Home"),
        ],
    )

    async with RuntimeHarness():
        agent = ProcessAgent(process, context="a simple errand")
        run = await agent.work(
            facts="You want to buy milk and you have money.",
            max_steps=6,
            model=small_model(),
        )

    assert run.final_state == "Done"
    defined = {t.name for t in process.transitions}
    assert set(run.path) <= defined, f"undefined transition in {run.path}"


@live
async def test_live_a_small_team_runs_end_to_end() -> None:
    """Team: one member, one lead, a real gate on the lead, full cleanup.

    The member is a Tutor answering one briefing question. The lead reads the briefing
    (delivered by the `Briefing` hook into its own prompt) and answers the request. The
    lead's own post_condition requires a non-empty answer.
    """
    from ai_functions import AIFunction
    from ai_functions.ai_thread.config import ThreadConfig

    from pneuma.team import Member, Team
    from pneuma.team.hooks import Briefing

    def not_empty(response: Any) -> None:
        assert isinstance(response, Answer)
        assert response.text.strip(), "empty answer"

    async def prompt(request: str) -> str:
        return (
            "You lead a quiz team. Using what your team reported in this conversation, "
            f"answer the request in one sentence. Request: {request}"
        )

    lead = AIFunction(
        prompt,
        Answer,
        ThreadConfig(
            name="quiz-lead",
            description="Answers the request from the team's briefings",
            model=small_model(),
            post_conditions=(not_empty,),
        ),
    )
    member = Member(Tutor("factual"), "answer", model=small_model())
    briefing = Briefing(
        lambda m: "What temperature does water boil at, at sea level? One sentence."
    )

    async with RuntimeHarness() as h:
        team = Team(lead, [member], hooks=[briefing])
        run = await team.run("At what temperature does water boil at sea level?", h.coordinator)

    assert "100" in run.answer.text or "212" in run.answer.text, (
        f"the answer did not state the boiling point: {run.answer.text}"
    )
    assert run.hooks_data["briefing"], "no briefing was recorded"
    assert not run.hooks_data["briefing"]["tutor.answer"].startswith("error: ")


@live
async def test_live_a_posted_discovery_lands_on_the_worklog_and_fans_out() -> None:
    """Team worklog: a real member posts a real discovery and the fan-out reaches the others.

    Plumbing, not model quality, like every test in this file: the member is *instructed* to
    call `post_discovery`, and the assertions check that the entry landed on `TeamRun.worklog`
    with the wired source, that it was delivered to the other member and the lead, and that no
    delivery failed. Offline tests already pin the wire from captured contexts; this answers
    whether the real model calls the injected tool and the notify path holds against the real
    runtime.
    """
    from ai_functions import AIFunction
    from ai_functions.ai_thread.config import ThreadConfig

    from pneuma.team import Member, Team
    from pneuma.team.hooks import Briefing, Worklog

    scout, archivist = Tutor("scout"), Tutor("archivist")
    scout.name, archivist.name = "scout", "archivist"  # Tutor.name is a class attr

    def question_for(member: Any) -> str:
        if "scout" in member.name:
            return (
                "First, call the post_discovery tool exactly once with kind='obstacle' "
                "and a body that contains the word VOLCANO. Then answer: what is the "
                "tallest mountain on Earth? One sentence."
            )
        return "What is the tallest mountain on Earth? One sentence."

    async def prompt(request: str) -> str:
        return (
            "You lead a field team. Using what your team reported in this conversation, "
            f"answer the request in one sentence. Request: {request}"
        )

    lead = AIFunction(
        prompt,
        Answer,
        ThreadConfig(
            name="field-lead",
            description="Answers the request from the team's briefings",
            model=small_model(),
        ),
    )
    members = [
        Member(scout, "answer", model=small_model()),
        Member(archivist, "answer", model=small_model()),
    ]

    async with RuntimeHarness() as h:
        team = Team(lead, members, hooks=[Briefing(question_for), Worklog()])
        run = await team.run("What is the tallest mountain on Earth?", h.coordinator)

    worklog = run.hooks_data.get("worklog", [])
    assert worklog, "the model never posted; the injected tool did not reach it"
    entry = worklog[0]
    assert entry["kind"] == "obstacle" and "VOLCANO" in entry["body"]
    assert entry["source"] == "scout.answer", "source is wired, not model-reported"
    assert "archivist.answer" in entry["delivered"], "the other member was reached"
    assert "lead" in entry["delivered"], "and so was the lead, via register's replay"
    assert entry["failed"] == {}, f"a live notify failed: {entry['failed']}"
    assert "scout.answer" not in entry["delivered"], "the poster is excluded"


@live
async def test_live_a_lead_synthesizes_a_dynamic_agent_and_delegates_to_it() -> None:
    """dynamic_subagents: a real lead writes a subagent's instructions and uses it.

    Plumbing, not model quality, like every test in this file: the lead is *instructed* to
    call hire_dynamic with instructions containing a marker word and then delegate. The
    assertions check that the synthesis landed on the hiring log with the instructions
    verbatim (the audit trail the feature's safety story rests on), that the delegate
    round-tripped a real answer, and that the run completed under the oracle. Offline tests
    already pin the wire from captured contexts; this answers whether a real model calls the
    injected tool and the synthesized thread holds against the real runtime.
    """
    from ai_functions import AIFunction
    from ai_functions.ai_thread.config import ThreadConfig

    from pneuma.team import DynamicAgent, Member, Recruit, Team
    from pneuma.team.hooks import Hiring

    class LiveHiring(Hiring):
        """The library default with the live model pinned to the cheap configuration —
        a synthesized agent must not fall back to the runtime's default model choice."""

        def __init__(self) -> None:
            super().__init__(dynamic=True)
            self.built: list[Member] = []

        def dynamic_recruit(self, name: str, instructions: str) -> Recruit:
            member = Member(DynamicAgent(name, instructions), "answer", model=small_model())
            self.built.append(member)
            return member

    async def prompt(request: str) -> str:
        return (
            "You lead a team with no members. First, call the hire_dynamic tool "
            "exactly once: name='haiku-writer', instructions that BEGIN with the "
            "word SYNTHMARK and tell the agent it writes one-sentence answers about "
            "geography, and a one-sentence mandate. Then call delegate with "
            "name='haiku-writer' and this request. Then answer in one sentence "
            f"using what it said. Request: {request}"
        )

    lead = AIFunction(
        prompt,
        Answer,
        ThreadConfig(
            name="synth-lead",
            description="Synthesizes a helper and answers through it",
            model=small_model(),
        ),
    )

    async with RuntimeHarness() as h:
        hiring = LiveHiring()
        team = Team(lead, [], hooks=[hiring])
        run = await team.run("What is the longest river in the world? One sentence.", h.coordinator)

    log = run.hooks_data["hiring"]
    synths = [e for e in log if e["action"] == "hire_dynamic"]
    assert synths, "the model never called hire_dynamic; the injected tool did not reach it"
    entry = synths[0]
    assert entry["name"] == "haiku-writer"
    assert "SYNTHMARK" in entry["instructions"], (
        "the instructions are recorded verbatim — the audit trail is the safety story"
    )
    delegated = [e for e in log if e["action"] == "delegate"]
    assert delegated and delegated[0]["name"] == "haiku-writer", (
        "the lead delegated to its synthesized agent through the ordinary delegate tool"
    )
    assert delegated[0]["answer"].strip(), "and a real answer came back"
    assert run.answer.text.strip()
    # The hook's teardown retired the synthesized hire: its live thread ended.
    for member in hiring.built:
        thread = member._thread
        assert thread is None or not thread.live, "a synthesized thread survived the unwind"


# The coordinator import is used by type checkers reading spawn signatures in this module.
_ = Coordinator
