"""Let the model write the mining code, and grade it against the fixed implementation.

`miner.py` is not a hand-written baseline. Nothing in this repository is hand-written —
a model produced that file too, in one pass, and it then froze into a constant that
applies to every log. So the comparison here is not human versus machine. It is
**written once in advance** versus **written per log, with the data in front of it**.

That framing changes what a win would even look like. `miner.py` cannot inspect the
support distribution of a log it has never seen; it applies whatever threshold the
caller passes. The agent can look first. Whether looking helps is the measurement.

The agent gets the log, a sandbox with polars and numpy in it, and the shape of the
answer. It writes the analysis itself and chooses its own cutoff.

The safety property is unchanged, and it is the reason this is worth doing at all.
The agent produces *data* — a list of states and edges validated by Pydantic — and the
IR it produces goes through the same model-checker and the same interpreter as a
hand-mined one. Generated analysis code is sandboxed; generated structure is verified.
Neither is trusted.

`grade` is what keeps this honest, and it scores twice. Against the fixed
implementation at its default setting, which flatters the agent because the agent also
picked its threshold. And against the fixed implementation re-run at the setting the
agent's *edges* imply, which isolates the analysis from the setting. Only the second is
a claim about method.

That second threshold is derived from the log, never read off the agent's report. The
agent reports a cutoff in `threshold_used` and that number is worth keeping, since the
stated rationale is the artifact, but it is a claim the agent makes about itself, and
using it to configure the baseline lets it set its opponent's handicap. Claiming a loose
cutoff cripples the baseline while leaving the agent's own edges, and so its own
coverage, untouched. So the claim is reported and the measurement is derived.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from ..process.ir import Process, State, Transition
from .miner import _identifier, conformance, directly_follows, mine

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
    The comparison that means something is against the baseline run at the setting the
    agent's edges imply: that isolates the method from the setting.

    `matched_threshold` is derived from the log, and `claimed_threshold` is what the
    agent said it did. They are separate fields because the agent can only be trusted
    with the second one, and an evaluation the agent can configure measures nothing.
    """

    process: Process
    discovered: Discovered
    coverage: float
    baseline_coverage: float
    matched_coverage: float
    matched_states: int
    matched_edges: int
    matched_threshold: int
    states: int
    edges: int
    baseline_states: int
    baseline_edges: int

    @property
    def claimed_threshold(self) -> int:
        """The cutoff the agent said it used. A self-report, not a measurement."""
        return self.discovered.threshold_used

    @property
    def threshold_misreported(self) -> bool:
        """The claim is impossible given the edges returned.

        One-sided on purpose. Keeping an edge only five real cases walked while
        claiming a cutoff of 300 cannot both be true, so that is flagged. Claiming a
        cutoff *below* the tightest edge kept is merely conservative, since the log may
        simply hold no edge in between, so it is not.
        """
        return self.claimed_threshold > self.matched_threshold

    @property
    def beat_default(self) -> bool:
        """Beat the baseline at its default setting. The weak claim."""
        return self.coverage > self.baseline_coverage

    @property
    def beat_method(self) -> bool:
        """Beat the baseline algorithm at the threshold its own edges imply.

        The real claim, and the reason the threshold is derived rather than read off
        the agent's report: this comparison decides who won.
        """
        return self.coverage > self.matched_coverage

    @property
    def summary(self) -> str:
        claim = f"claimed thr={self.claimed_threshold}"
        if self.threshold_misreported:
            claim += f", contradicted by its own edges (support implies {self.matched_threshold})"
        return (
            f"agent ({claim}): {self.states} states / "
            f"{self.edges} edges / {100 * self.coverage:.1f}% coverage\n"
            f"  vs baseline default:  {self.baseline_states} / {self.baseline_edges} / "
            f"{100 * self.baseline_coverage:.1f}%  -> "
            f"{'agent ahead' if self.beat_default else 'baseline ahead'}\n"
            f"  vs baseline at the threshold those edges imply "
            f"({self.matched_threshold}): {self.matched_states} / "
            f"{self.matched_edges} / {100 * self.matched_coverage:.1f}%  -> "
            f"{'agent ahead' if self.beat_method else 'baseline ahead'}  (the honest comparison)"
        )


def rejects_a_disconnected_model(response: Discovered) -> None:
    """Post-condition: every activity in an edge must be reachable from the start.

    This is what backs the prompt's promise that an unreachable state "will be rejected
    and you will be asked again"; without a post-condition that raises, the prompt
    asserts a property no code enforces. `to_process` prunes islands rather than raising,
    which is right for the compile step and wrong as the only response: pruning silently
    discards analysis the agent thought it was submitting.
    """
    stranded = unreachable_activities(response)
    if stranded:
        raise AssertionError(
            f"unreachable from start_activity {response.start_activity!r}: "
            f"{', '.join(sorted(stranded))}. Every activity in an edge must be reachable "
            "from the start activity by following edges. Either connect them or drop them."
        )


def rejects_a_misreported_threshold(response: Discovered) -> None:
    """Post-condition: `threshold_used` must describe the edges actually returned.

    The report is kept rather than derived away because the agent's stated cutoff and
    rationale are the interesting artifact. Keeping it means checking it: a cutoff
    higher than the support of an edge the agent kept cannot be the cutoff it applied.

    One-sided deliberately, and this only checks the claim against the agent's own
    `cases` numbers, which the agent also authored. Both fields agreeing is a
    consistency check, not proof. `grade` counts the real support from the log.
    """
    if not response.edges:
        return
    weakest = min(edge.cases for edge in response.edges)
    if response.threshold_used > weakest:
        raise AssertionError(
            f"threshold_used is {response.threshold_used} but you kept an edge walked by "
            f"{weakest} cases, so that is not the cutoff you applied. Report the cutoff "
            "your own edges reflect."
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
        post_conditions=[rejects_a_disconnected_model, rejects_a_misreported_threshold],
    )
    def discover(self, log_csv: str, activity_count: int, case_count: int) -> Discovered:
        """Discover the process behind this event log.

        The variable `log_csv` holds a CSV with columns case_id, position, activity —
        one row per event, already sorted by case then position. It covers
        {case_count} cases and {activity_count} distinct activities.

        Write Python in the executor to analyse it. `polars`, `numpy`, `statistics`,
        `collections`, `itertools`, and `math` are available. Read `log_csv` with
        `polars.read_csv(log_csv.encode())` if you prefer a DataFrame, or parse it
        directly. `io` is not an authorised import here, so the `io.StringIO` route
        raises; encoding the string is what works.

        What to decide, and what I am not deciding for you:

        - Which handoffs belong in the model. Counting consecutive activity pairs is
          the obvious starting point; whether to rank them by distinct cases, by total
          occurrences, or by something you think is better is your call.
        - Where to cut. Report the cutoff you used in `threshold_used`. A model that
          keeps everything describes the log rather than the process; one that keeps
          too little describes neither.
        - Which activities start and end a case. Look at first and last positions.

        Two things are checked, and failing either sends this back to you:

        - The model must be connected. Every activity in an edge is reachable from
          `start_activity` by following edges, and at least one terminal activity is
          reachable. An unreachable island is rejected.
        - `threshold_used` must match the edges you return. Claiming a cutoff higher
          than the support of an edge you kept is rejected.

        `threshold_used` does not set the bar you are measured against. Your model is
        compared to a fixed implementation run at the cutoff your edges actually imply,
        counted from the log, so a loose claim buys you nothing. Report the number you
        used and say why.

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


def unreachable_activities(discovered: Discovered) -> set[str]:
    """Activities in the agent's edges that no path from `start_activity` reaches.

    Works on the `Discovered` rather than the compiled IR because the compile step
    prunes them, and something has to see them in order to reject them.
    """
    successors: dict[str, list[str]] = {}
    activities: set[str] = set()
    for edge in discovered.edges:
        activities.update((edge.source, edge.target))
        successors.setdefault(edge.source, []).append(edge.target)

    reached = {discovered.start_activity}
    frontier = [discovered.start_activity]
    while frontier:
        for target in successors.get(frontier.pop(), ()):
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    return activities - reached


def observed_threshold(events: pl.DataFrame, discovered: Discovered) -> int:
    """The tightest cutoff under which every edge the agent kept survives.

    This is the setting to run the baseline at, and it is measured against `events`
    rather than read from the agent. `Discovered.threshold_used` is the agent's own
    account of itself and `Edge.cases` is too: both are fields the agent fills in, so
    trusting either lets it choose the handicap its opponent runs under. The support
    of an edge is a fact about the log, so it is counted here.

    An edge the log does not contain has support zero, which floors the result at 1:
    a model containing a handoff nobody walked is not a thresholded model at all, and
    it then faces the baseline at its most permissive, which is its strongest.

    Islands are counted before the compile step prunes them, so an invented edge that
    is also unreachable still floors the threshold. Reading the pruned graph instead
    would let one manipulation hide behind another.

    What this does not close: the derived threshold still moves if the agent drops its
    own weakest edges, and a higher threshold is a weaker baseline. That channel is not
    free the way the self-report was: the dropped edges leave the agent's model too, so
    its own coverage falls alongside the baseline's, and on this log the gap widens
    rather than narrows. Measured in `test_raising_the_derived_threshold_costs_what_it_buys`.
    """
    if not discovered.edges:
        return 1

    support = {
        (row["activity"], row["next_activity"]): row["cases"]
        for row in directly_follows(events).iter_rows(named=True)
    }
    real = [edge for edge in discovered.edges if edge.source != edge.target]
    if not real:
        return 1
    return max(1, min(support.get((edge.source, edge.target), 0) for edge in real))


def to_process(discovered: Discovered, name: str) -> Process:
    """Compile a `Discovered` into the same IR a hand-written miner produces.

    Every guard here exists because the agent is untrusted: it can name an activity
    in an edge that it never listed as terminal, produce a self-loop, or return a
    start activity absent from its own edges. Each of those yields an IR that fails
    validation with a confusing message, so they are normalised or dropped here.
    """
    # An island no path from the start reaches contributes no replayable case, so
    # keeping it would grow the model's edge count at no cost to its coverage. That is
    # the wrong incentive under any score that trades the two off, and `minelearn`
    # scores exactly that trade. Dropping it cannot lower coverage either: a case that
    # replays from the initial state never enters an unreachable state.
    unreachable = unreachable_activities(discovered)
    edges = [
        edge
        for edge in discovered.edges
        if edge.source not in unreachable and edge.target not in unreachable
    ]

    activities: list[str] = []
    for edge in edges:
        for end in (edge.source, edge.target):
            if end not in activities:
                activities.append(end)
    if discovered.start_activity not in activities:
        activities.insert(0, discovered.start_activity)

    identifiers = {activity: _identifier(activity) for activity in activities}
    has_successor = {edge.source for edge in edges if edge.source != edge.target}
    declared_terminal = {
        a for a in discovered.terminal_activities if a in identifiers and a not in has_successor
    }
    # An activity nobody continues from is terminal whether or not the agent said so;
    # without this the IR can have no terminal state and is rejected outright.
    terminals = declared_terminal or {a for a in activities if a not in has_successor}

    if not terminals:
        # Every activity has a successor, so nothing is structurally terminal. A live run
        # produced exactly this once the agent was pushed toward tighter models: cycles
        # among the frequent activities and no exit. The IR rejects it, correctly, and
        # raising there loses the whole training round. Trust the agent's declared
        # terminals instead, since a state that is reachable and marked terminal at least
        # gives the interpreter somewhere to stop.
        terminals = {a for a in discovered.terminal_activities if a in identifiers}
    if not terminals:
        # Nothing usable was declared either. Fall back to the target of the
        # lowest-support edge: the least-travelled destination is the best available
        # guess at where cases drain, and any terminal beats a rejected process.
        ranked = sorted((e for e in edges if e.target in identifiers), key=lambda e: e.cases)
        if ranked:
            terminals = {ranked[0].target}

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
    for edge in edges:
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
        description=f"Discovered by an agent, which reported threshold {discovered.threshold_used}",
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
    """Score a discovered model against the hand-written miner on the same log.

    The matched threshold must be derived from the log by `observed_threshold` and never
    read off `discovered.threshold_used`. A self-reported threshold makes the agent the
    author of its own handicap, and nothing downstream detects it: the summary line still
    prints the result labelled "the honest comparison".

    On the permit log, a model whose edges are the frozen miner's own at cutoff 5 scores
    96.4% coverage and ties the derived baseline at 96.4%, so it does not win. Change one
    self-reported field to claim a cutoff of 300, leaving its edges and therefore its
    coverage untouched, and the baseline it faces drops to 59.1%: the same model now wins
    by 37.2 points.
    """
    process = to_process(discovered, name)
    baseline = mine(events, name="Baseline", min_edge_cases=baseline_threshold)
    matched_threshold = observed_threshold(events, discovered)
    matched = mine(events, name="Matched", min_edge_cases=matched_threshold)
    identifiers = {state.description: state.name for state in process.states}

    return Graded(
        process=process,
        discovered=discovered,
        coverage=conformance(events, process, identifiers),
        baseline_coverage=baseline.coverage,
        matched_coverage=matched.coverage,
        matched_states=len(matched.process.states),
        matched_edges=len(matched.process.transitions),
        matched_threshold=matched_threshold,
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
