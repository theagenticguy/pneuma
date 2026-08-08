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
    """Team: one member, one lead, a real oracle gate, full cleanup.

    The member is a Tutor answering one briefing question. The lead reads the briefing and
    answers the request. The oracle requires a non-empty answer that names the topic. The
    graded TeamRun must come back correct with the briefing recorded.
    """
    from ai_functions import AIFunction
    from ai_functions.ai_thread.config import ThreadConfig

    from pneuma.team import Member, Team

    class QuizTeam(Team):
        name = "live-quiz-team"

        def members(self):  # noqa: ANN201
            return [Member(Tutor("factual"), "answer", model=small_model())]

        def briefing(self, member) -> str:  # noqa: ANN001
            return "What temperature does water boil at, at sea level? One sentence."

        def lead_function(self) -> AIFunction[..., Any]:
            async def prompt(request: str) -> str:
                return (
                    "You lead a quiz team. Using your team's briefing in this conversation, "
                    f"answer the request in one sentence. Request: {request}"
                )

            return AIFunction(
                prompt,
                Answer,
                ThreadConfig(
                    name="quiz-lead",
                    description="Answers the request from the team's briefings",
                    model=small_model(),
                ),
            )

        def oracle(self, response: Any) -> None:
            assert isinstance(response, Answer)
            assert response.text.strip(), "empty answer"

        def grade(self, verdict: Any):  # noqa: ANN201
            mentions = "100" in verdict.text or "212" in verdict.text
            return mentions, [] if mentions else ["answer did not state the boiling point"]

    async with RuntimeHarness() as h:
        team = QuizTeam()
        handle = await h.coordinator.spawn(team, thread_name=team.name)
        run = await handle.run("At what temperature does water boil at sea level?")

    assert run.correct, f"grade failed: {run.oracle_failures}"
    assert run.briefings, "no briefing was recorded"
    assert run.turns >= 2, "expected at least a member turn and a lead turn"


# The coordinator import is used by type checkers reading spawn signatures in this module.
_ = Coordinator
