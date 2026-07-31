"""The agent proposes the *harness*; the detectors admit or reject it.

Harness code, not the model's judgment, is where the defects live, and it is the one place a bad
rewrite is invisible: a broken objective does not error, it reports a confident number that gets
monotonically worse while looking exactly like training. So one numeric harness parameter goes to
the optimizer with the detectors in front of it as a gate. **A harness parameter may be learned only
if the detectors can reject the pathological settings of it, and the settings that would widen what
the runtime permits are not parameters at all.** Tables: `docs/design/harnesslearn.md`.

**Delegated: `coverage_weight`.** `weighted_score` is the one-parameter family whose `w = 0.5`
case is exactly `Attempt.score`, verified equal to 4 decimal places at all 605 points of a grid
including negative coverage and `edge_share` above 1. The gate discriminates over its domain — `w`
of 0.0 and 0.001 REFUSE at 0/3 rules live, 0.05 PASSES the probe yet is REJECTED for moving the
selected threshold 17 -> 114 where a derived precedence goes unsatisfiable, 0.5 and 0.9 admit at
3/3. The schema's `ge=0.05` floor therefore sits inside the refused region.

**Not delegated, safety-relevant: the threshold search window.** Widening it buys score and kills
rules: windows 30 and 60 PASS at 3/3 (peaks 0.8210 and 0.8274), 114 PASSES at 2/3, 150 PASSES at
0/3, 323 PASSES at 0.8184 and 0/3. Raising the mining threshold removes the edges that could reach
the forbidden state, so TLC explores the whole space, reports no error, and the green verdict is
about the graph's shape rather than the rule — invisible to the objective probe. It stays a
constant, and `admits` measures rule liveness so the exclusion is a number, not a promise.
**`sweep_resolution`** and the **vacuity sweep budget** stay fixed because the gate cannot judge
how hard it looks — resolution 2 REFUSES a sound objective with a false pole, so a coarse grid
refusing *more* would reward an optimizer for blinding it the other way. **`min_support`** moves
while nothing measurable changes; **finding severity** has no parameter at all.

Three structural enforcements. `HarnessKnobs` declares exactly one field and
`MemoryBackend._resolve_field` indexes `model_fields`, so `save("threshold_window", ...)` raises
`KeyError` — the excluded parameters are absent, not protected. `admit` composes the objective at
the *candidate* value as a post-condition on `propose`, so refusal is the default and the detector's
own report becomes the feedback. `Admission.ok` is conjunctive across two independent detectors,
since an objective-only gate PASSES a setting making all three rules vacuous.

`GradFeedback.score` drives `_numeric_update`, a deterministic trust-region search, and the text is
ignored except as rationale — `score=None` leaves the value byte-identical. `quality` is the
emptying margin (0.2500 -> 0.6250) and rule share (0.6667 -> 1.0000), not the objective's peak,
since peak is *maximised at the pathological end*: 0.9855 at `w = 0.0` where the empty model wins,
against 0.8184 at the honest `w = 0.5`.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from ai_functions.types.graph import GradFeedback
from pydantic import BaseModel, Field

from ..detect.discrimination import Discrimination
from ..detect.objective import (
    DEFAULT_RESOLUTION,
    Component,
    Domain,
    Objective,
    Probe,
    Space,
    Structure,
    probe,
)
from ..memory import TursoMemoryBackend
from ..method import MethodAgent, ai_method
from . import miner, rules

if TYPE_CHECKING:
    from ..process.ir import Process

SEED_WEIGHT = 0.5
"""The weight at which `weighted_score` is exactly `Attempt.score`. The honest baseline."""

WEIGHT_FLOOR = 0.05
WEIGHT_CEILING = 0.95
"""The delegated domain. Both ends are inside the region the gate accepts on the permit log,
which is deliberate: the schema bound is a convenience, not the safety argument. The gate is
the safety argument, and `admit` is exercised outside these bounds by the negative control
so that claim is measured rather than asserted."""


class HarnessKnobs(BaseModel):
    """The harness parameters an optimizer may move. Exactly one, and that is the point.

    This class is the allowlist and the enforcement at once. `MemoryBackend._resolve_field`
    resolves a parameter name through `model_fields`, so a name absent here raises
    `KeyError` on both `fetch` and `save`. The threshold search window, the sweep
    resolution, the vacuity budget, the rule support floor and every finding's severity are
    absent rather than defended, because a gradient cannot reach a field that does not
    exist. See the module docstring for the measurement behind each exclusion.
    """

    coverage_weight: float = Field(
        default=SEED_WEIGHT,
        ge=WEIGHT_FLOOR,
        le=WEIGHT_CEILING,
        description=(
            "How much the mining objective weights replay coverage against selectivity, in "
            "(0, 1). Learned from the score channel, not from text: this is a number and a "
            "rewritten number is an assertion, while a score is a measurement of how the "
            "current value performed. 0.5 is the equally weighted harmonic mean the harness "
            "used before it was learnable. Higher weights coverage, lower weights "
            "selectivity. Both extremes are pathological and the extreme toward selectivity "
            "is the dangerous one, because coverage is the term that punishes an empty model "
            "and weighting it out makes the emptiest model win."
        ),
    )


def weighted_score(
    coverage: float, edge_share: float, invented_share: float = 0.0, *, weight: float = SEED_WEIGHT
) -> float:
    """`Attempt.score` with the component weight exposed. At `weight=0.5` it is exactly that.

    The identity is the reason this is a rewrite of the arithmetic rather than a new formula.
    A weighted harmonic mean of coverage `c` and selectivity `s` with weight `w` on coverage
    is `c*s / (w*s + (1-w)*c)`, and at `w = 0.5` that is `2*c*s / (c+s)`, the unweighted
    mean `Attempt.score` computes. Verified equal at 4 decimal places over 605 points
    including negative coverage, `edge_share` above 1 and invented shares outside `[0, 1]`,
    so the seed harness is the shipped harness and every measured movement is against the
    real thing.

    Both clamps and the invention penalty are carried over unchanged, deliberately. The
    `edge_share` clamp is load-bearing and must not be dropped in any re-derivation: without
    it an `edge_share` above 1 makes selectivity negative, turning the harmonic mean into a
    rational function with a pole at `edge_share == 1 + coverage`, where a garbage model
    scores 319.386 and is selected as best. The weight moves how the two terms trade off and
    touches neither bound.
    """
    selectivity = 1.0 - min(max(edge_share, 0.0), 1.0)
    denominator = weight * selectivity + (1.0 - weight) * coverage
    honest = 0.0 if denominator <= 0 else coverage * selectivity / denominator
    invented = min(max(invented_share, 0.0), 1.0)
    return round(honest * (1.0 - invented) - invented, 4)


@dataclass(frozen=True)
class Harness:
    """A composed candidate harness: the objective, its shape, and its declared window.

    Returned by `compose` and consumed by `admit`, so the probe measures the callable the
    loop would actually run rather than a description of it. `window` is here as data
    precisely because it is *not* delegable: the value the gate probes over is fixed by
    `compose`'s caller and visible in the record, so a report can show what the search space
    was without there being any way for a gradient to change it.
    """

    objective: Objective
    structure: Structure
    components: tuple[Component, ...]
    window: int
    """Highest threshold the sweep may reach. A constant, and see the module docstring."""

    weight: float

    @property
    def axis(self) -> Domain:
        return Domain(
            "threshold", 1, self.window, integral=True, feasible=(1.0, float(self.window))
        )


def compose(
    events: pl.DataFrame,
    *,
    weight: float = SEED_WEIGHT,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    window: int | None = None,
) -> Harness:
    """Compose the mining objective at a candidate weight, through the real grading path.

    Mirrors `minelearn.threshold_objective` and reuses its `grade` and `score_edges` calls
    for the same stated reason: a re-implementation would let the gate clear an objective
    the loop does not use. The one difference is that the score is `weighted_score` at the
    candidate weight instead of `Attempt.score`, and at the seed weight those are the same
    function.

    Args:
        events: The log the loop trains on. Never modified.
        weight: The candidate component weight. Not validated here — `admit` is what judges
            a candidate, and clamping silently would hide exactly the proposal the gate
            exists to reject.
        sample_cases: Cases in the CSV the agent sees. Must match what the training loop
            uses or the probed objective is not the selected one.
        baseline_threshold: Passed to `grade`, same reason.
        window: Highest threshold the sweep may reach. Defaults to the log's maximum
            per-edge support, which is the feasible limit. A caller may narrow it; the
            optimizer cannot, because it is not a schema field.
    """
    import io

    from .aimine import Discovered, Edge, grade, to_csv
    from .minelearn import score_edges, visible_handoffs

    log_csv = to_csv(events, sample_cases=sample_cases)
    shown = visible_handoffs(log_csv)
    handoffs = shown.filter(pl.col("activity") != pl.col("next_activity"))
    firsts, lasts = miner.start_and_end_activities(pl.read_csv(io.StringIO(log_csv)))
    top = int(handoffs["cases"].max())
    cache: dict[int, float] = {}
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
            method="harness probe",
        )
        graded = grade(events, model, baseline_threshold=baseline_threshold)
        audit = score_edges(model, visible_handoffs=shown)
        terms[step] = (graded.coverage, 1.0 - min(max(audit.edge_share, 0.0), 1.0))
        cache[step] = weighted_score(
            graded.coverage,
            audit.edge_share,
            audit.invented / graded.edges if graded.edges else 0.0,
            weight=weight,
        )
        return cache[step]

    def term(index: int) -> Component:
        def read(threshold: float) -> float:
            step = max(1, int(round(threshold)))
            if step not in terms:
                objective(threshold)
            if step not in terms:
                # No model compiles here, so neither term has a value. Raising rather than
                # returning zero: a zero would be a value the score never used, and it would
                # make a dead term look like it moved.
                raise ValueError(f"no model compiles at threshold {step}")
            return terms[step][index]

        return read

    return Harness(
        objective=objective,
        structure=Structure(
            size=lambda threshold: float(surviving(threshold).height), units="handoffs kept"
        ),
        components=(
            Component(name="replay coverage", term=term(0)),
            Component(name="selectivity (1 - edge share)", term=term(1)),
        ),
        window=top if window is None else window,
        weight=weight,
    )


_EMPTYING = re.compile(r"of (\d+) grid-adjacent pairs where the answer shrinks, (\d+) cost score")


def emptying_margin(report: Probe) -> Discrimination:
    """How much of the swept space prefers a fuller answer, as a discrimination measurement.

    Read off the note `_check_emptying` already writes rather than recomputed, and that is
    load-bearing rather than lazy: a second implementation of the adjacency walk could
    disagree with the one the refusal is based on, and then the quality signal would be
    rating an objective the gate did not judge. The counters are the gate's own.

    `withheld` carries the case where the check did not run, which keeps this three-valued.
    A sweep where no adjacent pair changed the answer's size produced no evidence about
    whether emptying is free, and scoring that as zero would make an unmeasured harness look
    identical to one measured and found monotone in emptiness.
    """
    for note in report.notes:
        found = _EMPTYING.search(note)
        if found:
            return Discrimination(
                subject="emptying costs score",
                observations=int(found.group(1)),
                separating=int(found.group(2)),
                unit="shrinking pair",
                kind="harness parameter",
            )
    refused = any(f.check == "emptying-is-free" for f in report.refusals)
    if refused:
        # The refusal names the walked total but no pair cost score, which is the finding
        # itself rather than an abstention: examined in full, never fired.
        walked = 0
        for finding in report.refusals:
            if finding.check == "emptying-is-free":
                digits = re.search(r"across all (\d+) grid-adjacent pairs", finding.detail)
                walked = int(digits.group(1)) if digits else 0
        return Discrimination(
            subject="emptying costs score",
            observations=walked,
            separating=0,
            unit="shrinking pair",
            kind="harness parameter",
        )
    return Discrimination(
        subject="emptying costs score",
        observations=0,
        separating=0,
        withheld=(
            "the emptying check did not run on this sweep, so no shrinking pair was "
            "compared and this harness has no measurement either way",
        ),
        unit="shrinking pair",
        kind="harness parameter",
    )


def rule_liveness(
    events: pl.DataFrame,
    process: Process,
    *,
    min_support: int = 100,
    max_rules: int = 3,
) -> Discrimination:
    """Can the derived compliance rules still fire on the model this harness selects?

    The safety half of the gate, and the half an objective probe structurally cannot supply.
    Measured on the permit log: at a threshold window of 150 the objective probe reports
    PASS with a peak of 0.8210, while all three derived precedences are `unsatisfiable` —
    the mined graph no longer contains an edge that could reach the forbidden state, so TLC
    explores the whole space, finds no error, and reports green about a rule protecting
    nothing. A threshold change made the rule unsatisfiable and the checker still says green,
    and the score-shaped checks cannot see it.

    Expressed as a `Discrimination` because it is the same question one level up: a rule no
    reachable state can break cannot tell a compliant run from a violation. `unknown`
    verdicts land in `withheld` rather than counting as either live or dead, because a sweep
    that hit its budget is not evidence about the states it never reached.

    `min_support` and `max_rules` are arguments rather than learnable parameters. Measured
    across support floors of 1, 20, 100, 300 and 500 the outcome was identical every time on
    this log, so there is no gradient in it; see the module docstring.
    """
    with warnings.catch_warnings():
        # `RuleNotEnforced` is expected here: on the permit log five of the first nine
        # derived precedences are declined, which `Governed.skipped` records. The gate reads
        # the verdicts, and re-raising the warnings would make measuring the harness noisy
        # without adding information the record does not already carry.
        warnings.simplefilter("ignore", rules.RuleNotEnforced)
        governed = rules.apply_derived_rules(
            events, process, min_support=min_support, max_rules=max_rules, on_vacuous="ignore"
        )
    unknown = tuple(
        f"{p.rule_name}: the vacuity sweep did not settle, so this rule's liveness is "
        "unmeasured rather than absent"
        for p in governed.unknown
    )
    return Discrimination(
        subject="derived compliance rules can fire",
        observations=len(governed.applied),
        separating=len(governed.live),
        withheld=unknown,
        unit="attached rule",
        kind="harness parameter",
    )


@dataclass(frozen=True)
class Admission:
    """Whether a proposed harness may be used, what it scored, and why.

    `ok` is conjunctive and the two halves are not interchangeable. The objective probe
    decides whether the *score* is pathological; `rules` decides whether the model this
    harness selects still admits a rule that can fire. Folding the second into `quality`
    would let a high objective number outvote a dead compliance rule, which is precisely the
    trade the window measurement shows is available.
    """

    weight: float
    report: Probe
    emptying: Discrimination
    rules: Discrimination
    threshold: int
    """The threshold this harness selects. Reported because it is what the rules are read at."""

    baseline_rules: int = 0
    """Rules live at the seed harness. The bar the candidate must not fall below."""

    @property
    def refusals(self) -> tuple[str, ...]:
        return tuple(f.check for f in self.report.refusals)

    @property
    def regressed(self) -> bool:
        """Did this candidate kill a rule that was live at the seed?

        A regression against the seed rather than against an absolute floor, because how
        many precedences a log yields is a property of the log. Requiring three would be a
        constant fitted to the permit fixture, which is the same defect one level up: a check
        that passes because of what the fixture happens to contain.
        """
        return (self.rules.separating or 0) < self.baseline_rules

    @property
    def ok(self) -> bool:
        return self.report.ok and not self.regressed

    @property
    def quality(self) -> float:
        """The score channel's value, in `[0, 1]`. Zero for anything the gate rejects.

        Built from the two counters above rather than from the objective's peak, and that is
        a measurement not a preference: peak is *maximised at the pathological end*, 0.9855
        at `weight=0.0` where the empty model wins against 0.8184 at the honest seed. An
        optimizer climbing peak would climb into the refusing region and report a record.

        Zero rather than a partial credit for a rejected harness, so the search cannot
        approach the refusing region and be rewarded for getting closer.
        """
        if not self.ok:
            return 0.0
        rules_share = (
            self.rules.separating / self.rules.observations if self.rules.observations else 0.0
        )
        margin = (
            self.emptying.separating / self.emptying.observations
            if self.emptying.observations
            else 0.0
        )
        return round((margin + rules_share) / 2.0, 4)

    @property
    def discrimination(self) -> tuple[Discrimination, ...]:
        return (self.emptying, self.rules)

    def report_text(self) -> str:
        """Why this harness was admitted or rejected, in the detectors' own words.

        This string is what a rejected proposal is re-asked with, so it has to name the
        cause rather than the verdict: "rejected" teaches nothing, while
        "emptying-is-free, because the term that punishes an empty model has been weighted
        out" tells the proposer which direction to move.
        """
        verdict = "ADMITTED" if self.ok else "REJECTED"
        lines = [
            f"harness gate: {verdict} — coverage_weight={self.weight:g}, "
            f"quality={self.quality:.4f}, threshold selected={self.threshold}"
        ]
        if self.regressed:
            lines.append(
                f"  REJECTED by the safety half: {self.rules.separating} of "
                f"{self.rules.observations} derived compliance rules can still fire at the "
                f"threshold this harness selects, against {self.baseline_rules} at the seed "
                "harness. A rule no reachable state can break is decoration, and the "
                "model-checker reports green about it, so a harness that kills one has made "
                "the verification weaker while the score says nothing. Ranking may change; "
                "what the runtime admits may not."
            )
        lines.extend(f"  {finding}" for finding in self.report.refusals)
        lines.extend(f"  discrimination {d}" for d in self.discrimination)
        return "\n".join(lines)


def admit(
    events: pl.DataFrame,
    weight: float,
    *,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    window: int | None = None,
    resolution: int = DEFAULT_RESOLUTION,
    baseline_rules: int | None = None,
    min_support: int = 100,
    max_rules: int = 3,
) -> Admission:
    """Run both detectors against a candidate harness weight and decide.

    The gate. Composes the objective at `weight`, probes it over the fixed threshold window,
    mines at whatever threshold that objective selects, attaches the derived precedences and
    asks whether they can still fire. Both answers are required.

    Args:
        events: The log the loop trains on.
        weight: The candidate. Deliberately unvalidated and unclamped: a value outside the
            schema's own bounds must be *judged* here rather than silently corrected, and the
            negative control depends on being able to hand this a pathological value.
        sample_cases: Cases in the CSV the agent sees. Must match the training loop.
        baseline_threshold: Passed to `grade`, same reason.
        window: Highest threshold the sweep may reach. Not delegable; see the module
            docstring for the measurement.
        resolution: Samples per axis. Not delegable either, and for a sharper reason: at
            resolution 2 the prober manufactures a false pole and a false emptying refusal on
            a sound objective, so a coarser grid makes the gate refuse more. An optimizer
            rewarded for a passing gate would set this to make the gate blind.
        baseline_rules: Rules live at the seed harness. Measured here when omitted, which
            costs one extra mine plus one rule audit; pass it to reuse a measurement across
            several candidates.
        min_support: Support floor for derived precedences. Measured not to discriminate on
            the permit log, hence an argument rather than a parameter.
        max_rules: State-space guard, passed through. Each rule doubles TLC's work.
    """
    harness = compose(
        events,
        weight=weight,
        sample_cases=sample_cases,
        baseline_threshold=baseline_threshold,
        window=window,
    )
    report = probe(
        harness.objective,
        (harness.axis,),
        space=Space.DECISION,
        structure=harness.structure,
        components=harness.components,
        resolution=resolution,
    )
    best = report.sweeps[0].best if report.sweeps else None
    threshold = int(best.point["threshold"]) if best else 1
    mined = miner.mine(events, name="HarnessCandidate", min_edge_cases=threshold).process
    live = rule_liveness(events, mined, min_support=min_support, max_rules=max_rules)

    if baseline_rules is None:
        seed = compose(
            events,
            weight=SEED_WEIGHT,
            sample_cases=sample_cases,
            baseline_threshold=baseline_threshold,
            window=window,
        )
        seed_report = probe(
            seed.objective,
            (seed.axis,),
            space=Space.DECISION,
            structure=seed.structure,
            components=seed.components,
            resolution=resolution,
        )
        seed_best = seed_report.sweeps[0].best if seed_report.sweeps else None
        seed_threshold = int(seed_best.point["threshold"]) if seed_best else 1
        seed_process = miner.mine(events, name="HarnessSeed", min_edge_cases=seed_threshold).process
        baseline_rules = rule_liveness(
            events, seed_process, min_support=min_support, max_rules=max_rules
        ).separating

    return Admission(
        weight=weight,
        report=report,
        emptying=emptying_margin(report),
        rules=live,
        threshold=threshold,
        baseline_rules=baseline_rules,
    )


class HarnessProposal(BaseModel):
    """A proposed harness weight and the reasoning behind it."""

    coverage_weight: float = Field(
        description="Weight on replay coverage in the objective's harmonic mean, in (0, 1)."
    )
    evidence: str = Field(
        description="What in the measurements led to this weight. The auditable artifact."
    )


class HarnessProposer(MethodAgent):
    """An agent that proposes the harness it will then be graded by.

    One `@ai_method`, and the gate is wired as its post-conditions so a pathological
    proposal is rejected and re-asked with the detector's own report as the feedback. That
    placement is the difference between a gate and a convention: a manual check after the
    call is a check the loop can forget, while a post-condition cannot be skipped and its
    message reaches the model that has to fix it.

    Constructed with the log so the gate can compose the real objective, which means the
    post-conditions read `self` rather than a call argument. That is safe *for a validator*
    and would not be for a gradient target: `collect_nodes` scans call arguments, so state on
    `self` is invisible to the optimizer. The learnable weight therefore arrives as an
    argument and the fixed log arrives on `self`, which is the split the two mechanisms
    require.
    """

    name = "harness-proposer"

    def __init__(
        self,
        events: pl.DataFrame,
        *,
        sample_cases: int | None = 400,
        baseline_threshold: int = 25,
        window: int | None = None,
        resolution: int = DEFAULT_RESOLUTION,
    ) -> None:
        self.events = events
        self.sample_cases = sample_cases
        self.baseline_threshold = baseline_threshold
        self.window = window
        self.resolution = resolution
        self._baseline_rules: int | None = None
        self.rejected: list[Admission] = []
        """Every proposal the gate turned away, in order. The evidence the gate has teeth.

        Kept because a loop that silently re-asked and then succeeded looks, from the
        outside, exactly like a loop whose gate never fired.
        """

    def gate(self, weight: float) -> Admission:
        """Judge one candidate weight, reusing the seed measurement across calls."""
        verdict = admit(
            self.events,
            weight,
            sample_cases=self.sample_cases,
            baseline_threshold=self.baseline_threshold,
            window=self.window,
            resolution=self.resolution,
            baseline_rules=self._baseline_rules,
        )
        self._baseline_rules = verdict.baseline_rules
        return verdict

    def admits(self, response: HarnessProposal) -> None:
        """Post-condition: the detectors must admit the proposed weight.

        Raising is how a post-condition fails; `ai_thread` catches any exception from a
        validator and reports its text to the model as a validation failure. That is also
        the trap this method is written around: an unexpected exception is indistinguishable
        from a rejection and would burn every retry on a bug. So the gate call is wrapped
        and an internal failure is re-raised as a message that says it is internal, rather
        than being allowed to masquerade as a verdict about the proposal.

        The parameter is named `response` rather than `proposal` on purpose. A
        post-condition whose first parameter shares a name with an `ai_function` parameter
        raises `TypeError: got multiple values for argument`, which is then swallowed as a
        validation failure — a silent bug wearing a verdict's clothes.
        """
        try:
            verdict = self.gate(response.coverage_weight)
        except Exception as error:  # noqa: BLE001 — see the docstring: a bug must not read as a verdict
            raise AssertionError(
                f"the harness gate could not be evaluated for coverage_weight="
                f"{response.coverage_weight!r}, which is a fault in the gate rather than a "
                f"verdict about your proposal: {type(error).__name__}: {error}"
            ) from error
        if not verdict.ok:
            self.rejected.append(verdict)
            raise AssertionError(
                f"{verdict.report_text()}\n\nPropose a different coverage_weight. Weighting "
                "coverage toward zero removes the only term that punishes an empty model, "
                "and the emptiest model then wins outright."
            )

    @ai_method(
        HarnessProposal,
        description="Propose the objective's component weight, judged by the detectors",
        max_attempts=4,
    )
    def propose(self, coverage_weight: float, evidence: str) -> HarnessProposal:
        """Propose the component weight the mining objective should use.

        The objective balances two things about a mined process model. **Replay coverage** is
        the share of complete real cases the model can replay end to end. **Selectivity** is
        how small a fraction of the log's distinct handoffs the model needed. They are
        combined as a weighted harmonic mean, and `coverage_weight` is the weight on
        coverage: 0.5 weighs them equally, higher favours coverage, lower favours
        selectivity.

        The current weight is {coverage_weight}.

        What the last rounds measured:
        {evidence}

        Two things are checked, and failing either sends this back to you with the reason:

        - The objective at your weight must not be pathological. It is swept over the whole
          threshold range before any training round runs, and a weight whose objective is
          maximised by an empty model is rejected. Weighting coverage toward zero is the
          way to cause this: coverage is the only term that punishes an empty model, so
          removing it makes the smallest model the winner at any threshold.
        - The model your weight selects must still admit compliance rules that can fire.
          Precedence rules derived from the log are attached to the mined model and a
          reachability sweep asks whether any reachable state could break them. A rule no
          state can break is decoration and the model-checker reports green about it, so a
          weight that turns a live rule into decoration is rejected even when its score
          looks fine.

        Report the weight in `coverage_weight` and, in `evidence`, what in the measurements
        above led you there. Return via `final_answer`.
        """


@dataclass
class Round:
    """One harness proposal and everything measured about it."""

    index: int
    weight: float
    quality: float
    threshold: int
    emptying: float
    rule_share: float
    admitted: bool
    refusals: tuple[str, ...] = ()
    rejected_before: int = 0
    """Proposals the gate turned away before this one was admitted. Zero on a clean round."""


@dataclass
class HarnessTraining:
    """The whole harness loop."""

    rounds: list[Round] = field(default_factory=list)
    seed_quality: float = 0.0
    """Quality of the seed harness, which is `Attempt.score` exactly. The honest baseline."""

    final_weight: float = SEED_WEIGHT
    rejections: list[str] = field(default_factory=list)

    @property
    def best(self) -> Round | None:
        admitted = [r for r in self.rounds if r.admitted]
        return max(admitted, key=lambda r: r.quality) if admitted else None

    @property
    def beat_seed(self) -> bool:
        """Did any admitted proposal beat the seed? A negative answer is the finding."""
        best = self.best
        return best is not None and best.quality > self.seed_quality

    def summary(self) -> str:
        lines = [
            f"seed harness (coverage_weight={SEED_WEIGHT:g}, exactly Attempt.score): "
            f"quality {self.seed_quality:.4f}",
            f"{'round':>5} {'weight':>7} {'quality':>8} {'thr':>5} {'emptying':>9} "
            f"{'rules':>7} {'gate':>6} {'reasked':>8}",
        ]
        for entry in self.rounds:
            lines.append(
                f"{entry.index:>5} {entry.weight:>7.4f} {entry.quality:>8.4f} "
                f"{entry.threshold:>5} {entry.emptying:>9.4f} {entry.rule_share:>7.4f} "
                f"{'admit' if entry.admitted else 'REJECT':>6} {entry.rejected_before:>8}"
            )
        best = self.best
        if best is None:
            lines.append("no proposal was admitted, so the seed harness stands")
        elif self.beat_seed:
            lines.append(
                f"best admitted: weight {best.weight:.4f} at quality {best.quality:.4f}, "
                f"which is {best.quality - self.seed_quality:+.4f} against the seed"
            )
        else:
            lines.append(
                f"best admitted: weight {best.weight:.4f} at quality {best.quality:.4f}, "
                f"which does NOT beat the seed's {self.seed_quality:.4f}. The mechanism ran "
                "and did not win; that is the finding, not a reason to change the baseline."
            )
        if self.rejections:
            lines.append(f"the gate rejected {len(self.rejections)} proposal(s):")
            lines.extend(f"  {reason.splitlines()[0]}" for reason in self.rejections)
        return "\n".join(lines)


def evidence_for(verdict: Admission, seed_quality: float) -> str:
    """What the proposer is shown about the last round.

    States the quality the search is scored on, every round, unconditionally. That is not
    stylistic: an optimizer cannot climb a hill it is not told the height of, and a message
    that names the height only sometimes leaves the silent rounds steered by something else.
    A loop that reported coverage while selecting on a harmonic mean walked the agent's score
    from 0.804 down to 0.706 over four rounds while the number it was shown improved, which is
    what `probe_feedback` exists to prevent.
    """
    head = (
        f"coverage_weight={verdict.weight:g} scored quality {verdict.quality:.4f} "
        f"(the seed weight of {SEED_WEIGHT:g} scores {seed_quality:.4f}). It selected "
        f"threshold {verdict.threshold}."
    )
    detail = (
        f" Of the swept pairs where the model shrinks, {verdict.emptying.separating} of "
        f"{verdict.emptying.observations} cost score, so the objective does prefer a fuller "
        f"model there. {verdict.rules.separating} of {verdict.rules.observations} derived "
        "compliance rules can still fire on the model this weight selects."
    )
    if not verdict.ok:
        return head + " The gate REJECTED it:\n" + verdict.report_text()
    return head + detail


def substitute_score(graph: Any, name: str, quality: float) -> int:
    """Replace the backward model's opinion of `name` with the measured quality.

    `optimizer.step` looks like the whole mechanism, and for a *text* parameter it is. For a
    numeric one it is not, and the reason is a detail of where `GradFeedback.score` comes
    from: `_distribute` builds it as `GradFeedback(text=fb.feedback, score=fb.score)` where
    `fb` is the **backward model's** structured output, and `Feedback.score` is documented as
    the model "rating how well this input's VALUE actually served the agent's output".

    So `step` alone would drive the trust-region search on a language model's impression of a
    number's usefulness, read off a conversation trace. That is a plausible quantity standing
    in for a measured one, in a loop that cannot tell them apart because both are floats in
    `[0, 1]` and the search converges either way. It would look like it was working.

    Calling `store.consolidate` a second time with the real score does not fix that. Verified:
    two consolidations in one round record two observations, the first at the LLM's score, and
    `_numeric_update` reads that history to decide its next step. The search's own memory
    would carry a fabricated row per round.

    So the routing is kept and the number is replaced. `backward` decides *which* parameter
    this round's feedback belongs to, which is a judgment about attribution and is what a
    language model is for. The score is then overwritten with `quality`, which is a
    measurement, before `consolidate` folds it in. One observation per round, and its value
    is the one the detectors produced.

    Returns the number of gradients whose score was replaced, so a caller can assert the
    substitution happened rather than assume it. Zero means the graph carried no gradient for
    this parameter, which is a silent no-op that would leave the value at seed while the loop
    reported rounds, so `learn` refuses on it.
    """
    from ai_functions.optimizer._graph import topological_sort

    replaced = 0
    for node in topological_sort(graph):
        for parameter in node.parameters:
            if parameter.name != name:
                continue
            for index, gradient in enumerate(parameter.gradients):
                parameter.gradients[index] = GradFeedback(text=gradient.text, score=quality)
                replaced += 1
    return replaced


async def learn(
    optimizer: Any,
    traced: Any,
    store: TursoMemoryBackend,
    feedback: str,
    *,
    quality: float,
    name: str = "coverage_weight",
) -> int:
    """Backpropagate, replace the LLM's score with the measurement, then consolidate.

    `optimizer.step` decomposed into its three parts so the score channel carries a measured
    quantity. See `substitute_score` for why that decomposition is necessary rather than
    fussy. Raises rather than returning quietly when no gradient reached the parameter,
    because a training loop that reports rounds while the value stays at seed is the failure
    mode this whole project is about.
    """
    from ai_functions.optimizer import build_graph_from_result

    graph = await build_graph_from_result(traced, [store])
    await asyncio.to_thread(optimizer.backward, graph, feedback)
    replaced = substitute_score(graph, name, quality)
    if not replaced:
        raise RuntimeError(
            f"no gradient reached {name!r}, so this round would have consolidated nothing and "
            "the parameter would silently stay at its seed. Check that the recalled "
            "ParameterView was passed as a call argument and not interpolated into a string."
        )
    await asyncio.to_thread(optimizer.consolidate, graph)
    return replaced


async def train(
    events: pl.DataFrame,
    db_path: Path,
    *,
    rounds: int = 3,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
    window: int | None = None,
    resolution: int = DEFAULT_RESOLUTION,
    memory: TursoMemoryBackend | None = None,
) -> HarnessTraining:
    """Let the agent propose the harness, gate every proposal, and learn from the score.

    The loop is: recall the weight, ask the agent to propose one with the gate as its
    post-condition, measure the admitted proposal, and consolidate a `GradFeedback` whose
    `score` is that measurement. `learn` rather than `optimizer.step` does the last part, and
    `substitute_score` says why: `step` would drive the search on the backward model's own
    rating of the number instead of on the gate's.

    Three mechanics are load-bearing, and getting any of them wrong is silent:

    `RuntimeHarness` wraps the loop because `trace` records against the running runtime;
    without it the graph carries no parameter, `learn` raises rather than reporting a round
    that learned nothing, and the failure is loud instead of silent.

    The weight is recalled **per round** and passed as a **call argument**. Per round
    because a `ParameterView` emits one recall event, so a view reused across rounds carries
    a gradient target on the first traced call and none afterwards. As an argument because
    `collect_nodes` scans call arguments, so a value on `self` is structurally invisible to
    the optimizer.

    And the view is passed as the handle, never interpolated. An f-string of a view renders
    the same prompt and silently drops the dataflow edge.

    Args:
        events: The log to train on. Never modified.
        db_path: Database file for the parameter. Ignored when `memory` is given.
        rounds: Rounds to run. The last is measured but not learned from, since there is no
            later round to show the improvement in.
        sample_cases: Cases in the CSV the miner would see. Must match the mining loop or
            the gated objective is not the one that would be optimised.
        baseline_threshold: Passed to `grade`, same reason.
        window: Highest threshold the sweep may reach. Not delegable; see the module
            docstring.
        resolution: Samples per axis for the gate's sweep. Not delegable either.
        memory: An existing backend to train against. Ownership stays with the caller.
    """
    from ai_functions import TextGradOptimizer
    from ai_functions.testing import RuntimeHarness

    owned = memory is None
    store = memory or TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=db_path)
    optimizer = TextGradOptimizer()
    proposer = HarnessProposer(
        events,
        sample_cases=sample_cases,
        baseline_threshold=baseline_threshold,
        window=window,
        resolution=resolution,
    )
    compiled = proposer.compiled("propose", post_conditions=[proposer.admits])

    seed = proposer.gate(SEED_WEIGHT)
    training = HarnessTraining(seed_quality=seed.quality)
    evidence = evidence_for(seed, seed.quality)

    try:
        async with RuntimeHarness():
            for index in range(rounds):
                before = len(proposer.rejected)
                weight = await store.recall("coverage_weight")
                traced: Any = await compiled.trace(weight, evidence)
                proposal: HarnessProposal = traced.value

                verdict = proposer.gate(proposal.coverage_weight)
                training.rounds.append(
                    Round(
                        index=index,
                        weight=proposal.coverage_weight,
                        quality=verdict.quality,
                        threshold=verdict.threshold,
                        emptying=(
                            verdict.emptying.separating / verdict.emptying.observations
                            if verdict.emptying.observations
                            else 0.0
                        ),
                        rule_share=(
                            verdict.rules.separating / verdict.rules.observations
                            if verdict.rules.observations
                            else 0.0
                        ),
                        admitted=verdict.ok,
                        refusals=verdict.refusals,
                        rejected_before=len(proposer.rejected) - before,
                    )
                )
                training.rejections.extend(a.report_text() for a in proposer.rejected[before:])
                evidence = evidence_for(verdict, training.seed_quality)

                if index == rounds - 1:
                    break
                await learn(optimizer, traced, store, evidence, quality=verdict.quality)

            training.final_weight = store.numeric_value("coverage_weight")
    finally:
        if owned:
            store.close()

    return training
