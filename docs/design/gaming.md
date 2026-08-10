# `detect/gaming.py` — design rationale

Why a passing gate and a diverse-looking accepted set are the same defect as a rule nothing
can break, and why the two probes for them live in a new file rather than inside
`objective.py`. The module docstring states the invariants; this file carries the arguments.

## The question

`vacuity` asks whether a rule was ever in a position to break. `objective` asks whether a
scoring function rewards refusing to answer. This module asks the version of that question
that shows up one level out, where a check is *satisfied* rather than passed:

`probe_gate_fitting` takes the gate an optimizer is selected on plus a `held_out` evaluation
the gate never saw, and looks for a candidate that sits near the gate's maximum while sitting
near the held-out minimum. Such a candidate is an `Exploit`, and its existence means the gate
rewards fitting the gate — passing it is decoration.

`probe_duplicate_mechanisms` takes a `Checker` and a pool of items, then compares every
accepted pair under a `Similarity`. If every pair is a near-duplicate, the checker's accepted
set is materially one mechanism: it admits one answer wearing many coats, and the breadth of
its accepts is decoration too.

Both report through `discrimination.Discrimination`, the same primitive `vacuity` and
`objective` report through, because all four checks reduce to *can this thing tell its two
cases apart*. See [discrimination.md](discrimination.md).

## Why a new module rather than more of `objective.py`

`objective.py` sweeps one callable over a declared box, and its whole vocabulary — `Domain`,
`Space`, `Structure` — is about that box. These probes take a *pair* of evaluations, or a
checker over an item stream. Different inputs, different sweep, and no shared helper beyond
`.discrimination`, so putting them in the largest file in the repo would have bought
adjacency and nothing else.

The seam discipline is the other half. `.discrimination` is the only pneuma import here and
the rest is stdlib, so `gaming.py` is liftable rather than a seam, and
`tests/library/test_liftability.py` picks it up from the directory listing without being
told: its `liftable_modules` helper globs `detect/*.py` minus the declared `SEAMS`, and its
one hardcoded assertion is the guard-the-guard test naming the four-module set. Adding this
file changed exactly one line there. Nothing under `src/` imports the module and
`detect/__init__.py` does not re-export it, so a consumer reaches it as
`pneuma.detect.gaming`; today the only caller is `tests/library/test_gaming.py`.

## A found exploit settles the verdict even under a bound

This is the one place the module deliberately departs from how the primitive is normally
driven, and it inverts a polarity.

In `vacuity`, a truncated sweep with no violating state must not read as decoration, because
the violation may sit in the tail nobody visited. So `withheld` non-empty plus nothing
separating gives None. Here the *finding* is the witness. An `Exploit` is a concrete
candidate at the gate's top with nothing behind it on held-out, and truncation cannot
fabricate one. So `GateFitting.discrimination` reports `separating=0` with `withheld=()` when
`exploits` is non-empty — settled False — mirroring `vacuity` reporting a witnessed violation
as True under a truncated sweep. The alternative, letting the bound survive into the verdict,
would let a caller shrink `budget` until every gamed gate read as unsettled.

The same asymmetry runs the other way on the positive side, and it is the weaker one.
`contained` counts near-minimum held-out candidates the gate kept *below* its top band —
the gate demonstrably telling a worthless candidate from a good one — but one exploit
anywhere defeats any number of them, so containment is evidence only when the sweep
finished. Hence `separating=0 if self.withheld else self.contained`: under a bound with no
exploit, the contained candidates are discarded rather than allowed to settle True while the
exploit sits in the unexamined tail.

## The bands are fractions of the observed spans

`edge_fraction` (default `DEFAULT_EDGE_FRACTION`, 0.05) is a fraction of `max - min` over
the scored pool, not an absolute score. A gate scoring in [0, 1] and one scoring in
[0, 10000] have to draw "near the maximum" at the same relative place, or the probe only
works in whichever unit system its author had in hand.

The degenerate case is a span of zero, and the tempting handling is wrong. A gate that scores
every candidate identically has no top band; drawing one anyway makes every candidate an
exploit or none of them one depending on float luck. So `gate_span <= 0.0` and
`held_out_span <= 0.0` each become a named `withheld` reason instead — unsettled, which is
neither a pass nor a finding. `_score` follows the same rule for individual candidates: a
raise or a non-finite value makes a candidate unscorable and counted, never silently skipped,
because an infinite gate score would otherwise be the top of every band.

For `probe_duplicate_mechanisms` the analogous constant is `NEAR_DUPLICATE_JACCARD` at 0.85,
which is Shepherd's near-duplicate threshold from the CRO reflection contract this task came
from, kept as the default so a caller who says nothing gets a measured value rather than an
invented one. `token_set_jaccard` is the stdlib default under it: lowercased token *sets*, so
neither punctuation nor reordering makes two accepts different mechanisms. Both are
arguments, and `similarity` is the seam for a caller with a real embedding.
`DEFAULT_ITEM_BUDGET` caps the accepted set as well as the pool, so the pairwise comparison
is at most C(512, 2) calls rather than a surprise quadratic on a caller's stream.

## What the caller owns, and this module's weakest point

`held_out` is supplied, and its disjointness from the gate cannot be checked here. A
held-out evaluation that is the gate under another name passes every gate by construction —
which is precisely the defect class this module exists to catch, one level up. There is no
mechanism here that would notice. The docstring says so and the report states what was
compared and nothing more; the honest position is that the disjointness is a reviewable claim
at the call site, like `Domain.bounded_by` in [objective.md](objective.md).

`most_distant` exists for the same reason `Exploit` carries its candidate: a decoration
verdict that does not show how close the accepted set ever got to diverse leaves the caller
re-running the search to see the defect.

## How the tests pin it

`tests/library/test_gaming.py` writes every gate, held-out evaluation, and checker out as a
lambda or a closure over plain data, so the mechanics are checked with everything else
absent. Two planted fixtures are the ground truth: a length-only gate against a keyword
held-out scorer, gameable by padding, and a checker accepting only paraphrases of one
sentence.

Each probe is pinned at all three verdicts — works, decoration, and could-not-tell naming the
limit — because a probe that cannot produce one of the three would itself be a check that
cannot fire. Beyond those six, the tests pin the arguments above directly: an exploit beating
a hit budget, a flat gate reading unsettled, scale-freeness by re-running the same pool
against the same gate multiplied by 10,000 and requiring the same verdict, a caller-supplied
`similarity` overriding the default, and `most_distant` plus the verdict word appearing in
`report()`. One test guards the fixtures rather than the code: it asserts the paraphrase
floor sits at or above the threshold and the outsider ceiling below it, so a drifted fixture
fails loudly instead of failing the decoration test for the wrong reason. Every planted
invariant was broken once during authoring to prove its guard could fail, per this project's
standing rule.
