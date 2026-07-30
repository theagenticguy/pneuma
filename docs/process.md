# Verified processes, executed by agents

A mined business process becomes one typed artifact. Three consumers read it, two
of them verifiers that check different things, and the third the thing that runs.

```
              ┌─ tla.py ──────────► TLC: exhausts a bounded abstraction
IR (Pydantic) ┼─ interpreter.py ──► runs it, validating every agent choice
              └─ properties.py ───► Hypothesis: samples the running code
```

## Why data and not code

The model emits an instance of `Process`. It never emits Python.

Generated code cannot be checked before it runs, needs a sandbox, and puts the
thing you execute outside review. Generated data validated against a Pydantic
schema is checkable before anything happens, and the interpreter that acts on it is
written once and read once. `Guard` and `Effect` are deliberately not expressions:
a guard compares one variable to a literal, an effect assigns one. That is what
makes the TLA+ translation direct and keeps the state space small enough for TLC to
exhaust.

Natural language keeps its place. `Guard.stated_as` carries the sentence the
condition came from, `State.description` reaches the agent's prompt, and neither
reaches the verifier. The formal part is checked; the prose explains.

## What each layer catches

**The IR itself** rejects a dangling transition target, a guard on an unknown
variable, a guard value outside its domain, and a process with no terminal state.
These are typos in generated data, and catching them here means a TLC failure is
always a real property violation.

**TLC** exhausts the bounded state space. In the claims example it visits 10
distinct states and proves `LargeNeedsTwoApprovals` holds in all of them. Add one
plausible `Expedite` edge and it returns a 4-step counterexample naming the exact
transition.

**The interpreter** treats the agent as an untrusted oracle. The model proposes a
transition; the interpreter takes it only if it leaves the current state and its
guards hold. Illegal proposals are rejected and re-offered up to a budget, then the
run fails rather than guessing. Invariants are re-checked after every step, so a
disagreement between the verified model and the running code surfaces immediately.

**Hypothesis** drives that interpreter with adversarial proposal sequences and
shrinks any failure to the shortest one. It reaches the same conclusion as TLC on
the bugged process, by sampling code TLC never executes.

## The trap that bit us twice

A verification that passes without visiting the case you care about is worse than
no verification, because it produces a green result.

`amount_band` was originally pinned to `"small"`. TLC reported success over 5
distinct states, having never once entered the large-claim branch the invariant is
about — the property held **vacuously**. Making the variable free (`initial=None`,
rendered as `\in` over its domain rather than `=`) took it to 10 states and made
the check meaningful.

The Hypothesis layer had the identical bug independently: the state machine always
started from the first assignment, so `machine_for` reported no violation on a
process that provably has one. `@initialize` sampling the starting assignment fixed
it. Same mistake, two layers, and in both the symptom was a passing test.

## Where the boundary sits

Formal methods assume fixed semantics, and an LLM samples from a distribution, so
"for all inputs" does not survive contact with the model. You cannot prove
properties of the agent. You can prove properties of the harness, and then refuse
to let the agent leave it.

That is the whole design: verify the skeleton exhaustively, property-test the
interpreter that walks it, and validate the model's output at the one point where
it touches the verified structure. The residual trust is a small IR a person can
read and argue with.

## Costs worth knowing

TLC's state space grows with the product of variable domains, so the IR you verify
stays coarser than the one you execute, and keeping the two honest is real work.

Hypothesis sampling is luck-bounded. Only about 5% of random walks reach the
violating end state in the bugged claims process, because `SeniorApprove` usually
fires first and satisfies the rule legitimately. That test needs 1500 examples to
be reliable, and it is exactly why exhaustive checking earns its place alongside.

Every agent-in-the-loop example costs a model call, so full fuzzing belongs on the
deterministic layers. The interpreter helps by never consulting the model when only
one transition is enabled: in the claims run, 1 of 3 steps is a decision.

Hypothesis cannot run inside `code_execution_mode=LOCAL`. That sandbox allows
pure-computation stdlib only, and Hypothesis needs its own tracing and decorator
machinery. It lives around the AI function, not inside it.
