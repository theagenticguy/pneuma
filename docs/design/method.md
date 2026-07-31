# `method.py` — design rationale

Why `@ai_method` exists alongside `@ai_function`, and what the object-oriented route costs.
The module docstring states the three losses and the trick that recovers them; this file
carries the argument.

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

## What this does not fix

`@ai_method` inherits the decorator's constraints. The docstring is interpolated, so a brace
in prose is a formatting error, and a prompt that genuinely needs branching still needs the
branch somewhere. The honest position is that the branch is rarer than it looks: most of what
looks like prompt control flow is per-instance state, and per-instance state is what `self`
is for.
