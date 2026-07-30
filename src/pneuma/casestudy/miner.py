"""Discover a process model from a real event log, using Polars.

This is the front half of the pipeline the rest of the project verifies. It reads
what actually happened, not what a policy document claims, and emits the same
`Process` IR that `tla.py` model-checks and `interpreter.py` executes.

The discovery is a directly-follows graph with a frequency threshold, which is the
honest choice for an executive-facing artifact: every edge in the model is an edge
somebody really walked, and the threshold is a single number you can defend. Rare
paths are dropped deliberately and reported, because a model that includes every
one-off exception is a picture of the log rather than a description of the process.

`conformance` closes the loop the other way: given a model, how many real cases can
it replay? That number is what makes the difference between a diagram and a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

from ..process.ir import Guard, Invariant, Process, State, Transition, Variable


@dataclass(frozen=True)
class Discovery:
    """A mined model plus the evidence for and against it."""

    process: Process
    edges: pl.DataFrame
    dropped_edges: pl.DataFrame
    coverage: float

    @property
    def dropped_share(self) -> float:
        total = self.edges["count"].sum() + self.dropped_edges["count"].sum()
        return 0.0 if not total else float(self.dropped_edges["count"].sum()) / float(total)


def directly_follows(events: pl.DataFrame) -> pl.DataFrame:
    """Count every (activity → next activity) pair within a case.

    The foundation of process discovery: shift the activity column within each case
    and count the pairs. Everything else is filtering and naming.
    """
    return (
        events.sort(["case_id", "position"])
        .with_columns(pl.col("activity").shift(-1).over("case_id").alias("next_activity"))
        .drop_nulls("next_activity")
        .group_by(["activity", "next_activity"])
        .agg(pl.len().alias("count"), pl.col("case_id").n_unique().alias("cases"))
        .sort("count", descending=True)
    )


def start_and_end_activities(events: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    firsts = (
        events.filter(pl.col("position") == pl.col("position").min().over("case_id"))
        .group_by("activity")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
    )
    lasts = (
        events.filter(pl.col("position") == pl.col("position").max().over("case_id"))
        .group_by("activity")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
    )
    return firsts, lasts


def _identifier(activity: str) -> str:
    """Turn 'T06 Determine necessity of stop advice' into a TLA+-safe name."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", activity).title().replace(" ", "")
    if not cleaned:
        cleaned = "Activity"
    if cleaned[0].isdigit():
        cleaned = f"A{cleaned}"
    return cleaned[:40]


def mine(
    events: pl.DataFrame,
    *,
    name: str = "MinedProcess",
    min_edge_cases: int = 25,
    description: str = "",
) -> Discovery:
    """Discover a `Process` from `events`.

    Args:
        events: One row per event, as produced by `eventlog.parse_xes`.
        name: Module name for the TLA+ spec.
        min_edge_cases: An edge is kept when at least this many distinct cases walk
            it. The single number that decides model size, and the one to state
            plainly when presenting the model.
        description: Reaches the agent's prompt, never the verifier.
    """
    edges = directly_follows(events)
    kept = edges.filter(pl.col("cases") >= min_edge_cases)
    dropped = edges.filter(pl.col("cases") < min_edge_cases)

    firsts, lasts = start_and_end_activities(events)
    start_activity = firsts["activity"][0]

    activities = sorted(
        set(kept["activity"].to_list()) | set(kept["next_activity"].to_list()) | {start_activity}
    )
    identifiers = {activity: _identifier(activity) for activity in activities}

    # An activity nobody continues from is where cases really end.
    has_successor = set(kept["activity"].to_list())
    terminals = {a for a in activities if a not in has_successor}
    if not terminals:
        # Every kept activity leads somewhere, so fall back to the observed last
        # activity — otherwise the IR has no terminal state and is rejected.
        terminals = {lasts["activity"][0]}

    states = [
        State(
            name=identifiers[activity],
            description=activity,
            agent_method="handle",
            terminal=activity in terminals,
        )
        for activity in activities
    ]

    transitions = [
        Transition(
            name=f"{identifiers[row['activity']]}To{identifiers[row['next_activity']]}"[:60],
            source=identifiers[row["activity"]],
            target=identifiers[row["next_activity"]],
        )
        for row in kept.iter_rows(named=True)
        if row["activity"] in identifiers and row["next_activity"] in identifiers
    ]

    process = Process(
        name=name,
        description=description or f"Mined from {events['case_id'].n_unique()} real cases",
        initial_state=identifiers[start_activity],
        states=states,
        transitions=_dedupe(transitions),
    )
    return Discovery(
        process=process,
        edges=kept,
        dropped_edges=dropped,
        coverage=conformance(events, process, identifiers),
    )


def _dedupe(transitions: list[Transition]) -> list[Transition]:
    seen: set[str] = set()
    unique: list[Transition] = []
    for transition in transitions:
        if transition.name in seen:
            continue
        seen.add(transition.name)
        unique.append(transition)
    return unique


def conformance(
    events: pl.DataFrame, process: Process, identifiers: dict[str, str] | None = None
) -> float:
    """Share of real cases the model can replay end to end.

    This is the number that turns a diagram into a testable claim. A case conforms
    when its first activity is the model's initial state and every consecutive pair
    is an edge the model contains.
    """
    if identifiers is None:
        identifiers = {state.description: state.name for state in process.states}

    allowed = {(t.source, t.target) for t in process.transitions}
    known = {s.name for s in process.states}

    paths = (
        events.sort(["case_id", "position"])
        .group_by("case_id")
        .agg(pl.col("activity").alias("path"))
    )

    conforming = 0
    for row in paths.iter_rows(named=True):
        mapped = [identifiers.get(activity) for activity in row["path"]]
        if any(step is None or step not in known for step in mapped):
            continue
        if mapped[0] != process.initial_state:
            continue
        if all(
            (source, target) in allowed for source, target in zip(mapped, mapped[1:], strict=False)
        ):
            conforming += 1
    return round(conforming / paths.height, 4)


def rework_rate(events: pl.DataFrame) -> pl.DataFrame:
    """Activities repeated within the same case — the cost nobody budgeted for."""
    return (
        events.group_by(["case_id", "activity"])
        .agg(pl.len().alias("times"))
        .filter(pl.col("times") > 1)
        .group_by("activity")
        .agg(
            pl.col("case_id").n_unique().alias("cases_with_rework"),
            (pl.col("times") - 1).sum().alias("extra_touches"),
        )
        .sort("extra_touches", descending=True)
    )


def bottlenecks(events: pl.DataFrame) -> pl.DataFrame:
    """Median and p95 wait between consecutive activities, in hours.

    p95 is the column that matters. A median hides the queue; the tail is where the
    citizen waiting for a permit actually lives.
    """
    return (
        events.sort(["case_id", "position"])
        .with_columns(
            pl.col("activity").shift(-1).over("case_id").alias("next_activity"),
            (
                (pl.col("ts").shift(-1).over("case_id") - pl.col("ts")).dt.total_seconds() / 3600
            ).alias("wait_hours"),
        )
        .drop_nulls(["next_activity", "wait_hours"])
        .group_by(["activity", "next_activity"])
        .agg(
            pl.len().alias("transfers"),
            pl.col("wait_hours").median().round(2).alias("median_hours"),
            pl.col("wait_hours").quantile(0.95).round(2).alias("p95_hours"),
            pl.col("wait_hours").sum().round(0).alias("total_hours"),
        )
        .filter(pl.col("transfers") >= 25)
        .sort("total_hours", descending=True)
    )


def compliance_invariant(
    process: Process,
    *,
    name: str,
    stated_as: str,
    forbidden_state: str,
    counter: str,
    at_least: int,
) -> Process:
    """Attach a business rule to a mined model.

    The rule is the part mining cannot give you. A log says what happened; whether
    it *should* have is a policy question, so the invariant comes from a human and
    the verifier decides whether the mined reality respects it.
    """
    variables = [
        *process.variables,
        Variable(name=counter, low=0, high=max(at_least, 1) + 1, initial=0),
    ]
    invariants = [
        *process.invariants,
        Invariant(
            name=name,
            stated_as=stated_as,
            forbidden_state=forbidden_state,
            forbidden_when=[Guard(variable=counter, op="lt", value=at_least)],
        ),
    ]
    return process.model_copy(update={"variables": variables, "invariants": invariants})
