# `casestudy/harnesslearn.py` — design rationale

Why exactly one harness parameter is learnable and the other five are not, and how the
exclusion is enforced by a data structure rather than by discipline. The module docstring states
the property and the numbers; this file carries the arguments and the full measurement tables.

## What makes the harness the interesting thing to learn

Every other loop in this project learns the answer. `minelearn` learns how to mine, and `detect`
grades what it mined. What none of them can do is change the thing doing the grading.

Harness code, not the model's judgment, is where the defects live, so the harness is where the
leverage is. It is also the one place where a bad rewrite is invisible: a broken objective does
not error, it reports a confident number that gets monotonically worse while looking exactly
like training. So this module hands a numeric harness parameter to the optimizer and puts the
detectors in front of it as a gate.

The whole design is one sentence: **a harness parameter may be learned only if the detectors can
reject the pathological settings of it, and the settings that would widen what the runtime
permits are not parameters at all.**

## The one delegated parameter, and the measurement that licenses it

**`coverage_weight`, the objective's component weight.** `Attempt.score` is a harmonic mean of
replay coverage and selectivity, which is the *equally* weighted case of a one-parameter family.
`weighted_score` is that family, and at `w = 0.5` it is not approximately `Attempt.score` but
exactly it: verified equal to 4 decimal places at all 605 points of a grid that includes negative
coverage, `edge_share` above 1, and invented shares outside `[0, 1]`. So the seed is today's
harness, and any movement is measured against the real thing rather than against a re-derivation
that could differ.

It is delegable because the *gate discriminates over its domain*, which is the only test that
matters here and it is a measurement:

    w        objective probe  rules live  gate     threshold selected
    0.0      REFUSE            0/3        REJECT   323
    0.001    REFUSE            0/3        REJECT   323
    0.05     PASS              2/3        REJECT   114
    0.5      PASS              3/3        admit     17
    0.9      PASS              3/3        admit     17

A parameter whose whole domain the gate accepts would be a parameter the gate is not guarding.
This one has a refusing region, and it is where the term that punishes an empty answer has been
weighted out of existence.

The `w = 0.05` row is the best evidence in this file that the two halves of the gate are not
redundant. It passes the objective probe, because nothing about the *score* at that weight is
pathological. It is nonetheless *rejected*, by the safety half only: weighting selectivity that
heavily moves the selected threshold from 17 to 114, and at 114 one of the three derived
precedences becomes unsatisfiable. So the schema's own `ge=0.05` floor sits inside the region the
gate refuses, which is the right relationship between a convenience bound and a safety argument.
The bound is not what makes this safe, and this row is only visible to the second detector.

## The five exclusions, four of them measured

**Not delegated, because it is safety-relevant: the threshold search window.** This is the
exclusion that carries the argument. Widening the window the loop may search buys objective score
and silently destroys the compliance rules:

    window  probe  peak    argmax  derived rules live
      30    PASS   0.8210    20    3/3
      60    PASS   0.8274    19    3/3
     114    PASS   0.8210    24    2/3   <- one rule goes vacuous
     150    PASS   0.8210    23    0/3   <- all three are decoration
     323    PASS   0.8184    17    0/3

Read the first and last columns together. The objective probe says PASS at every window above 30,
because nothing about the *score* is pathological there. Meanwhile at window 150 every derived
precedence is `unsatisfiable`: raising the mining threshold removes the edges that could reach the
forbidden state, TLC explores the whole space, reports no error, and the green verdict is about the
shape of the graph rather than about the rule.

That is a rule made unsatisfiable by a threshold change and then reported green by the checker,
reachable through a harness parameter, and *invisible to the objective probe*. A parameter that
can turn a live rule into decoration while every score-shaped check reports PASS is not a tuning
knob. It stays a constant, and `rule_liveness` measures rule liveness precisely so the exclusion
is enforced by a number rather than trusted.

**Not delegated, because the gate cannot judge it: `sweep_resolution`.** The prober's own grid
density. Measured on the permit log, whose composed objective is sound:

    resolution  2   REFUSE  degenerate-optimum, emptying-is-free, pole
    resolution  3+  PASS

Resolution 2 samples only the two endpoints, and on a sound objective that manufactures a false
pole and a false emptying refusal. The direction of the error is what disqualifies it: a coarse
grid makes the gate *more* likely to refuse, so an optimizer maximising a gate-derived quality
signal would be rewarded for making the gate blind in the other direction, and the parameter
controlling how hard the gate looks would be set by the thing being looked at. Circular, so it is
fixed at the module's declared constant and the `under-sampled` note keeps the bound visible.

**Not delegated, because it does not discriminate: `min_support` for derived rules.** Measured,
and this is a negative result. Across `min_support` in 1, 20, 100, 300, 500 at a fixed mining
threshold, the outcome is identical every time: 7 candidates considered, 3 applied, 2 live, 1
vacuous. The candidate count does move (156 down to 9) but the three strongest precedences hold in
1,303 or more cases on this log, so every support floor below 1,303 selects the same three rules.
Delegating it would produce a parameter that moves while nothing measurable changes, which is a
gradient with no signal in it, and a loop reporting movement on it would be reporting noise.

**Not delegated, because it is a verification bound, not a tuning knob: the vacuity sweep budget.**
`DEFAULT_LIMIT` caps the states a relaxation sweep visits, and hitting it makes `live` and
`vacuous` three-valued rather than optimistic. Lowering it converts findings into `unknown`, which
is *not* a pass but is also not a refusal, so an optimizer that wanted fewer refusals could buy
them with a smaller budget. Same circularity as the resolution, one detector over. See
[vacuity.md](vacuity.md).

**Not delegated, and this one is structural rather than measured: the score floor and the severity
of any finding.** Whether `emptying-is-free` refuses or warns decides whether the loop starts at
all. A parameter that can downgrade a refusal to a warning is a parameter that can turn the gate
off, so no such parameter exists in the schema.

## The safety property, and how it is enforced structurally

`learning.py` states the property this file inherits: learned text is *advice*, never a rule,
because rules live in the verified IR and nothing an optimizer writes can widen what the runtime
permits. See [learning.md](learning.md). The numeric equivalent is:

    A learned harness parameter may change how candidate answers are RANKED.
    It may never change which answers are ADMISSIBLE, and it may never change what
    the gate is able to see.

Three structural enforcements, in descending order of how much they rely on argument:

1. **The schema is the allowlist, and it is enforced by `KeyError`.** `HarnessKnobs` declares
   exactly one field. `MemoryBackend._resolve_field` does
   `current_model.model_fields[parts[-1]]`, so `save("threshold_window", ...)` and
   `fetch("sweep_resolution")` both raise `KeyError` — verified, not assumed. The non-delegable
   parameters are not "protected", they are *absent*, and a gradient cannot land on a name the
   store will not resolve. This is the numeric analogue of rules living in the IR: the
   optimizer's reach is bounded by a data structure rather than by a check somebody has to
   remember to run.

2. **The gate runs on the proposal before the proposal is used, and refusal is the default.**
   `admit` composes the objective at the *candidate* value and probes it. A refusing probe means
   the candidate never reaches a training round. Wired as the post-condition on `propose` —
   `proposer.gated("propose")` is what installs it — so a pathological proposal is rejected and
   re-asked with the detector's own report as the feedback rather than requiring a manual check.
   The placement itself is `GatedProposer`'s; see the section below for the split.

3. **Admission is conjunctive across two independent detectors, and the safety half cannot be
   traded away.** `Admission.ok` requires the objective probe to pass *and* every derived rule
   that was live at the seed to still be live at the candidate. Those are separate detectors over
   separate state spaces, and the window measurement above is why: an objective-only gate reports
   PASS on a setting that makes all three compliance rules vacuous. The rule half is not folded
   into the quality score where a high objective number could outvote it; it is a veto.

## What of that enforcement is this module's, and what belongs to the library

Enforcement 2 above splits cleanly in two, and the split is worth naming because only one half is
about harnesses.

The *placement* generalises. `HarnessProposer` subclasses `GatedProposer`, which owns `admits` as
a post-condition, the `rejected` ledger, the wrapping that stops an internal fault from wearing a
verdict's clothes, and the guard on the post-condition's parameter name. Not one of those is a
statement about mining objectives — they are what any proposer judged by its own gate needs — and
a second copy of them here would mean the next such agent either re-derives them or does without.
See [gated.md](gated.md) for the arguments behind each.

The *judgment* does not generalise, and the reason is measurable rather than aesthetic: `admit`
needs polars, a process miner and a reachability sweep, and the library boundary forbids all
three. So the base takes its gate as a **value** rather than as an abstract method, and this
module passes one in. What stays here is the whole of what is domain: the log and the sweep
settings the gate composes over, `admit` itself, `candidate_of` picking `coverage_weight` out of
the proposal because `evidence` is an auditable artifact and not a thing that is admissible, and
`REASK` — the sentence naming *which direction* on this weight axis causes the pathology just
refused. The base guarantees a rejection reaches the model with its cause named; `REASK` is what
makes the retry worth an attempt, because "rejected" teaches nothing.

Two details of the binding are load-bearing and easy to undo by accident. The gate is declared as
an attribute at the narrower `Callable[[float], Admission]` type and *bound* in `__init__` from
the private `_gate` method, because the base's declaration returns the wide `Verdict` and every
caller here reads `Admission`-only members. Defining a method named `gate` instead would not
narrow it — the base's attribute declaration still wins — which is the only reason two names
exist. And `_gate` names its parameter `candidate` rather than `weight`, because a protocol
member's parameter names are part of its contract for anything callable by keyword: the rename is
what makes this assignable to the base's gate type at all.

## The score channel: a number, not a rewrite

`GradFeedback` carries `text` and `score`. `TursoMemoryBackend._consolidate` routes a numeric
field to `_numeric_update`, a deterministic trust-region search over the domain the schema
declares, and *ignores the text* except as the observation's rationale.

That is the right split for a weight: asking a model to rewrite `0.5` produces a number with a
justification and no evidence, while the score is a measurement of how the current value
performed. Verified offline: a `GradFeedback` with `score=None` on a numeric field leaves the
value byte-identical, so the loop cannot invent movement it did not measure. See
[turso_backend.md](turso_backend.md).

## Why `quality` is built from the gate's counters and not from the objective's peak

The obvious meta-objective is "the inner peak the harness achieves", and it is wrong in exactly
the way this module guards against: peak is *maximised at the pathological end*, 0.9855 at
`w = 0.0` where the empty model wins, against 0.8184 at the honest `w = 0.5`. An optimizer
climbing peak would walk straight into the refusing region and report a record.

So quality is the mean of two things the gate measures directly, both verified to discriminate:

    emptying margin  the share of grid-adjacent shrinking pairs that cost score. Moves
                     0.2500 -> 0.6250 across the weight domain, so it separates.
    rule share       derived precedences still live at the threshold this harness selects.
                     Moves 0.6667 -> 1.0000, so it separates.

Both are `Discrimination`-shaped questions about the harness, which is the same primitive one
level up: a quality signal that is flat across the domain it scores cannot tell a good harness
from a bad one. See [discrimination.md](discrimination.md). `Admission.discrimination` reports
them in that vocabulary, and it is what disqualifies the obvious alternative signal — the
honest-optimum-versus-degenerate separation is exactly 1.0 at every passing weight, so it is flat
and useless.

## Why every round records that it did not replay a suffix

`replay_path` asks, per round, whether the new weight could be judged by forking the prior round's
recorded thread at the first decision the edit alters and replaying only the suffix. The answer is
always from-scratch, and the reason is sharper here than it is for a toolkit.

A weight is consumed at *every* step of everything that judges it. `compose` closes the objective
over it, so the gate's sweep evaluates it at every threshold on the grid, and the propose prompt
interpolates it in its opening lines. Any change therefore alters decision 0, and the suffix after
the first altered decision is the whole trajectory. On top of that the gate is deterministic and
model-free, and the propose cycles run through `trace`, which tears its thread down before
returning: there is neither a saving to capture nor a live thread to fork. Prompt caching does not
change the arithmetic — a shared prefix would bill at cache-read rates, but a prefix of length
zero compounds nothing.

So the decline is structural, and it would be tempting to assert it once in prose and stop. The
reason `Round.path` and `Round.path_reason` exist instead is that a loop whose replay path never
fires and a loop with no replay path look identical unless each round says which it was and why.
The `ReplayDecision` type is shared with `minelearn` deliberately: the same question about a
different parameter should be legible in the same vocabulary, and see
[minelearn.md](minelearn.md) for the code-parameter form of the same finding.

## What this does not claim

The gate admits or rejects; it does not certify. A candidate that passes has been shown not to be
pathological in the four ways `detect` can measure, and not to have killed a rule that was live at
the seed. It has not been shown to be *better*, which is what `quality` is for and what the honest
baseline in `tests/app/test_harnesslearn.py` measures.
