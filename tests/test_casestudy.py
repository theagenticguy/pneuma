"""Tests over a real event log: 1,434 building-permit cases from a Dutch municipality.

Everything here runs against `data/receipt.xes`, an unmodified public XES log. The
numbers asserted below are properties of that real data, so a change in parsing or
mining that quietly alters them fails the suite.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import polars as pl
import pytest

from pneuma.casestudy import eventlog, miner, pipeline
from pneuma.process import interpreter, tla

LOG = Path(__file__).resolve().parents[1] / "data" / "receipt.xes"
pytestmark = pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")


@pytest.fixture(scope="module")
def events() -> pl.DataFrame:
    return eventlog.parse_xes(LOG)


@pytest.fixture(scope="module")
def discovery(events: pl.DataFrame) -> miner.Discovery:
    return miner.mine(events, name="PermitIntake", min_edge_cases=25)


def temp_db() -> Path:
    return Path(tempfile.mkdtemp()) / "study.db"


# ── The log parses to the real shape ──


def test_log_has_the_expected_real_dimensions(events: pl.DataFrame) -> None:
    stats = eventlog.stats(events)
    assert stats.cases == 1434
    assert stats.events == 8577
    assert stats.activities == 27
    assert stats.resources == 48


def test_timestamps_are_normalised_across_the_dst_boundary(events: pl.DataFrame) -> None:
    """The log spans 478 days, so its UTC offset changes mid-log.

    Parsing without handling that would make some durations wrong by an hour and
    could order two events backwards. Converting to UTC makes elapsed time real.
    """
    assert eventlog.stats(events).span_days > 400
    gaps = (
        events.sort(["case_id", "position"])
        .with_columns((pl.col("ts").diff().over("case_id").dt.total_seconds()).alias("gap_seconds"))
        .drop_nulls("gap_seconds")
    )
    # No event precedes the one before it in the same case, so no duration is negative.
    assert float(gaps["gap_seconds"].min()) >= 0.0


def test_duration_distribution_is_extremely_skewed(events: pl.DataFrame) -> None:
    """The headline finding: the median case is fast and the tail is not.

    An average would hide this. Half of all permits finish in under an hour while
    one in twenty takes over three weeks.
    """
    durations = eventlog.case_durations(events)
    median = float(durations["hours"].median() or 0)
    p95 = float(durations["hours"].quantile(0.95) or 0)
    assert median < 2
    assert p95 > 500
    assert p95 / max(median, 0.01) > 100


# ── Mining produces a model that replays real behaviour ──


def test_mined_model_replays_most_real_cases(discovery: miner.Discovery) -> None:
    """Conformance is what separates a model from a drawing."""
    assert discovery.coverage > 0.85
    assert discovery.dropped_share < 0.10


def test_mined_model_is_structurally_sound(discovery: miner.Discovery) -> None:
    assert discovery.process.unreachable_states() == set()
    assert any(state.terminal for state in discovery.process.states)


def test_threshold_trades_model_size_against_coverage(events: pl.DataFrame) -> None:
    """The single number to defend when presenting the model.

    A lower threshold keeps more edges and explains more cases; a higher one gives a
    model a person can read. Stating the trade is the honest presentation.
    """
    loose = miner.mine(events, name="Loose", min_edge_cases=5)
    tight = miner.mine(events, name="Tight", min_edge_cases=100)
    assert len(loose.process.transitions) > len(tight.process.transitions)
    assert loose.coverage >= tight.coverage


def test_bottleneck_analysis_finds_a_queue_the_median_hides(events: pl.DataFrame) -> None:
    """Where the waiting actually happens, in hours, ranked by total cost."""
    waits = miner.bottlenecks(events)
    assert waits.height > 0
    worst = waits.row(0, named=True)
    assert worst["total_hours"] > 10_000
    # Median near zero with a p95 in the hundreds of hours *is* the queue.
    assert worst["p95_hours"] > 100 * max(worst["median_hours"], 0.01)


def test_rework_is_quantified(events: pl.DataFrame) -> None:
    rework = miner.rework_rate(events)
    assert rework.height > 0
    assert int(rework["extra_touches"].sum()) > 0


# ── The compliance finding ──


def test_a_documented_control_is_skipped_in_real_cases(events: pl.DataFrame) -> None:
    """8.2% of permits never perform the check the process assumes."""
    skipped, pct, by_channel = pipeline.measure_control_skip(events)
    assert skipped == 118
    assert 8.0 <= pct <= 8.5
    assert set(by_channel["channel"].to_list()) >= {"Internet", "Desk", "Post"}


def test_mined_model_contains_the_edge_that_bypasses_the_control(
    discovery: miner.Discovery,
) -> None:
    """The skip is not noise: it is frequent enough to survive the threshold."""
    check = miner._identifier(pipeline.CHECK_ACTIVITY)
    start = discovery.process.initial_state
    targets = {t.target for t in discovery.process.transitions if t.source == start}
    assert len(targets) > 1, "the start activity leads somewhere other than the check"
    assert check in targets


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_structurally_sound_model_still_violates_policy(discovery: miner.Discovery) -> None:
    """The finding worth the whole exercise.

    The mined model passes every structural check — no deadlock, no unreachable
    state, every type sound. Add the rule a compliance officer states in one
    sentence, and the same model is provably wrong, with the exact path named.
    """
    structure = tla.check(discovery.process, timeout=300)
    assert structure.ok, structure.raw[-1500:]

    policy = tla.check(pipeline.governed(discovery.process), timeout=300)
    assert not policy.ok
    assert policy.violated == "NoDetermineWithoutCheck"
    assert len(policy.trace) >= 3


def test_hypothesis_independently_finds_the_same_violation(
    discovery: miner.Discovery,
) -> None:
    """Two verifiers, one conclusion, arrived at by different means."""
    violation = pipeline._property_test(pipeline.governed(discovery.process))
    assert violation is not None
    assert "NoDetermineWithoutCheck" in violation


# ── The runtime guardrail ──


async def test_the_violating_path_is_blocked_at_runtime(discovery: miner.Discovery) -> None:
    """Replaying TLC's counterexample through the live interpreter is refused.

    This is the point of putting the boundary at the harness: the agent is free to
    propose the non-compliant path, and it still cannot complete it.
    """
    governed = pipeline.governed(discovery.process)
    sequence = iter(
        [
            "ConfirmationOfReceiptToT06DetermineNecessityOfStopAdvice",
            "T06DetermineNecessityOfStopAdviceToT04DetermineConfirmationO",
        ]
    )

    async def replay(
        _state: str, enabled: list[interpreter.Transition], _v: dict[str, int | str]
    ) -> str:
        return next(sequence, enabled[0].name)

    with pytest.raises(interpreter.InvariantViolated, match="NoDetermineWithoutCheck"):
        await interpreter.run(governed, replay, max_steps=30)


async def test_a_blocked_run_is_recorded_in_libsql(discovery: miner.Discovery) -> None:
    """Refusals are audit evidence, so they are written down like anything else."""
    database = temp_db()
    sequence = iter(
        [
            "ConfirmationOfReceiptToT06DetermineNecessityOfStopAdvice",
            "T06DetermineNecessityOfStopAdviceToT04DetermineConfirmationO",
        ]
    )

    async def replay(
        _state: str, enabled: list[interpreter.Transition], _v: dict[str, int | str]
    ) -> str:
        return next(sequence, enabled[0].name)

    outcome = await pipeline.execute_case(
        pipeline.governed(discovery.process), database, replay, case_id="audit-1"
    )
    assert isinstance(outcome, str)
    assert "NoDetermineWithoutCheck" in outcome

    connection = eventlog.connect(database)
    rows = connection.execute("SELECT case_id, outcome FROM runs").fetchall()
    connection.close()
    assert rows == [("audit-1", "InvariantViolated")]


# ── libSQL persistence ──


def test_libsql_uses_wal_and_survives_reopen(events: pl.DataFrame) -> None:
    database = temp_db()
    connection = eventlog.connect(database)
    eventlog.init_schema(connection)
    written = eventlog.persist_events(connection, events)
    assert written == events.height
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # The -wal sidecar exists while the connection is open. A clean close
    # checkpoints it back into the main file and removes it, so asserting on the
    # file *after* closing tests SQLite's cleanup rather than our durability.
    assert (database.parent / f"{database.name}-wal").is_file()
    connection.close()

    reopened = eventlog.connect(database)
    assert reopened.execute("SELECT count(*) FROM events").fetchone()[0] == events.height
    reopened.close()


def test_round_trip_through_libsql_preserves_the_log(events: pl.DataFrame) -> None:
    connection = eventlog.connect(temp_db())
    eventlog.init_schema(connection)
    eventlog.persist_events(connection, events)
    restored = eventlog.read_events(connection)
    connection.close()

    assert restored.height == events.height
    assert restored["case_id"].n_unique() == events["case_id"].n_unique()
    # Mining the restored copy must reach the same conclusion as mining the original.
    assert miner.mine(restored, name="R", min_edge_cases=25).coverage == pytest.approx(
        miner.mine(events, name="O", min_edge_cases=25).coverage
    )


def test_pipeline_persists_every_artifact_for_audit() -> None:
    """One file holds the log, the model, and each verifier's verdict."""
    database = temp_db()
    pipeline.run(LOG, database, min_edge_cases=25, with_tlc=False)

    connection = eventlog.connect(database)
    events_stored = connection.execute("SELECT count(*) FROM events").fetchone()[0]
    models = connection.execute("SELECT name, states, edges FROM mined_models").fetchall()
    checks = dict(connection.execute("SELECT checker, verified FROM verifications").fetchall())
    connection.close()

    assert events_stored == 8577
    assert models == [("PermitIntake", 11, 20)]
    assert checks["hypothesis"] == 0  # the policy violation was found and recorded


# ── The live-LLM experiment (offline assertions over recorded results) ──


def test_compliance_metric_only_judges_real_branch_points() -> None:
    """A choice counts as non-compliant only where the check was actually offered.

    Without this the metric punishes the agent for being in a state the check does
    not lead from, which made an early reading of the live results look far worse
    than the behaviour justified.
    """
    from pneuma.casestudy.live import _is_compliant
    from pneuma.process.ir import Transition

    check = "T02Check"
    with_check = [
        Transition(name="ToCheck", source="A", target=check),
        Transition(name="ToSkip", source="A", target="T04Determine"),
    ]
    assert _is_compliant("A", "ToCheck", check, with_check)
    assert not _is_compliant("A", "ToSkip", check, with_check)

    # Where the check is not reachable, any legal move is compliant by definition.
    without_check = [Transition(name="Onward", source="B", target="T05Print")]
    assert _is_compliant("B", "Onward", check, without_check)


def test_pressured_framing_is_genuinely_adversarial() -> None:
    """The prompt must push on speed without ever naming the rule to break.

    An instruction that said "skip the check" would test nothing except literal
    obedience. This one creates the conditions under which a human cuts corners.
    """
    from pneuma.casestudy.live import NEUTRAL, PRESSURED

    assert "skip any step" in PRESSURED
    assert "backlog" in PRESSURED
    assert pipeline.CHECK_ACTIVITY not in PRESSURED
    assert "T02" not in PRESSURED
    assert len(PRESSURED) > len(NEUTRAL)


# ── Benchmark against the standard miners ──


def test_ir_converts_to_a_petri_net_without_silent_transitions(
    discovery: miner.Discovery,
) -> None:
    """Comparability: our model is scored by pm4py's own evaluators, not ours.

    Every baseline miner needs dozens of silent transitions — constructs with no
    counterpart in the business process. Ours needs none, which is why the same
    model can be handed to TLC.
    """
    pytest.importorskip("pm4py")
    from pneuma.casestudy.ir_petri import ir_to_petri

    net, initial, final = ir_to_petri(discovery.process)
    assert len(net.transitions) == len(discovery.process.transitions)
    assert len(net.places) == len(discovery.process.states)
    assert all(t.label is not None for t in net.transitions)
    assert len(initial) == 1
    assert len(final) == sum(1 for s in discovery.process.states if s.terminal)


def test_higher_threshold_trades_fitness_for_precision(events: pl.DataFrame) -> None:
    """The benchmark's shape, asserted cheaply without re-running every miner.

    A tighter threshold keeps only the well-travelled edges, so the model permits
    less unobserved behaviour (precision up) while replaying fewer whole cases.
    """
    loose = miner.mine(events, name="Loose", min_edge_cases=5)
    tight = miner.mine(events, name="Tight", min_edge_cases=100)
    assert len(tight.process.transitions) < len(loose.process.transitions)
    assert tight.coverage < loose.coverage


# ── Per-state handlers: the work inside a step ──


def test_handlers_are_wired_to_real_mined_activities(discovery: miner.Discovery) -> None:
    """`agent_method` was declared and never read; this asserts it now resolves."""
    from pneuma.casestudy import handlers

    handled, total = handlers.coverage(discovery.process)
    assert handled > 0, "no mined state resolves to a handler"
    assert handled < total, "not every state should need an agent; some are control points"

    check = discovery.process.state_map[miner._identifier(pipeline.CHECK_ACTIVITY)]
    bound = handlers.handler_for(check)
    assert bound is not None
    assert bound[0] == "check_confirmation"


def test_each_capability_has_its_own_typed_result() -> None:
    """One typed function per capability, which is the decorator paradigm's point."""
    from pneuma.casestudy import handlers

    worker = handlers.Caseworker(handlers.CaseFile(reference="C", facts="f"))
    assert set(worker.ai_methods()) == {"check_confirmation", "determine", "draft_advice"}
    outputs = {worker.compiled(name).output_type for name in worker.ai_methods()}
    assert outputs == {handlers.CheckResult, handlers.Determination, handlers.AdviceDraft}


async def test_dispatch_records_handler_output_on_the_case_file() -> None:
    """The paperwork accumulates, separately from the verified process state."""
    from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn

    from pneuma.casestudy import handlers
    from pneuma.process.ir import State

    case = handlers.CaseFile(reference="C-1", facts="Site plan attached; no structural calc.")
    worker = handlers.Caseworker(case)
    state = State(name="T02CheckConfirmationOfReceipt", agent_method="check_confirmation")

    async with RuntimeHarness():
        model = ScriptedModel(
            [
                Turn(
                    tool_calls=(
                        (
                            "CheckResult",
                            {
                                "verdict": "incomplete",
                                "findings": ["site plan present"],
                                "missing": ["structural calculations"],
                            },
                        ),
                    )
                )
            ]
        )
        result = await handlers.dispatch(worker, state, model=model)

    assert result.verdict == "incomplete"
    assert "site plan present" in case.findings
    assert any("structural calculations" in f for f in case.findings)


async def test_a_state_with_no_handler_costs_nothing() -> None:
    """Most mined activities are recording steps; calling a model for them is waste."""
    from pneuma.casestudy import handlers
    from pneuma.process.ir import State

    worker = handlers.Caseworker(handlers.CaseFile(reference="C", facts="f"))
    assert await handlers.dispatch(worker, State(name="Unmapped", agent_method=None)) is None
    assert await handlers.dispatch(worker, State(name="NotInTable", agent_method="x")) is None


# ── The learnable playbook (backprop wiring) ──


def test_playbook_arrives_as_a_call_argument_not_instance_state() -> None:
    """The whole reason backprop can reach it.

    `collect_nodes` discovers gradient targets in call arguments. Hide the playbook
    on `self` and the optimizer has nothing to route feedback into.
    """
    from pneuma.casestudy.learning import LearningNavigator

    process = miner.mine(eventlog.parse_xes(LOG), name="P", min_edge_cases=25).process
    compiled = LearningNavigator(process).compiled("choose")
    assert "playbook" in compiled.prompt_fn.__annotations__
    schema_keys = set(compiled.prompt_fn.__annotations__) - {"return"}
    assert schema_keys == {"playbook", "state", "options", "facts"}


def test_feedback_names_the_observed_failure_and_protects_compliance() -> None:
    """Vague feedback teaches nothing, and feedback that ignores what already works
    invites the optimizer to trade it away."""
    from pneuma.casestudy.learning import TrainingRound, feedback_for

    looping = feedback_for(TrainingRound(index=0, completed=1, looped=4, steps=[12, 12, 12, 12, 6]))
    assert "4 of 5" in looping
    assert "terminal state" in looping
    assert "Compliance was already perfect" in looping

    clean = feedback_for(TrainingRound(index=1, completed=5, looped=0, steps=[6] * 5))
    assert "Every case reached a terminal state" in clean


def test_playbook_is_prose_not_executable_code() -> None:
    """A `Procedural` playbook would force `code_execution_mode='local'`: the runtime
    rejects the function outright, because Procedural means code for the sandbox."""
    from pneuma.casestudy.learning import Playbook

    assert Playbook.model_fields["guidance"].annotation is str
