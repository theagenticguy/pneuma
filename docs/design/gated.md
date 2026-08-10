# `gated.py` — design rationale

Why the gate is a post-condition rather than a check the loop runs, and why the two ways of
proposing under a gate are deliberately not the same mechanism. The module docstring states the
shape; this file carries the arguments and the measurements behind the three choices a reader
would otherwise have to guess at.

## What was lifted, and what stayed behind

`casestudy/harnesslearn.py` built an agent that proposes the harness parameter its own score is
then computed with, and put the detectors in front of it as a gate. The idea generalises. What
does not generalise is the gate: `harnesslearn.admit` composes an objective, runs a resolution
sweep over the whole threshold range, mines a process model, and asks a reachability sweep
whether any compliance rule can still fire — polars, pm4py, and three of this project's own
packages. `tests/library/test_boundary.py` forbids all of that on the library side, and rightly:
the boundary's measurable form is that no library module needs a dataframe engine.

So the split is between the skeleton and the judgment. `GatedProposer` owns the post-condition,
the ledger, the wiring guard, and the beam; the gate arrives as a value. That is the difference
between a base class with one real subclass in the application and a base class whose contract
can be exercised in three lines — the tests here construct their gate as
`lambda candidate: Threshold(candidate.value, floor)` and need no fixture at all.

## Why a post-condition rather than a check after the call

A manual check after the call is a check the loop can forget, and it is forgotten in exactly the
loops that are under pressure. A post-condition cannot be skipped: `ai_thread` runs every
validator against the result before the cycle returns, and any exception a validator raises
becomes the text of a `[VALIDATION ERROR]` user turn the *next* attempt reads
(`ai_thread.py:640-664`). Refusal is therefore the default rather than a step, and the gate's own
report is the re-ask feedback — the model that has to fix the proposal is handed the reason, in
the reason's own words, with no glue written by the caller.

Measured end to end (`test_the_rejection_reaches_the_model_as_the_next_attempts_prompt`): a model
scripted to propose a rejected value then an admitted one is called exactly twice, and the second
call's context contains

    [VALIDATION ERROR]
    Your previous response failed validation with the following errors:
    - value 1 is below the floor of 5

    Propose a different candidate.

    Please try again and ensure your output satisfies all requirements.

which is `report_text()`, then `REASK`, inside the runtime's own wrapper. `REASK` is a class
attribute because the instruction is domain wording — `HarnessProposer` says which direction on
its weight axis causes the pathology it just refused — while the placement is the base's.

## Why a bug must not be able to wear a verdict's clothes

The runtime reports *every* exception out of a validator to the model as a validation failure. It
has no way to distinguish a considered refusal from a `KeyError`, and it should not try: from
where it sits they are the same event. But the consequences differ completely. A refusal is
information the model can act on; a bug is not, and a loop that treats one as the other spends
every retry on something the model cannot fix and then reports that the gate never admitted
anything — the least debuggable failure shape available.

`admits` therefore wraps the gate call and re-raises an internal failure with wording that says
it is internal, and — just as load-bearing — does not append it to `rejected`. The ledger stays
readable as refusals and nothing else, which is what makes it usable as evidence that the gate
has teeth. `judge` raises the same wording on the beam path, so a fault reads the same way
whichever path found it.

There is a third failure in this family, and it is the one a gate taken as a value brings with it.
A gate may be async, and an async gate on the synchronous path returns a coroutine. Every coroutine is
truthy, so `not verdict.ok` would raise `AttributeError` at best and, for any verdict-shaped
proxy, silently admit everything — a gate that appears wired and refuses nothing. `admits`
closes the coroutine and raises "a fault in the wiring", and `judge` awaits it. Two paths, one
each, both tested.

## Why the collision guard exists at all, and why it checks only the first parameter

`ai_thread` passes the result to a validator positionally and then injects, by keyword, every
bound argument whose name appears in the validator's signature (`ai_thread.py:1016-1018`). Both
rules are useful. Together they are fatal for the *first* parameter, which already holds the
result: the same slot filled twice raises `TypeError: got multiple values for argument`, the
runtime catches it, and the model is told its output failed validation for a reason that names
nothing it can change.

`HarnessProposer` avoided this by naming its parameter `response`, and
`tests/app/test_harnesslearn.py:611` pins the convention by comparing signatures. A convention
whose violation is one careless rename away and whose failure is silent is the kind that should
be a guarantee, so `_check_no_collision` runs at wiring time from both entry points and names
both sides of the collision.

The narrowness is the part worth recording. The obvious guard covers *any* of the validator's
parameter names, and that is measurably too strict. Two proposers, identical but for their
validator signature:

    check(self, hint)                 propose(self, hint)  →  TypeError, swallowed as a
                                                              validation failure, then
                                                              AIFunctionError after retries
    check(self, response, hint)       propose(self, hint)  →  works; `hint` is injected

The second is the mechanism `postcondition.py`'s own docstring describes, and forbidding it would
outlaw a validator that legitimately wants a bound argument beside the result — for no benefit,
since the runtime handles that case correctly. So the guard covers the position the result
occupies and nothing else, and there is a test on each side of the line.

## Why the beam path is a different mechanism, not a loop over the first one

`propose_k` forks k branches off one seeded context, takes one proposal from each, and filters
with the gate directly. It deliberately does not attach `admits` to the branches.

Attaching it would let each branch retry until admitted, and k would stop meaning what it says.
A k of 3 could be twelve model calls and three admitted candidates that reveal nothing about the
width of the search, because the branches that were bad would have quietly become good ones. One
shot per branch keeps k a count of branches and keeps `rejected` a measurement of how much of the
beam the gate turned away. `test_propose_k_attaches_no_post_condition_so_k_stays_a_count_of_branches`
asserts the call count rather than trusting the sentence.

The single-thread path retries because that is what a post-condition is for; the beam filters
because that is what a beam is for. The two are not the same operation at different widths.

### Why the seed is a sequence of cycles rather than a `notify`

The obvious way to give every branch shared context is to `notify()` the root before forking.
It does not work, and the failure is silent: a forked branch does not see it.

`notify` routes to the worker's inject buffer (`handle.py:120-131` → `coordinator.notify` →
adapter), which is per-thread live state, while `fork` copies the *event log*. Measured — after
`await root.notify("SEEDTEXT")`, `reconstruct_messages(await h.events(root.id))` is empty, the
root's next cycle sees the text, and both forks see nothing. A seed *cycle* run before the fork
behaves as expected: every branch's first model call contains it, and each branch's own turn
reaches only that branch.

So seeding means running the method, which is also the more honest shape — what a branch inherits
is a real turn it could have produced rather than an assertion injected beside the conversation.
Each seed entry is one cycle's keyword arguments, because that is `MethodThread.run`'s contract
and a thread hosts exactly one signature.

### Why the branches run serially, and what the seed turns out to be worth

The loop over `threads` is sequential, and that is a choice rather than the shape a loop happens
to take. A beam is the one place in this library where every request is *deliberately* built to
share a prefix — that is what "byte-identical up to the fork point" means — and a shared prefix
is exactly what a provider cache bills at a discount. Serial ordering is what makes the discount
reachable: branch 0's response, which writes the cache, completes before branch 1's request is
sent. Fanning the branches out with `gather` would have them all miss on a cache none of them had
written yet, and pay full price to arrive at the same k proposals.

The discount is not free by default, and the reason is worth recording because it is invisible.
Bedrock's Converse API caches nothing for Anthropic models unless the request carries an explicit
`cachePoint` block — a byte-identical prefix is re-ingested at full input price with no warning
that anything was missed. `pneuma.model.opus5` therefore builds with `CacheConfig(strategy="auto")`
by default, which has the runtime append one cache point to the last user message of every request,
putting the whole conversation prefix inside the cached span. Measured on a live `k=2` beam with a
~22k-token seed: 22,161 of roughly 22,300 input tokens on the second branch were served at
cache-read rates, about 99% of the prefix
(`test_fork_beam_branch_two_reads_the_cache_branch_one_wrote`, behind `PNEUMA_LIVE_CACHE=1`).

Two properties of the arrangement keep it honest. It is a cost property and never a correctness
one: a model built with `cache=False`, or any model without a cache point at all, gets beams that
are correct and uncached, so nothing in `propose_k` branches on whether caching happened. And the
failure mode is silence rather than an error — a prefix shorter than the
provider's minimum cacheable length (a few thousand tokens) has its cache point ignored, not
rejected — which is why the offline tests assert on the *request* the model constructs through
`format_request` rather than inferring the wiring from a bill.

This is also where the previous subsection's decision pays a second time. A seed run as real
cycles is a seed that lives in the event log every branch replays, which is precisely the span a
cache can serve; a seed injected as a pending `notify` would be per-thread live state that the
forks never see, so there would be nothing shared to cache even if the branches were identical in
every other respect. The honest shape and the cheap one are the same shape.

### Why the unwind is a `finally` and why that is safe

Every thread the beam created is retired in a `finally`, so a gate fault on branch 2 does not
leave three threads running. The unwind can be a plain loop rather than something defensive
because `MethodThread.retire` is idempotent against the runtime and not merely against the
object: it suppresses `ThreadNotFoundError`, which is a `KeyError` and would otherwise sail past
every handler written for `RuntimeError` and abort the unwind halfway — leaving alive exactly the
threads the loop existed to release.

Retirement is asserted in the tests from `await coordinator.list_threads()`, the runtime's own
registry, rather than from `MethodThread.live`. A lifecycle wrapper's local flag can desync from
the runtime, so the flag proves the object *thinks* it is retired, which is the weaker claim and
the one that survives a broken `finally`. Deregistration is complete by the time `retire()`
returns — measured across six trials with a 5ms poll loop, always on the first poll — so the
assertion needs no waiting.

## What this module deliberately does not do

No score, no optimizer, no memory. A gate answers *admissible or not*; turning admitted
candidates into a scalar an optimizer climbs is a separate concern with a trap of its own, and
`harnesslearn` documents that trap concretely: the objective's own peak is *maximised at the
pathological end* (0.9855 at `weight=0.0`, where the empty model wins, against 0.8184 at the
honest seed), which is why `Admission.quality` is built from detector counters instead. A base
class that computed a scalar would be imposing one shape of that decision on every subclass.

`propose_k` accordingly returns every admitted `(candidate, verdict)` pair in branch order and
imposes no ordering. Which admitted candidate is best is the caller's to define.

It also does not ask whether the gate is any good — whether passing it means anything, or whether
everything it admits is one answer wearing several coats. Those are `detect/gaming.py`'s questions,
and the split is not tidiness: `probe_gate_fitting` takes an `Evaluation`, a scalar, because
finding a candidate near a gate's *maximum* whose disjoint held-out evaluation sits near the
minimum needs an ordering over candidates that a `Verdict` deliberately does not carry.
`probe_duplicate_mechanisms` takes a boolean `Checker` and so does compose with a gate directly —
`lambda candidate: gate(candidate).ok` is the whole adapter — which is the useful measurement to
run against a `rejected` ledger that looks healthy: a gate can refuse plenty and still admit only
near-duplicates.

Neither probe belongs on this class, and the reason is the boundary the first section drew.
`gaming.py`'s only pneuma import is `.discrimination`, which is what keeps it liftable;
`gated.py` importing it to offer a `self.audit_gate()` convenience would point the dependency the
wrong way and hand every subclass a method it has no held-out evaluation to call.
