# `casestudy/learning.py` — design rationale

Why the navigator's playbook is learned the way it is. The module docstring states the
invariants; this file carries the arguments.

## The defect this loop is aimed at, and why verification cannot touch it

The live experiment found the real defect, and it is not the one a verification-first reading
predicts. The agent never broke a rule. It *dithered*, cycling between valid states until the
step cap stopped it, in 6 of 10 cases.

No amount of verification helps, because looping is legal. The model-checker proved the
process permits it, and that proof is correct — a process that forbids revisiting a state it
allows you to leave and re-enter is a different process. The defect is in the *choice*, not in
the space of choices, and a choice is a prompt problem.

That is the whole reason `TextGradOptimizer` is here rather than another IR constraint. The
alternative is hand-editing a docstring forever, once per observed failure, with no record of
which edit helped.

## The wiring, and the failure mode it avoids

`LearningNavigator.choose` takes `playbook` as a **real parameter**, so a recalled
`ParameterView` arrives in the call arguments where `collect_nodes` can find it. Hide the same
text on `self` and the gradient has nothing to land on: not a degraded gradient, none at all.
This is `pneuma.method`'s argument in production form — see [method.md](method.md), where the
same constraint is stated from the decorator's side.

The loop itself is deliberately plain: run cases, observe how many looped, phrase that as
feedback in plain English, let the optimizer rewrite the playbook, run again. The rules are
never touched, so verification stays valid without being re-run — the process did not change,
only the advice the agent reads before choosing.

## Why the playbook is a list of addressable entries rather than one string

The blob is the limit, and the mechanism is routing.

A round produces one gradient about one observed failure — "the agent revisited states it had
already passed through". Against a single `guidance` parameter, that gradient is routed to
*all* the accumulated advice at once, and the consolidating model then rewrites whatever it
likes. Advice that was working is paraphrased or dropped for reasons no round measured. The
loop cannot see that happening, because it reads completion rate and completion rate does not
say which sentence bought it.

Splitting the playbook into addressable entries and recalling by *search* fixes the routing.
`TursoMemoryBackend.search` puts `{entry_id: value}` for the retrieved entries in the recall
event's meta; that travels to the reconstructed `ParameterNode` and back out as
`consolidate`'s `retrieved=`, so consolidation edits those entries and leaves the rest
byte-identical. `test_turso_memory.py` asserts exactly that: a gradient about entry A does not
modify entry B. See [turso_backend.md](turso_backend.md) for the storage side.

The query is built from the decision context — the state, the legal moves, whether any of them
is a revisit — so what the agent reads is the advice that bears on the choice in front of it
rather than everything ever learned. That is also a retrieval risk worth naming: advice that is
never retrieved is never reinforced and never corrected, so an entry can sit in the store
being wrong at a decision the query does not describe.

## The safety property, and why it survives the parameter changing shape

The playbook is *advice*. Rules live in the verified IR where a checker can see them, and the
interpreter rejects any transition the IR does not permit. Nothing an optimizer writes here can
widen what the runtime allows; the worst a bad rewrite can do is make the agent slower.

That is the entire reason this loop is allowed to let a model rewrite its own guidance. The
property is structural rather than a policy someone enforces, and it does not depend on the
advice being good — which matters, because the loop's whole premise is that the advice starts
bad. `casestudy/harnesslearn.py` states the numeric analogue of the same property for a
learned *number*, where the enforcement has to be a schema allowlist instead of an IR.
