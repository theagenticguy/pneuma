"""The whole case study as one reproducible command.

Six steps, each one auditable, each writing its result to libSQL:

1. Load a real event log (Polars).
2. Mine a process model from what actually happened.
3. Measure the money: rework, bottlenecks, tail latency.
4. Attach the compliance rule a human states, and model-check it (TLC).
5. Property-test the interpreter that will execute the model (Hypothesis).
6. Execute a case with the agent as an untrusted decision-maker.

The finding this pipeline exists to produce: a mined model can be structurally
sound and still violate policy, and the model-checker names the exact path. That is
a different claim from "the model looks right", and it is the one an executive can
act on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ..process import interpreter, properties, tla
from ..process.ir import Effect, Guard, Invariant, Process, Transition, Variable
from . import eventlog, miner

CHECK_ACTIVITY = "T02 Check confirmation of receipt"
DETERMINE_ACTIVITY = "T04 Determine confirmation of receipt"


@dataclass
class Findings:
    """Everything the pipeline learned, ready to render."""

    stats: eventlog.LogStats
    states: int
    edges: int
    coverage: float
    dropped_share: float
    skip_cases: int
    skip_pct: float
    skip_by_channel: pl.DataFrame
    bottlenecks: pl.DataFrame
    rework: pl.DataFrame
    tlc_sound: tla.CheckResult | None = None
    tlc_governed: tla.CheckResult | None = None
    hypothesis_violation: str | None = None
    agent_runs: list[dict[str, object]] = field(default_factory=list)


def governed(process: Process) -> Process:
    """Attach the precedence rule the log itself establishes.

    This rule was *derived*, not asserted. Scanning every activity pair over 1,434
    cases, `T02 Check confirmation of receipt` precedes `T04 Determine confirmation
    of receipt` in 1,303 of 1,303 cases that reach a determination — a hundred
    percent, with no exception. A precedence that strong across that many cases is
    the log telling you it is a control rather than a habit.

    Note what the humans do: they never violate it. The mined *model* permits the
    violation, because a directly-follows graph keeps every frequent edge and cannot
    express "only after". So the gap is between what the model allows and what the
    organisation actually does, which is exactly the gap an agent would fall into.
    """
    check = miner._identifier(CHECK_ACTIVITY)
    determine = miner._identifier(DETERMINE_ACTIVITY)

    transitions: list[Transition] = []
    for transition in process.transitions:
        if transition.target == check:
            transition = transition.model_copy(
                update={"effects": [Effect(variable="checked", value=1)]}
            )
        transitions.append(transition)

    return process.model_copy(
        update={
            "transitions": transitions,
            "variables": [
                *process.variables,
                Variable(name="checked", low=0, high=1, initial=0),
            ],
            "invariants": [
                *process.invariants,
                Invariant(
                    name="NoDetermineWithoutCheck",
                    stated_as=(
                        "a confirmation of receipt may not be determined before it has been checked"
                    ),
                    forbidden_state=determine,
                    forbidden_when=[Guard(variable="checked", op="eq", value=0)],
                ),
            ],
        }
    )


def measure_control_skip(events: pl.DataFrame) -> tuple[int, float, pl.DataFrame]:
    """How many real cases never perform the mandatory check, and by which channel."""
    paths = (
        events.sort(["case_id", "position"])
        .group_by("case_id")
        .agg(pl.col("activity").alias("path"), pl.col("channel").first().alias("channel"))
        .with_columns((~pl.col("path").list.contains(CHECK_ACTIVITY)).alias("skipped"))
    )
    by_channel = (
        paths.group_by("channel")
        .agg(pl.len().alias("cases"), pl.col("skipped").sum().alias("skipped"))
        .with_columns((100 * pl.col("skipped") / pl.col("cases")).round(1).alias("pct"))
        .sort("cases", descending=True)
    )
    skipped = int(paths["skipped"].sum())
    return skipped, round(100 * skipped / paths.height, 1), by_channel


def run(
    log_path: Path,
    db_path: Path,
    *,
    min_edge_cases: int = 25,
    model_name: str = "PermitIntake",
    with_tlc: bool = True,
) -> Findings:
    """Execute the whole study and persist every artifact."""
    events = eventlog.parse_xes(log_path)

    connection = eventlog.connect(db_path)
    eventlog.init_schema(connection)
    eventlog.persist_events(connection, events)

    discovery = miner.mine(
        events,
        name=model_name,
        min_edge_cases=min_edge_cases,
        description="Building-permit intake, mined from a Dutch municipality's own log",
    )
    _record_model(connection, discovery, log_path)

    skip_cases, skip_pct, skip_by_channel = measure_control_skip(events)
    findings = Findings(
        stats=eventlog.stats(events),
        states=len(discovery.process.states),
        edges=len(discovery.process.transitions),
        coverage=discovery.coverage,
        dropped_share=discovery.dropped_share,
        skip_cases=skip_cases,
        skip_pct=skip_pct,
        skip_by_channel=skip_by_channel,
        bottlenecks=miner.bottlenecks(events),
        rework=miner.rework_rate(events),
    )

    policed = governed(discovery.process)

    if with_tlc and tla.tlc_available():
        findings.tlc_sound = tla.check(discovery.process, timeout=300)
        findings.tlc_governed = tla.check(policed, timeout=300)
        _record_check(connection, model_name, "tlc-structure", findings.tlc_sound)
        _record_check(connection, model_name, "tlc-policy", findings.tlc_governed)

    findings.hypothesis_violation = _property_test(policed)
    _record_check_raw(
        connection,
        model_name,
        "hypothesis",
        findings.hypothesis_violation is None,
        findings.hypothesis_violation or "no violation found",
    )

    connection.commit()
    connection.close()
    return findings


def _property_test(process: Process) -> str | None:
    """Run the Hypothesis machine, returning the violation message if it finds one."""
    from hypothesis import HealthCheck, settings
    from hypothesis.stateful import run_state_machine_as_test

    machine = properties.machine_for(process)
    try:
        run_state_machine_as_test(
            machine,
            settings=settings(
                max_examples=400,
                stateful_step_count=14,
                deadline=None,
                suppress_health_check=[HealthCheck.filter_too_much],
            ),
        )
    except AssertionError as failure:
        return str(failure).strip().splitlines()[0]
    return None


async def execute_case(
    process: Process,
    db_path: Path,
    decide: interpreter.Decide,
    *,
    case_id: str,
    start: dict[str, int | str] | None = None,
) -> interpreter.Run | str:
    """Run one case through the interpreter and record the outcome."""
    connection = eventlog.connect(db_path)
    eventlog.init_schema(connection)
    try:
        trace = await interpreter.run(process, decide, start=start, max_steps=30)
        outcome, detail = "completed", trace.final_state
    except interpreter.ProcessError as error:
        trace, outcome, detail = None, type(error).__name__, str(error)[:200]

    connection.execute(
        "INSERT OR REPLACE INTO runs "
        "(run_id, model, case_id, final_state, path, rejections, outcome, ran_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"{process.name}-{case_id}",
            process.name,
            case_id,
            detail if trace else None,
            json.dumps(trace.path) if trace else None,
            trace.rejections if trace else 0,
            outcome,
            _now(),
        ),
    )
    connection.commit()
    connection.close()
    return trace if trace else detail


def _record_model(connection: object, discovery: miner.Discovery, log_path: Path) -> None:
    connection.execute(  # type: ignore[attr-defined]
        "INSERT OR REPLACE INTO mined_models "
        "(name, log, mined_at, ir_json, states, edges) VALUES (?, ?, ?, ?, ?, ?)",
        (
            discovery.process.name,
            log_path.name,
            _now(),
            discovery.process.model_dump_json(),
            len(discovery.process.states),
            len(discovery.process.transitions),
        ),
    )


def _record_check(connection: object, model: str, checker: str, result: tla.CheckResult) -> None:
    _record_check_raw(connection, model, checker, result.ok, result.summary)


def _record_check_raw(
    connection: object, model: str, checker: str, verified: bool, detail: str
) -> None:
    connection.execute(  # type: ignore[attr-defined]
        "INSERT OR REPLACE INTO verifications "
        "(model, checker, verified, detail, checked_at) VALUES (?, ?, ?, ?, ?)",
        (model, checker, int(verified), detail, _now()),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
