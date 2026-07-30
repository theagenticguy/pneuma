"""Tests for the agent-written miner.

The agent's analysis code runs in a sandbox and its output is validated, so the tests
that matter here are about the *compile* step: `to_process` takes untrusted structure
and has to produce an IR the model-checker will accept, or reject it clearly. Every
guard below exists because a live run produced the malformed shape it handles.

The live comparison is not asserted, because it is a measurement rather than a
contract. On both logs, the agent's discovery verified cleanly and lost to the
hand-written miner at a matched threshold — recorded in docs/case-study.md.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from pneuma.casestudy import aimine, eventlog, miner
from pneuma.process import tla

LOG = Path(__file__).resolve().parents[1] / "data" / "receipt.xes"
pytestmark = pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    return eventlog.parse_xes(LOG)


def discovered(**overrides: object) -> aimine.Discovered:
    """A minimal well-formed discovery, overridable per test."""
    base: dict[str, object] = {
        "start_activity": "A",
        "terminal_activities": ["C"],
        "edges": [
            aimine.Edge(source="A", target="B", cases=10),
            aimine.Edge(source="B", target="C", cases=9),
        ],
        "threshold_used": 5,
        "method": "counted pairs",
    }
    return aimine.Discovered(**{**base, **overrides})  # type: ignore[arg-type]


# ── The sandbox contract ──


def test_analysis_imports_are_pure_computation() -> None:
    """Widening the allowlist must not widen what the agent can reach.

    The executor blocks `os` and `open` regardless, but listing a module with side
    effects here would be a real escalation, so the set is pinned.
    """
    assert set(aimine.ANALYSIS_IMPORTS) == {
        "polars",
        "numpy",
        "statistics",
        "collections",
        "itertools",
        "math",
    }


def test_the_log_is_sampled_by_case_never_by_row(events: pl.DataFrame) -> None:
    """Half a case is a different process: a truncated trace would teach the agent a
    handoff that does not exist."""
    csv = aimine.to_csv(events, sample_cases=20)
    frame = pl.read_csv(csv.encode())
    assert frame["case_id"].n_unique() == 20

    # Every sampled case is complete — its event count matches the full log's.
    full = events.group_by("case_id").len()
    sampled = frame.group_by("case_id").len()
    joined = sampled.join(full, on="case_id", how="inner", suffix="_full")
    assert (joined["len"] == joined["len_full"]).all()


# ── Compiling untrusted structure into a verifiable IR ──


def test_a_well_formed_discovery_compiles_and_verifies() -> None:
    process = aimine.to_process(discovered(), "T")
    assert {s.name for s in process.states} == {"A", "B", "C"}
    assert process.initial_state == "A"
    assert process.unreachable_states() == set()
    assert [s.name for s in process.states if s.terminal] == ["C"]


def test_self_loops_are_dropped() -> None:
    """A repeated activity is a rework marker; as an IR edge it is a cycle with no
    counterpart in the handoff graph."""
    process = aimine.to_process(
        discovered(
            edges=[
                aimine.Edge(source="A", target="A", cases=8),
                aimine.Edge(source="A", target="B", cases=10),
                aimine.Edge(source="B", target="C", cases=9),
            ]
        ),
        "T",
    )
    assert all(t.source != t.target for t in process.transitions)


def test_duplicate_edges_collapse() -> None:
    process = aimine.to_process(
        discovered(
            edges=[
                aimine.Edge(source="A", target="B", cases=10),
                aimine.Edge(source="A", target="B", cases=3),
                aimine.Edge(source="B", target="C", cases=9),
            ]
        ),
        "T",
    )
    assert len(process.transitions) == 2


def test_a_start_activity_absent_from_the_edges_is_still_added() -> None:
    """The agent can name a start activity it never used in an edge. Dropping it would
    leave the IR with an initial state that is not a declared state at all."""
    process = aimine.to_process(
        discovered(
            start_activity="Z",
            edges=[aimine.Edge(source="A", target="B", cases=10)],
            terminal_activities=["B"],
        ),
        "T",
    )
    assert process.initial_state == "Z"
    assert "Z" in {s.name for s in process.states}


def test_terminal_activities_are_corrected_against_the_edges() -> None:
    """An activity the agent calls terminal while also giving it a successor is not
    terminal. Trusting the label would produce a state that is both an exit and a
    waypoint, and the interpreter would stop early."""
    process = aimine.to_process(
        discovered(terminal_activities=["A", "C"]),  # A has a successor
        "T",
    )
    terminals = {s.name for s in process.states if s.terminal}
    assert terminals == {"C"}


def test_a_discovery_with_no_declared_terminal_still_compiles() -> None:
    """The IR rejects a process with no terminal state, so an activity nobody
    continues from is treated as terminal whether the agent said so or not."""
    process = aimine.to_process(discovered(terminal_activities=[]), "T")
    assert any(s.terminal for s in process.states)


# ── Grading is honest about method versus setting ──


def test_grading_compares_against_the_agents_own_threshold(events: pl.DataFrame) -> None:
    """A looser cut mechanically buys coverage, so beating the baseline's *default*
    proves nothing. The matched comparison is the one that isolates the method."""
    graded = aimine.grade(events, discovered(threshold_used=5), baseline_threshold=25)

    matched = miner.mine(events, name="M", min_edge_cases=5)
    assert graded.matched_coverage == matched.coverage
    assert graded.matched_edges == len(matched.process.transitions)
    assert graded.baseline_coverage != graded.matched_coverage, (
        "default and matched baselines should differ, or the test proves nothing"
    )


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_an_agent_written_model_goes_through_the_same_checker() -> None:
    """The safety claim: generated structure is verified, not trusted."""
    result = tla.check(aimine.to_process(discovered(), "AgentWritten"), timeout=120)
    assert result.ok, result.raw[-1200:]


def test_a_fully_cyclic_discovery_still_compiles() -> None:
    """A live run crashed here once the agent was pushed toward tighter models: it
    returned cycles among the frequent activities with no exit, so nothing was
    structurally terminal and the IR rejected the whole process. Raising loses the
    training round, so the compile step falls back rather than failing."""
    cyclic = discovered(
        terminal_activities=[],
        edges=[
            aimine.Edge(source="A", target="B", cases=10),
            aimine.Edge(source="B", target="C", cases=5),
            aimine.Edge(source="C", target="A", cases=2),
        ],
    )
    process = aimine.to_process(cyclic, "Cyclic")
    assert any(s.terminal for s in process.states)


def test_a_declared_terminal_is_preferred_over_the_fallback() -> None:
    """When the agent names a terminal and the graph offers none, believe the agent
    before guessing from edge support."""
    cyclic = discovered(
        terminal_activities=["C"],
        edges=[
            aimine.Edge(source="A", target="B", cases=10),
            aimine.Edge(source="B", target="C", cases=5),
            aimine.Edge(source="C", target="A", cases=2),
        ],
    )
    process = aimine.to_process(cyclic, "Declared")
    assert [s.name for s in process.states if s.terminal] == ["C"]
