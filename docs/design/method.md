# `method.py` — design rationale

Why `@ai_method` exists alongside `@ai_function`, what the object-oriented route costs, and why
the live unit built on top of it is one method rather than one agent. The module docstring states
the three losses and the trick that recovers them; this file carries the argument, and then the
consequence the same argument has for a capability that has to stay alive across cycles.

## The route this is a reaction to

`agent.py` takes the object-oriented route to its logical end and pays for it. An `Agent`
subclass is addressable by peers, and to stay addressable it compiles every agent down to a
single `str` parameter. That erasure is not a detail of the implementation — it is what
being uniformly callable costs when the uniform shape is a string.

The decorator paradigm has the opposite property. `@ai_function` operates on a function, so
the function's signature, its docstring, and its call arguments are all still there at
compile time, and each of those three carries a capability the library depends on.

## What the single `str` erases, one capability at a time

**The typed contract.** `AIFunction` is a `ToolProvider`, and `load_tools` builds its schema
from `inspect.signature(prompt_fn)`. Composition is this library's whole point — an agent
*is* a typed tool another agent calls — so the schema is the interface between agents. A
decorated function offers `plane: Literal[...]`, `window: str`, `max_records: int = 20`; an
`Agent` subclass offers `request: string`. The second is callable but not checkable, and the
caller learns nothing about what the callee will accept until it fails.

**The docstring as prompt template.** The decorator interpolates the docstring with the
call's bound arguments, so the prompt is declarative text sitting next to the signature it
is parameterised by. `Agent.brief()` builds the prompt with string concatenation in Python
instead. The prompt then becomes control flow: conditionals decide what the model reads, and
reviewing the prompt means reading a function rather than reading the prompt.

**Learnable parameters.** `TextGradOptimizer` routes textual gradients into `ParameterNode`s,
and those are discovered by `collect_nodes((args, kwargs))` over the *call arguments*. This
is the sharpest of the three, because it is not a degradation but an absence: state hidden
on `self` is invisible to `collect_nodes`, so an `Agent` subclass cannot be optimized at
all. Every learning loop in this project depends on the recalled parameter arriving as an
argument — see `casestudy/learning.py`, where `LearningNavigator.choose` takes `playbook`
positionally for exactly this reason, and `casestudy/minelearn.py`, which restates it as a
constraint on how a recall is wired.

## Why a bound method recovers all three at once

Python removes `self` from a bound method's signature. So `inspect.signature(instance.method)`
is already the typed contract the model should see, with no filtering step and nothing to
keep in sync, while `self` stays reachable inside the docstring template and inside any
validator. The decorator keeps operating on a function; the instance supplies the closure.

The consequence worth stating: per-instance state and per-call arguments end up in different
places *by construction*, and that separation is load-bearing rather than stylistic. A
gradient target must be a call argument to be discoverable. A fixed input a validator needs —
an event log, a fleet topology — belongs on `self`, where the optimizer cannot reach it. See
`HarnessProposer` in `casestudy/harnesslearn.py`, whose post-conditions read `self` while its
learnable weight arrives as an argument; that split is not a convention, it is what the two
mechanisms each require.

## Why the live unit is a method-thread rather than an agent-thread

A compiled method is stateless: awaiting the `AIFunction` spawns a thread, runs one cycle, tears
it down, and two calls share nothing. That is the right default and the wrong shape for anything
that is a *conversation*, so `spawn(name, coordinator)` returns a `MethodThread` whose successive
`run` calls see each other's turns.

The unit is one method and not one agent, and that is forced rather than chosen. A runtime thread
wraps exactly one `Spawnable` — one `AIFunction`, one `prompt_fn`, one typed signature — and there
is no signature that is simultaneously `verify(claim: str)` and `determine(facts: list[str])`. An
agent with three `@ai_method`s therefore holds three threads if it wants three live capabilities,
which sounds like a cost until you notice it is the same argument the whole module rests on: the
thing that cannot be multiplexed is precisely the typed contract the single `str` would have
erased. An agent-thread would have to accept some union of its methods' parameters, and the only
union that always works is a string.

The handoff between siblings is therefore explicit. `spawn(..., seed_from=other.id)` copies
another thread's log into the new one at spawn time, so `determine` can inherit `verify`'s context
without sharing a thread with it — a named point in the code rather than ambient shared state. It
has one honest rough edge: when the two methods have different output types, the inherited history
carries `toolUse` blocks naming a tool absent from this thread's schema. Reconstruction handles
that offline; whether a live provider accepts historical tool calls it was never offered is the
provider's decision, not this library's.

**History stays in the event log, not on `self`.** Accumulating turns on the instance and
re-rendering them into each prompt is the obvious alternative, and it fails for the same reason
`Agent` cannot be optimized: anything on `self` is invisible to `collect_nodes`. It would also
duplicate machinery the runtime already owns — history *is* the coordinator's event log,
reconstructed fresh per cycle — and the two copies would drift the first time a cycle was
summarized, forked, or replayed. So `MethodThread` holds a `ThreadHandle` and nothing else that
resembles state.

**What the typed contract costs here, stated plainly.** A `MethodThread` is not addressable by
`send_message`: that tool only targets threads whose input shape is `STR_PROMPT`, and a
`MethodAgent` compiles to `STRUCTURED` exactly so its parameters stay typed. This is the module's
central tradeoff arriving at its bill, and it is a small one — peers reach a capability as a typed
tool through `agents()`, which is checkable where a chat box is not. `notify()` covers the cases
that still want an inbound side channel: it appends to the log without starting a cycle, so the
next `run` reads it as context.

Two smaller lifecycle decisions are worth recording, because each turns a silent failure into a
loud one. `spawn` requires the caller's `coordinator` rather than defaulting to one:
`AIFunction.spawn()` with no coordinator builds a *private* in-memory coordinator and worker per
call, which would place the thread outside the caller's registry — invisible to peers, no parent
edge, its own event log — and taking the parameter makes that unrepresentable. And `retire` is
idempotent against the runtime and not merely against the object, suppressing
`ThreadNotFoundError` because that is a `KeyError` and would otherwise sail past a caller's
`RuntimeError` handler and abort an unwind halfway, leaving alive exactly the threads the unwind
existed to release. Every operation on a retired thread raises instead of silently respawning: a
caller that believes it is continuing a conversation would get a blank one with the same typed
signature, which is a wrong answer wearing a right one's shape.

`_owner_name` is one function for one reason. The name an instance publishes under is both the
compiled tool's prefix (`{owner}.{method}`) and the subject of every lifecycle error message, and
if those two disagree an error names a thread the caller cannot find in the tool schema it was
reading. `spawn` passes the *compiled* name through to the thread, so an `@ai_method(..., name=...)`
rename wins in both places at once.

## What this does not fix

`@ai_method` inherits the decorator's constraints. The docstring is interpolated, so a brace
in prose is a formatting error, and a prompt that genuinely needs branching still needs the
branch somewhere. The honest position is that the branch is rarer than it looks: most of what
looks like prompt control flow is per-instance state, and per-instance state is what `self`
is for.
