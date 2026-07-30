"""Let the model write the mining code, and grade it against a hand-written baseline.

`miner.py` encodes one person's decision about what discovery means: count
directly-follows pairs, keep the frequent ones, drop the rest. That is a defensible
choice and it is still a choice, made once, in advance, for every process.

This module removes the choice. The agent gets the log, a sandbox with polars and
numpy in it, and the shape of the answer. It writes the analysis itself, so it can
decide what "frequent enough" means for the log in front of it, and whether to weight
by cases or events, or to look at anything else it can compute.

The safety property is unchanged, and it is the reason this is worth doing at all.
The agent produces *data* — a list of states and edges validated by Pydantic — and the
IR it produces goes through the same model-checker and the same interpreter as a
hand-mined one. Generated analysis code is sandboxed; generated structure is verified.
Neither is trusted.

`grade` is what keeps this honest. A model-written miner is only interesting if you
can say whether it beat the baseline, so every run is scored on the same conformance
measure the hand-written miner reports.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from ..process.ir import Process, State, Transition
from .miner import _identifier, conformance, mine

# The sandbox allowlist. Pure computation only: the executor blocks `os` and `open`
# regardless, so this widens what the agent can compute with, never what it can reach.
ANALYSIS_IMPORTS = ["polars", "numpy", "statistics", "collections", "itertools", "math"]


class Edge(BaseModel):
    """One discovered handoff between activities."""

    source: str = Field(description="Activity name exactly as it appears in the log")
    target: str = Field(description="Activity name exactly as it appears in the log")
    cases: int = Field(description="Distinct cases that walked this handoff")


class Discovered(BaseModel):
    """A process the agent discovered, plus its account of how."""

    start_activity: str = Field(description="The activity cases begin at")
    terminal_activities: list[str] = Field(description="Activities where cases end")
    edges: list[Edge] = Field(description="The handoffs worth keeping")
    threshold_used: int = Field(description="The support cutoff the agent chose")
    method: str = Field(
        description="Two or three sentences: what you computed, and why you cut where you did"
    )


@dataclass(frozen=True)
class Graded:
    """A discovered model measured against the hand-written baseline, two ways.

    Comparing against the baseline's *default* threshold flatters the agent, because
    the agent also chose its threshold and a looser cut mechanically buys coverage.
    The comparison that means something is against the baseline run at whatever
    threshold the agent picked: that isolates the method from the setting.
    """

    process: Process
    discovered: Discovered
    coverage: float
    baseline_coverage: float
    matched_coverage: float
    matched_states: int
    matched_edges: int
    states: int
    edges: int
    baseline_states: int
    baseline_edges: int

    @property
    def beat_default(self) -> bool:
        """Beat the baseline at its default setting. The weak claim."""
        return self.coverage > self.baseline_coverage

    @property
    def beat_method(self) -> bool:
        """Beat the baseline algorithm at the agent's own threshold. The real claim."""
        return self.coverage > self.matched_coverage

    @property
    def summary(self) -> str:
        return (
            f"agent (thr={self.discovered.threshold_used}): {self.states} states / "
            f"{self.edges} edges / {100 * self.coverage:.1f}% coverage\n"
            f"  vs baseline default:  {self.baseline_states} / {self.baseline_edges} / "
            f"{100 * self.baseline_coverage:.1f}%  -> "
            f"{'agent ahead' if self.beat_default else 'baseline ahead'}\n"
            f"  vs baseline at thr={self.discovered.threshold_used}: {self.matched_states} / "
            f"{self.matched_edges} / {100 * self.matched_coverage:.1f}%  -> "
            f"{'agent ahead' if self.beat_method else 'baseline ahead'}  (the honest comparison)"
        )


class Miner(MethodAgent):
    """Discovers a process by writing and running its own analysis.

    The log is passed as CSV text rather than a DataFrame handle: the sandbox is a
    fresh interpreter per call, so the data has to arrive as an argument the executor
    can see. That caps practical log size, which is why `sample_cases` exists.
    """

    name = "ai-miner"

    @ai_method(
        Discovered,
        description="Discover a process model from an event log by writing the analysis",
        code_execution_mode="local",
        code_executor_additional_imports=ANALYSIS_IMPORTS,
        max_attempts=3,
    )
    def discover(self, log_csv: str, activity_count: int, case_count: int) -> Discovered:
        """Discover the process behind this event log.

        The variable `log_csv` holds a CSV with columns case_id, position, activity —
        one row per event, already sorted by case then position. It covers
        {case_count} cases and {activity_count} distinct activities.

        Write Python in the executor to analyse it. `polars`, `numpy`, `statistics`,
        `collections`, `itertools`, and `math` are available. Read `log_csv` with
        `polars.read_csv(io.StringIO(log_csv))` if you prefer a DataFrame, or parse it
        directly.

        What to decide, and what I am not deciding for you:

        - Which handoffs belong in the model. Counting consecutive activity pairs is
          the obvious starting point; whether to rank them by distinct cases, by total
          occurrences, or by something you think is better is your call.
        - Where to cut. Report the cutoff you used in `threshold_used`. A model that
          keeps everything describes the log rather than the process; one that keeps
          too little describes neither.
        - Which activities start and end a case. Look at first and last positions.

        The model you return must be connected: every activity in an edge is reachable
        from `start_activity`, and at least one terminal activity is reachable. A model
        with an unreachable state will be rejected and you will be asked again.

        In `method`, say what you actually computed. Return via `final_answer`.
        """


def to_csv(events: pl.DataFrame, *, sample_cases: int | None = None) -> str:
    """Render the log as CSV for the sandbox, optionally sampling whole cases.

    Sampling is by case, never by row: half a case is a different process, and a
    truncated trace would teach the agent a handoff that does not exist.
    """
    frame = events.select("case_id", "position", "activity").sort(["case_id", "position"])
    if sample_cases is not None:
        keep = frame["case_id"].unique().sort().head(sample_cases).implode()
        frame = frame.filter(pl.col("case_id").is_in(keep))
    return frame.write_csv()


def to_process(discovered: Discovered, name: str) -> Process:
    """Compile a `Discovered` into the same IR a hand-written miner produces.

    Every guard here exists because the agent is untrusted: it can name an activity
    in an edge that it never listed as terminal, produce a self-loop, or return a
    start activity absent from its own edges. Each of those yields an IR that fails
    validation with a confusing message, so they are normalised or dropped here.
    """
    activities: list[str] = []
    for edge in discovered.edges:
        for end in (edge.source, edge.target):
            if end not in activities:
                activities.append(end)
    if discovered.start_activity not in activities:
        activities.insert(0, discovered.start_activity)

    identifiers = {activity: _identifier(activity) for activity in activities}
    has_successor = {edge.source for edge in discovered.edges if edge.source != edge.target}
    declared_terminal = {
        a for a in discovered.terminal_activities if a in identifiers and a not in has_successor
    }
    # An activity nobody continues from is terminal whether or not the agent said so;
    # without this the IR can have no terminal state and is rejected outright.
    terminals = declared_terminal or {a for a in activities if a not in has_successor}

    states = [
        State(
            name=identifiers[activity],
            description=activity,
            agent_method="handle",
            terminal=activity in terminals,
        )
        for activity in activities
    ]

    seen: set[str] = set()
    transitions: list[Transition] = []
    for edge in discovered.edges:
        if edge.source == edge.target:
            continue  # a self-loop is a rework marker, not a handoff the IR can use
        source, target = identifiers[edge.source], identifiers[edge.target]
        transition_name = f"{source}To{target}"[:60]
        if transition_name in seen:
            continue
        seen.add(transition_name)
        transitions.append(Transition(name=transition_name, source=source, target=target))

    return Process(
        name=name,
        description=f"Discovered by an agent at threshold {discovered.threshold_used}",
        initial_state=identifiers[discovered.start_activity],
        states=states,
        transitions=transitions,
    )


def grade(
    events: pl.DataFrame,
    discovered: Discovered,
    *,
    name: str = "AgentMined",
    baseline_threshold: int = 25,
) -> Graded:
    """Score a discovered model against the hand-written miner on the same log."""
    process = to_process(discovered, name)
    baseline = mine(events, name="Baseline", min_edge_cases=baseline_threshold)
    # The baseline run at the agent's own cutoff. Without this the score rewards the
    # agent for choosing a looser threshold rather than for analysing better.
    matched = mine(events, name="Matched", min_edge_cases=max(1, discovered.threshold_used))
    identifiers = {state.description: state.name for state in process.states}

    return Graded(
        process=process,
        discovered=discovered,
        coverage=conformance(events, process, identifiers),
        baseline_coverage=baseline.coverage,
        matched_coverage=matched.coverage,
        matched_states=len(matched.process.states),
        matched_edges=len(matched.process.transitions),
        states=len(process.states),
        edges=len(process.transitions),
        baseline_states=len(baseline.process.states),
        baseline_edges=len(baseline.process.transitions),
    )


async def discover_and_grade(
    events: pl.DataFrame,
    *,
    name: str = "AgentMined",
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    **overrides: object,
) -> Graded:
    """Have the agent mine `events`, then grade what it produced."""
    miner_agent = Miner()
    compiled = miner_agent.compiled("discover", **overrides)
    discovered = await compiled(
        to_csv(events, sample_cases=sample_cases),
        events["activity"].n_unique(),
        events["case_id"].n_unique(),
    )
    return grade(events, discovered, name=name, baseline_threshold=baseline_threshold)
