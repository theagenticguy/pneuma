"""The learning loop hears the voiced dead ends, and counts cheap failures as cheap.

`interpreter.Revisit` and `NoProgress` gave the process layer a voice for dithering
(`tests/library/test_interpreter_no_progress.py` pins that layer). These pin the
application side: `decision_query` puts the voiced dead ends into the retrieval query,
so anti-looping advice is reachable at the moment a loop is *in progress* rather than
only when a backward edge is on offer; and `run_batch` counts a `NoProgress` halt
separately from a burned step budget — both are `looped` for the training feedback,
because the agent dithered either way, but only one of them spent the whole budget
finding that out.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn

from pneuma.casestudy import learning
from pneuma.memory import TursoMemoryBackend
from pneuma.memory.embedding import DOCUMENT, QUERY
from pneuma.process import interpreter
from pneuma.process.ir import Process, State, Transition


class BagOfWords:
    """Deterministic embedder, so nothing here depends on a network or a model."""

    model_id = "bagofwords:v1"

    def __init__(self, dimensions: int = 128) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        assert input_type in (DOCUMENT, QUERY)
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in text.lower().replace(".", " ").replace(",", " ").split():
                vector[_stable_bucket(token) % self._dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


def _stable_bucket(token: str) -> int:
    total = 0
    for character in token:
        total = (total * 131 + ord(character)) % 1_000_003
    return total


def cycling() -> Process:
    """A two-state cycle with an exit the scripted model refuses to take."""
    return Process(
        name="Cycling",
        description="A cycle with an exit",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="Branch"),
            State(name="T", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoA", source="B", target="A"),
            Transition(name="BtoT", source="B", target="T"),
        ],
    )


def turn_back(times: int) -> ScriptedModel:
    return ScriptedModel(
        [Turn(tool_calls=(("Choice", {"transition": "BtoA", "reason": "r"}),))] * times
    )


# ── decision_query voices the dead ends ──


def test_the_decision_query_voices_the_runs_dead_ends() -> None:
    """A run that has already circled says so in its retrieval query.

    Naming the in-progress loop is what makes break-out-of-the-loop advice
    retrievable at the moment it matters — the same argument the query already makes
    for backward *edges*, extended to backward *history*.
    """
    moves = [Transition(name="Advance", source="A", target="B")]
    dead_ends = [
        interpreter.Revisit(state="Review", step=3, alternatives=("Approve",)),
        interpreter.Revisit(state="Intake", step=4, alternatives=()),
    ]

    voiced = learning.decision_query("A", moves, {}, visited=["A"], dead_ends=dead_ends)
    quiet = learning.decision_query("A", moves, {}, visited=["A"], dead_ends=[])

    assert "already dead-ended 2 time(s)" in voiced
    assert "Review" in voiced and "Intake" in voiced
    assert "dead-ended" not in quiet


async def test_the_dead_ends_default_to_the_enclosing_runs_own_record() -> None:
    """Inside a run, `decision_query` reads `interpreter.revisits()` by itself.

    The decider passes nothing — the same contract `visited` already has — so the
    query a live `run_batch` builds mid-loop names the dead ends without any caller
    maintaining a list.
    """
    queries: list[str] = []
    calls = 0

    async def watching(
        state: str, enabled: list[Transition], variables: dict[str, int | str]
    ) -> str:
        nonlocal calls
        calls += 1
        queries.append(learning.decision_query(state, enabled, variables))
        return "BtoA" if calls == 1 else "BtoT"

    await interpreter.run(cycling(), watching)

    assert "dead-ended" not in queries[0]
    assert "already dead-ended 2 time(s)" in queries[1], "A and the forced re-entry into B"


# ── run_batch counts cheap failures as cheap ──


async def test_run_batch_counts_an_early_halt_as_looped_and_as_halted(tmp_path: Path) -> None:
    """A `NoProgress` case is `looped` for the feedback and `halted_early` for the cost.

    The scripted model turns back forever; the interpreter's default revisit limit
    stops it long before the generous step budget would. The round must show one
    looped case (the failure is real) and one early halt (it was caught cheaply) —
    and the step count proves the budget was not burned.
    """
    memory = TursoMemoryBackend(
        learning.Playbook, actor_id="navigator", path=tmp_path / "pb.db", embedder=BagOfWords()
    )
    navigator = learning.LearningNavigator(cycling())

    async with RuntimeHarness():
        original = navigator.compiled

        def compiled(name: str, **overrides: Any) -> Any:
            overrides.setdefault("model", turn_back(40))
            return original(name, **overrides)

        navigator.compiled = compiled  # type: ignore[method-assign]
        result, _ = await learning.run_batch(
            navigator, cycling(), memory, "a case", cases=1, max_steps=30
        )
    memory.close()

    assert result.completed == 0
    assert result.looped == 1
    assert result.halted_early == 1
    assert result.completion_rate == 0.0, "an early halt is not a completion"
    assert result.steps[0] < 30, "the halt must fire before the budget would"


async def test_run_batch_still_counts_a_burned_budget_as_looped_but_not_halted(
    tmp_path: Path,
) -> None:
    """A budget exhausted before the revisit limit fires is the old failure, so counted.

    `max_steps=4` runs out after only two revisits — under the default limit of
    five — so this case burns its (small) budget exactly as every looped case did
    before `NoProgress` existed, and `halted_early` stays zero.
    """
    memory = TursoMemoryBackend(
        learning.Playbook, actor_id="navigator", path=tmp_path / "pb.db", embedder=BagOfWords()
    )
    navigator = learning.LearningNavigator(cycling())

    async with RuntimeHarness():
        original = navigator.compiled

        def compiled(name: str, **overrides: Any) -> Any:
            overrides.setdefault("model", turn_back(10))
            return original(name, **overrides)

        navigator.compiled = compiled  # type: ignore[method-assign]
        result, _ = await learning.run_batch(
            navigator, cycling(), memory, "a case", cases=1, max_steps=4
        )
    memory.close()

    assert result.completed == 0
    assert result.looped == 1
    assert result.halted_early == 0


def test_summarise_shows_the_halted_column() -> None:
    """The cost difference is visible in the report, not only on the dataclass."""
    round_result = learning.TrainingRound(
        index=0, completed=2, looped=2, halted_early=1, steps=[4, 5, 9, 12]
    )
    table = learning.summarise([round_result])
    assert "halted" in table
