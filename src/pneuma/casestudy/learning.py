"""Backpropagation over the navigator: fix the looping, without touching the rules.

The live experiment found the real defect. The agent never broke a rule; it *dithered*,
cycling between valid states until the step cap stopped it — 6 of 10 cases. No amount
of verification helps, because looping is legal. The model-checker proved the process
permits it, correctly.

That is a prompt problem, and `TextGradOptimizer` is the mechanism for prompt problems
that you do not want to solve by hand-editing a docstring forever.

The wiring that matters is the shape of the agent. `LearningNavigator.choose` takes
`playbook` as a **real parameter**, so a recalled `ParameterView` arrives in the call
arguments where `collect_nodes` can find it. Hide the same text on `self` and the
gradient has nothing to land on — the exact failure mode `pneuma.method`'s docstring
describes, in production form.

The loop is: run cases, observe how many looped, phrase that as feedback in plain
English, let the optimizer rewrite the playbook, run again. The rules are never
touched. Verification stays valid because the process did not change — only the
advice the agent reads before choosing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_functions import JSONMemoryBackend, TextGradOptimizer
from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from ..process import interpreter
from ..process.ir import Process, Transition

SEED_PLAYBOOK = "# No guidance learned yet.\n"


class Playbook(BaseModel):
    """The learnable parameter: advice for choosing the next step.

    A plain `str`, deliberately. `Procedural` marks a parameter as reusable *code*
    and the runtime then requires `code_execution_mode='local'` to define it in the
    sandbox — correct for helper functions, wrong for prose. What the optimizer
    rewrites here is guidance the model reads, so it needs no executor.

    The text is advice, never a rule. Rules live in the verified IR where a checker
    can see them, so nothing an optimizer writes here can widen what the runtime
    permits: the worst a bad rewrite can do is make the agent slower.
    """

    guidance: str = Field(
        default=SEED_PLAYBOOK,
        description=(
            "Accumulated guidance on choosing the next step in a business process: "
            "how to make progress toward completion and avoid revisiting states."
        ),
    )


class Choice(BaseModel):
    """One transition, with the reasoning that produced it."""

    transition: str = Field(description="Exactly one transition name from the offered list")
    reason: str = Field(description="One sentence citing the condition or the goal")


class LearningNavigator(MethodAgent):
    """A navigator whose guidance is a gradient target."""

    def __init__(self, process: Process) -> None:
        self.process = process
        self.name = f"{process.name.lower()}-learner"

    @ai_method(
        Choice,
        description="Choose the next transition in a business process",
        max_attempts=2,
    )
    def choose(self, playbook: str, state: str, options: str, facts: str) -> Choice:
        """You are executing the `{self.process.name}` process.

        {self.process.description}

        Guidance learned from previous runs:
        {playbook}

        Case facts:
        {facts}

        {options}

        Pick the transition that both respects the process rules and moves this case
        toward completion. Name what you relied on.
        """


@dataclass
class TrainingRound:
    """One pass over a batch of cases."""

    index: int
    completed: int = 0
    looped: int = 0
    steps: list[int] = field(default_factory=list)
    playbook_chars: int = 0

    @property
    def completion_rate(self) -> float:
        total = self.completed + self.looped
        return 0.0 if not total else round(self.completed / total, 3)

    @property
    def mean_steps(self) -> float:
        return 0.0 if not self.steps else round(sum(self.steps) / len(self.steps), 1)


async def run_batch(
    navigator: LearningNavigator,
    process: Process,
    memory: JSONMemoryBackend,
    facts: str,
    *,
    cases: int,
    max_steps: int,
) -> tuple[TrainingRound, list[Any]]:
    """Run `cases` through the process, returning the round and each traced result.

    `trace` rather than a plain call: the optimizer needs the `Result` graph, and only
    a traced call carries parameter nodes.

    The recall happens per call, and that detail is load-bearing. A `ParameterView`
    is emitted once — "one logical recall, one event" — so reusing a single recalled
    view across a batch produces a parameter node on the *first* traced call and none
    on any later one. Keeping the last trace then hands the optimizer a graph with
    nothing to update, and the loop reports rounds while learning nothing.
    """
    compiled = navigator.compiled("choose")
    round_result = TrainingRound(index=0)
    traces: list[Any] = []

    for _ in range(cases):
        steps = 0
        first_trace: Any = None

        async def decide(
            state: str,
            enabled: list[Transition],
            variables: dict[str, int | str],
        ) -> str:
            nonlocal steps, first_trace
            steps += 1
            traced = await compiled.trace(
                await memory.recall("guidance"),
                state,
                interpreter.offer(state, enabled, variables),
                facts,
            )
            if first_trace is None:
                first_trace = traced
            return traced.value.transition

        try:
            await interpreter.run(process, decide, max_steps=max_steps)
            round_result.completed += 1
        except interpreter.ProcessError:
            round_result.looped += 1

        round_result.steps.append(steps)
        if first_trace is not None:
            traces.append(first_trace)

    return round_result, traces


def feedback_for(round_result: TrainingRound) -> str:
    """Turn the round's numbers into the feedback a human would give.

    Concrete and behavioural. "Do better" teaches nothing; naming the observed
    failure and the property to preserve gives the backward pass something to route.
    """
    if round_result.looped == 0:
        return (
            "Every case reached a terminal state. Keep the guidance that produced "
            "direct progress and do not add caution that would lengthen paths."
        )
    return (
        f"{round_result.looped} of {round_result.completed + round_result.looped} cases "
        f"failed to finish: the agent revisited states it had already passed through and "
        f"ran out of steps, averaging {round_result.mean_steps} steps per case. Add "
        "guidance that makes the agent prefer transitions leading toward a terminal "
        "state and never re-enter a state already visited in this case. Compliance was "
        "already perfect, so do not add guidance about following rules."
    )


async def train(
    process: Process,
    db_path: Path,
    *,
    facts: str,
    rounds: int = 2,
    cases_per_round: int = 3,
    max_steps: int = 12,
) -> list[TrainingRound]:
    """Run the improvement loop, returning one record per round.

    Each round runs a batch, measures completion, phrases feedback, and lets the
    optimizer rewrite the playbook in memory. The next round recalls the updated
    text, so improvement is carried in a parameter rather than in our code.
    """
    from ai_functions.testing import RuntimeHarness

    memory = JSONMemoryBackend(Playbook, actor_id="navigator", path=str(db_path))
    optimizer = TextGradOptimizer()
    navigator = LearningNavigator(process)
    history: list[TrainingRound] = []

    # A live coordinator is required, not optional. `trace` records a graph against
    # the running runtime and `optimizer.step` rebuilds it from there; without a
    # harness the step finds nothing to update and the playbook silently stays at its
    # seed value — a training loop that reports rounds and learns nothing.
    try:
        async with RuntimeHarness():
            for index in range(rounds):
                result, traces = await run_batch(
                    navigator,
                    process,
                    memory,
                    facts,
                    cases=cases_per_round,
                    max_steps=max_steps,
                )
                result.index = index
                result.playbook_chars = len(str(await memory.recall("guidance")))
                history.append(result)

                if index == rounds - 1 or not traces:
                    break
                await optimizer.step(traces[-1], feedback_for(result), backends=[memory])
    finally:
        memory.close()

    return history


def summarise(history: list[TrainingRound]) -> str:
    lines = [f"{'round':>5} {'completed':>10} {'looped':>7} {'completion':>11} {'mean steps':>11}"]
    for record in history:
        lines.append(
            f"{record.index:>5} {record.completed:>10} {record.looped:>7} "
            f"{100 * record.completion_rate:>10.0f}% {record.mean_steps:>11.1f}"
        )
    return "\n".join(lines)
