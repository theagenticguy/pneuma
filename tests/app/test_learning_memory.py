"""The memory backend as `casestudy.learning` actually drives it, plus the one-file claim.

`tests/library/test_turso_memory.py` establishes the backend's own contract against doubles.
These establish the two things it cannot: that the application's `Playbook` shape and its
retrieval query reach the backend as designed, and that `libsql`-written evidence and
`pyturso`-written parameters land in a single readable file.

That last one is also why `casestudy/eventlog.py` keeps its existing driver. `pyturso` is the
successor to `libsql` and both speak the same on-disk format, so the audit database and the
learned parameters coexist without migrating either.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types.graph import GradFeedback
from pydantic import BaseModel, Field

from pneuma.memory import TursoMemoryBackend
from pneuma.memory.embedding import DOCUMENT, QUERY


class BagOfWords:
    """Deterministic embedder: L2-normalised token-count vectors.

    Cosine over token counts ranks by lexical overlap, which is the wrong retrieval model,
    and that is the point. Every assertion here is about plumbing, so a fake that cannot be
    confused with a real embedding keeps them honest.
    """

    model_id = "bagofwords:v1"

    def __init__(self, dimensions: int = 128) -> None:
        self._dimensions = dimensions
        self.calls = 0
        self.texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        assert input_type in (DOCUMENT, QUERY)
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            self.texts.append(text)
            vector = [0.0] * self._dimensions
            for token in text.lower().replace(".", " ").replace(",", " ").split():
                vector[_stable_bucket(token) % self._dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


def _stable_bucket(token: str) -> int:
    """Hash a token without `hash()`, whose salt varies per interpreter run."""
    total = 0
    for character in token:
        total = (total * 131 + ord(character)) % 1_000_003
    return total


class Advice(BaseModel):
    """A playbook-shaped schema: entries, a scalar, code, and a numeric parameter."""

    guidance: list[str] = Field(default_factory=list, description="Advice entries.")
    summary: str = Field("nothing yet", description="A scalar note.")
    threshold: int = Field(20, ge=1, le=100, description="Support threshold.")
    ratio: float = Field(0.5, ge=0.0, le=1.0, description="A fractional knob.")


def test_the_audit_database_and_the_parameters_live_in_one_file(tmp_path: Path) -> None:
    """`libsql` writes the evidence, `pyturso` writes the parameters, one file holds both.

    This is the "one artifact" claim, measured rather than asserted in prose, and it
    is also *why `eventlog.py` was not migrated*. `pyturso` is the successor to
    `libsql` and both speak the same on-disk format, so the audit database can keep
    its existing driver while the backend uses the new one and a reader still sees a
    single coherent file. Swapping `eventlog.py` to `turso` was tried and the full
    suite passed, so the migration is available — but it buys nothing this test does
    not already show, and it would churn a file this task does not need to touch.
    """
    from pneuma.casestudy import eventlog

    database = tmp_path / "audit.db"

    written_by_libsql = eventlog.connect(database)
    eventlog.init_schema(written_by_libsql)
    written_by_libsql.execute(
        "INSERT INTO mined_models (name, log, mined_at, ir_json, states, edges) "
        "VALUES ('Model', 'log.xes', 'now', '{}', 3, 2)"
    )
    written_by_libsql.commit()
    written_by_libsql.close()

    memory = TursoMemoryBackend(Advice, actor_id="nav", path=database, embedder=BagOfWords())
    memory.add_entry("guidance", "advice learned under that very model")
    memory.consolidate("threshold", [GradFeedback(text="measured", score=0.4)])
    memory.close()

    reader = eventlog.connect(database)
    assert reader.execute("SELECT name FROM mined_models").fetchall() == [("Model",)]
    assert reader.execute("SELECT value FROM memory_entry").fetchall() == [
        ("advice learned under that very model",)
    ]
    assert reader.execute("SELECT COUNT(*) FROM memory_score_observation").fetchall() == [(1,)]
    reader.close()


def test_the_playbook_is_a_list_of_entries_not_a_blob() -> None:
    """The shape change D6 is about: `guidance` is addressable, entry by entry."""
    from pneuma.casestudy.learning import Playbook

    assert Playbook.model_fields["guidance"].annotation == list[str]
    assert len(Playbook().guidance) >= 2, "an empty corpus makes round one teach nothing"


def test_the_playbook_entries_are_prose_not_code() -> None:
    """Still not `Procedural`: advice a model reads needs no executor."""
    from ai_functions.memory.procedural import ProceduralMarker

    from pneuma.casestudy.learning import Playbook

    metadata = Playbook.model_fields["guidance"].metadata
    assert not any(isinstance(m, ProceduralMarker) for m in metadata)


def test_the_decision_query_names_the_looping_situation() -> None:
    """The query must mention a revisit, or anti-looping advice is unreachable.

    This is the retrieval side of the failure the live experiment measured. If the
    query never says a legal move goes backwards, the entry about not going backwards
    ranks no higher at the moment it matters than at any other step.
    """
    from pneuma.casestudy.learning import decision_query
    from pneuma.process.ir import Transition

    forward = [Transition(name="Advance", source="A", target="B")]
    backward = [Transition(name="GoBack", source="A", target="Seen")]

    with_revisit = decision_query("A", backward, {"paid": 0}, visited=["Seen", "A"])
    without = decision_query("A", forward, {"paid": 0}, visited=["Seen", "A"])

    assert "already been through" in with_revisit
    assert "None of them revisit" in without
    assert "GoBack" in with_revisit and "paid" in with_revisit


def test_render_advice_says_so_when_nothing_was_retrieved() -> None:
    """An empty retrieval is stated, not rendered as a blank section to fill in."""
    from pneuma.casestudy.learning import render_advice

    assert "no relevant guidance" in render_advice([])
    assert render_advice(["a", "b"]) == "- a\n- b"


async def test_run_batch_searches_per_decision_and_records_what_was_read(
    tmp_path: Path,
) -> None:
    """The loop's contract: a live parameter node per trace, and the ids it read.

    Guards the two failure modes together. `traces[0]` must carry a `guidance`
    parameter or `optimizer.step` has nothing to update; and `retrieved_ids` must be
    non-empty or the round consumed advice nobody can attribute.
    """
    from pneuma.casestudy import learning
    from pneuma.process.ir import Process, State, Transition

    # Two enabled transitions, because `_elicit` takes a lone option without asking
    # the agent — a one-way process would run to completion with no decision, no
    # trace, and nothing for the optimizer to learn from.
    process = Process(
        name="Tiny",
        description="A process with a real choice at the first step.",
        states=[
            State(name="start", agent_method="choose"),
            State(name="detour", agent_method="choose"),
            State(name="done", terminal=True),
        ],
        initial_state="start",
        transitions=[
            Transition(name="Finish", source="start", target="done"),
            Transition(name="Detour", source="start", target="detour"),
            Transition(name="Recover", source="detour", target="done"),
        ],
    )
    memory = TursoMemoryBackend(
        learning.Playbook, actor_id="navigator", path=tmp_path / "pb.db", embedder=BagOfWords()
    )
    navigator = learning.LearningNavigator(process)

    async with RuntimeHarness():
        original = navigator.compiled

        def compiled(name: str, **overrides: Any) -> Any:
            overrides.setdefault(
                "model",
                ScriptedModel(
                    [Turn(tool_calls=(("Choice", {"transition": "Finish", "reason": "r"}),))] * 8
                ),
            )
            return original(name, **overrides)

        navigator.compiled = compiled  # type: ignore[method-assign]
        result, traces = await learning.run_batch(
            navigator, process, memory, "a routine case", cases=1, max_steps=4
        )
        assert traces, "no trace to learn from"
        graph = await build_graph_from_result(traces[0], [memory])

    assert [p.name for p in graph.parameters] == ["guidance"]
    assert graph.parameters[0].derivation == "search"
    assert result.retrieved_ids, "the round read no attributable advice"
    assert graph.parameters[0].meta["results"], "the gradient would land on the whole corpus"
    memory.close()


def test_summarise_reports_entries_and_reads_alongside_completion() -> None:
    """Completion rate alone cannot distinguish learning from accumulation."""
    from pneuma.casestudy.learning import TrainingRound, summarise

    table = summarise([TrainingRound(index=0, completed=2, looped=1, steps=[4, 5, 12], entries=3)])
    assert "entries" in table and "read" in table
