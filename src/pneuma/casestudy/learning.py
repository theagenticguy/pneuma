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

## Why the playbook is a list of entries and not one string

It was one string, and the blob was the limit. A round produces one gradient about
one observed failure — "the agent revisited states it had already passed through" —
and against a single `guidance` parameter that gradient is routed to *all* the
accumulated advice at once. The consolidating model then rewrites whatever it likes.
Advice that was working is paraphrased or dropped for reasons no round measured, and
because the loop only reads completion rate it cannot see that happening.

Splitting the playbook into addressable entries and recalling by *search* fixes the
routing. `TursoMemoryBackend.search` puts `{entry_id: value}` for the retrieved
entries in the recall event's meta; that travels to the reconstructed
`ParameterNode` and back out as `consolidate`'s `retrieved=`, so consolidation edits
those entries and leaves the rest byte-identical. `test_turso_memory.py` asserts
exactly that: a gradient about entry A does not modify entry B.

The query is built from the decision context — the state, the legal moves, whether
any of them is a revisit — so what the agent reads is the advice that bears on the
choice in front of it rather than everything ever learned.

The safety property is unchanged, and it is worth restating because the parameter
changed shape. The playbook is *advice*. Rules live in the verified IR where a
checker can see them, and the interpreter rejects any transition the IR does not
permit. Nothing an optimizer writes here can widen what the runtime allows: the
worst a bad rewrite can do is make the agent slower. That is why this loop is
allowed to let a model rewrite its own guidance at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_functions import TextGradOptimizer
from pydantic import BaseModel, Field

from ..memory import TursoMemoryBackend
from ..method import MethodAgent, ai_method
from ..process import interpreter
from ..process.ir import Process, Transition

SEED_PLAYBOOK = "# No guidance learned yet.\n"

SEED_ENTRIES: tuple[str, ...] = (
    "Prefer a transition whose target you have not already visited in this case; "
    "revisiting a state is legal but makes no progress.",
    "When several transitions all make progress, prefer the one that moves the case "
    "closer to a terminal state rather than one that opens more work.",
)
"""Starting entries, so the first round retrieves advice rather than nothing.

An empty corpus is not a neutral start. `search` over it returns `[]`, the first
round runs with no guidance at all, and the gradient from that round has no entry to
land on — so the loop's first step teaches nothing and the seeding happens by
accident in whatever the consolidating agent decides to add. Two general entries make
round one a real measurement. They are deliberately weak: the loop's job is to
sharpen them.
"""

TOP_K = 3
"""Entries retrieved per decision.

Three rather than one because retrieval on this corpus is good but not perfect: the
live measurement in `memory.turso_backend` has a query whose correct entry ranked
second. Retrieving three keeps that entry in the prompt. It also bounds how much
advice one decision can be blamed for, which is the point of searching at all.
"""


class Playbook(BaseModel):
    """The learnable parameter: advice for choosing the next step.

    A `list[str]`, so entries are individually addressable and a gradient can land
    on the two or three that a decision actually read. Each entry is plain prose,
    deliberately not `Procedural`: `Procedural` marks a parameter as reusable *code*
    and the runtime then requires `code_execution_mode='local'` to define it in the
    sandbox — correct for helper functions, wrong for advice a model reads.

    The text is advice, never a rule. Rules live in the verified IR where a checker
    can see them, so nothing an optimizer writes here can widen what the runtime
    permits: the worst a bad rewrite can do is make the agent slower.
    """

    guidance: list[str] = Field(
        default_factory=lambda: list(SEED_ENTRIES),
        description=(
            "Accumulated guidance on choosing the next step in a business process: "
            "how to make progress toward completion and avoid revisiting states. "
            "Each entry is one self-contained piece of advice, so that feedback about "
            "one situation can be applied without rewriting advice about another. "
            "Prefer updating the entry a piece of feedback is about over adding a "
            "near-duplicate; an entry that bundles several unrelated points cannot be "
            "retrieved for any one of them."
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
    def choose(self, playbook: list[str], state: str, options: str, facts: str) -> Choice:
        """You are executing the `{self.process.name}` process.

        {self.process.description}

        Guidance learned from previous runs, retrieved as relevant to this decision:
        {render_advice(playbook)}

        Case facts:
        {facts}

        {options}

        Pick the transition that both respects the process rules and moves this case
        toward completion. Name what you relied on.
        """


def decision_query(
    state: str,
    enabled: list[Transition],
    variables: dict[str, int | str],
    visited: list[str] | None = None,
) -> str:
    """Phrase the decision in front of the agent as a retrieval query.

    What the query says is what the round can learn about, so this is a design
    decision rather than string assembly. Three things go in, and each one is a
    situation a piece of advice could be *about*:

    - The state and the names of the legal moves, so advice mentioning this part of
      the process is reachable.
    - Whether any legal move revisits a state already passed through. This is the
      failure the live experiment measured, and naming it in the query is what makes
      the anti-looping entries retrievable at the moment they matter instead of at
      every step equally.
    - The process variables, which is what advice about a condition keys on.

    Deliberately written the way an operator would describe the situation, not as
    keywords. The corpus is prose advice and the embedding is asymmetric
    (`search_query` against `search_document`), so a phrase retrieves better than a
    bag of identifiers — and this is the shape the discrimination measurement in
    `memory.turso_backend` was taken against.
    """
    path = interpreter.history() if visited is None else visited
    seen = set(path)
    revisits = [t.name for t in enabled if t.target in seen]
    moves = ", ".join(t.name for t in enabled) or "none"

    parts = [f"At step `{state}` the legal moves are: {moves}."]
    if revisits:
        parts.append(
            f"{len(revisits)} of them go back to a state this case has already been "
            "through, so choosing one would repeat work instead of making progress."
        )
    else:
        parts.append("None of them revisit a state this case has already been through.")
    if variables:
        parts.append(f"Case variables: {variables}.")
    parts.append("Which move should be chosen, and what should be avoided here?")
    return " ".join(parts)


@dataclass
class TrainingRound:
    """One pass over a batch of cases."""

    index: int
    completed: int = 0
    looped: int = 0
    steps: list[int] = field(default_factory=list)
    playbook_chars: int = 0
    entries: int = 0
    """Playbook entries at the end of the round. Growth without improvement is a finding."""
    retrieved_ids: set[str] = field(default_factory=set)
    """Entry ids any decision in this round actually read.

    Recorded because it is the honest denominator for "did the round learn anything".
    An entry that was never retrieved cannot have earned or lost anything this round,
    so a loop reporting a changed playbook without a changed retrieval set has moved
    text nobody read.
    """

    @property
    def completion_rate(self) -> float:
        total = self.completed + self.looped
        return 0.0 if not total else round(self.completed / total, 3)

    @property
    def mean_steps(self) -> float:
        return 0.0 if not self.steps else round(sum(self.steps) / len(self.steps), 1)


def render_advice(entries: list[str]) -> str:
    """Render retrieved entries for the prompt, or say plainly that there are none.

    The empty case is not cosmetic. An empty string would leave the prompt's
    "Guidance learned from previous runs:" heading followed by nothing, which reads
    to a model as a section it should fill in rather than an absence. Saying so
    outright is also the only way an operator reading a transcript can tell "the
    playbook is empty" apart from "retrieval found nothing relevant" — and under a
    calibrated `distance_ceiling` the second happens on purpose.
    """
    if not entries:
        return "(no relevant guidance retrieved for this decision)"
    return "\n".join(f"- {entry}" for entry in entries)


async def run_batch(
    navigator: LearningNavigator,
    process: Process,
    memory: TursoMemoryBackend,
    facts: str,
    *,
    cases: int,
    max_steps: int,
) -> tuple[TrainingRound, list[Any]]:
    """Run `cases` through the process, returning the round and each traced result.

    `trace` rather than a plain call: the optimizer needs the `Result` graph, and only
    a traced call carries parameter nodes.

    The prompt here has to be the one the live experiment measured, or the optimizer
    learns to fix a looping problem the training prompt never exhibits. `offer` reads
    the interpreter's visit history itself, so both call sites get the same text
    without either of them maintaining a list.

    Retrieval is per decision and by query, not a full recall. `search` narrows the
    gradient (its meta names the entries this decision read, and consolidation edits
    only those), and it narrows the prompt, which is the same property from the
    agent's side: it reads the two or three pieces of advice about the choice in front
    of it rather than everything ever learned.

    The recall happens per call, and that detail is load-bearing. A `ParameterView`
    is emitted once — "one logical recall, one event" — so reusing a single recalled
    view across a batch produces a parameter node on the *first* traced call and none
    on any later one. Keeping the last trace then hands the optimizer a graph with
    nothing to update, and the loop reports rounds while learning nothing.

    The view is passed as a handle, never interpolated. `render_advice(view.value)`
    would compute the identical prompt and drop the gradient edge, because
    `collect_nodes` finds targets by scanning the call's arguments for handle
    *objects*. So the view goes in whole and the prompt template renders it.
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
            view = await memory.search(
                "guidance",
                decision_query(state, enabled, variables),
                k=TOP_K,
            )
            round_result.retrieved_ids.update(view.meta.get("results", {}))
            traced = await compiled.trace(
                view,
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

    Now that the playbook is a set of retrieved entries, the feedback also says what
    kind of edit is wanted. The gradient reaches the two or three entries the failing
    decision read, and telling the consolidator to sharpen *those* is the difference
    between the mechanism working and it appending a fresh near-duplicate every round
    while the entry that was actually retrieved stays vague.
    """
    if round_result.looped == 0:
        return (
            "Every case reached a terminal state. Keep the guidance that produced "
            "direct progress and do not add caution that would lengthen paths. Do not "
            "add entries: nothing was observed to be missing."
        )
    return (
        f"{round_result.looped} of {round_result.completed + round_result.looped} cases "
        f"failed to finish: the agent revisited states it had already passed through and "
        f"ran out of steps, averaging {round_result.mean_steps} steps per case. Sharpen "
        "the retrieved guidance so it makes the agent prefer transitions leading toward "
        "a terminal state and never re-enter a state already visited in this case; "
        "update the entry that should have prevented this rather than adding a similar "
        "one beside it. Compliance was already perfect, so do not add guidance about "
        "following rules."
    )


async def train(
    process: Process,
    db_path: Path,
    *,
    facts: str,
    rounds: int = 2,
    cases_per_round: int = 3,
    max_steps: int = 12,
    memory: TursoMemoryBackend | None = None,
) -> list[TrainingRound]:
    """Run the improvement loop, returning one record per round.

    Each round runs a batch, measures completion, phrases feedback, and lets the
    optimizer edit the playbook entries the round actually read. The next round
    retrieves the updated entries, so improvement is carried in a parameter rather
    than in our code.

    Args:
        process: The verified process IR. Never modified by this loop.
        db_path: Database file for the playbook. Ignored when `memory` is given.
        facts: Case facts handed to every decision.
        rounds: Batches to run. The last one is measured but not learned from,
            since there is no later round to show the improvement.
        cases_per_round: Cases per batch.
        max_steps: Step cap per case. Hitting it is the looping failure.
        memory: An existing backend to train against — pass one opened on the audit
            database to keep parameters and evidence in a single file, which is the
            arrangement this backend exists for. Ownership stays with the caller:
            a supplied backend is not closed here.
    """
    from ai_functions.testing import RuntimeHarness

    owned = memory is None
    store = memory or TursoMemoryBackend(Playbook, actor_id="navigator", path=db_path)
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
                    store,
                    facts,
                    cases=cases_per_round,
                    max_steps=max_steps,
                )
                result.index = index
                entries = store.list_entries("guidance")
                result.entries = len(entries)
                result.playbook_chars = sum(len(v) for v in entries.values())
                history.append(result)

                if index == rounds - 1 or not traces:
                    break
                await optimizer.step(traces[-1], feedback_for(result), backends=[store])
    finally:
        if owned:
            store.close()

    return history


def summarise(history: list[TrainingRound]) -> str:
    """Render the rounds as a table.

    `entries` and `read` are in the table on purpose. Completion rate alone cannot
    distinguish a loop that improved its advice from one that accumulated entries
    nobody retrieved, and those two look identical in a completion column.
    """
    lines = [
        f"{'round':>5} {'completed':>10} {'looped':>7} {'completion':>11} "
        f"{'mean steps':>11} {'entries':>8} {'read':>5} {'chars':>7}"
    ]
    for record in history:
        lines.append(
            f"{record.index:>5} {record.completed:>10} {record.looped:>7} "
            f"{100 * record.completion_rate:>10.0f}% {record.mean_steps:>11.1f} "
            f"{record.entries:>8} {len(record.retrieved_ids):>5} {record.playbook_chars:>7}"
        )
    return "\n".join(lines)
