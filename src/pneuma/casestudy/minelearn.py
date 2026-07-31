"""Improve the miner's own instructions *and* its own tools by backpropagation.

An agent asked to write mining analysis with the obvious algorithm named in its prompt reproduces
that algorithm rather than improving on it: `aimine` loses to the fixed implementation by counting
directly-follows pairs ranked by distinct cases, exactly what the frozen code does. Optimising the
prompt alone has a ceiling — `advice` is prose the miner reads before analysing and the loop
rewrites it from measured feedback, but better advice is remembered while better *code* is not,
since `aimine` has the agent write its analysis fresh in a sandbox every run. Advice accumulates;
capability does not. Hence two learnable parameters. Rationale: `docs/design/minelearn.md`.

**Code and prose cannot be folded into one parameter, and both folds fail concretely.**
`toolkit` is `Procedural`: source the runtime *executes at sandbox setup*, then *advertises* by
signature and docstring. Folding prose in as comments deletes it — `procedural_signatures`
advertises top-level `def` lines and their docstrings only, dropping module docstrings, comments,
and module-level constants. The reverse fold fails harder: the sandbox forbids `exec`, so code
arriving as a string variable is inert while code arriving as a `Procedural` is callable.

**The crosstalk is accepted, not solved, because it is measurable.**
`TextGradOptimizer._distribute` makes one backward call see both parameters and route feedback to
whichever it judges responsible; nothing forces an honest split. `render_inputs` labels a
procedural node `type: code` and a prose node `type: parameter`, so the parameter descriptions
here are written as routing instructions rather than documentation. And `Attempt` records
`toolkit_chars` beside `guidance_chars` and the advertised-helper count, so prose written into the
code parameter shows up as code growing while the helper count does not. One parameter would make
that unmeasurable. How often the routing is wrong on a live run is **unverified**.

**A rewritten toolkit is rehearsed before the round that depends on it.** `Procedural` setup
failures raise loudly by design — `ValueError: Failed to load procedural code into the executor
namespace` kills the cycle — and losing every accumulated helper to that is not acceptable. So
`rehearse` runs first and a failing toolkit rolls back to the last that passed, kept in a
`Frozen[Procedural]` the optimizer cannot target, with the rollback recorded on the `Attempt` and
printed. Rehearsal catches load and call-time failures, not a helper that runs and returns
something subtly worse. Two wiring details the loop needs: a recalled value arrives as a **call
argument**, since gradient targets are discovered there and anything on `self` is invisible; and
each recall happens **per call**, since a `ParameterView` is emitted once and reusing one across a
batch leaves only the first traced call carrying a gradient target.

The code parameter does not weaken the safety property. The toolkit runs in the same
AST-interpreted sandbox as any agent-written analysis — no `os`, no `open`, no `exec`, only what
`ANALYSIS_IMPORTS` authorises — and what it *returns* is still Pydantic-validated and TLC-checked.
"""

from __future__ import annotations

import inspect
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
from ai_functions import Procedural, TextGradOptimizer
from ai_functions.memory.frozen import Frozen
from pydantic import BaseModel, Field

from ..detect.objective import (
    Component,
    Domain,
    Objective,
    Probe,
    Search,
    Space,
    Structure,
    probe,
)
from ..memory import TursoMemoryBackend
from ..method import MethodAgent, ai_method
from .aimine import ANALYSIS_IMPORTS, Discovered, Edge, grade, to_csv
from .miner import directly_follows, start_and_end_activities
from .toolkit import SEED_TOOLKIT, rehearsal_probe

SEED_GUIDANCE = "# No guidance learned yet.\n"


class Guidance(BaseModel):
    """The two learnable parameters: the miner's tools, and the miner's advice.

    Both descriptions are written as routing instructions rather than as
    documentation, because the backward model reads them to decide which parameter a
    piece of feedback belongs to. `render_inputs` shows the optimizer each target's
    description alongside its `type: code` / `type: parameter` label and its own
    prompt says feedback "MUST be relevant to the node description", so a description
    that merely says what the field holds gives the router nothing to route on.
    """

    toolkit: Procedural = Field(
        default=SEED_TOOLKIT,
        description=(
            "Python helper functions the mining agent can call directly in its sandbox. "
            "Feedback here MUST be code: a function to add, or a named function to "
            "change, with the changed body. Route feedback here when the agent needed a "
            "computation it did not have, recomputed something a helper should own, or "
            "used a helper wrongly because its docstring did not say when to use it. "
            "Do NOT route strategy or policy here: a comment in this code is never "
            "shown to the agent, only top-level function signatures and docstrings are. "
            "Every import must be inside a function body, because an import at module "
            "level runs at sandbox setup and a failure there loses the whole round. "
            "The functions load_log, handoff_support, and start_activity must keep "
            "existing with those names and their first parameter unchanged; their "
            "bodies may change. Code that deletes or renames one is rejected before it "
            "is used, because they are what the pre-round check builds its inputs from."
        ),
    )

    advice: str = Field(
        default=SEED_GUIDANCE,
        description=(
            "Accumulated prose guidance on how to approach process discovery: which "
            "quantities to weigh against each other, how to choose a support cutoff, "
            "and what to avoid. Route feedback here when the agent had the right tools "
            "and used them to reach a worse answer than they permit — a judgment "
            "problem. Do NOT put code here; it is interpolated into the prompt as text "
            "and never executed."
        ),
    )

    last_good_toolkit: Frozen[Procedural] = Field(
        default=SEED_TOOLKIT,
        description=(
            "The most recent toolkit that loaded and ran in the sandbox. Bookkeeping "
            "for rollback, never a gradient target: `Frozen` sets requires_grad=False."
        ),
    )


class LearningMiner(MethodAgent):
    """A miner whose tools and whose approach are both gradient targets."""

    name = "learning-miner"

    @ai_method(
        Discovered,
        description="Discover a process model with accumulated tools and guidance",
        code_execution_mode="local",
        code_executor_additional_imports=ANALYSIS_IMPORTS,
        max_attempts=3,
    )
    def discover(
        self,
        toolkit: Procedural,
        advice: str,
        log_csv: str,
        activity_count: int,
        case_count: int,
    ) -> Discovered:
        """Discover the process behind this event log.

        Guidance learned from previous attempts:
        {advice}

        The variable `log_csv` holds a CSV with columns case_id, position, activity —
        one row per event, sorted by case then position. It covers {case_count} cases
        and {activity_count} distinct activities.

        Write Python in the executor to analyse it. `polars`, `numpy`, `statistics`,
        `collections`, `itertools`, and `math` are available. `io` is **not**, so
        `polars.read_csv(io.StringIO(log_csv))` raises; the environment block above
        lists helpers that are already defined and handle this.

        Prefer a helper that already exists over rewriting its logic. Where none fits,
        write what you need — and if it is worth having next time, say so in `method`
        so the round's feedback can add it to the toolkit permanently.

        Decide for yourself what belongs in the model, how to rank candidate handoffs,
        and where to cut. Report the cutoff in `threshold_used`. The model must be
        connected: every activity in an edge reachable from `start_activity`, and at
        least one terminal activity reachable.

        You are scored on two things at once. Coverage: how many complete real cases the
        model can replay end to end. Selectivity: how small a fraction of the distinct
        handoffs in `log_csv` you needed to get there. Keeping every handoff scores
        perfect coverage and fails, because a model containing every one-off deviation
        describes the log rather than the process.

        Every edge you return must be a consecutive activity pair you counted in
        `log_csv`. An edge no case walked is scored below keeping every real handoff.

        In `method`, say what you computed, which helpers you called, and what you had
        to write yourself. Return via `final_answer`.
        """


@dataclass(frozen=True)
class EdgeAudit:
    """The agent's returned edges, checked against the handoffs it was actually shown."""

    kept: int
    """Returned edges that are real handoffs in the log the agent saw."""

    invented: int
    """Returned edges no case in that log ever walked."""

    visible: int
    """Distinct handoffs present in the log the agent saw. The selectivity denominator."""

    @property
    def edge_share(self) -> float:
        """Fraction of the visible handoffs this model kept, in [0, 1] by construction."""
        return round(self.kept / self.visible, 4) if self.visible else 0.0

    @property
    def invented_share(self) -> float:
        """Fraction of the returned edges that are not handoffs at all."""
        total = self.kept + self.invented
        return round(self.invented / total, 4) if total else 0.0


def score_edges(discovered: Discovered, *, visible_handoffs: pl.DataFrame) -> EdgeAudit:
    """Audit returned edges against the handoffs the agent could actually have seen.

    Both halves of selectivity must be measured on the same population, and this
    function is where that is enforced. Measuring them on different populations breaks
    twice over. A numerator counting edges the agent returned, with nothing constraining
    them to be real handoffs, lets `edges / total_handoffs` exceed 1 and drives the score
    through a division pole. A denominator counting handoffs in the *full* log while the
    agent was only ever shown a sample overstates selectivity for every attempt: on the
    receipt log that denominator is 99 against 69 reachable handoffs.

    Normalising the same way the IR compile step does — drop self-loops, dedupe by
    endpoint pair — keeps `kept` comparable to the transition count that is graded.
    """
    real = {
        (row["activity"], row["next_activity"])
        for row in visible_handoffs.select("activity", "next_activity").iter_rows(named=True)
        if row["activity"] != row["next_activity"]
    }
    returned = {
        (edge.source, edge.target) for edge in discovered.edges if edge.source != edge.target
    }
    kept = returned & real
    return EdgeAudit(kept=len(kept), invented=len(returned - real), visible=len(real))


def visible_handoffs(log_csv: str) -> pl.DataFrame:
    """Distinct handoffs in the exact CSV the agent was handed."""
    return directly_follows(pl.read_csv(io.StringIO(log_csv)))


@dataclass
class Attempt:
    """One mining attempt and how it scored."""

    index: int
    coverage: float
    matched_coverage: float
    threshold: int
    states: int
    edges: int
    guidance_chars: int
    method: str = ""

    edge_share: float = 1.0
    """Fraction of the handoffs the agent was shown that this model kept."""

    invented_edges: int = 0
    """Returned edges that are not handoffs in the log at all."""

    toolkit_chars: int = 0
    """Size of the `Procedural` toolkit this round ran with.

    Reported separately from `guidance_chars` because the whole reason there are two
    parameters is that the loop can tell which one moved. One combined size column
    cannot distinguish a round that grew its tools from one that grew its prose, and
    it cannot show the failure where the optimizer wrote a paragraph of advice into
    the code parameter.
    """

    helpers: int = 0
    """Callable helpers the toolkit advertised. Growth here is capability, not text."""

    rehearsed: int = 0
    """Helpers actually called by the rehearsal before this round ran."""

    unrehearsed: tuple[str, ...] = ()
    """Advertised helpers the rehearsal could not construct arguments for.

    Named rather than counted because "we did not check these" and "these passed"
    must not read the same. A helper the agent added with an unfamiliar parameter
    name lands here, and that is a finding about the rehearsal's fixtures.
    """

    rolled_back: bool = False
    """A rewritten toolkit failed rehearsal and the last good one was restored.

    On the `Attempt` rather than in a log line because a loop that silently reverted
    the parameter it was supposed to be learning looks, from the outside, exactly like
    a loop that learned nothing.
    """

    rehearsal_error: str = ""
    """Why rehearsal failed, when it did. Empty on a healthy round."""

    @property
    def gap(self) -> float:
        """How far behind the fixed implementation, at the same threshold."""
        return round(self.matched_coverage - self.coverage, 4)

    @property
    def invented_share(self) -> float:
        return round(self.invented_edges / self.edges, 4) if self.edges else 0.0

    @property
    def score(self) -> float:
        """Coverage balanced against generalisation, penalised for invented handoffs.

        Coverage alone has a degenerate optimum: keep every handoff, including the
        thirty walked by exactly one case, and you replay the log perfectly while
        describing no process. A first run optimising coverage alone found exactly
        that — it drove the threshold to 1 and reported 98.6%.

        `1 - edge_share` stands in for generalisation: a model using a small share of
        the handoffs it was shown has abstracted, one using all of them has memorised.
        The harmonic mean forces both to be respectable.

        The clamp is load-bearing and must not be dropped in any re-derivation. Without
        it an `edge_share` above 1 makes selectivity negative, turning this harmonic mean
        into a rational function with a pole at `edge_share == 1 + coverage`: a model with
        185 edges against 99 handoffs and 86.4% coverage scores 319.386 and is selected as
        best. `score_edges` bounds the share at source; clamping here means a hand-built
        `Attempt` cannot reintroduce the pole.

        Inventing is graded below memorising. Memorising keeps every real handoff and
        earns zero; inventing puts behaviour in the model that no case ever walked,
        which is unsupported by the evidence rather than merely over-fitted to it, so
        it scores negative and can never be the best round.
        """
        share = min(max(self.edge_share, 0.0), 1.0)
        selectivity = 1.0 - share
        total = self.coverage + selectivity
        honest = 0.0 if total <= 0 else 2 * self.coverage * selectivity / total
        invented = min(max(self.invented_share, 0.0), 1.0)
        return round(honest * (1.0 - invented) - invented, 4)

    @property
    def won(self) -> bool:
        return self.coverage > self.matched_coverage


@dataclass
class Training:
    """The whole run."""

    attempts: list[Attempt] = field(default_factory=list)
    final_guidance: str = ""
    final_toolkit: str = ""
    """The toolkit the last round ran with. The artifact this loop actually produces."""

    @property
    def best(self) -> Attempt | None:
        """Best on the balanced score, not on raw coverage.

        Attempts with invented handoffs are excluded outright rather than left to lose on
        score: their model contains behaviour the log does not support, so it is not a
        candidate for "the approach that worked" no matter how it ranks.
        """
        if not self.attempts:
            return None
        honest = [a for a in self.attempts if not a.invented_edges]
        return max(honest or self.attempts, key=lambda a: a.score)

    def summary(self) -> str:
        """Render the rounds as a table, with each parameter's size in its own column.

        `helpers`, `code`, and `advice` are separate columns because the two-parameter
        design is only worth having if the table can attribute a round's movement to one
        of them. A single combined size column makes a round that grew the toolkit and a
        round that grew the prose look identical, and makes the crosstalk failure
        invisible.

        `!` marks a round whose rewritten toolkit failed rehearsal and was rolled back.
        """
        lines = [
            f"{'round':>5} {'thr':>4} {'states':>7} {'edges':>6} {'kept':>6} {'made-up':>8} "
            f"{'coverage':>9} {'matched':>8} {'score':>7} {'helpers':>8} {'code':>6} "
            f"{'advice':>7} {'':>2}"
        ]
        for attempt in self.attempts:
            lines.append(
                f"{attempt.index:>5} {attempt.threshold:>4} {attempt.states:>7} "
                f"{attempt.edges:>6} {100 * attempt.edge_share:>5.0f}% "
                f"{attempt.invented_edges:>8} "
                f"{100 * attempt.coverage:>8.1f}% {100 * attempt.matched_coverage:>7.1f}% "
                f"{attempt.score:>7.3f} {attempt.helpers:>8} {attempt.toolkit_chars:>6} "
                f"{attempt.guidance_chars:>7} {'!' if attempt.rolled_back else '':>2}"
            )
        if any(a.rolled_back for a in self.attempts):
            lines.append(
                "! toolkit failed rehearsal and was rolled back to the last good one; "
                "see Attempt.rehearsal_error"
            )
        unrehearsed = sorted({name for a in self.attempts for name in a.unrehearsed})
        if unrehearsed:
            lines.append(
                "unrehearsed helpers (no fixture matched a required parameter name): "
                + ", ".join(unrehearsed)
            )
        return "\n".join(lines)


def feedback_for(attempt: Attempt, best_so_far: float | None = None) -> str:
    """Turn the score into feedback the optimizer can route.

    Two feedback designs teach the agent the wrong lesson, and both failures are in the
    feedback rather than the mechanism.

    Reporting coverage alone drives the threshold to 1: the agent keeps every one-off
    handoff, scores 98.6%, and produces a model indistinguishable from the raw log.

    Reporting the balanced score but only *complaining* about memorisation above 60%
    edge share is worse, because it is silently one-sided. At 29% share the agent hears
    nothing but "you are behind on coverage", so it loosens the threshold every round and
    walks its score from 0.804 down to 0.706, away from its own best attempt, obediently,
    while the objective it is being scored on gets worse.

    So the score itself is reported every round, along with whether it moved up or down
    against the best attempt so far. An optimizer cannot climb a hill it is not told the
    height of.

    Invented edges are reported before anything else, and the standing clause is
    suppressed for them: a message that scolds the agent for memorising while telling it
    319.386 is a record teaches nothing. An attempt that returned handoffs no case walked
    has not set a record whatever the arithmetic says.

    Two clauses exist because the toolkit is a second parameter.

    A rollback is reported *first*, ahead of the score, because a round that ran on
    restored code did not test the rewrite it was supposed to be testing, and reading
    its score as evidence about that rewrite is a mistake. Saying so ahead of the
    number is the only ordering that prevents it.

    And every non-invented message ends by naming both channels the feedback can be
    routed to. This is not decoration: `TextGradOptimizer._distribute` shows one
    backward model both parameters and asks it to attribute, and feedback that only
    ever says "say in the guidance" trains it to route everything to the prose target,
    which is the two-parameter design failing quietly rather than the crosstalk it is
    usually mistaken for.
    """
    kept = f"{100 * attempt.edge_share:.0f}% of the handoffs the log you were shown contains"
    rollback = (
        ""
        if not attempt.rolled_back
        else (
            "Before anything else: the toolkit rewritten from last round's feedback "
            f"failed to load or run in the sandbox ({attempt.rehearsal_error.strip()[:300]}), "
            "so this round ran on the previously working toolkit and its score is not "
            "evidence about that rewrite. When you next change the code, keep every "
            "import inside a function body and change one function at a time. "
        )
    )
    if attempt.invented_edges:
        return rollback + (
            f"{attempt.invented_edges} of the {attempt.edges} edges in this model are "
            "handoffs that no case in the log ever walked — you invented them. That is a "
            "harder failure than keeping too many real edges: a memorised model at least "
            "describes something that happened, while an invented handoff puts behaviour "
            "into the model the evidence does not support, and the model is executed. "
            "Every edge you return must be a consecutive activity pair you actually "
            "counted in `log_csv`. Recompute the pairs, keep only pairs present in that "
            "count, and do not fill gaps in the graph with plausible-looking transitions. "
            f"Scored {attempt.score:.3f} this round; anything with an invented edge scores "
            "below a model that keeps every real handoff."
        )
    routing = (
        " There are two things you can change and they are not interchangeable. If the "
        "agent lacked a computation, recomputed something by hand, or misused a helper "
        "because its docstring did not say when to reach for it, that is a change to the "
        "toolkit code: give the function. If it had the right tools and still chose "
        "badly, that is a change to the prose guidance: give the judgment. Do not put "
        "prose in the code, because comments there are never shown to the agent, and do "
        "not put code in the prose, because prose is never executed."
    )
    standing = (
        ""
        if best_so_far is None
        else (
            f" Your balanced score this round is {attempt.score:.3f}; the best so far is "
            f"{best_so_far:.3f}."
            + (
                " You moved backwards — whatever changed since the best attempt made "
                "things worse, so revert toward it."
                if attempt.score < best_so_far
                else " That is the best yet."
            )
        )
    )

    if attempt.edge_share > 0.6:
        return rollback + (
            f"This model replayed {100 * attempt.coverage:.1f}% of complete cases, but it "
            f"kept {kept} at a threshold of {attempt.threshold}. That is memorising the "
            "log rather than describing the process: a handoff walked by one case out of "
            "hundreds is an exception, and a model containing all of them permits every "
            "one-off deviation as normal practice. Coverage is not the only goal. Aim for "
            "high coverage using a small fraction of the distinct handoffs, and say in "
            "the guidance how to tell a load-bearing rare edge from noise — for example "
            "by asking whether the cases needing it share anything, or whether dropping "
            "it disconnects part of the graph." + standing + routing
        )

    if attempt.won:
        return rollback + (
            f"This model replayed {100 * attempt.coverage:.1f}% of complete cases using "
            f"only {kept}, beating the reference implementation's "
            f"{100 * attempt.matched_coverage:.1f}% at the same threshold. Keep what "
            "produced that. Do not add caution that would drop more edges, and do not "
            "loosen the threshold to chase the last points of coverage." + standing + routing
        )

    return rollback + (
        f"This model replayed {100 * attempt.coverage:.1f}% of complete cases using "
        f"{kept}. The reference implementation, a plain directly-follows count at the "
        f"same threshold of {attempt.threshold}, reached "
        f"{100 * attempt.matched_coverage:.1f}%. You are {100 * attempt.gap:.1f} points "
        "behind at equal selectivity, so the model is missing handoffs real cases walk "
        "while keeping others that are not needed. Coverage is measured on whole traces: "
        "a case counts only if every consecutive pair in it is an edge, so one missing "
        "handoff disqualifies an otherwise ordinary case. Improve which edges you keep "
        "at a given threshold rather than lowering the threshold. Raising the threshold "
        "costs coverage but buys selectivity, and the balanced score often improves "
        "when you keep fewer edges, so tighter is worth testing before looser." + standing + routing
    )


def score_of(coverage: float, edge_share: float, invented_share: float = 0.0) -> float:
    """`Attempt.score` as a plain callable, so a prober can sweep it.

    The prober takes a function of its declared inputs and nothing else. Reading the score
    through a constructed `Attempt` keeps the probed quantity identical to the selected one:
    a re-implementation here could drift from `Attempt.score` and the probe would then be
    clearing an objective the loop does not use.
    """
    edges = 100
    return Attempt(
        index=0,
        coverage=coverage,
        matched_coverage=coverage,
        threshold=1,
        states=1,
        edges=edges,
        guidance_chars=0,
        edge_share=edge_share,
        invented_edges=round(invented_share * edges),
    ).score


METRIC_DOMAINS = (
    Domain(
        "coverage",
        0.0,
        1.0,
        bounded_by="miner.conformance divides conforming cases by the case count",
    ),
    Domain(
        "edge_share",
        0.0,
        1.0,
        bounded_by="score_edges intersects returned edges with real ones",
    ),
    Domain("invented_share", 0.0, 1.0, bounded_by="score_edges partitions returned edges"),
)

METRIC_STRUCTURE = Structure(
    size=lambda coverage, edge_share, invented_share: edge_share,
    viable=lambda coverage, edge_share, invented_share: edge_share > 0.0,
    units="share of the handoffs kept",
)
"""How much answer a metric point represents: the share of visible handoffs it kept.

`edge_share` and not the score's other inputs, because it is the only one that measures the
*model* rather than the model's performance. Coverage is a result; invented share is a defect.

Passed to the metric probe knowing it will not produce a degenerate finding there, and that
is deliberate rather than an oversight. `probe` will not enumerate in metric space — free
axes make the strongest empty answer converge on the ideal corner, so every sound objective
would eventually be refused — and it says so in a note. Supplying the structure anyway keeps
the report explicit about which space each check belongs to, and means a future metric-space
check derived from `size` gets it without a call-site change. The finding comes from
`threshold_objective`'s structure, in decision space, where the question is well posed.
"""


def threshold_objective(
    events: pl.DataFrame, *, sample_cases: int | None = 400, baseline_threshold: int = 25
) -> tuple[Objective, Structure, int, tuple[Component, ...]]:
    """The objective as a function of the one variable the loop actually moves.

    Composed through the real `grade` and `score_edges` on the real log, not re-derived,
    because a re-implementation would let the probe clear an objective the loop does not use.
    Returns the callable, the structure the prober enumerates degenerates from, the highest
    per-edge case support (the feasible upper bound of the threshold), and the score's two
    terms as `Component`s so the prober can say which of them stopped discriminating.

    The components read the values the composed objective recorded while the prober swept it,
    which is why they are built here rather than by the caller. Measured on the transcript
    log, `replay coverage` is 0.0227 at every one of the 44 feasible thresholds and reports
    idle; `selectivity` moves from 0.0 to 0.994 and reports discriminating. That is the cause
    behind the `emptying-is-free` refusal on the same log, named rather than inferred.

    This is the probe that catches the defect the metric-space one structurally cannot. A
    mining threshold is a decision; coverage and selectivity are coupled functions of it. In
    metric space "hold coverage and shrink the model" is always on the grid and is the ideal
    corner, so the emptying check cannot fire there. In decision space it is a real move
    with a real cost, and whether it has one is the question.
    """
    log_csv = to_csv(events, sample_cases=sample_cases)
    shown = visible_handoffs(log_csv)
    handoffs = shown.filter(pl.col("activity") != pl.col("next_activity"))
    firsts, lasts = start_and_end_activities(pl.read_csv(io.StringIO(log_csv)))
    top = int(handoffs["cases"].max())
    cache: dict[int, float] = {}
    # The score's own two inputs, recorded as the composed objective computes them, so the
    # component probe reads the quantities selection used rather than re-deriving them. A
    # re-derivation could drift and would then clear a term the loop does not actually use.
    terms: dict[int, tuple[float, float]] = {}

    def surviving(threshold: float) -> pl.DataFrame:
        return handoffs.filter(pl.col("cases") >= max(1, int(round(threshold))))

    def objective(threshold: float) -> float:
        step = max(1, int(round(threshold)))
        if step in cache:
            return cache[step]
        kept = surviving(threshold)
        if not kept.height:
            cache[step] = 0.0
            return 0.0
        model = Discovered(
            start_activity=firsts["activity"][0],
            terminal_activities=[lasts["activity"][0]],
            edges=[
                Edge(source=row["activity"], target=row["next_activity"], cases=row["cases"])
                for row in kept.iter_rows(named=True)
            ],
            threshold_used=step,
            method="probe",
        )
        graded = grade(events, model, baseline_threshold=baseline_threshold)
        audit = score_edges(model, visible_handoffs=shown)
        terms[step] = (graded.coverage, 1.0 - min(max(audit.edge_share, 0.0), 1.0))
        cache[step] = Attempt(
            index=0,
            coverage=graded.coverage,
            matched_coverage=graded.coverage,
            threshold=step,
            states=graded.states,
            edges=graded.edges,
            guidance_chars=0,
            edge_share=audit.edge_share,
            invented_edges=audit.invented,
        ).score
        return cache[step]

    structure = Structure(
        size=lambda threshold: float(surviving(threshold).height),
        units="handoffs kept",
    )

    def term(index: int) -> Component:
        def read(threshold: float) -> float:
            step = max(1, int(round(threshold)))
            if step not in terms:
                objective(threshold)
            if step not in terms:
                # No model compiles here, so neither term has a value. Reported as
                # unmeasurable rather than as zero: a zero would be a value the score never
                # used, and it would make a dead term look like it moved.
                raise ValueError(f"no model compiles at threshold {step}")
            return terms[step][index]

        return read

    components = (
        Component(name="replay coverage", term=term(0)),
        Component(name="selectivity (1 - edge share)", term=term(1)),
    )
    return objective, structure, top, components


def probe_objective(
    events: pl.DataFrame | None = None,
    *,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    search: Search | None = None,
) -> Probe:
    """Pre-flight the objective this loop optimises, before a single round runs.

    The failures this loop is exposed to live in the objective or the feedback, not the
    mechanism, and each produces a confident monotonically worsening run that looks exactly
    like training. Checking afterwards cannot distinguish the two. This checks first.

    Both bounds are declared as established by code rather than merely intended, and both
    claims are true today: `miner.conformance` divides conforming cases by the case count,
    and `score_edges` intersects the returned edges with the real ones before dividing. They
    are declared rather than assumed because the objective's own arithmetic does not enforce
    either one, and the prober reports what that leaves exposed. Run at
    `trust_declared_bounds=False` it refuses, because the clamp added for `edge_share` was
    never added for `coverage` and the division pole is still reachable on that axis.

    Two probes when `events` is given, and the second one is the one that earns its keep.

    The metric probe checks the score's arithmetic. It is the one that catches the pole, the
    escape, and the memorising and inventing corners, and it cannot check for an emptying
    optimum: its axes vary independently, so the ideal corner *is* an empty answer scoring
    the maximum, and a check that fired there would fire on every sound objective too.

    The decision probe checks the composed objective over the threshold the loop moves, and
    it is where the emptiest-answer defect lives. Measured on the permit log, the composed
    score peaks in the interior and falls from 0.8184 to 0.7680 one grid step toward emptier
    models, so emptying costs something and the probe passes. Measured on a log of AI
    coding-agent tool-use transcripts, whole-trace coverage through this exact path is
    0.0227 at *every* threshold from 1 to 44, the score reduces to selectivity alone, and a
    single-edge two-state model ties the optimum. That refuses. Nothing about it was
    declared: the emptiest answer is enumerated from `Structure.size`, which counts surviving
    handoffs.

    The decision probe also names *why*, which is the point of `components`. Those two
    findings say a degenerate input wins; they do not say that one term of the metric has no
    discriminating power on this dataset. `component-does-not-discriminate` says exactly
    that, in the same three-valued vocabulary `detect.vacuity` reports an unfirable rule
    through. Measured: on this log `replay coverage` is idle over all 44 thresholds while
    `selectivity` separates 43 of them, and on the permit log both terms discriminate.

    One thing that measurement does *not* say, and the two must not be conflated: `grade`'s
    coverage is whole-trace conformance of the *mined process* and it is the flat one.
    `miner.mine(...).coverage` on the same log is not flat, moving 0.5795 down to 0.1023 over
    the same thresholds. Two different quantities, both called coverage, and only the first is
    the one this score divides.

    `events=None` runs the metric probe alone, and the report says the decision-space checks
    did not run rather than passing quietly.

    Args:
        events: The log the loop will train on. Without it the decision probe is skipped.
        sample_cases: Cases in the CSV the agent is shown; must match what `train` uses or
            the probed objective is not the selected one.
        baseline_threshold: Passed to `grade`, same reason.
        search: An optional `Search` for adversarial candidates; see
            `pneuma.detect.adversary`. Off by default because it costs model calls, and the
            deterministic checks are what the loop must not start without.
    """
    metric = probe(
        score_of,
        METRIC_DOMAINS,
        space=Space.METRIC,
        structure=METRIC_STRUCTURE,
        source=inspect.getsource(Attempt.score.fget) if Attempt.score.fget else None,
        search=search,
    )
    if events is None:
        return Probe(
            findings=metric.findings,
            sweeps=metric.sweeps,
            notes=(
                *metric.notes,
                "decision-space checks did not run: no event log was supplied, so the "
                "objective could not be composed over the threshold the loop actually moves. "
                "emptying-is-free and the enumerated emptiest answer are decision-space "
                "checks, and they are the ones that catch a coverage term that has stopped "
                "discriminating.",
            ),
        )

    objective, structure, top, components = threshold_objective(
        events, sample_cases=sample_cases, baseline_threshold=baseline_threshold
    )
    decision = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
        components=components,
        source=inspect.getsource(Attempt.score.fget) if Attempt.score.fget else None,
        search=search,
    )
    return Probe(
        findings=metric.findings + decision.findings,
        sweeps=metric.sweeps + decision.sweeps,
        notes=(
            *(f"metric: {note}" for note in metric.notes),
            *(f"decision: {note}" for note in decision.notes),
        ),
        discrimination=decision.discrimination,
    )


@dataclass(frozen=True)
class Rehearsal:
    """Whether a candidate toolkit loads into the sandbox and its helpers run.

    The guard for the one way a code parameter can be worse than a prose one. A bad
    prose rewrite makes the next round's answer worse; a bad code rewrite makes the
    next round *impossible*, because `Procedural` setup failures raise
    `ValueError: Failed to load procedural code into the executor namespace` and take
    the whole cycle with them. Raising is the right behaviour and losing every
    accumulated helper to it is not, so the failure is provoked here, cheaply, before
    a round depends on it.

    Three outcomes and they are deliberately distinct. Loading and running is a pass.
    Failing to load or raising on a call is a fail with the message attached, and the
    caller rolls back. `unrehearsed` is neither: those helpers exist and were not
    exercised, which is a gap in the fixtures rather than a verdict on the code, and
    reporting it as a pass would be a check that never was in a position to fail.
    """

    ok: bool
    error: str = ""
    rehearsed: tuple[str, ...] = ()
    unrehearsed: tuple[str, ...] = ()
    helpers: int = 0

    def __str__(self) -> str:
        if not self.ok:
            return f"toolkit rehearsal FAILED: {self.error}"
        head = f"toolkit rehearsal ok: {len(self.rehearsed)}/{self.helpers} helpers called"
        if self.unrehearsed:
            return f"{head}; unrehearsed: {', '.join(self.unrehearsed)}"
        return head


def rehearse(code: str) -> Rehearsal:
    """Load `code` into a real sandbox and call every helper a fixture can reach.

    Uses the same `LocalPythonExecutorTool` and the same `ANALYSIS_IMPORTS` the round
    will, and provokes the identical setup path — the executor is constructed with
    `initial_code=[code]`, which is exactly what `CodeExecutionPlan.fresh_tool` does.
    A rehearsal against a different executor, or against `ast.parse` alone, would
    clear code that then fails in the round: parsing proves nothing about a
    module-level `import os`, which parses fine and aborts the cycle.

    No LLM is involved, which is what makes this affordable on every round and
    testable offline.
    """
    from ai_functions.tools.local_python_executor import (
        LocalPythonExecutorTool,
        procedural_signatures,
    )

    advertised = len(procedural_signatures(code))
    probe_source, rehearsed, skipped = rehearsal_probe(code)
    try:
        executor = LocalPythonExecutorTool(
            output_type=Discovered,
            initial_code=[code],
            additional_authorized_imports=list(ANALYSIS_IMPORTS),
        )
    except Exception as error:  # noqa: BLE001 — any setup failure is the finding
        return Rehearsal(ok=False, error=str(error), helpers=advertised)

    outcome = executor._execute_code(probe_source)  # noqa: SLF001
    if not outcome.success:
        return Rehearsal(ok=False, error=outcome.error or "", helpers=advertised)
    return Rehearsal(
        ok=True,
        rehearsed=tuple(rehearsed),
        unrehearsed=tuple(skipped),
        helpers=advertised,
    )


@dataclass(frozen=True)
class ToolkitCheck:
    """What the pre-round toolkit check decided, and why.

    Separate from `Rehearsal` because a rollback has two rehearsals in it — the
    candidate's failure and the fallback's pass — and one record cannot carry both.
    Collapsing them loses precisely the information a rolled-back round needs to
    report: the fallback passing does not make the round healthy.
    """

    report: Rehearsal
    """The rehearsal of the code the round will actually run."""

    rolled_back: bool = False
    """A rewritten toolkit failed and the last good one was put back."""

    reason: str = ""
    """The failing candidate's error, when a rollback happened."""


def check_toolkit(memory: TursoMemoryBackend) -> ToolkitCheck:
    """Decide which toolkit this round runs with, rolling back a broken rewrite.

    Reads the current `toolkit`, rehearses it, and on failure restores
    `last_good_toolkit`. The restore writes through `save`, which for a `Procedural`
    parameter re-parses before storing, so a rollback cannot itself install
    unparseable code.

    The last good value is rehearsed too when it is used, and that is not paranoia:
    a fallback is only a fallback if we know it works, and a fallback nobody checked
    is how a guard becomes theatre. When both fail, the candidate's failing rehearsal
    is returned unchanged and no rollback is claimed — the caller then lets the round
    die loudly rather than mining with code known to be broken, which is the whole
    point of `Procedural` setup raising in the first place.
    """
    candidate = str(memory.fetch("toolkit"))
    report = rehearse(candidate)
    if report.ok:
        memory.save("last_good_toolkit", candidate)
        return ToolkitCheck(report=report)

    fallback = str(memory.fetch("last_good_toolkit"))
    if fallback == candidate:
        return ToolkitCheck(report=report)
    fallback_report = rehearse(fallback)
    if not fallback_report.ok:
        return ToolkitCheck(report=report)
    memory.save("toolkit", fallback)
    return ToolkitCheck(report=fallback_report, rolled_back=True, reason=report.error)


async def train(
    events: pl.DataFrame,
    db_path: Path,
    *,
    rounds: int = 3,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    memory: TursoMemoryBackend | None = None,
) -> Training:
    """Run the improvement loop over the miner's toolkit and its guidance.

    Refuses to start against a pathological objective rather than discovering the pathology
    by climbing it.

    Both parameters are recalled per round and passed as call arguments, in that order,
    so both are gradient targets. Recalling per round rather than once is the
    single-use `ParameterView` rule: a view emits one recall event, so a view reused
    across rounds would produce a parameter node on the first traced call and none
    afterwards, and the loop would report rounds while learning nothing after the first.

    Args:
        events: The event log. Never modified.
        db_path: Database file for the parameters. Ignored when `memory` is given.
        rounds: Rounds to run. The last is measured but not learned from, since there
            is no later round to show the improvement in.
        sample_cases: Cases in the CSV handed to the agent, sampled whole.
        baseline_threshold: The fixed implementation's default setting, for the
            flattering comparison. The honest one is derived per round by `grade`.
        memory: An existing backend to train against — pass one opened on the audit
            database to keep parameters and evidence in one file. Ownership stays with
            the caller: a supplied backend is not closed here.
    """
    from ai_functions.testing import RuntimeHarness

    # `events` is passed so the decision-space half runs. Without it the pre-flight is the
    # metric probe alone, which structurally cannot see an emptying optimum: the emptiest
    # answer only becomes a distinguishable point once the objective is composed over the
    # threshold the loop moves. The metric probe alone passes on the permit log only because
    # the permit log's coverage term happens to discriminate.
    probe_objective(
        events, sample_cases=sample_cases, baseline_threshold=baseline_threshold
    ).raise_if_pathological("the miner's balanced score")

    owned = memory is None
    store = memory or TursoMemoryBackend(Guidance, actor_id="miner", path=db_path)
    optimizer = TextGradOptimizer()
    agent = LearningMiner()
    compiled = agent.compiled("discover")
    log_csv = to_csv(events, sample_cases=sample_cases)
    # Selectivity is measured against the handoffs present in the CSV the agent is
    # handed, not the full log. Sampling by case means the two differ: on the receipt
    # log a 400-case sample holds 69 distinct handoffs against the full log's 99, so
    # dividing by 99 credited the agent for handoffs it was never shown.
    shown_handoffs = visible_handoffs(log_csv)
    activities = events["activity"].n_unique()
    cases = events["case_id"].n_unique()
    training = Training()

    try:
        async with RuntimeHarness():
            for index in range(rounds):
                # Rehearse and possibly roll back *before* recalling, so the recalled
                # view carries the value the round will actually run with. Recalling
                # first and repairing after would put a value in the event log that no
                # sandbox ever loaded, and the gradient would land on text nobody ran.
                check = check_toolkit(store)
                report = check.report
                toolkit = await store.recall("toolkit")
                advice = await store.recall("advice")
                traced: Any = await compiled.trace(toolkit, advice, log_csv, activities, cases)
                discovered: Discovered = traced.value

                scored = grade(events, discovered, baseline_threshold=baseline_threshold)
                audit = score_edges(discovered, visible_handoffs=shown_handoffs)
                attempt = Attempt(
                    edge_share=audit.edge_share,
                    invented_edges=audit.invented,
                    index=index,
                    coverage=scored.coverage,
                    matched_coverage=scored.matched_coverage,
                    threshold=discovered.threshold_used,
                    states=scored.states,
                    edges=scored.edges,
                    guidance_chars=len(str(advice)),
                    toolkit_chars=len(str(toolkit)),
                    helpers=report.helpers,
                    rehearsed=len(report.rehearsed),
                    unrehearsed=report.unrehearsed,
                    rolled_back=check.rolled_back,
                    rehearsal_error=check.reason or report.error,
                    method=discovered.method,
                )
                training.attempts.append(attempt)

                if index == rounds - 1:
                    break
                best = max((a.score for a in training.attempts[:-1]), default=None)
                await optimizer.step(
                    traced, feedback_for(attempt, best_so_far=best), backends=[store]
                )

            training.final_guidance = str(store.fetch("advice"))
            training.final_toolkit = str(store.fetch("toolkit"))
    finally:
        if owned:
            store.close()

    return training
