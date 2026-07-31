# `detect/vacuity.py` — design rationale

Why relaxation is the mechanism, why the four levels are ordered as they are, and why every
bound is reported. The module docstring states the invariants; this file carries the
arguments.

## The question a model-checker does not answer

A model-checker answers one question: did any reachable state break this rule? It does not
answer the question a reviewer actually has, which is whether the rule was ever in a
position to break. Those two look identical from the outside — both print green — and the
whole point of this module is to make them different objects.

The mechanism is a reachability sweep plus *relaxation*. Sweeping the reachable
`(location, assignment)` pairs and counting how many break each rule reproduces the
checker's verdict. Re-sweeping the same rules over a deliberately weakened system answers
the counterfactual the verdict cannot: if the rule still cannot break when the guards are
ignored and the initial values are freed, then nothing about the model's *logic* was
keeping it safe, and the green verdict was about the shape of the graph rather than about
the rule.

## The four relaxations, and the diagnosis each level produces

The level at which a rule first becomes breakable is its diagnosis:

    exact         the system as written. Agrees with the model-checker.
    free_initial  every variable starts at every value in its domain.
    free_guards   transitions fire whether or not their guards hold.
    free_both     both at once. The weakest system the formalism admits.

    broken at exact         the rule can fire; the checker will report it
    only at free_initial    a pinned initial value hid the case  <-- the classic bug
    only at free_guards     a guard is load-bearing; the pass is real
    only at free_both       needs both, so a pinned value is still load-bearing
    broken at no level      the rule cannot fire under any assignment: decoration

## Why the ordering is sound, and why it is what makes the gate correct

The order is not arbitrary. Freeing an initial value is a *sound* widening: it is exactly
what TLA+'s `x \in Domain` does in `Init`, and the model's own guards keep enforcing
themselves throughout. Freeing guards is unsound, since it walks edges the system forbids.
So the sound relaxation is tried first, and a rule that becomes breakable under it is
diagnosed as hidden by a pinned variable even though ignoring guards would also have
surfaced it.

That ordering is what makes the gate correct. A checker pins initial values exactly as the
model does, so a rule rescued only by `free_initial` was never checked at all and its pass
must be withdrawn. A rule rescued by `free_guards` under the model's own initial values has
a guard doing real work, and its pass stands.

`adapter.py` is where the two unsound-vs-sound widenings are actually implemented for the
process IR — `ProcessSystem.free_guards` steps every outgoing transition regardless of its
guards, `free_initial` starts every variable at every value in its domain, and neither
rewrites the process. The relaxation lives in the walk, so the object the checker sees and
the object the detector sweeps are the same object. Reordering the levels here without
reordering the diagnosis there would silently invert the gate.

## Liftability

Nothing here imports its host project. The consumer supplies a `System` (how to start, how
to step) and a list of `Rule`s (name, scope, broken), and gets counts, shortest witness
traces, and a named cause back. Lifting this into another project means writing one adapter
against those two protocols; see `adapter.py` for the process-IR one and treat it as the
file you replace rather than the file you edit.

## Every bound is reported, and relaxed truncation is tracked separately

`limit` caps the states any one sweep will visit, and a sweep that hits it says `truncated`,
which makes `live` and `vacuous` three-valued rather than optimistic. A search that gave up
is not evidence of safety, and recording it as such would rebuild the defect one level up.
See [discrimination.md](discrimination.md) for the shared three-valued primitive.

That applies to the *relaxed* sweeps too, and their truncation must be tracked separately
from `exact`'s, because the levels bind at wildly different sizes. `free_initial` starts
every variable at every value, so with n free booleans its start set is 2^n against
`exact`'s one. A model can therefore finish at `exact` and exhaust the budget at
`free_initial`, and when it does, `free_guards` is never swept at all.

Since `free_guards` is the level that earns a guarded rule its pass, without a separate flag
such a rule reads as "0 violating, search finished, no witness", which is indistinguishable
from decoration. `relaxation_truncated` separates that case out: the cause becomes
`unknown`, `vacuous` stays False, and the witness count stays 0 so the checker's pass is
still withdrawn. Not knowing is not the same finding as decoration, and neither of them is a
pass.
