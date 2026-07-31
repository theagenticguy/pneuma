# `detect/objective.py` — design rationale

Why the objective prober is shaped the way it is. The module docstring states the
invariants; this file carries the arguments behind them.

## Scope

The scoring function is the artifact that needs adversarial review, and a checklist is
not review. `probe` takes an objective, a declared feasible domain, and the shape of its
answer space, then sweeps, refines, and refuses — rather than letting a training loop
discover the pathology by climbing it.

Four failure modes organise the checks. Each is a way an objective can be wrong while
still returning a plausible number, and each has been measured rather than imagined:

1. A degenerate input is the optimum, so the best score is reached by refusing to
   generalise. `degenerate-optimum` from `_check_degenerate`, and its general form
   `emptying-is-free` from `_check_emptying`.
2. The feedback the optimizer reads states some quantity other than the score selection
   uses, so it climbs a hill it was never shown. See `probe_feedback`.
3. The objective raises on a declared-feasible input, so the round is lost rather than
   scored. `raises-inside-the-domain`.
4. The optimum sits on the swept window's own edge, so the loop optimises correctly
   inside a window that excludes the real peak. `window-too-narrow`.

An input escaping its declared range is the fifth, and the one a prober that checked only
the declared box would pass. See "Out-of-domain semantics".

Any system with a learned objective needs this, and process mining is incidental to all
of it. The only pneuma import is `.discrimination`, a small primitive shared with
`vacuity`; everything else is stdlib. The consumer supplies a callable, a box, and the
shape of its answer space, and every domain-specific fact lives at the call site.

## The two spaces, and why conflating them makes the prober useless

An objective has *metric* inputs (coverage, selectivity) and the loop has a *decision*
variable (the support threshold) that it actually controls. The metrics are coupled
functions of the decision; the decision is what the optimizer moves.

Check "is the maximum in the interior" in metric space and the check is worthless. Any
sane F-score is maximised at the ideal corner — perfect coverage using no edges — and
that corner is a boundary. Measured on a sound composed objective: `coverage=1.0,
edge_share=0.0` scores `1.0`, the grid maximum, on the face of the box. A coverage-only
objective has its maximum at a corner too. A boundary-max check in metric space fires on
the good objective and the broken one alike.

In decision space the same check is exactly right. Sweeping the threshold and composing
the measurement, that same objective peaks at threshold 40 with `0.8613` — interior.
Swept over a narrower window, 1 to 24, it peaks at 24 with `0.8448`: on the window's own
upper edge. That is failure mode four, "optimised correctly inside a window that was too
narrow", detected mechanically.

`Space.METRIC` and `Space.DECISION` therefore run different checks, and the mode is a
required argument rather than a default, because picking it wrong is the failure this
section exists to prevent.

## Out-of-domain semantics: not clamp, not refuse, but "must not reward"

The concrete escape this is built against: `edge_share` was
`edges the model returned / handoffs in the log` with nothing constraining the numerator
to the denominator's population, so it exceeded 1, selectivity went negative, and the
harmonic mean became a rational function with a pole at `edge_share == 1 + coverage`. The
objective's *shape* was sound. Its *domain* was not. A prober checking only the declared
range would have passed it.

Three candidate semantics, and only one of them is right.

**Clamp** is wrong as the fix, and it is what is easy to reach for. Clamping
`edge_share=1.86` to `1.0` scores `0.0`, which is exactly what honest memorisation
scores. The harness then mis-grades in silence: a model that returned 86 handoffs no case
ever walked is recorded as indistinguishable from one that kept every real handoff. The
clamp does not repair the measurement, it hides that the measurement was wrong. Worth
keeping as defence in depth, and a call site should clamp, but never as the answer. The
pole is what the clamp conceals, and `casestudy/harnesslearn.py:267` and
`casestudy/minelearn.py:362,465` record the score `319.386` that the unbounded form
produced and selected as best.

**Refuse** is wrong as a general rule, because it makes the objective the wrong place to
enforce a fact about measurement. `score_edges` bounds the share by intersecting the
returned edges with the real ones, which is a repair at the boundary where the input is
computed. That is where an escape should be impossible, not where it should raise.

**"Must not reward" is what is mechanically checkable, and it is sound.** Sweep the
escape region and require that no out-of-domain input outscores the best in-domain input.
The argument does not depend on believing the escape is unreachable: a training loop is a
search for the argmax, so a reward outside the declared domain is a reward, and
reachability is a claim about code that this prober cannot verify. The unclamped objective
fails this outright — at `coverage=0.75, edge_share=2.0` it scores `6.0` against an honest
maximum of `1.0`, and at `coverage=1.0, edge_share=11.0` it scores `2.2222`.

### `bounded_by`, and this module's own weakest point

`Domain.bounded_by` is how a caller states that a bound is established by code rather
than intended. It downgrades an escape finding from refusal to warning and it demands the
name of the code that does the bounding, which is a reviewable claim in the call site. It
does not skip the check. Nothing here is skipped silently, because a prober that
under-samples and reports "looks fine" is the defect class this module detects, one level
up.

The weakness is measured rather than assumed. Declare `bounded_by` on *every* axis and
the unclamped objective passes with seven warnings and no refusal, because every pathology
it has lives outside the declared box. That is the intended semantics — the caller has
asserted the bounds are enforced elsewhere, and the prober cannot verify a claim about
another file — but it means a false `bounded_by` defeats the refusal. So
`trust_declared_bounds=False` re-runs the same probe with every claim ignored, which is
the view a reviewer should look at at least once, and it is what `Probe.report` points at
when warnings are all that is left.

## Degenerate inputs are computed, not declared

A caller-declared `Degenerate` list must not be the only way the prober learns what a bad
answer looks like. A hand-written list of bad answers is a harness artifact written by
the same hand as the scoring formula, and it is wrong in the same direction. `Structure`
exists because a two-fixture measurement proved that.

The measurement: with a declared list alone the prober passed a genuinely degenerate
objective with zero findings. Through the real `grade`/`score_edges` path, whole-trace
coverage on an agent-transcript log is `0.0227` at *every* mining threshold from 1 to 44,
so the score reduces to a monotone function of selectivity alone and the winner is a
two-state model replaying two cases out of 88. Every check passed: the argmax was
interior, nothing was non-finite, there was no pole, and the function was bounded. Adding
one `Degenerate` naming the smallest surviving model turned the same probe into a refusal.
On a curated business-process (permit) log the smallest surviving model scores `0.1496`
against an optimum of `0.8606`, so the missing declaration was invisible on that fixture
*by construction*. One fixture cannot show that a declared list is load-bearing; two can.

`Structure` is what the prober relies on instead, and the shift it encodes is the point: a
caller supplies the *shape of the search space* rather than a list of guesses. "The
smallest thing that still counts as an answer" is a property of the space, not a value
someone remembers to write down. One callable, `size`, says how much answer a point
represents — handoffs kept, states in the model, features selected — and every degenerate
input follows mechanically: the emptiest viable point, the fullest, the empty one, the box
corners.

### `emptying-is-free` is the stronger of the two derived checks

`degenerate-optimum` runs the enumerated points through the same test a declared one got.
It fires the moment the emptiest viable answer ties the grid maximum.

`emptying-is-free` is the general statement, and it is provable rather than sampled: walk
every grid-adjacent pair where `size` falls, and require the score to fall across at least
one of them. An objective where shrinking the answer never costs score has its optimum at
whatever the space admits last, whatever that happens to be. The strict form is deliberate
— the check fires only when the score *never* falls — because a fraction-of-pairs
threshold would be a number fitted to whichever fixture was in hand.

Where it is stronger, honestly. Mostly the two coincide, and they have to: if emptying
never costs score then the score is non-decreasing toward emptier, so the emptiest viable
grid point holds the grid maximum and the point test fires as well. They part when the
grid maximum is held by a point that is not a viable answer at all — then no viable point
can tie it, the point test is quiet, and only the walk sees that shrinking a real answer
is free. That is an objective rewarding the return of nothing, which is not exotic.

The other half of the general form's value is not about which check fires. It is what the
finding *says*: the score is monotone in emptiness across every pair walked, rather than
one coordinate that happened to win. A caller who fixes the winning point without fixing
the monotonicity has fixed nothing, and the point test alone would then go quiet.

## Every degenerate check is decision-space only, and that must not be relaxed

The same argument the boundary-max check rests on, applied one layer out.

Metric axes are varied freely and independently, so "hold everything else and shrink this
term" is always available on the grid and is usually the right answer — the ideal corner
is perfect coverage using no edges, which is exactly an empty answer scoring the maximum.
In decision space the axes are what the optimizer moves, the metrics are coupled functions
of them, and "shrink the answer" is a real move with a real cost. Measured:
`emptying-is-free` does not fire on the permit log's composed objective, where the score
falls from `0.8184` at the argmax to `0.7680` one grid step toward emptier models, and
does fire on the agent-transcript log's, where every step from the argmax to a single-edge
model scores `0.0444`.

The tempting relaxation is to apply the space discipline to `emptying-is-free` and the box
corners but *not* to the size-derived points, on the reasoning that "the emptiest point
that is still an answer" is meaningful on any axes. That is unsound in two ways at once,
and both have been measured, so the enumeration is decision-space only and metric space
says so in a note.

**It is order-dependent.** On the composed objective's metric grid, 21 points tie for the
smallest non-zero `edge_share`, scoring anywhere from `0.0` to `0.9744`, so which one
becomes "the emptiest answer" is decided by `product`'s iteration order.

**Tiebreaking on score makes it worse.** That is the obvious fix for the order dependence,
and with free axes the best point at any fixed size is the one holding every other term at
its ideal, so as the grid refines it converges on the ideal corner: `coverage=1.0,
edge_share=0.05` already scores `0.9744` against a grid maximum of `1.0`. A sound objective
would eventually be refused for having a good optimum.

Why this reads as wrong at first: "an empty answer scoring the maximum" looks like a defect
right up until you notice that free metric axes make it the definition of a *good*
objective. Only the space discipline separates the two, and it is deterministic.

## The adversarial half, and why a search is not a declaration

Enumeration only finds degenerates that follow from the declared structure. `search` is
the seam for the ones nobody enumerated: a callable handed a `Brief` — the objective, the
axes, the grid it was swept on, the ceiling, and the structure — that returns `Degenerate`
candidates. `pneuma.detect.adversary` implements it with a fan-out of LLM adversaries and
a judge panel; nothing in this module imports it, and `probe` works with `search=None`.
See [adversary.md](adversary.md).

The division of labour is deliberate. Whatever a searcher claims, the prober re-evaluates
the candidate and only records a finding if it actually reaches the ceiling, so the
arithmetic half of adjudication happens here, in code that cannot be argued with. The
searcher owns the half that is a judgment call: whether the input is *worthless*.

## Naming the cause, not only the symptom

Every check above is downstream of one thing. `emptying-is-free` and `degenerate-optimum`
both say *a degenerate input wins*; neither says why. Measured on the agent-transcript
log, the why is that one term of the metric has no discriminating power on that dataset:
whole-trace replay coverage through the real `grade` path reads `0.0227` at every
threshold from 1 to 44. With coverage held constant the score is a monotone function of
emptiness by algebra, so the winner is whatever the space admits last. A caller told only
that a point wins can fix the point; a caller told the coverage term never moves knows the
fix is the measurement.

`Component` and `_check_components` are that, and they are deliberately the *same
primitive* `vacuity` reports a rule through. A rule catching zero reachable states cannot
tell a compliant run from a violation. A term whose value never moves across the swept
space cannot tell a good answer from a bad one. Both are checks that pass without ever
having been in a position to fail, both need the verdict to be three-valued so an
abandoned measurement is not a pass, and `discrimination.py` is what they have in common.
See [discrimination.md](discrimination.md), which also records where the unification
stops.

## What this cannot do

`probe_feedback` checks that the feedback text states the quantity selection uses, and
that it states it on every round rather than some. It cannot check that the prose's
*advice* points uphill. See that function for the reasoning.

`components` are declared, not decomposed. Splitting an arbitrary callable into terms
needs its source and an algebra over it, so a caller lists what it wants measured. The
declaration is auditable in the way a hand-written `Degenerate` list is not — a term is
evaluated on the same points the objective is, so a term declared wrong reports its own
variance rather than the score's — but a term nobody declares is a term nobody measured,
and the report says so.
