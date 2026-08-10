# `casestudy/minelearn.py` — design rationale

Why the mining loop learns *two* parameters rather than one, and what keeps it alive when a
rewrite is bad. The module docstring states the invariants; this file carries the arguments.

## The ceiling that motivates a second parameter

An agent asked to write mining analysis with the obvious algorithm named in its prompt
reproduces that algorithm rather than improving on it. `aimine` loses to the fixed
implementation by counting directly-follows pairs ranked by distinct cases, which is exactly
what the frozen code does — because the prompt describes that algorithm as the obvious starting
point and then asks for a judgment call.

Optimising the prompt alone has its own ceiling, and it is a structural one. `advice` is prose
the miner reads before analysing and the loop rewrites it from measured feedback, which works as
a mechanism. But better advice is remembered while better *code* is not: `aimine` has the agent
write its analysis fresh in a sandbox every run, so a helper it got right in round one is
retyped from scratch in round two and can be got wrong. Advice accumulates; capability does not.

`toolkit` is the fix, and it is a `Procedural`: Python source the runtime *executes at sandbox
setup*, so its functions are defined and callable, then *advertises* by signature and docstring
in the prompt preamble. See [toolkit.md](toolkit.md) for what the seed contains and what the
sandbox permits.

## Why code and prose cannot be folded into one parameter

Both folds fail, in different ways, and neither failure is a matter of taste.

**Prose as comments in the code fails, and it is measured.** `procedural_signatures` advertises
top-level `def` lines and their docstrings only. Module docstrings, comments, and module-level
constants are dropped. A policy written as a comment in the toolkit is invisible to the agent
that would have to follow it, so folding this direction silently deletes the advice.

**Code as prose fails harder.** The whole point of `Procedural` is that the sandbox forbids
`exec`, so code arriving as a string variable is inert while code arriving as a `Procedural`
parameter is callable. The fold does not degrade the code parameter, it removes it.

## The crosstalk, and why it is accepted rather than solved

Two simultaneous gradient targets can interfere, and the mechanism is visible in
`TextGradOptimizer._distribute`: one backward model call sees both parameters and routes
feedback to whichever it judges responsible. Nothing forces it to split honestly, so a round
whose feedback is about the cutoff can land on the code, or vice versa.

Two things make that acceptable here rather than merely tolerated.

First, the split is legible in the rendered inputs. `render_inputs` labels a procedural node
`type: code` and a prose node `type: parameter`, and the optimizer's own prompt tells it to
respect each target's description. So the parameter descriptions in this module are written as
routing instructions, not as documentation, and editing one to read like prose documentation
degrades the routing.

Second — and this is what makes the interference a finding rather than a silent failure — the
two parameters are *separately measurable*. `Attempt` records `toolkit_chars` beside
`guidance_chars` per round, plus the count of advertised helpers, and the summary table prints
all three. A round that improved by growing the toolkit looks different from one that improved
by sharpening the advice, and a round where the optimizer wrote a paragraph of prose into the
code parameter shows up as the code growing while the helper count does not.

Collapsing to one parameter would have made that unmeasurable, which is the reason to prefer two
even accepting the crosstalk. How often the routing is actually wrong on a live run is
**unverified**: it needs live calls, and it is stated as unverified rather than assumed away.

## What keeps the loop alive when a rewrite is bad

`Procedural` setup failures raise loudly, by design: malformed or erroring recalled code raises
`ValueError: Failed to load procedural code into the executor namespace` and the whole cycle
dies. Loud is right, and losing every accumulated helper to it is not.

So a rewritten toolkit is *rehearsed* before the round that would depend on it (see `rehearse`),
and a toolkit that fails rehearsal is rolled back to the last one that passed, which is kept in
a `Frozen[Procedural]` parameter the optimizer cannot target. The rollback is recorded on the
`Attempt` and printed in the table, because a loop that silently reverted the thing it was
supposed to be learning reports rounds it never ran the rewrite through.

Rehearsal catches load failures and call-time failures on the seed inputs. It does not catch a
helper that loads, runs, and returns something subtly worse — that is what the score is for.

## Why the rehearsal never replays a suffix, and why that is recorded per round

There is a cheaper-looking way to test a rewritten toolkit: fork the thread the prior attempt
recorded at the first decision the edit alters and replay only the suffix from there. That is
counterfactual suffix replay, and `replay_decision` asks for it every round and declines it every
round. The decline is a measurement rather than a policy, and it is worth having both.

`toolkit_edit_reach` is the measurement, and its answer for this parameter is structural rather
than empirical: any textual change reaches decision **0**. A toolkit is a `Procedural`, so the
runtime executes it at sandbox setup and advertises its signatures in the *first* prompt the model
sees. The suffix after the first altered decision is therefore the whole trajectory — the strong
edit-to-side-effect coupling that bounds the technique — and replay degenerates into a full
re-run. Returning 0 for every real edit is the honest answer, not a shortcut: a cleverer analysis
of which helper changed and which cycle first called it would be wrong twice over, because the
advertisement changes even for a helper nobody calls and the sandbox namespace the whole
trajectory ran in changes at setup regardless of call order.

Two more things follow, and they push the same way. The scratch path is strictly cheaper here,
because `rehearse` makes no model call at all while a suffix from decision 0 would re-take every
model decision. That holds even though `pneuma.model.opus5` enables prompt caching, so a
shared prefix bills at cache-read rates rather than being re-ingested: a discount on a prefix of
length zero is still zero, and the comparison is against a rehearsal that spends nothing. And
`train` runs its rounds through `trace`, which tears its thread down before returning — a
torn-down thread is deregistered, so its id cannot seed a fork — and `train` passes no
`recorded_thread` at all, so there is no live thread to fork even if there were a saving to
capture. `replay_decision` checks `recorded.live` rather than trusting a caller's bookkeeping.
It reports the coupling first when both conditions hold, because that is the durable cause and
the record should name the durable one rather than the incidental one.

So why keep the path at all, and why write the reason down every round? Because a loop whose
replay never fires and a loop with no replay path look identical from outside until the bill
arrives. `ToolkitCheck.replay` carries the `ReplayDecision`, the `Attempt` carries
`rehearsal_path` and `rehearsal_fallback_reason`, and a reader of the training record can tell a
declined replay from a missing one and see which evidence each round's verdict rests on. The
alternative — asserting the vacuity once in prose — leaves the record unable to say it.

The two values are named `SCRATCH` and `REPLAY` after the *path taken*, not after the outcome,
and the fallback reason names the condition that lost. That pairing is what makes the record
auditable: `SCRATCH` alone would be a verdict with no argument in it, while "scratch, because the
edit is globally coupled" and "scratch, because no recorded thread exists" are two different
findings about this loop that a reader should not have to guess between. The same question is
asked and answered the same way one level up, for a number instead of code: see
`harnesslearn.replay_path`.

Where a replay ever does run, two runtime caveats bound what it may assume, both named on
`replay_decision`. A seeded history can carry tool-use blocks for tools absent from the new
thread's schema, which is safe here only because a fork replays the *same* method with the same
schema. And a pending `notify` is worker-side inject state a fork does not copy, so a caller
holding one must run it into the log first or the replay silently drops it.

## Two wiring details the loop does not work without

A recalled value arrives as a **call argument**, because gradient targets are discovered in call
arguments and anything hidden on `self` is invisible to the optimizer. See [method.md](method.md).

And each recall happens **per call**, because a `ParameterView` is emitted once — reuse one
across a batch and only the first traced call carries a gradient target, so the remaining rounds
contribute measurements with no gradient attached.

## The safety property, unchanged by the code parameter

The toolkit runs in the same AST-interpreted sandbox as any other agent-written analysis: no
`os`, no `open`, no `exec`, and only the modules `ANALYSIS_IMPORTS` authorises. What the agent
*returns* is still validated by Pydantic, still model-checked by TLC, and still executed by the
same interpreter. A bad rewrite produces a worse model, or a round that fails rehearsal and says
so, never an unverified one.

## Feedback must name the mechanism, not the score

`Attempt.score` is a harmonic mean of replay coverage and selectivity, and the `edge_share`
clamp inside it is load-bearing: without it, `edge_share` above 1 makes selectivity negative and
the mean becomes a rational function with a pole at `edge_share == 1 + coverage`, where 185 edges
against 99 handoffs and 86.4% coverage scores 319.386 and is selected as best.

That number is also the argument for how feedback is phrased. Telling an agent that 319.386 is a
record teaches it nothing — an attempt that returned handoffs no case ever walked needs to be
told *that*, in those terms, or the next round optimises the same pathology with more
confidence. See [objective.md](objective.md), where the same score is the worked example for why
clamping is defence in depth and never the repair.
