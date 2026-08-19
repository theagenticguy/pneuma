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

It also rejects a transition, invariant, or variable whose name collides with one
the TLA+ renderer defines itself — `Spec`, `Init`, `Next`, `Termination`, `pc` and
the rest. A generated name is data too, and a colliding one would silently redefine
the renderer's own identifier. That failure is loud either way, but a Pydantic
message naming the collision beats a TLC parse error naming a line in a file nobody
wrote. State names are exempt, because a state renders only as a string and never
as a definition.

**TLC** exhausts the bounded state space. In the claims example it visits 10
distinct states and proves `LargeNeedsTwoApprovals` holds in all of them. Add one
plausible `Expedite` edge and it returns a 4-step counterexample naming the exact
transition.

**The interpreter** treats the agent as an untrusted oracle. The model proposes a
transition; the interpreter takes it only if it leaves the current state and its
guards hold. Illegal proposals are rejected and re-offered up to a budget, then the
run fails rather than guessing. Invariants are re-checked after every step, so a
disagreement between the verified model and the running code surfaces immediately.
It also dispatches the work inside each state it enters, and halts a run that is
going nowhere; both are their own sections below.

**Hypothesis** drives that interpreter with adversarial proposal sequences and
shrinks any failure to the shortest one. It reaches the same conclusion as TLC on
the bugged process, by sampling code TLC never executes.

## Choosing and working are two callbacks, not one

`decide` covers the choice *between* states. The work *within* one reaches the run
through a second hook, `on_enter`, called once per state occupied — and the two
must not be collapsed. `decide` is consulted only where there is a choice, because
a state with one enabled transition is stepped through without asking anybody, so
dispatching per-state work from inside the decider would skip every deterministic
step, which in a mined process is most of them. Doing it afterwards over the
finished trace would run the work in the right places and the wrong order, after
the choices it was supposed to inform.

The hook takes the state name and nothing else. Whoever installs it already holds
the `Process` and can look the state up; passing the object, the variables, or the
partial trace would widen the surface of the one file in this package meant to stay
fixed, to save a dictionary lookup. `ProcessAgent` is what installs a dispatcher
into it, so one agent both walks the process and does the work inside each state it
enters — see `docs/design/process_agent.md` for that half.

## The failure no rule can express: going nowhere legally

Every layer above catches something a rule forbids. The live run found a failure
with no rule to break: the agent cycled between valid states until the step cap
stopped it. That is legal, and TLC is right to say so — a process that forbade
re-entering a state it lets you leave would be a different process.

So the interpreter counts it instead of forbidding it. `max_revisits`, defaulting
to `DEFAULT_MAX_REVISITS` of 5, is how many *consecutive* re-entries into
already-visited states a run may make — consecutive meaning no state it had never
seen was reached in between — before it raises `NoProgress`. Five is conservative:
a real detour re-enters a state once or twice, while dithering re-enters until the
budget runs out. Pass `None` to disable the halt and let a dithering run spend its
whole budget.

Three details in that exception are the design, not the implementation.

**It names the limit it hit.** `NoProgress` is neither `Deadlock` — every
transition here was enabled and legal — nor the exhausted-budget `ProcessError`,
because the budget was *not* spent: the run declared it would have been. An outcome
that stops short must say which bound stopped it, or a reader cannot tell a finding
from the harness's own cap. That is the same three-valued honesty `detect/` reports
through.

**It is still a `ProcessError`.** Callers that catch `ProcessError` to count
blocked cases see exactly what they saw before, so no accounting silently changes
shape when the halt starts firing.

**A revisit is data, not prose.** The prompt has always marked an already-visited
target for the *model*, and nothing else could hear it — the fact lived in a string
and died with it. A typed `Revisit` records the state, the step, and the
alternatives the run passed up, on `Run.revisits` for afterwards and readable
mid-run through `revisits()` for during. That is what lets a retrieval query say
"this case has already dead-ended twice" rather than re-deriving it from the path,
which is the seam `casestudy/learning.py` learns through.

The halt is a cost control, not a repair. The agent still dithered; it just stopped
paying for it. The repair is a prompt problem, and it lives outside this package.

## Liveness is a second question, asked only when asked for

Everything above is safety: does any reachable state break a rule. Whether the
process *finishes* is a different question, and `tla.check(..., liveness=True)` is
where it is asked — it defines `Termination == <>(pc \in {terminals})` and attaches
`WF_vars(Next)` to the spec.

Opt-in rather than default, and the reason is honest bookkeeping. Two mined models
in the case study have real rework loops and are asserted sound today. They are
sound *on safety*, and switching liveness on by default would turn those green
results red without anything about them having changed. The fairness attached is
deliberately weak: `WF_vars(Next)` rules out stalling forever while a move is
enabled and nothing more, so a cycle among real transitions still refutes
`Termination` — which is the correct verdict for a process that can loop forever,
and the formal counterpart of the `NoProgress` halt above.

Two ways to misread a liveness result, both worth stating. Safety masks liveness:
if an invariant also fails, TLC stops on the invariant and never evaluates the
temporal property. And a violation is reported under the name `TemporalProperty`
rather than `Termination`, because TLC does not name which temporal property
failed.

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

