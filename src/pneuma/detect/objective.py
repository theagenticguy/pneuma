"""Probe a scoring function for degenerate optima before a training loop trusts it.

This project's own case study documents four consecutive ways the miner's objective was
wrong, and the conclusion it reached was that "the scoring function is the artifact that
needs adversarial review". A checklist is not review. This is the mechanical form: hand
the objective and a declared feasible domain to `probe`, and it sweeps, refines, and
refuses rather than letting the loop discover the pathology by climbing it.

Nothing here imports from pneuma. The consumer supplies a callable, a box, and the
degenerate inputs it wants ruled out; every pneuma-specific fact lives at the call site.
That is deliberate: any system with a learned objective needs this, and process mining is
incidental to all of it.

## The two spaces, and why conflating them makes the prober useless

The single most important design point, and the one that took a measurement to find.

An objective has *metric* inputs (coverage, selectivity) and the loop has a *decision*
variable (the support threshold) that it actually controls. The metrics are coupled
functions of the decision; the decision is what the optimizer moves.

Check "is the maximum in the interior" in metric space and the check is worthless. Any
sane F-score is maximised at the ideal corner — perfect coverage using no edges — and
that corner is a boundary. Measured on this project's fixed objective: `coverage=1.0,
edge_share=0.0` scores 1.0, the grid maximum, on the face of the box. The historical
coverage-only objective has its maximum at a corner too. A boundary-max check in metric
space fires on the good objective and the broken one alike.

In decision space the same check is exactly right, and it caught the real failure.
Sweeping the threshold and composing the measurement, the fixed objective peaks at 40
with 0.8613 — interior. Swept over the window the live agent chose for itself, 1 to 24,
it peaks at 24 with 0.8448: on the window's own upper edge. That is failure four,
"optimised correctly inside a window that was too narrow", detected mechanically.

So `Space.METRIC` and `Space.DECISION` run different checks, and the mode is a required
argument rather than a default, because picking it wrong is the failure this paragraph
exists to prevent.

## Out-of-domain semantics: not clamp, not refuse, but "must not reward"

The bug that shipped was an input escaping its range: `edge_share` was
`edges the agent returned / handoffs in the log` with nothing constraining the numerator
to the denominator's population, so it exceeded 1, selectivity went negative, and the
harmonic mean became a rational function with a pole. The objective's *shape* was sound.
Its *domain* was not. A prober checking only the declared range would have passed it.

There are three candidate semantics and only one of them is right.

**Clamp** is wrong as the fix, and it is what is easy to reach for. Clamping
`edge_share=1.86` to `1.0` scores 0.0, which is exactly what honest memorisation scores.
The harness then mis-grades in silence: a model that returned 86 handoffs no case ever
walked is recorded as indistinguishable from one that kept every real handoff. The
clamp does not repair the measurement, it hides that the measurement was wrong. Worth
keeping as depth — this project does clamp, and should — but never as the answer.

**Refuse** is wrong as a general rule, because it makes the objective the wrong place to
enforce a fact about measurement. `score_edges` bounds the share by intersecting the
returned edges with the real ones, which is a repair at the boundary where the input is
computed. That is where an escape should be impossible, not where it should raise.

**"Must not reward" is what is mechanically checkable, and it is sound.** Sweep the
escape region and require that no out-of-domain input outscores the best in-domain
input. The argument does not depend on believing the escape is unreachable: a training
loop is a search for the argmax, so a reward outside the declared domain is a reward,
and reachability is a claim about code that this prober cannot verify. The historical
objective fails this outright — at `coverage=0.75, edge_share=2.0` it scores 6.0 against
an honest maximum of 1.0, and at `coverage=1.0, edge_share=11.0` it scores 2.2222.

`Domain.bounded_by` is how a caller states that a bound is established by code rather
than intended. It downgrades an escape finding from refusal to warning and it demands
the name of the code that does the bounding, which is a reviewable claim in the call
site. It does not skip the check. Nothing here is skipped silently, because a prober
that under-samples and reports "looks fine" is this session's defect one level up.

`bounded_by` is also this module's own weakest point, and the weakness is measured rather
than assumed. Declare it on *every* axis and the historical broken objective passes with
seven warnings and no refusal, because every pathology it has lives outside the declared
box. That is the intended semantics — the caller has asserted the bounds are enforced
elsewhere, and the prober cannot verify a claim about another file — but it means a false
`bounded_by` defeats the refusal. So `trust_declared_bounds=False` re-runs the same probe
with every claim ignored, which is the view a reviewer should look at at least once, and
it is what `Probe.report` points at when warnings are all that is left.

## Degenerate inputs are computed, not declared, because a declared list is the same defect

`Degenerate` used to be the only way the prober learned what a bad answer looked like:
the caller wrote down the inputs it wanted ruled out and the prober checked none of them
won. That contract has the shape of the failure this module exists to catch. A
hand-written list of bad answers is a harness artifact written by the same hand as the
scoring formula, and it is wrong in the same direction.

Measured, on a second fixture: the prober passed a genuinely degenerate objective with
zero findings. Through the real `grade`/`score_edges` path, whole-trace coverage on a log
of AI coding-agent transcripts is 0.0227 at *every* mining threshold from 1 to 44, so the
score reduces to a monotone function of selectivity alone and the winner is a two-state
model replaying two cases out of 88. Every check here passed: the argmax was interior,
nothing was non-finite, there was no pole, and the function was bounded. Adding one
`Degenerate` naming the smallest surviving model turned the same probe into a refusal.
On the permit log the smallest surviving model scores 0.1496 against an optimum of
0.8606, so the missing declaration was invisible on fixture one *by construction*.

`Structure` is the replacement, and the shift it encodes is the point: a caller supplies
the *shape of the search space* rather than a list of guesses. "The smallest thing that
still counts as an answer" is a property of the space, not a value someone remembers to
write down. One callable, `size`, says how much answer a point represents — handoffs
kept, states in the model, features selected — and every degenerate input follows
mechanically: the emptiest viable point, the fullest, the empty one, the box corners.

Two checks are derived from it, and the second is the stronger one.

`degenerate-optimum` runs the enumerated points through the same test a declared one got.
It fires the moment the emptiest viable answer ties the grid maximum.

`emptying-is-free` is the general statement, and it is provable rather than sampled: walk
every grid-adjacent pair where `size` falls, and require the score to fall across at least
one of them. An objective where shrinking the answer never costs score has its optimum at
whatever the space admits last, whatever that happens to be. The strict form is deliberate
— the check fires only when the score *never* falls — because a fraction-of-pairs threshold
would be a number fitted to whichever fixture was in hand.

Where it is stronger than the point test, honestly. Mostly the two coincide, and they have
to: if emptying never costs score then the score is non-decreasing toward emptier, so the
emptiest viable grid point holds the grid maximum and the point test fires as well. They
part when the grid maximum is held by a point that is not a viable answer at all — then no
viable point can tie it, the point test is quiet, and only the walk sees that shrinking a
real answer is free. That is an objective rewarding the return of nothing, which is not
exotic.

The other half of the general form's value is not about which check fires. It is what the
finding *says*: the score is monotone in emptiness across every pair walked, rather than one
coordinate that happened to win. A caller who fixes the winning point without fixing the
monotonicity has fixed nothing, and the point test alone would then go quiet.

## Every one of these is a decision-space check, and that was corrected by measurement

The same argument the boundary-max check rests on, applied one layer out. Nothing new.

Metric axes are varied freely and independently, so "hold everything else and shrink this
term" is always available on the grid and is usually the right answer — the ideal corner is
perfect coverage using no edges, which is exactly an empty answer scoring the maximum. In
decision space the axes are what the optimizer moves, the metrics are coupled functions of
them, and "shrink the answer" is a real move with a real cost. Measured: `emptying-is-free`
does not fire on the permit log's composed objective, where the score falls from 0.8184 at
the argmax to 0.7680 one grid step toward emptier models, and does fire on the transcript
log's, where every step from the argmax to a single-edge model scores 0.0444.

An earlier version of this module applied that discipline to `emptying-is-free` and to the
box corners, and *not* to the size-derived points, on the reasoning that "the emptiest point
that is still an answer" is meaningful on any axes. That was wrong, and a live adversarial
run is what found it. On the current objective's metric grid, 21 points tie for the smallest
non-zero `edge_share`, scoring 0.0 to 0.9744, so which one was picked came down to iteration
order. Tiebreaking on score, which is the fix for the order dependence, makes the underlying
problem worse rather than better: with free axes the best point at any fixed size holds every
other term at its ideal, so as the grid refines it converges on the ideal corner, and a sound
objective would eventually be refused for having a good optimum. So the whole enumeration is
decision-space only and metric space says so in a note.

That correction is the strongest thing the LLM half produced this session, and it is worth
recording precisely because of what it says about the LLM half's limits. Five adversaries
with distinct mandates and a three-judge panel all upheld the metric-space empty-answer
candidates unanimously, arguing correctly that they were degenerate. They found the flaw and
then walked into it, because "an empty answer scoring the maximum" reads as a defect right up
until you notice metric axes make it the definition of a good objective. Only the space
discipline separates the two, and the space discipline is deterministic.

## The adversarial half, and why a search is not a declaration

Enumeration only finds degenerates that follow from the declared structure. `search` is
the seam for the ones nobody enumerated: a callable handed a `Brief` — the objective, the
axes, the grid it was swept on, the ceiling, and the structure — that returns `Degenerate`
candidates. `pneuma.detect.adversary` implements it with a fan-out of LLM adversaries and
a judge panel; nothing in this module imports it, and `probe` works with `search=None`.

The division of labour is deliberate. Whatever a searcher claims, the prober re-evaluates
the candidate and only records a finding if it actually reaches the ceiling, so the
arithmetic half of adjudication happens here, in code that cannot be argued with. The
searcher owns the half that is a judgment call: whether the input is *worthless*.

## Naming the cause, not only the symptom, and why it is the same check `vacuity` runs

Every check above is downstream of one thing. `emptying-is-free` and `degenerate-optimum`
both say *a degenerate input wins*; neither says why. Measured on the transcript log, the
why is that one term of the metric has no discriminating power on that dataset: whole-trace
replay coverage through the real `grade` path reads 0.0227 at every threshold from 1 to 44.
With coverage held constant the score is a monotone function of emptiness by algebra, so the
winner is whatever the space admits last. A caller told only that a point wins can fix the
point; a caller told the coverage term never moves knows the fix is the measurement.

`Component` and `_check_components` are that, and they are deliberately the *same primitive*
`vacuity` reports a rule through. A rule catching zero reachable states cannot tell a
compliant run from a violation. A term whose value never moves across the swept space cannot
tell a good answer from a bad one. Both are checks that pass without ever having been in a
position to fail, both need the verdict to be three-valued so an abandoned measurement is not
a pass, and `discrimination.py` is the twenty lines they have in common. That module's
docstring also records where the unification stops, which is a real limit rather than a gap.

## What this cannot do

`probe_feedback` checks that the feedback text states the quantity selection uses, and
that it states it on every round rather than some. It cannot check that the prose's
*advice* points uphill. See that function for the reasoning.

`components` are declared, not decomposed. Splitting an arbitrary callable into terms needs
its source and an algebra over it, so a caller lists what it wants measured. The declaration
is auditable in the way a hand-written `Degenerate` list was not — a term is evaluated on the
same points the objective is, so a term declared wrong reports its own variance rather than
the score's — but a term nobody declares is a term nobody measured, and the report says so.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import product

from .discrimination import Discrimination

Point = dict[str, float]
"""One input to the objective, keyed by domain name."""

Objective = Callable[..., float]
"""Called as `objective(**point)`. May raise; raising inside the declared box is a finding."""


class Space(Enum):
    """Which space a sweep is over. Required, never defaulted: see the module docstring."""

    METRIC = "metric"
    """Axes are the objective's numeric inputs, varied freely and independently."""

    DECISION = "decision"
    """Axes are what the optimizer controls; the objective composes in the measurement."""


class Severity(Enum):
    REFUSE = "refuse"
    WARN = "warn"


class ObjectiveRefused(Exception):
    """Raised instead of starting a training loop against a pathological objective."""


@dataclass(frozen=True)
class Domain:
    """The declared feasible range of one input.

    `feasible` is the hard limit, distinct from `low`/`high` which are the window being
    swept. In decision space they differ and the difference is the point: a threshold's
    window may be 1 to 24 while anything at or above 1 is feasible, and the prober earns
    its keep by looking at 25 and up when the maximum lands on 24.
    """

    name: str
    low: float
    high: float

    integral: bool = False
    """Snap grid values to whole numbers and dedupe. Support thresholds are integers."""

    bounded_by: str | None = None
    """Names the code that establishes this bound, if any. See the module docstring."""

    feasible: tuple[float, float] | None = None
    """Hard limit outside which a value is meaningless. Defaults to `(low, high)` widened
    by `reach` when the prober builds escape boxes."""

    @property
    def span(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class Degenerate:
    """An input the objective must not be maximised by, and the reason it is degenerate.

    The label is not decoration. "keep everything" is what threshold 1 means, and the
    first live run reported 98.6% coverage while producing it, so the finding has to say
    which degenerate input won rather than only that one did.

    Still the *unit* the checks work in, but no longer the *contract*: a caller declares a
    `Structure` and these are enumerated from it, or a `search` proposes them. Passing a
    hand-written list is still allowed and is still checked, because a caller who does know
    a specific bad answer should be able to say so. It is no longer what the prober relies
    on, which is the whole change; see the module docstring.
    """

    label: str
    point: Mapping[str, float]

    found_by: str = "declared"
    """Where this came from: `declared`, `enumerated`, or a searcher's name.

    In the report rather than derivable, because "the caller wrote this down" and "the
    prober computed it from the declared space" are different strengths of evidence and a
    reviewer has to be able to tell which one fired.
    """

    worthless_because: str = ""
    """Why this input is worthless. Empty for enumerated points, where the label says it.

    A searcher's adjudication lands here, so the finding it produces carries the argument
    rather than only the coordinates.
    """


Size = Callable[..., float]
"""Called as `size(**point)`. How much answer a point represents; smaller is emptier."""


@dataclass(frozen=True)
class Structure:
    """The shape of the answer space, from which degenerate inputs follow mechanically.

    This is the seam that replaced the declared list, and the reason it is a *callable*
    rather than points is in the module docstring: "the smallest thing that still counts as
    an answer" is a property of the space, and a caller who could name the point already
    knows the answer the prober is supposed to find.

    `size` is the only thing required, and it is deliberately loose about units. Handoffs
    kept, states in a model, features selected, rules retained: anything monotone in "how
    much of an answer is this". The prober never compares a size to a constant, only to
    other sizes, so no scale assumption is made on the caller's behalf.

    `viable` is separate from `size > 0` because an empty answer and an *unrepresentable*
    one are different, and conflating them hides the finding. On the permit log the
    threshold above maximum support keeps zero handoffs and compiles nothing, so it scores
    zero and cannot win; the interesting point is the last threshold that still compiles a
    model. Without `viable` the emptiest enumerated point is the one that scores zero and
    the check passes for the wrong reason.
    """

    size: Size
    """How much answer this point represents. Smaller is emptier."""

    viable: Callable[..., bool] | None = None
    """Whether a point represents an answer at all. Defaults to `size(**point) > 0`."""

    units: str = "size"
    """What `size` counts, for the report. `"handoffs kept"` reads better than `"size"`."""

    def measure(self, point: Point) -> float | None:
        """`size` at a point, or None if it raised. Raising is the caller's business."""
        try:
            return float(self.size(**point))
        except Exception:  # noqa: BLE001 — a structure that cannot measure a point is not a finding
            return None

    def is_viable(self, point: Point) -> bool:
        if self.viable is not None:
            try:
                return bool(self.viable(**point))
            except Exception:  # noqa: BLE001
                return False
        measured = self.measure(point)
        return measured is not None and measured > 0.0


Term = Callable[..., float]
"""Called as `term(**point)`. One named component of the objective's own arithmetic."""


@dataclass(frozen=True)
class Component:
    """One term of the objective, so the prober can say which term stopped discriminating.

    The reason this exists is a measured gap between a *cause* and a *symptom*. On the
    transcript log the prober already refuses, with `emptying-is-free` and two enumerated
    `degenerate-optimum` findings, and every one of those is downstream: they say a
    degenerate input wins. What none of them says is *why*, and the why is that one term of
    the metric has no discriminating power on this dataset. Whole-trace replay coverage
    through the real `grade` path is 0.0227 at every threshold from 1 to 44, so the score
    reduces to a monotone function of emptiness and the winner is whatever the space admits
    last. A caller told only "a degenerate input wins" can fix the winning point; a caller
    told "the coverage term never moves" knows the fix is the measurement.

    This is the same question `vacuity` asks about a rule, and `discrimination.py` is where
    the two are one primitive. A rule catching zero reachable states cannot tell a compliant
    run from a violation; a term whose value never moves across the swept space cannot tell a
    good answer from a bad one. Both are checks that pass without testing anything.

    Declared rather than inferred, and that is a real limit worth naming. The prober cannot
    decompose an arbitrary callable into terms — that would need the source and an algebra —
    so a caller lists the ones it wants measured. The declaration is *auditable* in the way a
    hand-written `Degenerate` list was not: a term is a function of the same point the
    objective takes, so the prober evaluates it rather than believing anything about it, and
    a term declared wrong reports its own variance rather than the score's.

    Attributes:
        name: What this term is, as the report will name it.
        term: Called as `term(**point)`. Returns the term's value at that point.
        floor: Variation at or below this counts as no variation. Absolute, because a term
            is the caller's own quantity on the caller's own scale, and the prober guessing
            a relative floor would be substituting its opinion for a declared one.
    """

    name: str
    term: Term
    floor: float = 0.0

    def measure(self, point: Point) -> float | None:
        """The term at a point, or None if it raised. Raising is not a finding here: a
        term that cannot be evaluated somewhere is measured over where it can be."""
        try:
            value = float(self.term(**point))
        except Exception:  # noqa: BLE001
            return None
        return value if math.isfinite(value) else None


@dataclass(frozen=True)
class Sample:
    point: Point
    value: float | None = None
    error: str | None = None

    @property
    def finite(self) -> bool:
        return self.value is not None and math.isfinite(self.value)


@dataclass(frozen=True)
class Sweep:
    """One swept box, carrying the resolution it was swept at.

    The resolution travels with the samples so a report can never claim a clean sweep
    without saying how densely it looked.

    Grid samples and refinement samples are kept in separate fields. Mixing them would
    let a refinement point taken *outside* the grid become the box's reported `best`,
    which would quietly move the ceiling that every escape comparison is made against.
    """

    label: str
    space: Space
    axes: tuple[Domain, ...]
    resolution: dict[str, int]
    samples: tuple[Sample, ...]
    refined: tuple[Sample, ...] = ()

    @property
    def refinements(self) -> int:
        return len(self.refined)

    @property
    def finite(self) -> tuple[Sample, ...]:
        return tuple(s for s in self.samples if s.finite)

    @property
    def raised(self) -> tuple[Sample, ...]:
        return tuple(s for s in self.samples if s.error is not None)

    @property
    def nonfinite(self) -> tuple[Sample, ...]:
        return tuple(s for s in self.samples if s.error is None and not s.finite)

    @property
    def best(self) -> Sample | None:
        finite = self.finite
        return max(finite, key=lambda s: s.value or 0.0) if finite else None

    @property
    def peak(self) -> float:
        """The largest magnitude seen. What unboundedness is measured against."""
        return max((abs(s.value or 0.0) for s in self.finite), default=0.0)

    @property
    def evaluations(self) -> int:
        return len(self.samples) + len(self.refined)


@dataclass(frozen=True)
class Brief:
    """Everything a searcher is shown. The prober's own view, handed over unedited.

    A searcher that saw less than the prober would be searching a different problem, so
    this carries the objective itself, the swept grid, and the ceiling every finding is
    measured against. `source` is the scoring code when the caller can supply it: an
    adversary reading the arithmetic finds things sampling does not, and an adversary that
    cannot read it is guessing at the shape from 21 points.
    """

    objective: Objective
    axes: tuple[Domain, ...]
    space: Space
    ceiling: float
    """The in-domain grid maximum. A candidate must reach this to be a finding."""

    best_point: Point
    structure: Structure | None = None
    samples: tuple[Sample, ...] = ()
    source: str | None = None
    """The scoring function's source, if the caller supplied it."""

    def score(self, point: Mapping[str, float]) -> Sample:
        """Evaluate the objective. What makes a searcher a search rather than a guess."""
        return _evaluate(self.objective, dict(point))


Search = Callable[[Brief], Sequence[Degenerate]]
"""Called as `search(brief)`; returns candidate degenerate inputs, judged by the caller.

Whatever it claims, `probe` re-evaluates every candidate and only records a finding if the
arithmetic agrees. See `pneuma.detect.adversary` for the LLM implementation.
"""


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    detail: str
    point: Point | None = None
    value: float | None = None

    downgraded_by: str | None = None
    """Set when a `bounded_by` claim turned this from a refusal into a warning.

    A field rather than something a report re-derives by matching on its own prose: the
    count of downgraded findings decides what the summary says, and deriving it from text
    would be a check that silently stops firing the day the wording changes.
    """

    def __str__(self) -> str:
        where = f" at {_render(self.point)}" if self.point else ""
        return f"[{self.severity.value}] {self.check}: {self.detail}{where}"


@dataclass(frozen=True)
class Probe:
    """Everything the prober looked at and everything it found.

    `notes` is where checks that did *not* run say so. A prober whose report is silent
    about what it skipped is the same defect as an objective whose feedback is silent
    about the quantity being optimised.
    """

    findings: tuple[Finding, ...] = ()
    sweeps: tuple[Sweep, ...] = ()
    notes: tuple[str, ...] = ()

    discrimination: tuple[Discrimination, ...] = field(default_factory=tuple)
    """Per-component discrimination, in the shared primitive. Empty when none was declared.

    Carried alongside the findings rather than folded into them because an idle component is
    a *cause* and a finding is what it caused. A reader looking at a refusal wants both, and
    a reader looking at a pass wants this anyway: a term that discriminates by a hair today
    is the one to watch, and that is a number rather than a finding.
    """

    @property
    def idle_components(self) -> tuple[Discrimination, ...]:
        """Declared terms that never moved across the swept space. The named cause."""
        return tuple(d for d in self.discrimination if d.idle)

    @property
    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.REFUSE)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARN)

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def evaluations(self) -> int:
        return sum(s.evaluations for s in self.sweeps)

    def report(self) -> str:
        lines = [
            f"objective probe: {'PASS' if self.ok else 'REFUSE'} — "
            f"{len(self.refusals)} refusal(s), {len(self.warnings)} warning(s), "
            f"{self.evaluations} evaluations over {len(self.sweeps)} box(es)"
        ]
        for sweep in self.sweeps:
            grid = ", ".join(f"{name}x{count}" for name, count in sweep.resolution.items())
            refined = f", {sweep.refinements} refinements" if sweep.refinements else ""
            lines.append(f"  swept {sweep.label} [{sweep.space.value}]: {grid}{refined}")
        lines.extend(f"  {finding}" for finding in self.findings)
        lines.extend(f"  component {d}" for d in self.discrimination)
        lines.extend(f"  note: {note}" for note in self.notes)
        claimed = [f for f in self.warnings if f.downgraded_by]
        if self.ok and claimed:
            lines.append(
                f"  note: {len(claimed)} finding(s) were downgraded to warnings only because a "
                "bound was declared established elsewhere. Re-run with "
                "trust_declared_bounds=False to see this objective judged on its own arithmetic."
            )
        return "\n".join(lines)

    def raise_if_pathological(self, what: str = "objective") -> None:
        """Refuse to start. The whole reason this module exists."""
        if self.ok:
            return
        raise ObjectiveRefused(
            f"{what} is pathological, so no training loop will be started against it:\n"
            + self.report()
        )


DEFAULT_RESOLUTION = 21
"""Samples per axis. Odd so the midpoint of every axis is sampled exactly."""

DEFAULT_REACH = 2.0
"""How far past a declared bound an escape box reaches, in units of the declared span."""

DEFAULT_REFINE = 12
"""Bisection depth when chasing a sign flip or a growing maximum."""

DEFAULT_GROWTH = 2.0
"""Magnitude factor per refinement that counts as blowing up rather than curving."""

MAX_CORNERS = 64
"""Box corners enumerated before the enumeration stops and says so in the report.

A cap because corners are 2^axes and a six-axis decision space is 64 objective calls for
the corners alone. Reported rather than silent, and it does not touch the emptiest and
fullest points, which come from the whole grid.
"""

TOLERANCE = 1e-9


def _render(point: Mapping[str, float] | None) -> str:
    if not point:
        return "{}"
    return "{" + ", ".join(f"{k}={v:g}" for k, v in point.items()) + "}"


def _axis_values(domain: Domain, resolution: int) -> list[float]:
    if resolution < 2:
        raise ValueError(f"resolution must be at least 2, got {resolution}")
    if domain.span <= 0:
        return [domain.low]
    step = domain.span / (resolution - 1)
    values = [domain.low + step * i for i in range(resolution)]
    if domain.integral:
        # Dedupe preserving order rather than through a set: the grid's ordering is what
        # adjacency, and so pole detection, is computed from.
        seen: dict[float, None] = {}
        for value in values:
            seen.setdefault(float(round(value)), None)
        return list(seen)
    return values


def _evaluate(objective: Objective, point: Point) -> Sample:
    try:
        value = float(objective(**point))
    except Exception as error:  # noqa: BLE001 — a raising objective is a finding, not a crash
        return Sample(point=point, error=f"{type(error).__name__}: {error}")
    return Sample(point=point, value=value)


def _sweep_box(
    objective: Objective,
    axes: Sequence[Domain],
    *,
    label: str,
    space: Space,
    resolution: int,
) -> Sweep:
    grids = [_axis_values(axis, resolution) for axis in axes]
    samples = tuple(
        _evaluate(objective, {axis.name: value for axis, value in zip(axes, combo, strict=True)})
        for combo in product(*grids)
    )
    return Sweep(
        label=label,
        space=space,
        axes=tuple(axes),
        resolution={axis.name: len(grid) for axis, grid in zip(axes, grids, strict=True)},
        samples=samples,
    )


def _adjacent_pairs(axes: Sequence[Domain], grids: Sequence[Sequence[float]]) -> Iterable[
    tuple[Point, Point]
]:
    """Grid-adjacent point pairs along each axis. Where a pole shows up as a sign flip."""
    for index in range(len(axes)):
        others = [range(len(grid)) for grid in grids]
        others[index] = range(len(grids[index]) - 1)
        for combo in product(*others):
            low = dict(zip((a.name for a in axes), (grids[i][j] for i, j in enumerate(combo)),
                           strict=True))
            high = dict(low)
            high[axes[index].name] = grids[index][combo[index] + 1]
            yield low, high


def _chase_pole(
    objective: Objective,
    low: Point,
    high: Point,
    axis: str,
    *,
    refine: int,
    growth: float,
) -> tuple[list[Sample], bool]:
    """Bisect between a sign-flipped pair and report whether the magnitude blows up.

    A zero crossing and a pole both flip sign. They differ under refinement: across a
    crossing the midpoint's magnitude sits between its neighbours', while across a pole
    it exceeds both and keeps exceeding them. That difference is the whole detector, and
    it is why the pole in this project's history is distinguishable from the legitimate
    place where the fixed objective passes through zero.
    """
    samples: list[Sample] = []
    left, right = dict(low), dict(high)
    magnitude = max(abs(_finite_or_zero(objective, left)), abs(_finite_or_zero(objective, right)))
    grew = 0
    for _ in range(refine):
        middle = dict(left)
        middle[axis] = (left[axis] + right[axis]) / 2.0
        sample = _evaluate(objective, middle)
        samples.append(sample)
        if not sample.finite:
            return samples, True
        current = abs(sample.value or 0.0)
        if current > magnitude * growth:
            grew += 1
        magnitude = max(magnitude, current)
        # Walk toward whichever end still straddles the flip.
        if _sign(sample.value) == _sign_at(objective, left, default=_sign(sample.value)):
            left = middle
        else:
            right = middle
    return samples, grew >= max(2, refine // 4)


def _finite_or_zero(objective: Objective, point: Point) -> float:
    sample = _evaluate(objective, point)
    return sample.value if sample.finite else 0.0


def _sign_at(objective: Objective, point: Point, *, default: int) -> int:
    sample = _evaluate(objective, point)
    return _sign(sample.value) if sample.finite else default


def _sign(value: float | None) -> int:
    if value is None or value == 0.0:
        return 0
    return 1 if value > 0 else -1


def _refine_maximum(
    objective: Objective,
    sweep: Sweep,
    *,
    refine: int,
    growth: float,
) -> tuple[list[Sample], bool]:
    """Halve the spacing around the grid maximum and see whether the maximum keeps rising.

    Constant-free unboundedness: if refining the grid raises the peak by a factor every
    time, the supremum is not attained on the box and the objective is unbounded there.
    A prober that instead compared against a hardcoded "too big" number would be making
    the caller's scale assumption silently.
    """
    best = sweep.best
    if best is None:
        return [], False
    samples: list[Sample] = []
    peak = abs(best.value or 0.0)
    radius = {
        axis.name: axis.span / (sweep.resolution.get(axis.name, 2) - 1 or 1)
        for axis in sweep.axes
    }
    grew = 0
    for _ in range(refine):
        radius = {name: value / 2.0 for name, value in radius.items()}
        rose = False
        for axis in sweep.axes:
            for direction in (-1.0, 1.0):
                point = dict(best.point)
                point[axis.name] = best.point[axis.name] + direction * radius[axis.name]
                sample = _evaluate(objective, point)
                samples.append(sample)
                if sample.error is not None:
                    # Refinement can step just outside a strict objective's accepted range.
                    # Raising is not evidence of a growing supremum, and reporting it as
                    # unboundedness would be a detector inventing its own finding.
                    continue
                if not sample.finite:
                    return samples, True
                current = abs(sample.value or 0.0)
                if current > peak * growth:
                    peak, rose = current, True
                    best = sample
        if rose:
            grew += 1
    return samples, grew >= max(2, refine // 4)


def _escape_boxes(
    axes: Sequence[Domain], reach: float
) -> list[tuple[str, Domain, tuple[Domain, ...]]]:
    """One box per (axis, direction) just outside the declared range, clipped to feasible.

    The escaped axis is returned alongside the box rather than recovered from the label,
    so `bounded_by` is read off the axis that actually escaped.
    """
    boxes: list[tuple[str, Domain, tuple[Domain, ...]]] = []
    for index, axis in enumerate(axes):
        span = axis.span if axis.span > 0 else max(abs(axis.high), 1.0)
        limit_low, limit_high = axis.feasible or (-math.inf, math.inf)
        for label, low, high in (
            (
                f"{axis.name} above {axis.high:g}",
                axis.high,
                min(axis.high + reach * span, limit_high),
            ),
            (f"{axis.name} below {axis.low:g}", max(axis.low - reach * span, limit_low), axis.low),
        ):
            if not math.isfinite(low) or not math.isfinite(high) or high - low <= TOLERANCE:
                continue
            escaped = list(axes)
            escaped[index] = Domain(
                name=axis.name,
                low=low,
                high=high,
                integral=axis.integral,
                bounded_by=axis.bounded_by,
            )
            boxes.append((label, axis, tuple(escaped)))
    return boxes


def probe(
    objective: Objective,
    domains: Sequence[Domain],
    *,
    space: Space,
    structure: Structure | None = None,
    components: Sequence[Component] = (),
    degenerate: Sequence[Degenerate] = (),
    search: Search | None = None,
    source: str | None = None,
    resolution: int = DEFAULT_RESOLUTION,
    reach: float = DEFAULT_REACH,
    refine: int = DEFAULT_REFINE,
    growth: float = DEFAULT_GROWTH,
    trust_declared_bounds: bool = True,
) -> Probe:
    """Sweep an objective over its declared domain and just outside it, and report.

    Args:
        objective: Called as `objective(**point)` with one float per domain name.
        domains: The declared feasible box, one `Domain` per input.
        space: `Space.METRIC` or `Space.DECISION`. Required; see the module docstring for
            why defaulting it would make the boundary check meaningless.
        structure: The shape of the answer space. Degenerate inputs are enumerated from it
            and the emptying check is derived from it. Prefer this over `degenerate`: a
            declared list of bad answers is written by the same hand as the scoring formula
            and is wrong in the same direction, which is what this argument replaced.
        components: Named terms of the objective's own arithmetic, measured for whether they
            vary across the swept space at all. This is the check that names the *cause* an
            emptying or degenerate finding is the symptom of; see `Component`.
        degenerate: Inputs the objective must not be maximised by, each with a label. Still
            checked, and merged with whatever `structure` and `search` produce.
        search: Called with a `Brief` and returns candidate degenerate inputs. Every
            candidate is re-scored here, so a searcher's claim is never taken on trust. See
            `pneuma.detect.adversary`.
        source: The scoring function's source, passed through to a searcher. An adversary
            reading the arithmetic finds things sampling does not.
        resolution: Samples per axis. Recorded in the report, never assumed adequate.
        reach: How far past each declared bound to look, in units of the declared span.
        refine: Bisection depth for chasing sign flips and rising maxima.
        growth: Magnitude factor per refinement that counts as blowing up.
        trust_declared_bounds: When false, every `bounded_by` claim is ignored and its
            findings refuse instead of warning. The paranoid view; see the module docstring
            for why it exists and why it is worth running once.

    Returns:
        A `Probe`. Call `raise_if_pathological()` to turn refusals into a refusal.
    """
    if not domains:
        raise ValueError("probe needs at least one domain")
    if not trust_declared_bounds:
        domains = [
            Domain(
                name=axis.name,
                low=axis.low,
                high=axis.high,
                integral=axis.integral,
                bounded_by=None,
                feasible=axis.feasible,
            )
            for axis in domains
        ]

    findings: list[Finding] = []
    notes: list[str] = []
    declared = _sweep_box(
        objective, domains, label="declared", space=space, resolution=resolution
    )
    notes.extend(_undersampled(domains, declared, resolution))

    findings.extend(_check_raises(declared))
    findings.extend(_check_nonfinite(declared))
    pole_samples, pole_findings = _check_poles(
        objective, declared, refine=refine, growth=growth
    )
    findings.extend(pole_findings)
    growth_samples, growth_findings = _check_unbounded(
        objective, declared, refine=refine, growth=growth
    )
    findings.extend(growth_findings)

    # Before the checks that depend on the shape of the answer space, because an idle term is
    # the cause of what those find and a reader wants the cause first.
    discrimination, component_findings = _check_components(components, declared)
    findings.extend(component_findings)
    if not components:
        notes.append(
            "no `components` were declared, so no term of the objective was measured for "
            "whether it discriminates at all. A term that is constant across the swept space "
            "cannot contribute to selection, and every downstream finding then reports the "
            "symptom without naming the cause: see `Component`."
        )

    candidates = list(degenerate)
    if structure is not None:
        enumerated, enumeration_notes = _enumerate_degenerate(
            structure, declared, domains, space=space, resolution=resolution
        )
        candidates.extend(enumerated)
        notes.extend(enumeration_notes)
        emptying_findings, emptying_notes = _check_emptying(structure, declared, space=space)
        findings.extend(emptying_findings)
        notes.extend(emptying_notes)
    else:
        notes.append(
            "no `structure` was declared, so degenerate inputs could not be enumerated and "
            "emptying-is-free could not be checked. Whatever is in `degenerate` is a list "
            "written by hand, which is the contract this argument replaced: see the module "
            "docstring."
        )

    if search is not None:
        best = declared.best
        brief = Brief(
            objective=objective,
            axes=tuple(domains),
            space=space,
            ceiling=(best.value or 0.0) if best else 0.0,
            best_point=dict(best.point) if best else {},
            structure=structure,
            samples=declared.samples,
            source=source,
        )
        proposed = list(search(brief))
        candidates.extend(proposed)
        notes.append(
            f"adversarial search proposed {len(proposed)} candidate(s); each is re-scored "
            "here, so a searcher's claim about its own candidate is never the evidence"
        )

    findings.extend(_check_degenerate(objective, declared, candidates, notes))

    sweeps = [
        Sweep(
            label=declared.label,
            space=declared.space,
            axes=declared.axes,
            resolution=declared.resolution,
            samples=declared.samples,
            refined=tuple(pole_samples) + tuple(growth_samples),
        )
    ]

    escapes, escape_findings, escape_notes = _check_escape(
        objective,
        declared,
        domains,
        space=space,
        resolution=resolution,
        reach=reach,
        refine=refine,
        growth=growth,
    )
    sweeps.extend(escapes)
    findings.extend(escape_findings)
    notes.extend(escape_notes)

    if space is Space.METRIC:
        notes.append(
            "boundary-max not checked: in metric space the ideal corner is a boundary and "
            "is supposed to win, so the check cannot separate a sound objective from a "
            "broken one. Run a Space.DECISION probe over the variable the loop moves."
        )
    else:
        findings.extend(
            _check_boundary(
                objective,
                declared,
                domains,
                resolution=resolution,
                reach=reach,
            )
        )

    return Probe(
        findings=tuple(findings),
        sweeps=tuple(sweeps),
        notes=tuple(notes),
        discrimination=tuple(discrimination),
    )


def _undersampled(
    domains: Sequence[Domain], sweep: Sweep, resolution: int
) -> list[str]:
    """Say when the grid skipped feasible values, because the ceiling is then a lower bound.

    Found by a live adversary rather than by design, and it is the one thing the LLM half
    contributed that no enumeration was ever going to find. On the permit log's composed
    objective an adversary swept the integers directly, reported 0.8274 at threshold 19, and
    observed that the probe's stated ceiling was 0.8184 at 17. Reproduced: the 21-point grid
    over 1 to 323 lands on 1, 17, 33, 49 and skips 19 entirely.

    That is not a defect in the objective and it is not one in the grid either — a sweep is a
    sweep. It is a defect in a report that calls a sampled maximum "the in-domain maximum",
    because every escape and degenerate comparison is made against it and a ceiling that is
    too low makes those comparisons *lenient*. So the report says the ceiling is a lower
    bound, and says how much of the axis was skipped.

    A note rather than a finding, deliberately. Refusing an objective because the caller
    chose a resolution would be the prober substituting its own opinion about sampling for
    the caller's, and there is no honest threshold to put it at.
    """
    notes: list[str] = []
    for axis in domains:
        if not axis.integral or axis.span <= 0:
            continue
        feasible_values = int(round(axis.span)) + 1
        sampled = sweep.resolution.get(axis.name, resolution)
        if sampled >= feasible_values:
            continue
        notes.append(
            f"under-sampled: `{axis.name}` is integral over [{axis.low:g}, {axis.high:g}], "
            f"which is {feasible_values} feasible values, and the grid sampled {sampled} of "
            "them. Every ceiling in this report is therefore a lower bound on the true "
            "in-domain maximum, which makes every escape and degenerate comparison lenient "
            f"rather than strict. Raise `resolution` to {feasible_values} to sweep it "
            "exhaustively."
        )
    return notes


def _check_raises(sweep: Sweep) -> list[Finding]:
    """An objective that raises inside its own declared domain loses the round.

    Failure three in the case study: tightening produced a graph with no terminal state
    and the compile step raised instead of degrading. The training round was lost rather
    than scored. Nothing about that is specific to process mining — any objective that
    can raise on a feasible input has an input the loop cannot learn from.
    """
    raised = sweep.raised
    if not raised:
        return []
    first = raised[0]
    return [
        Finding(
            check="raises-inside-the-domain",
            severity=Severity.REFUSE,
            detail=(
                f"{len(raised)} of {sweep.evaluations} declared-feasible inputs raised rather "
                f"than scoring, so the loop cannot learn from them. First: {first.error}"
            ),
            point=first.point,
        )
    ]


def _check_nonfinite(sweep: Sweep) -> list[Finding]:
    bad = sweep.nonfinite
    if not bad:
        return []
    first = bad[0]
    return [
        Finding(
            check="non-finite-value",
            severity=Severity.REFUSE,
            detail=(
                f"{len(bad)} of {sweep.evaluations} declared-feasible inputs scored "
                f"non-finite ({first.value}). Selection by `max` over nan is order-dependent, "
                "so which round is picked as best depends on evaluation order."
            ),
            point=first.point,
            value=first.value,
        )
    ]


SPIKE_RATIO = 1e3
"""How far one grid value must exceed both neighbours' magnitudes to count as a spike.

Scale-free by construction: it is a ratio between the objective's own adjacent values, so
it makes no assumption about the caller's units. A smooth objective sampled on a uniform
grid does not produce three orders of magnitude between neighbours.
"""

SPIKE_PEAK_DOMINANCE = True
"""A spiking grid point must also carry the sweep's largest finite magnitude.

Not a tunable, a documented property of the check, kept named so the report and the tests can
refer to one thing. It is the fix for a measured false positive, and the reasoning is in
`_find_spike`.

The rejected alternative is recorded because it is the one that looks right. Flooring the
ratio's denominator relative to the sweep's peak, instead of at the absolute `TOLERANCE`, was
tried first and *never changes an outcome*: a relative floor only exceeds `TOLERANCE` once the
peak passes 1e3, and by then a real spike clears either floor by orders of magnitude. Mutation
testing is what showed it — reverting the floor to `TOLERANCE` left every test passing, and no
objective could be constructed where it mattered. So it was removed rather than shipped as an
unmeasured knob, which is the defect class this package exists to detect.
"""


def _check_poles(
    objective: Objective, sweep: Sweep, *, refine: int, growth: float
) -> tuple[list[Sample], list[Finding]]:
    """Find singularities two ways, because a pole need not change sign.

    A sign-flipped adjacent pair is the classic odd-order pole, and the one this project's
    history actually had: `1 - edge_share` going negative flipped the harmonic mean's sign
    across `edge_share == 1 + coverage`.

    An *even*-order pole does not flip sign at all. `1 / abs(x - c)` is positive on both
    sides, so a sign-flip detector cannot see it, and a detector that could not see it
    would be a check with a hole in exactly the place poles like to hide. That case shows
    up instead as a magnitude spike against both grid neighbours, which is scale-free.
    """
    grids = [_axis_values(axis, sweep.resolution[axis.name]) for axis in sweep.axes]
    values = {tuple(s.point.items()): s for s in sweep.samples}
    collected: list[Sample] = []
    findings: list[Finding] = []

    for low, high in _adjacent_pairs(sweep.axes, grids):
        left, right = values.get(tuple(low.items())), values.get(tuple(high.items()))
        if left is None or right is None or not left.finite or not right.finite:
            continue
        if _sign(left.value) * _sign(right.value) >= 0:
            continue
        axis = next(name for name in low if low[name] != high[name])
        samples, blew_up = _chase_pole(objective, low, high, axis, refine=refine, growth=growth)
        collected.extend(samples)
        if blew_up:
            worst = max(samples, key=lambda s: abs(s.value) if s.finite else math.inf)
            findings.append(
                Finding(
                    check="pole",
                    severity=Severity.REFUSE,
                    detail=(
                        f"the objective changes sign between {_render(low)} and {_render(high)} "
                        f"while its magnitude grows under refinement, reaching "
                        f"{worst.value!r}. That is a division pole, not a zero crossing: the "
                        "argmax near it is an artifact of arithmetic and the loop will climb it."
                    ),
                    point=worst.point,
                    value=worst.value,
                )
            )
            return collected, findings  # one pole is a refusal; more adds cost, not information

    spike = _find_spike(sweep, grids, values)
    if spike is not None:
        middle, neighbours = spike
        around = ", ".join(repr(s.value) for s in neighbours)
        findings.append(
            Finding(
                check="pole",
                severity=Severity.REFUSE,
                detail=(
                    f"the objective spikes to {middle.value!r} at one grid point while its "
                    f"neighbour(s) along the same axis measure {around}, a ratio above "
                    f"{SPIKE_RATIO:g}. That is a singularity that does not change sign, so it is "
                    "invisible to a sign test, and the loop will climb it."
                ),
                point=middle.point,
                value=middle.value,
            )
        )
    return collected, findings


def _find_spike(
    sweep: Sweep, grids: Sequence[Sequence[float]], values: Mapping[tuple, Sample]
) -> tuple[Sample, tuple[Sample, ...]] | None:
    """A grid point whose magnitude dwarfs its neighbours *and* dominates the whole sweep.

    Interior points are compared against both neighbours, and the two ends of each axis
    against their single one. Including the ends matters: a singularity likes to sit
    exactly where a denominator vanishes, and a denominator often vanishes at zero, which
    is where a declared domain tends to start. Skipping the ends would leave the hole in
    the place most likely to hold the defect.

    The neighbour ratio alone is not enough, and that was found by measurement rather than
    reasoned about. It fires on a three-axis piecewise-linear objective bounded in [0, 1],
    twice over and for the same underlying reason. Inside the declared box one grid point
    pairs an exact `0.0` neighbour with an entirely ordinary `-0.0125`, three orders up.
    Outside it, `0.25*b - 0.25*c` returns `5.551115123125783e-17` where it is algebraically
    zero, and an ordinary neighbouring `0.05` then reads as fifteen orders. Neither is a
    singularity; both are two ways of writing zero next to an ordinary value.

    So the spiking point must also carry the sweep's largest finite magnitude. A singularity
    *dominates* the space it sits in, which is the property that makes it dangerous to a loop
    climbing the objective; a point that dwarfs its neighbours while something else in the same
    sweep is larger is a point the objective's own scale already accommodates. Scale-free, like
    the ratio, because it compares the objective's values only against each other.

    What that costs, stated rather than assumed: with several spikes in one sweep only the
    largest is named. Nothing is lost, because one pole is a refusal and the finding is the
    refusal rather than the inventory.
    """
    names = [axis.name for axis in sweep.axes]
    peak = sweep.peak
    for index in range(len(sweep.axes)):
        length = len(grids[index])
        if length < 2:
            continue
        spans = [range(len(grid)) for grid in grids]
        for combo in product(*spans):
            position = combo[index]
            steps = [step for step in (-1, 1) if 0 <= position + step < length]
            if not steps:
                continue
            point = dict(zip(names, (grids[i][j] for i, j in enumerate(combo)), strict=True))
            middle = values.get(tuple(point.items()))
            if middle is None or not middle.finite:
                continue
            sides: list[Sample] = []
            for step in steps:
                side = dict(point)
                side[names[index]] = grids[index][position + step]
                found = values.get(tuple(side.items()))
                if found is None or not found.finite:
                    sides = []
                    break
                sides.append(found)
            if not sides:
                continue
            here = abs(middle.value or 0.0)
            around = max(abs(s.value or 0.0) for s in sides)
            if here < peak:
                continue
            if here > max(around, TOLERANCE) * SPIKE_RATIO:
                return middle, tuple(sides)
    return None


def _check_unbounded(
    objective: Objective, sweep: Sweep, *, refine: int, growth: float
) -> tuple[list[Sample], list[Finding]]:
    samples, unbounded = _refine_maximum(objective, sweep, refine=refine, growth=growth)
    if not unbounded:
        return samples, []
    worst = max(samples, key=lambda s: abs(s.value) if s.finite else math.inf)
    return samples, [
        Finding(
            check="unbounded",
            severity=Severity.REFUSE,
            detail=(
                "refining the grid around the maximum kept raising it, reaching "
                f"{worst.value!r}, so the supremum is not attained on the declared box. "
                "Whatever the loop reports as its best score is a function of how densely "
                "it happened to sample."
            ),
            point=worst.point,
            value=worst.value,
        )
    ]


def _check_degenerate(
    objective: Objective,
    sweep: Sweep,
    degenerate: Sequence[Degenerate],
    notes: list[str],
) -> list[Finding]:
    """A degenerate input must not be the winner. Failure one, exactly.

    Coverage alone was maximised by keeping every handoff including the thirty walked by
    a single case out of 1434. It reported 98.6% and looked like a win. The optimum was
    achieved by refusing to generalise.

    This is the *arithmetic* half of the check and it is where every candidate lands
    whatever proposed it, so a declared point, an enumerated one, and one an LLM adversary
    argued for are all held to the same test: score it, compare it to the grid maximum,
    refuse if it ties. No candidate's own account of itself is taken as evidence.
    """
    if not degenerate:
        # Says only that no candidate reached the check, and not why. The enumeration and the
        # search each already say what they did or did not contribute, and restating a cause
        # here would be a second place to keep the same fact in sync.
        notes.append(
            "degenerate-winner not checked: no candidate input reached it, from a declaration, "
            "an enumeration, or a search. The objective may still be maximised by 'keep "
            "everything' or 'keep nothing'."
        )
        return []
    best = sweep.best
    if best is None:
        return []
    ceiling = best.value or 0.0
    findings: list[Finding] = []
    for case in degenerate:
        provenance = f" [{case.found_by}]"
        because = f" {case.worthless_because}" if case.worthless_because else ""
        sample = _evaluate(objective, dict(case.point))
        if not sample.finite:
            findings.append(
                Finding(
                    check="degenerate-not-scorable",
                    severity=Severity.REFUSE,
                    detail=(
                        f"the degenerate input {case.label!r}{provenance} scored "
                        f"{sample.value} ({sample.error or 'non-finite'}), so whether it wins "
                        f"cannot be decided.{because}"
                    ),
                    point=dict(case.point),
                )
            )
            continue
        if (sample.value or 0.0) >= ceiling - TOLERANCE:
            findings.append(
                Finding(
                    check="degenerate-optimum",
                    severity=Severity.REFUSE,
                    detail=(
                        f"the degenerate input {case.label!r}{provenance} scores "
                        f"{sample.value:.4f} against a grid maximum of {ceiling:.4f}, so it "
                        "ties or wins. The optimizer will find it, report it as a record, and "
                        f"have learned nothing.{because}"
                    ),
                    point=dict(case.point),
                    value=sample.value,
                )
            )
    return findings


def _enumerate_degenerate(
    structure: Structure,
    sweep: Sweep,
    domains: Sequence[Domain],
    *,
    space: Space,
    resolution: int,
) -> tuple[list[Degenerate], list[str]]:
    """Compute the degenerate inputs from the declared space rather than imagining them.

    Five kinds, and each one is a bad answer for a different reason:

    The **emptiest viable** point is the one that matters, and it is the one a caller
    writing a list forgets. It is the answer that still counts as an answer while
    representing as little as possible, and it is exactly what an objective whose other
    terms have gone flat is maximised by. On the transcript log it is a two-state model
    replaying two cases out of 88, and no version of this call site ever named it.

    The **fullest** point is memorisation: keep everything, describe nothing. Failure one.

    The **empty** point, where `size` is zero or the point is not viable at all, is "keep
    nothing". Included even though it usually scores zero, because an objective that
    rewards it is broken in a way worth naming outright.

    The **box corners** are the combinatorial extremes of the decision variables, and they
    are enumerated *only in decision space*. In metric space the ideal corner is a
    boundary and is supposed to win — the same argument the boundary-max check rests on —
    so enumerating corners there would produce a finding on every sound objective. Corners
    are capped at `MAX_CORNERS`; the cap is reported.

    Sizes are measured on the grid the prober already swept, so the emptiest and fullest
    points cost no extra objective evaluations to locate.
    """
    notes: list[str] = []
    found: list[Degenerate] = []
    seen: set[tuple] = set()

    if space is not Space.DECISION:
        # Corrected after a live adversary run, and this is the sharpest thing the LLM half
        # produced. An earlier version of this function enumerated the size-derived points in
        # metric space too, and the adversaries proved that unsound in two ways at once.
        #
        # It is order-dependent: on the current objective's 21^3 metric grid, 21 points tie
        # for the smallest non-zero `edge_share`, scoring anywhere from 0.0 to 0.9744, so
        # which one becomes "the emptiest answer" is decided by `product`'s iteration order.
        #
        # And tiebreaking that correctly makes it worse rather than better, which is the real
        # argument. Free axes mean the best point at any fixed size is the one holding every
        # other term at its ideal, so as the grid refines the strongest emptiest point
        # converges on the ideal corner: coverage 1.0 with share 0.05 already scores 0.9744
        # against a grid maximum of 1.0. A sound objective would eventually be refused for
        # having a good optimum. That is the same argument the boundary-max check rests on.
        #
        # Worth recording that five diverse adversaries and a three-judge panel all upheld
        # metric-space empty-answer candidates unanimously. The LLM half found the flaw in
        # the deterministic half, and then reproduced it; only the space discipline catches
        # both, and it is deterministic.
        notes.append(
            "degenerate inputs not enumerated: every one of them is a decision-space "
            "property. In metric space the axes vary independently, so the strongest empty "
            "answer converges on the ideal corner as the grid refines and every sound "
            "objective would eventually be refused for having a good optimum. Run a "
            "Space.DECISION probe over the variable the loop moves."
        )
        return found, notes

    def add(label: str, point: Point) -> None:
        key = tuple(sorted(point.items()))
        if key in seen:
            return
        seen.add(key)
        found.append(Degenerate(label=label, point=point, found_by="enumerated"))

    measured = [
        (sweep_point, size)
        for sweep_point, size in (
            (s.point, structure.measure(s.point)) for s in sweep.samples
        )
        if size is not None
    ]
    if not measured:
        notes.append(
            "degenerate enumeration measured no sizes: `structure.size` raised on every "
            "swept point, so nothing could be derived from the declared space."
        )
        return found, notes

    viable = [(p, size) for p, size in measured if structure.is_viable(p)]
    if viable:
        # Tiebreak on score, not on grid position. Points tying on size are otherwise
        # resolved by `product`'s iteration order, which would make the finding an artifact
        # of how the axes happen to be listed.
        smallest = min(size for _, size in viable)
        largest = max(size for _, size in viable)
        scored = {tuple(s.point.items()): s for s in sweep.samples}

        def strongest(target: float) -> tuple[Point, float]:
            candidates = [(p, size) for p, size in viable if size == target]
            return max(
                candidates,
                key=lambda pair: (
                    scored[tuple(pair[0].items())].value or 0.0
                    if scored.get(tuple(pair[0].items())) is not None
                    and scored[tuple(pair[0].items())].finite
                    else -math.inf
                ),
            )
        emptiest, emptiest_size = strongest(smallest)
        fullest, fullest_size = strongest(largest)
        add(
            f"emptiest answer the space still admits ({emptiest_size:g} {structure.units})",
            dict(emptiest),
        )
        add(
            f"fullest answer, keep everything ({fullest_size:g} {structure.units})",
            dict(fullest),
        )
    else:
        notes.append(
            "degenerate enumeration found no viable point on the swept grid, so the "
            "emptiest-answer check could not run. Widen the window or loosen `viable`."
        )

    unviable = [(p, size) for p, size in measured if not structure.is_viable(p)]
    if unviable:
        emptiest_unviable = min(unviable, key=lambda pair: pair[1])
        add("empty answer, keep nothing", dict(emptiest_unviable[0]))

    spanning = [axis for axis in domains if axis.span > 0]
    corners = list(product(*[(axis.low, axis.high) for axis in spanning]))
    if len(corners) > MAX_CORNERS:
        notes.append(
            f"degenerate enumeration capped box corners at {MAX_CORNERS} of "
            f"{len(corners)} ({len(spanning)} spanning axes); the emptiest and fullest "
            "points are derived from the whole grid and are not affected by the cap."
        )
        corners = corners[:MAX_CORNERS]
    for corner in corners:
        point = {axis.name: value for axis, value in zip(spanning, corner, strict=True)}
        for axis in domains:
            point.setdefault(axis.name, axis.low)
        add(f"box corner {_render(point)}", point)

    notes.append(
        f"enumerated {len(found)} degenerate input(s) from the declared structure "
        f"({structure.units}), rather than taking a caller's list on trust"
    )
    return found, notes


def _check_emptying(
    structure: Structure, sweep: Sweep, *, space: Space
) -> tuple[list[Finding], list[str]]:
    """Does shrinking the answer ever cost score? If never, the optimum is the empty one.

    The general form of the degenerate-optimum finding, and the stronger half of this
    module's answer to the declared-list defect. The point test asks whether one specific
    emptiest answer wins; this asks whether the objective has any preference for a fuller
    answer *at all*, which is the property that makes the winner degenerate no matter which
    point the space happens to admit last.

    Provable on the grid rather than sampled: walk every grid-adjacent pair, keep the ones
    where `size` strictly falls, and require the score to strictly fall across at least one
    of them. `size` falling with the score never falling means the objective has reduced to
    a function that is monotone in emptiness, and its argmax is whatever the space admits
    last.

    Strict by design. A "fewer than N% of pairs cost score" threshold would be a number
    fitted to whichever fixture was in hand, which is this session's defect one level up,
    so the check fires only on "never" and the report states how many pairs it walked.

    Decision space only, and for the same reason corners are. Metric axes vary freely, so
    "shrink this term holding the rest" is always on the grid and is usually the correct
    answer; the ideal corner *is* an empty answer scoring the maximum. Measured: on the
    permit log's composed decision objective the score falls from 0.8184 at the argmax to
    0.7680 one grid step emptier, so this does not fire; on the transcript log's it holds
    0.0444 all the way to a single-edge model, so it does.
    """
    if space is not Space.DECISION:
        return [], [
            "emptying-is-free not checked: it is a decision-space property. In metric "
            "space every term varies independently, so shrinking one is always available "
            "on the grid and is usually the right answer. Run a Space.DECISION probe over "
            "the variable the loop moves."
        ]

    grids = [_axis_values(axis, sweep.resolution[axis.name]) for axis in sweep.axes]
    values = {tuple(s.point.items()): s for s in sweep.samples}
    walked = 0
    cost = 0
    example: tuple[Point, Point, float, float] | None = None

    for low, high in _adjacent_pairs(sweep.axes, grids):
        left, right = values.get(tuple(low.items())), values.get(tuple(high.items()))
        if left is None or right is None or not left.finite or not right.finite:
            continue
        left_size, right_size = structure.measure(low), structure.measure(high)
        if left_size is None or right_size is None or left_size == right_size:
            continue
        # Orient the pair so `fuller` is the bigger answer, whichever grid direction that is.
        if left_size > right_size:
            fuller, emptier = left, right
        else:
            fuller, emptier = right, left
        if not structure.is_viable(emptier.point) or not structure.is_viable(fuller.point):
            continue
        walked += 1
        if (emptier.value or 0.0) < (fuller.value or 0.0) - TOLERANCE:
            cost += 1
        elif example is None:
            example = (
                fuller.point,
                emptier.point,
                fuller.value or 0.0,
                emptier.value or 0.0,
            )

    if not walked:
        return [], [
            "emptying-is-free not checked: no grid-adjacent pair changed the structure's "
            "size, so the sweep never compares a fuller answer against an emptier one."
        ]
    if cost:
        return [], [
            f"emptying-is-free passed: of {walked} grid-adjacent pairs where the answer "
            f"shrinks, {cost} cost score, so the objective does prefer a fuller answer "
            "somewhere"
        ]

    detail = (
        f"across all {walked} grid-adjacent pairs where the answer shrinks, the score "
        "never fell. Shrinking is free, so the objective is monotone in emptiness and its "
        "optimum is whatever the space admits last, however little that describes. "
        "Whichever term was supposed to punish an empty answer has stopped discriminating."
    )
    if example is not None:
        fuller, emptier, fuller_value, emptier_value = example
        detail += (
            f" For instance {_render(fuller)} scores {fuller_value:.4f} and the emptier "
            f"{_render(emptier)} scores {emptier_value:.4f}."
        )
    return [
        Finding(
            check="emptying-is-free",
            severity=Severity.REFUSE,
            detail=detail,
            point=dict(example[1]) if example else None,
        )
    ], []


def _check_components(
    components: Sequence[Component], sweep: Sweep
) -> tuple[list[Discrimination], list[Finding]]:
    """Measure each declared term's variation across the swept grid, and name the idle ones.

    The whole check is one sentence: a term whose value is the same everywhere the loop can
    look cannot contribute to selection, so the score is a function of the remaining terms
    and whatever that term was supposed to punish is unpunished. Measurable without knowing
    anything about what the term means.

    An observation is a swept point where the term evaluated finitely; a separating
    observation is one where it differs from the term's minimum by more than `floor`. That
    counting choice is deliberate and it is what makes `separating == 0` mean "constant"
    rather than "we compared adjacent points and they happened to tie": comparing against
    the extreme rather than against a neighbour cannot be fooled by a term that is flat in
    patches while still moving overall.

    Three-valued, through the shared primitive. `withheld` carries the two ways this cannot
    settle: a term that raised or went non-finite on some points was measured over fewer
    than the sweep visited, and a term measurable at one point or none has no variation to
    have. Neither is a pass and neither is a finding.

    Space-agnostic, unlike the emptying and enumeration checks, and that asymmetry has a
    reason. Those two ask about a *direction* in the space, so free metric axes make the move
    they test always available and the check meaningless there. This asks whether a quantity
    has any range at all, which is the same question in both spaces: a coverage term reading
    0.0227 everywhere is dead whether the threshold or coverage itself is the axis.
    """
    measured: list[Discrimination] = []
    findings: list[Finding] = []
    finite = sweep.finite
    for component in components:
        values = [v for v in (component.measure(s.point) for s in finite) if v is not None]
        withheld: list[str] = []
        unmeasurable = len(finite) - len(values)
        if unmeasurable:
            withheld.append(
                f"the term did not evaluate finitely at {unmeasurable} of {len(finite)} "
                "swept points, so it was measured over fewer points than the sweep visited"
            )
        if len(values) < 2:
            withheld.append(
                f"the term was measurable at {len(values)} point(s), which is too few for it "
                "to have a range at all"
            )
        low = min(values) if values else 0.0
        separating = sum(1 for v in values if v - low > component.floor)
        report = Discrimination(
            subject=component.name,
            observations=len(values),
            separating=separating,
            withheld=tuple(withheld),
            unit="swept point",
            kind="objective term",
        )
        measured.append(report)
        if not report.idle:
            continue
        span = (max(values) - low) if values else 0.0
        findings.append(
            Finding(
                check="component-does-not-discriminate",
                severity=Severity.WARN,
                detail=(
                    f"the term {component.name!r} is {low:.4g} at all {len(values)} swept "
                    f"points (range {span:.4g}, at or below the declared floor of "
                    f"{component.floor:g}), so it cannot contribute to selection anywhere the "
                    "loop can look. The score reduces to a function of the remaining terms, "
                    "and whatever this one was supposed to punish is unpunished. This is the "
                    "cause; a degenerate optimum is what it causes."
                ),
                value=low,
            )
        )
    return measured, findings


def _check_escape(
    objective: Objective,
    declared: Sweep,
    domains: Sequence[Domain],
    *,
    space: Space,
    resolution: int,
    reach: float,
    refine: int,
    growth: float,
) -> tuple[list[Sweep], list[Finding], list[str]]:
    """Sweep just outside the declared domain and require that escaping is not rewarded.

    This is the check that would have caught the bug that shipped, and the reasoning for
    its semantics is in the module docstring. In short: a training loop searches for the
    argmax, so a reward outside the declared domain is a reward, whether or not anyone
    believes that input is reachable.
    """
    best = declared.best
    ceiling = (best.value or 0.0) if best else 0.0
    sweeps: list[Sweep] = []
    findings: list[Finding] = []
    notes: list[str] = []

    boxes = _escape_boxes(domains, reach)
    if not boxes:
        notes.append(
            "escape not checked: every declared bound coincides with its feasible limit, "
            "so there is nowhere outside the domain to look."
        )
        return sweeps, findings, notes

    for label, axis, axes in boxes:
        escaped = _sweep_box(
            objective, axes, label=f"escape: {label}", space=space, resolution=resolution
        )
        pole_samples, pole_findings = _check_poles(
            objective, escaped, refine=refine, growth=growth
        )
        growth_samples, growth_findings = _check_unbounded(
            objective, escaped, refine=refine, growth=growth
        )
        bounded = axis.bounded_by
        severity = Severity.WARN if bounded else Severity.REFUSE

        for finding in (*pole_findings, *growth_findings):
            findings.append(
                Finding(
                    check=f"escape-{finding.check}",
                    severity=severity,
                    detail=(
                        f"outside the declared domain ({label}"
                        + (f", claimed bounded by {bounded}" if bounded else "")
                        + f"): {finding.detail}"
                    ),
                    point=finding.point,
                    value=finding.value,
                    downgraded_by=bounded,
                )
            )

        top = escaped.best
        if top is not None and (top.value or 0.0) > ceiling + TOLERANCE:
            findings.append(
                Finding(
                    check="escape-rewarded",
                    severity=severity,
                    detail=(
                        f"an input outside the declared domain ({label}) scores "
                        f"{top.value:.4f}, above the in-domain maximum of {ceiling:.4f}"
                        + (
                            f". The bound is claimed to be established by {bounded}, so this is "
                            "a warning rather than a refusal, but the claim is the only thing "
                            "standing between the loop and a reward it should not reach."
                            if bounded
                            else ". A loop searching for the argmax will leave the domain the "
                            "objective was designed on, and score higher for doing it."
                        )
                    ),
                    point=top.point,
                    value=top.value,
                    downgraded_by=bounded,
                )
            )
        raised = escaped.raised
        if raised:
            findings.append(
                Finding(
                    check="escape-raises",
                    severity=Severity.WARN,
                    detail=(
                        f"{len(raised)} of {len(escaped.samples)} inputs outside the declared "
                        f"domain ({label}) raised rather than degrading: {raised[0].error}. Not "
                        "rewarded, so not a refusal, but the caller sees a crash where it "
                        "expected a bad score."
                    ),
                    point=raised[0].point,
                )
            )
        sweeps.append(
            Sweep(
                label=escaped.label,
                space=escaped.space,
                axes=escaped.axes,
                resolution=escaped.resolution,
                samples=escaped.samples,
                refined=tuple(pole_samples) + tuple(growth_samples),
            )
        )
    return sweeps, findings, notes


def _check_boundary(
    objective: Objective,
    declared: Sweep,
    domains: Sequence[Domain],
    *,
    resolution: int,
    reach: float,
) -> list[Finding]:
    """In decision space, a maximum on the window's own edge means the window may be wrong.

    Failure four. The agent swept thresholds 1 to 24, optimised correctly inside that
    window, and settled short of the real peak at 40. Measured here: over 1 to 24 the
    argmax is 24, the window's upper edge.

    A boundary maximum is not automatically a defect — the true optimum can genuinely sit
    at a feasible extreme. So the prober does the thing the agent did not: it looks past
    the edge. Improving outside means the window was too narrow, which is a refusal.
    Degrading outside means the boundary is a real optimum, which is a note.
    """
    best = declared.best
    if best is None:
        return []
    findings: list[Finding] = []
    for axis in domains:
        if axis.span <= 0:
            continue
        at = best.point[axis.name]
        edges = {"low": axis.low, "high": axis.high}
        near = max(TOLERANCE, axis.span * 1e-9)
        side = next((k for k, v in edges.items() if abs(at - v) <= near), None)
        if side is None:
            continue
        span = axis.span
        limit_low, limit_high = axis.feasible or (-math.inf, math.inf)
        if side == "high":
            low, high = axis.high, min(axis.high + reach * span, limit_high)
        else:
            low, high = max(axis.low - reach * span, limit_low), axis.low
        if not math.isfinite(low) or not math.isfinite(high) or high - low <= TOLERANCE:
            findings.append(
                Finding(
                    check="boundary-optimum-at-feasible-limit",
                    severity=Severity.WARN,
                    detail=(
                        f"the maximum {best.value:.4f} sits at {axis.name}={at:g}, the {side} "
                        f"edge of the swept window, and that edge is the feasible limit, so "
                        "there is nowhere further to look. Confirm the limit is real."
                    ),
                    point=best.point,
                    value=best.value,
                )
            )
            continue
        outside = _sweep_box(
            objective,
            [Domain(name=axis.name, low=low, high=high, integral=axis.integral)]
            + [a for a in domains if a.name != axis.name],
            label=f"beyond {axis.name} {side}",
            space=Space.DECISION,
            resolution=resolution,
        )
        beyond = outside.best
        if beyond is not None and (beyond.value or 0.0) > (best.value or 0.0) + TOLERANCE:
            findings.append(
                Finding(
                    check="window-too-narrow",
                    severity=Severity.REFUSE,
                    detail=(
                        f"the maximum {best.value:.4f} sits at {axis.name}={at:g}, the {side} "
                        f"edge of the swept window, and continuing past it to "
                        f"{beyond.point[axis.name]:g} scores {beyond.value:.4f}. The loop would "
                        "optimise correctly inside a window that excludes the real optimum."
                    ),
                    point=beyond.point,
                    value=beyond.value,
                )
            )
        else:
            findings.append(
                Finding(
                    check="boundary-optimum-confirmed",
                    severity=Severity.WARN,
                    detail=(
                        f"the maximum {best.value:.4f} sits at {axis.name}={at:g}, the {side} "
                        f"edge of the window, but the objective degrades past it (best outside: "
                        f"{beyond.value if beyond else None}), so the edge is a genuine optimum."
                    ),
                    point=best.point,
                    value=best.value,
                )
            )
    return findings


# ── The feedback channel ──

Render = Callable[..., str]
"""Called as `render(point, best_so_far)` and returns the text the optimizer reads."""

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

DEFAULT_PRECISIONS = (1, 2, 3, 4)
"""Decimal places a reported quantity is looked for at. Reported, so the search is auditable."""


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def _mentions(text: str, value: float, precisions: Sequence[int]) -> str | None:
    """Whether `value` appears in `text`, raw or as a percentage, at any tried precision.

    Returns the rendering that matched so a finding can say how it decided. Percentage
    form is tried because feedback conventionally renders shares that way, and a check
    that missed "93.2%" while the code wrote `100 * coverage` would be a check that
    cannot fire.
    """
    for scale, suffix in ((1.0, ""), (100.0, "%")):
        for digits in precisions:
            rendered = f"{value * scale:.{digits}f}"
            if rendered in text:
                return rendered + suffix
    return None


def probe_feedback(
    render: Render,
    objective: Objective,
    points: Sequence[Point],
    *,
    components: Sequence[str] = (),
    best_so_far: float | None = 0.0,
    precisions: Sequence[int] = DEFAULT_PRECISIONS,
) -> Probe:
    """Check that the feedback the optimizer reads states the quantity selection uses.

    Failure two in the case study was not the objective's shape at all. Scoring was the
    harmonic mean; the feedback reported coverage and only complained about memorisation
    above 60% edge share. At 29% share the agent heard nothing but "you are behind on
    coverage", loosened the threshold every round, and walked its score from 0.804 down
    to 0.706 while its coverage rose from 93.2% to 96.9%. It walked away from its own
    best attempt, obediently. The metric was never shown to the thing optimising it.

    Two things about that are mechanically decidable and both are checked here.

    **Does the text state the score?** Render the feedback at each probe point, format
    the score, and look for it. This fires on a feedback function that reports coverage
    alone and passes on one that reports the score. It is a string test, which is exactly
    the right altitude: the optimizer reads a string, and a number that is not in the
    string is a number the optimizer was not told.

    **Does it state the score on every round, or only some?** Conditional reporting is
    the same defect narrowed. A message that names the score only when there is a
    previous best leaves round zero one-sided.

    **What is not checkable, and why saying so matters more than pretending.** Whether
    the feedback's *advice* points uphill is not decidable from the text. "Raising the
    threshold buys selectivity" is either true guidance or a plausible sentence, and
    telling those apart requires knowing what the objective's gradient is in the space of
    things the prose can ask for, which is a semantic question about English. It could be
    faked — accept a caller-supplied extractor and check rank correlation — and that fake
    is precisely this session's defect class: the extractor would be written by the same
    hand and wrong in the same direction, and the check would pass while never firing.
    So this function checks presence, and the report says presence is what it checked.

    Args:
        render: `render(point, best_so_far)` returning the feedback text.
        objective: The scoring callable selection uses.
        points: Probe inputs. At least two, so conditional reporting is visible.
        components: Input names that are themselves plausible things to report. Used to
            attach concrete evidence to a finding, not to decide it.
        best_so_far: What to pass as the standing best. `None` probes the first round.
        precisions: Decimal places to look for the score at.
    """
    if len(points) < 2:
        raise ValueError("probe_feedback needs at least two points to see conditional reporting")

    findings: list[Finding] = []
    notes: list[str] = []
    reported: list[tuple[Point, float, str | None, str]] = []

    for point in points:
        sample = _evaluate(objective, dict(point))
        if not sample.finite:
            findings.append(
                Finding(
                    check="feedback-point-not-scorable",
                    severity=Severity.WARN,
                    detail=f"probe point scored {sample.value} ({sample.error or 'non-finite'})",
                    point=dict(point),
                )
            )
            continue
        text = render(dict(point), best_so_far)
        score = sample.value or 0.0
        reported.append((dict(point), score, _mentions(text, score, precisions), text))

    if not reported:
        return Probe(findings=tuple(findings), notes=("no probe point scored finitely",))

    missing = [entry for entry in reported if entry[2] is None]
    if len(missing) == len(reported):
        point, value, _, text = missing[0]
        named = [
            name
            for name in components
            if _mentions(text, float(point.get(name, math.nan)), precisions)
        ]
        instead = (
            f" It does report {', '.join(named)}, so the optimizer is being shown a quantity "
            "that is not the one selecting it."
            if named
            else ""
        )
        findings.append(
            Finding(
                check="feedback-omits-the-score",
                severity=Severity.REFUSE,
                detail=(
                    f"none of {len(reported)} rendered messages state the score selection uses "
                    f"(looked for {value:.4f} at {list(precisions)} decimals, raw and as a "
                    f"percentage).{instead} An optimizer cannot climb a hill it is not told the "
                    "height of."
                ),
                point=point,
                value=value,
            )
        )
    elif missing:
        point, value, _, _ = missing[0]
        findings.append(
            Finding(
                check="feedback-reports-the-score-conditionally",
                severity=Severity.REFUSE,
                detail=(
                    f"{len(missing)} of {len(reported)} rendered messages omit the score "
                    f"({value:.4f}) while the rest state it. Whichever rounds fall in the silent "
                    "branch are rounds the optimizer is steered by something else."
                ),
                point=point,
                value=value,
            )
        )

    anticorrelated = _anticorrelated_pair(reported, components)
    if anticorrelated is not None:
        worse, better, name = anticorrelated
        notes.append(
            f"evidence: score falls from {better[1]:.4f} to {worse[1]:.4f} while {name} rises "
            f"from {better[0][name]:g} to {worse[0][name]:g}, so {name} and the score disagree "
            "about which of these two rounds was better"
        )
    notes.append(
        "checked that the text states the score, not that its advice points uphill; the "
        "latter is a semantic property of English and is not decidable here"
    )
    return Probe(findings=tuple(findings), notes=tuple(notes))


def _anticorrelated_pair(
    reported: Sequence[tuple[Point, float, str | None, str]],
    components: Sequence[str],
) -> tuple[tuple[Point, float, str | None, str], tuple[Point, float, str | None, str], str] | None:
    """A pair where a component and the score rank the two rounds oppositely."""
    for name in components:
        for left in reported:
            for right in reported:
                if left[1] >= right[1]:
                    continue
                if name in left[0] and name in right[0] and left[0][name] > right[0][name]:
                    return left, right, name
    return None
