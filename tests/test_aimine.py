"""Tests for the agent-written miner.

The agent's analysis code runs in a sandbox and its output is validated, so the tests
that matter here are about the *compile* step: `to_process` takes untrusted structure
and has to produce an IR the model-checker will accept, or reject it clearly. Every
guard below exists because a live run produced the malformed shape it handles.

The second thing these tests hold down is that the agent cannot configure its own
evaluation. Every field on `Discovered` is written by the party being graded, so any of
them reaching a knob on the baseline is a way to win without analysing better. The
grading tests vary only a self-reported field and assert the score does not move.

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


def mined_by_hand(events: pl.DataFrame, threshold: int) -> aimine.Discovered:
    """The frozen miner's own answer at `threshold`, dressed up as agent output.

    Identical structure to what `miner.mine` keeps, so any coverage difference in a
    test below comes from the harness rather than from the model being compared.
    """
    edges = miner.directly_follows(events).filter(pl.col("cases") >= threshold)
    sources = set(edges["activity"].to_list())
    firsts, _ = miner.start_and_end_activities(events)
    return aimine.Discovered(
        start_activity=firsts["activity"][0],
        terminal_activities=sorted(set(edges["next_activity"].to_list()) - sources),
        edges=[
            aimine.Edge(source=r["activity"], target=r["next_activity"], cases=r["cases"])
            for r in edges.iter_rows(named=True)
        ],
        threshold_used=threshold,
        method="directly-follows count ranked by distinct cases",
    )


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


def test_grading_compares_against_a_matched_threshold(events: pl.DataFrame) -> None:
    """A looser cut mechanically buys coverage, so beating the baseline's *default*
    proves nothing. The matched comparison is the one that isolates the method.

    This asserted the matched run used `threshold_used` directly, which was the defect:
    that number is the agent's own account of itself. The contract is the same shape,
    with the setting counted from the log."""
    graded = aimine.grade(events, mined_by_hand(events, 5), baseline_threshold=25)

    matched = miner.mine(events, name="M", min_edge_cases=5)
    assert graded.matched_threshold == 5
    assert graded.matched_coverage == matched.coverage
    assert graded.matched_edges == len(matched.process.transitions)
    assert graded.baseline_coverage != graded.matched_coverage, (
        "default and matched baselines should differ, or the test proves nothing"
    )


# ── The agent must not be able to configure its own opponent ──


def test_the_matched_baseline_ignores_the_self_reported_threshold(events: pl.DataFrame) -> None:
    """`threshold_used` is a number the agent writes about itself. Feeding it to the
    baseline lets the same model pick its opponent's handicap: claiming a loose cutoff
    cripples the baseline while the agent's own edges, and so its own coverage, are
    unchanged. The handicap has to come from the edges and the log."""
    model = mined_by_hand(events, 5)

    scores = {
        claimed: aimine.grade(events, model.model_copy(update={"threshold_used": claimed}))
        for claimed in (3, 5, 25, 300)
    }

    coverages = {graded.coverage for graded in scores.values()}
    assert len(coverages) == 1, (
        f"the agent's own model changed, so nothing is isolated: {coverages}"
    )

    handicaps = {claimed: graded.matched_coverage for claimed, graded in scores.items()}
    assert len(set(handicaps.values())) == 1, (
        f"the claimed threshold moved the baseline: {handicaps}"
    )
    verdicts = {claimed: graded.beat_method for claimed, graded in scores.items()}
    assert len(set(verdicts.values())) == 1, f"the claim decided who won: {verdicts}"


def test_the_matched_baseline_is_the_one_the_edges_imply(events: pl.DataFrame) -> None:
    """The apples-to-apples setting is the tightest cutoff that keeps every edge the
    agent kept, which is the true support of its weakest edge."""
    model = mined_by_hand(events, 5)
    graded = aimine.grade(events, model.model_copy(update={"threshold_used": 300}))

    assert graded.matched_threshold == 5
    assert graded.matched_coverage == miner.mine(events, name="M", min_edge_cases=5).coverage


def test_inflated_edge_supports_cannot_soften_the_baseline(events: pl.DataFrame) -> None:
    """Deriving the cutoff from the agent's reported `cases` only moves the lie one
    field over, so the support comes from the log rather than from the report."""
    model = mined_by_hand(events, 5)
    inflated = model.model_copy(
        update={
            "threshold_used": 300,
            "edges": [edge.model_copy(update={"cases": 300}) for edge in model.edges],
        }
    )
    graded = aimine.grade(events, inflated)

    assert graded.matched_threshold == 5
    assert graded.matched_coverage == miner.mine(events, name="M", min_edge_cases=5).coverage


def test_an_invented_edge_faces_the_strictest_baseline(events: pl.DataFrame) -> None:
    """An edge nobody walked has zero support, so the model is not a thresholded model
    at all and gets no handicap. Failing towards the strongest baseline is the safe
    direction for a claim the agent is trying to win."""
    model = mined_by_hand(events, 5)
    invented = model.model_copy(
        update={
            "edges": [
                *model.edges,
                aimine.Edge(source=model.start_activity, target="Nobody walked this", cases=999),
            ]
        }
    )
    graded = aimine.grade(events, invented)

    assert graded.matched_threshold == 1
    assert graded.matched_coverage == miner.mine(events, name="M", min_edge_cases=1).coverage


def test_a_misreported_threshold_is_reported_not_swallowed(events: pl.DataFrame) -> None:
    """The self-report has real value: the agent's stated cutoff and its rationale are
    the interesting artifact. It just must not configure the measurement, and a
    disagreement with the edges must be visible rather than silently overridden."""
    model = mined_by_hand(events, 5)
    honest = aimine.grade(events, model)
    lying = aimine.grade(events, model.model_copy(update={"threshold_used": 300}))

    assert not honest.threshold_misreported
    assert lying.threshold_misreported
    assert lying.claimed_threshold == 300
    assert lying.matched_threshold == 5
    assert "300" in lying.summary and "claimed" in lying.summary.lower()


def test_the_honest_label_names_the_comparison_that_cannot_be_moved(
    events: pl.DataFrame,
) -> None:
    """The label asserting honesty must sit on the derived comparison. Attached to a
    manipulable one it is worse than no label at all."""
    model = mined_by_hand(events, 5).model_copy(update={"threshold_used": 300})
    line = next(
        line for line in aimine.grade(events, model).summary.splitlines() if "honest" in line
    )
    assert f"thr={model.threshold_used}" not in line


def test_raising_the_derived_threshold_costs_what_it_buys(events: pl.DataFrame) -> None:
    """The one channel deriving cannot close, pinned so its size is known.

    A derived threshold still moves when the agent drops its own weakest edges, and a
    higher threshold means a weaker baseline. The difference from the self-report is that
    this is not free: the dropped edges are gone from the agent's model too, so its own
    coverage falls with the baseline's. Dropping the 16 weakest edges of a thr=5 model
    lifts the derived threshold from 5 to 20 and still loses, by more than it started."""
    edges = sorted(mined_by_hand(events, 5).edges, key=lambda edge: edge.cases)
    scores = []
    for dropped in (0, 8, 16):
        kept = edges[dropped:]
        sources = {edge.source for edge in kept}
        scores.append(
            aimine.grade(
                events,
                aimine.Discovered(
                    start_activity=mined_by_hand(events, 5).start_activity,
                    terminal_activities=sorted({e.target for e in kept} - sources),
                    edges=kept,
                    threshold_used=1,
                    method="dropped my own weakest edges",
                ),
            )
        )

    assert [g.matched_threshold for g in scores] == [5, 10, 20], (
        "dropping weak edges should raise the derived threshold, or this test is stale"
    )
    assert not any(g.beat_method for g in scores)
    assert scores[-1].matched_coverage - scores[-1].coverage > scores[0].matched_coverage - (
        scores[0].coverage
    ), "gaming the derived threshold must not narrow the gap"


# ── The rejection the prompt promises ──


def test_the_prompt_promise_of_rejection_is_backed_by_code() -> None:
    """The prompt tells the agent an unreachable state 'will be rejected and you will
    be asked again'. Nothing performed that rejection, so the promise was a claim about
    code that did not exist."""
    compiled = aimine.Miner().compiled("discover")
    assert compiled.config.post_conditions, "the prompt promises a check nothing performs"
    assert compiled.config.max_attempts > 0, "a rejection with no retry is not 'asked again'"


async def test_a_rejection_reaches_the_library_as_feedback_not_a_crash() -> None:
    """Registering a validator is not the same as it running. This drives the library's
    own validation path with a bad model and no model call, so the wiring is what is
    under test rather than the functions in isolation."""
    compiled = aimine.Miner().compiled("discover")
    thread = compiled.to_thread()
    stranded_and_lying = discovered(
        threshold_used=300,
        edges=[
            aimine.Edge(source="A", target="B", cases=10),
            aimine.Edge(source="B", target="C", cases=9),
            aimine.Edge(source="X", target="Y", cases=8),
        ],
    )

    errors = await thread._validate_result(
        stranded_and_lying, {}, compiled.config.post_conditions, "discover"
    )
    assert len(errors) == 2
    assert any("unreachable" in error for error in errors)
    assert any("threshold_used" in error for error in errors)
    assert not await thread._validate_result(
        discovered(), {}, compiled.config.post_conditions, "discover"
    )


def test_an_unreachable_island_is_rejected_and_re_asked() -> None:
    island = discovered(
        edges=[
            aimine.Edge(source="A", target="B", cases=10),
            aimine.Edge(source="B", target="C", cases=9),
            aimine.Edge(source="X", target="Y", cases=8),
        ]
    )
    assert aimine.unreachable_activities(island) == {"X", "Y"}
    with pytest.raises(AssertionError, match="unreachable"):
        aimine.rejects_a_disconnected_model(island)
    assert aimine.rejects_a_disconnected_model(discovered()) is None


def test_a_self_inconsistent_threshold_is_rejected_and_re_asked() -> None:
    """The stated cutoff is only worth keeping if it describes the edges returned."""
    with pytest.raises(AssertionError, match="threshold_used"):
        aimine.rejects_a_misreported_threshold(discovered(threshold_used=300))
    assert aimine.rejects_a_misreported_threshold(discovered(threshold_used=9)) is None


def test_an_unreachable_island_cannot_inflate_the_edge_count() -> None:
    """An island contributes no conforming case, so counting its edges inflates model
    size at no cost to coverage, which is exactly the wrong incentive."""
    connected = aimine.to_process(discovered(), "Connected")
    with_island = aimine.to_process(
        discovered(
            edges=[
                aimine.Edge(source="A", target="B", cases=10),
                aimine.Edge(source="B", target="C", cases=9),
                aimine.Edge(source="X", target="Y", cases=8),
            ]
        ),
        "Island",
    )
    assert len(with_island.transitions) == len(connected.transitions)
    assert {s.name for s in with_island.states} == {s.name for s in connected.states}
    assert with_island.unreachable_states() == set()


def test_pruning_an_island_leaves_coverage_alone(events: pl.DataFrame) -> None:
    """Dropping unreachable states is free: no case that replays end to end from the
    initial state can pass through one, so coverage cannot fall."""
    model = mined_by_hand(events, 25)
    with_island = model.model_copy(
        update={
            "edges": [
                *model.edges,
                aimine.Edge(source="Island in", target="Island out", cases=40),
            ]
        }
    )
    assert aimine.grade(events, with_island).coverage == aimine.grade(events, model).coverage


def test_pruning_is_coverage_neutral_on_the_real_islands_in_this_log(
    events: pl.DataFrame,
) -> None:
    """The islands are not hypothetical. The frozen miner's own output at thresholds 5,
    11, 31, 33 and 35 contains states nothing reaches from the start activity, and at 35
    it is five of eleven. Feeding those same edge sets through the compile step must drop
    them without moving the number they are compared on."""
    found = {}
    for threshold in (5, 11, 31, 33, 35):
        model = mined_by_hand(events, threshold)
        frozen = miner.mine(events, name="M", min_edge_cases=threshold)
        stranded = aimine.unreachable_activities(model)
        found[threshold] = len(stranded)

        compiled = aimine.to_process(model, "Pruned")
        assert compiled.unreachable_states() == set()
        assert miner.conformance(events, compiled) == frozen.coverage

    assert all(count for count in found.values()), (
        f"expected a real island at every listed threshold, got {found}"
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
