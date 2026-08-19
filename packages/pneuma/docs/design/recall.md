# `recall.py` — design rationale

Why a recalled parameter is declared on the signature and filled at the trace boundary, and why
neither of the two more obvious designs — a descriptor, or a marker that strips the parameter from
the tool schema — survives contact with the runtime. The module docstring states the shape; this
file carries the arguments, the measurements, and the two alternatives that lost.

## What was lifted, and what stayed behind

`casestudy/learning.py`'s `run_batch` retrieves guidance per decision and hands it to `trace` as a
handle, and its docstring spends four paragraphs on *why*: recall freshly per call, pass the view
whole, never interpolate it, never stash it on `self`. Those four sentences are the most
re-derived paragraph in this repo — `casestudy/harnesslearn.py:920-928` restates them, and
`casestudy/minelearn.py` restates them again as a constraint on how a recall is wired. Each is a
rule whose violation is *silent*, and a loop that breaks one reports rounds while learning nothing.

What generalises is the discipline. What does not generalise is the query: `decision_query(state,
enabled, variables)` is about Petri-net markings and stays in `casestudy`. So the split is the
same one `gated.py` made — the skeleton is the library's, the domain judgment arrives as a value.
Here the value is a query string per call, and the skeleton is a marker plus a binder.

## Why the injection happens at the trace boundary and nowhere else

There is exactly one place a recalled value can enter a cycle and still be a gradient target.

`AIFunction.trace` calls `collect_nodes((args, kwargs))` on the way in (`ai_function.py:378`),
emits a recall event for each unemitted view (`:381-383`), runs the cycle, and returns a `Result`
carrying those handles as `inputs` (`:387-392`). `collect_nodes` walks dicts, lists, tuples, and
sets, matching on `isinstance(v, (ParameterView, Result))` and deduplicating by identity
(`graph.py:233-256`). Everything about the graph edge is decided in that one scan, before
`prompt_fn` has run.

Two consequences, and each rules out a design that would otherwise be natural:

**A view created inside `prompt_fn` is invisible.** The scan already happened. So
`self.memory.search(...)` inside the method body computes the right prompt and produces no
parameter node — the loop trains and nothing is optimized. This is the entire reason a binder
exists rather than a line inside the method.

**A view rendered into the template is invisible twice over.** `ParameterView.__str__` returns
`str(self.value)` (`graph.py:183-185`), so `{advice}` in a docstring interpolates cleanly and drops
the edge with no warning. `ThreadHandle.run` unwraps every handle to `.value` before a prompt is
built anyway (`handle.py:109-116`), which is why passing the view *whole* costs nothing: the
template sees the same text either way, and only the handle path also sees the graph.

`collect_nodes` also does not walk pydantic model fields or dataclass attributes, so a view tucked
inside a `BaseModel` argument is invisible too. The binder therefore injects at top level, as a
keyword argument, and nowhere else.

**And one consequence discovered by adversarial review, not by design: the retrieval must run
outside the ambient thread scope.** The runtime opens a `thread_scope` for every executing cycle
(`runtime/worker.py:613`), and `recall`/`search` with no explicit ids emit their
`ParameterRecalledEvent` against whatever scope is ambient, marking the view emitted
(`base.py:275-291`). Call the binder from inside a running cycle — a capability body, a gate, any
orchestration hosted on a thread — and without countermeasures the event lands on the *hosting*
thread's log; `AIFunction.trace`'s own flush then no-ops ("one logical recall, one event"), the
traced thread's log carries nothing, and `build_graph_from_result` reconstructs zero parameters.
Measured: under an active scope the unwrapped binder produced `graph.parameters == []` with the
recall event sitting on the outer thread. Rounds reported, nothing learned, no error anywhere —
the module's own named enemy, reachable through the most ordinary composition. So `Recall.trace`
wraps the retrieval loop in `no_thread_scope()`, deferring emission to the trace-time flush, which
lands it on the thread the gradient must come from. There is a regression test that traces under
an active `thread_scope` and asserts the node survives.

## Why the marker declares and the binder performs

`Annotated[T, Recalled(source, k)]` says where a parameter comes from. `Recall(agent,
backend).trace(...)` is what fetches it. Two objects instead of one, deliberately, and the
alternative that collapses them is the first rejected design below.

The consequence worth stating up front: **the marked parameter stays in the tool schema.**
`Navigator.choose`'s schema still has `advice` in `properties` and in `required`, and there is a
test asserting it. That is not a limitation the binder works around, it is the shape that makes
the capability composable. A peer agent calling `choose` as a typed tool has the guidance in hand
and supplies it; a training loop does not and supplies it from memory. One capability, two callers,
and the marker is metadata for the second that the first can ignore.

## Rejected: a descriptor

The natural object-oriented move is to make the recalled parameter a descriptor on the agent —
`self.advice` fetching from a bound backend on attribute access — so the method body reads it and
no binder is needed.

It cannot work, and the reason is the same one `method.py`'s header is built around: state on
`self` is invisible to `collect_nodes`, which walks the *call arguments*. A descriptor puts the
recalled value in the one place the optimizer provably cannot reach. It would also break the
single-use rule quietly in the other direction — an attribute read twice in one cycle either
returns a cached view (one node for two logical recalls) or performs two retrievals (two events
for one decision), and nothing in the type system says which.

`docs/design/method.md` makes this argument for history and for gradient targets generally; a
descriptor is the same mistake wearing a property's clothes.

## Rejected: a marker that strips the parameter from the schema

The tempting version of the marker hides the parameter from the model and fills it automatically,
so a recalled parameter would look to a caller like a parameter that does not exist.

It would need new upstream code, and the grounding is specific. `load_tools`
(`ai_function.py:412-442`) builds the tool schema from `inspect.signature(self._prompt_fn)` and
copies `prompt_fn.__annotations__` wholesale; it has no filter, no skip list, and no hook. The only
existing marker with runtime meaning is `ProceduralMarker`, and `detect_procedural_params`
(`code_execution.py:288-310`) reads it to change *execution* semantics — whether the sandbox
`define`s the value or injects it as a string — while leaving the parameter fully visible in the
schema. There is no precedent anywhere in the runtime for dropping a parameter from the model-facing
schema or auto-filling one at call time. Building it here would mean shadowing `load_tools` and the
call path from a downstream library, which is an upstream change in disguise: it would drift on the
next release, and it would be invisible to anyone reading `ai_functions`.

So the marker is metadata this library reads and the runtime ignores, which is the honest version.
The cost is that a peer agent sees a parameter a training loop fills — and on inspection that is
not a cost at all, per the section above.

## Why nothing is cached: not the view, not the compiled function

**The view.** A `ParameterView` is single-use. `recall`/`search` emit a `ParameterRecalledEvent` at
recall time when a thread is resolvable, and `trace` flushes the rest through `emit_recall`, which
is a no-op on an already-emitted view (`base.py:409-434`). "One logical recall, one event." Reuse
one view across a batch and the *first* traced call carries a parameter node and every later one
carries none — `tests/app/test_casestudy.py:463` pins exactly that, and it is the bug that made a
training loop report rounds while learning nothing. The binder makes reuse unrepresentable rather
than discouraged: it holds an agent and a backend, and never a view.

`test_a_second_trace_gets_a_fresh_node_because_nothing_is_cached` asserts two traces, two live
nodes — the library edition of the per-round rule `tests/app/test_harnesslearn.py:633` asserts for
the harness loop.

**The compiled function.** `trace` calls `self._agent.compiled(method, **overrides)` per call, not
once at wiring time, for the reason `MethodAgent.spawn` does the same (`method.py:407-411`): tests
bind a scripted model by rebinding `compiled` on the *instance*, and a function compiled in
`__init__` would have captured the real model before the rebinding happened. The failure mode is
the worst available in an offline suite — a test that reaches the network instead of failing.
`test_trace_compiles_through_the_instances_own_compiled` asserts the binding is honoured.

## Why retrieval errors are not fault-wrapped, unlike every hook in `gated.py`

`gated.py` wraps every callable it invokes and re-raises internal failures as messages that say
they are internal, and `docs/design/gated.md` argues that at length. None of it applies here, and
the difference is not a judgment call — it is *where the code runs*.

A `GatedProposer` hook runs inside a post-condition validator, and the runtime turns any exception
out of a validator into the text of a `[VALIDATION ERROR]` user turn the next attempt reads
(`ai_thread.py:640-664`). A `KeyError` in a gate is therefore indistinguishable from a considered
refusal and burns every retry on something the model cannot fix. That is why the wrap exists.

Retrieval runs before the model call and outside the validation path entirely. There is no model to
report to and no attempts to burn, so a backend that raises has nothing to gain from being
re-dressed: the exception propagates to the caller with its type and traceback intact.
`test_retrieval_errors_propagate_unwrapped` asserts the `KeyError` arrives as a `KeyError` and that
no cycle started. Wrapping it would only hide the traceback of a real bug.

The lesson generalises as: *every callable the framework re-raises must be fault-wrapped* — and the
qualifier is load-bearing. Retrieval is not on that path.

## The guards, and why each is a guard rather than a convention

Each of these was broken deliberately, the failing test observed, and the break reverted. A guard
nobody has watched fail is a comment.

**No query means no call.** A search with a defaulted or derived query is the fail-soft
`memory/turso_backend.py:20-28` warns about from the retrieval-quality side: an embedding backend
returns a full ranked list for any input, so the run *succeeds* with confidently-ranked garbage in
the prompt and the loop trains on advice about a decision nobody was making. There is no defensible
default, so there is no default. The test scripts a model with **zero** turns, which is what makes
it able to fail in the informative direction: with the guard replaced by `queries.get(name, "")`
the observed failure is `ScriptedModel: agent requested turn 1 but script has only 0 turns` — the
call sailed past retrieval and reached the model, which is precisely the behaviour being forbidden.

**A parameter named `queries` or `overrides` is refused at wiring time.** Measured: `trace(self,
method, /, *args, queries=None, overrides=None, **kwargs)` given `queries={...}` binds it to
*trace's* parameter, leaves `kwargs` empty, and the bound method's own `queries` keeps its default.
No error, no retrieval for it, and a prompt missing whatever it carried. This is the
`gated._check_no_collision` precedent — a convention whose violation is one rename away and whose
failure is silent should be a guarantee — and it lives in `bound()` so the introspection path and
the call path cannot disagree about it.

`method` is deliberately *not* on that list, and the narrowness is the point. It is positional-only
on `trace`, so a bound parameter of that name forwards through `**kwargs` and arrives correctly;
`method="x"` as a keyword would raise `TypeError: got multiple values for argument 'method'`, which
is loud. Making it positional-only shrank the collision surface instead of growing a third refusal,
and there is a test asserting a `method` parameter reaches the prompt.

**Positional arguments may not shift onto a marked parameter.** This is the one the settled design
did not anticipate, and it is the sharpest of the three because its bad outcome is silent. Python
binds positionals left to right and cannot skip a slot, so on `choose(advice, state, options)` —
which is `learning.py`'s real shape, marked parameter first — a call written `trace("choose",
state, options)` puts the state string in the *advice* slot. Measured both ways:
`signature.bind_partial("S", "O")` reports `{'advice': 'S', 'state': 'O'}`, and injecting anyway
raises `TypeError: Navigator.choose() got multiple values for argument 'advice'`. The loud outcome
is survivable; the quiet one — a binder inferring "the caller supplied it" from a partial bind, and
so performing no retrieval at all — is a wrong prompt that raises nothing. Refused up front, naming
the fix.

The guard counts only the slots the positionals actually reach, in the same spirit as
`gated._check_no_collision` checking only the first parameter: a method whose markers sit after its
plain parameters can be called positionally with no complaint, and there is a test on each side of
the line.

Two more refusals came out of adversarial review, and each closes a first-wins or fails-late hole:

**Two distinct `Recalled` markers on one annotation are refused.** `Annotated` metadata is ordered,
so first-wins resolution would make *which store a parameter reads from* depend on annotation
order — and the shape is easy to write by accident: a merge that keeps both sides' metadata, or a
union whose members were marked separately. Measured before the fix: the second marker was simply
dead, no signal. The identical marker twice is accepted, since there is nothing to disambiguate.

**A positional-only marked parameter is refused at wiring time.** The binder injects by keyword,
so a marked slot behind `/` is unfillable — and without the refusal the failure arrives *after*
the retrieval was spent, as `TypeError: positional-only arguments passed as keyword arguments`,
Python calling mechanics three frames from the wiring mistake. The test's spy backend asserts the
refusal precedes the retrieval it protects, which is the same invariant the query guard holds to.

## Why the test backend is `JSONMemoryBackend`, and one measurement that changed a fixture

`JSONMemoryBackend._search` ranks with BM25 and returns `{"results": {entry_id: value}}`
(`json_backend.py:381-403`) — the same narrow-gradient mapping `TursoMemoryBackend` carries
(`turso_backend.py:1021-1026`) — with no embedder and no model call. `TursoMemoryBackend` would
need an embedder, and the deterministic one lives in `tests/app`, which a library test file cannot
import: `test_boundary.py:217-256` requires every `tests/library` module to *collect* with
`polars`/`libsql`/`pm4py` blocked.

The fixture's entry count is measured rather than chosen, and the first draft got it wrong.
`BM25Okapi`'s IDF is `log((N - df + 0.5) / (df + 0.5))`, which for a term appearing in one of two
documents is `log(1.0) == 0`. At N=2 every score is 0.0 and `sorted` falls back to insertion order,
so a two-entry fixture returns the first entry for every query:

    N=2  "invoice disputed escalate finance"  →  [0.0, 0.0]
    N=4  "invoice disputed escalate finance"  →  [0.0, 2.461, 0.0, 0.0]
    N=4  "permit pending missing document"    →  [2.461, 0.0, 0.0, 0.0]

So `Playbook.guidance` seeds four entries with disjoint vocabulary and
`test_the_query_selects_which_entries_reach_the_prompt` asserts the query selects. At two entries
that test would have passed or failed on insertion order and said nothing about the query, which is
the vacuity defect `detect/` and `tests/library/test_vacuity.py` exist to catch.

One more test-side trap worth recording: `SpyBackend` counts retrievals by overriding the **public**
`recall`/`search`, not the `_`-hooks. The base class emits `ParameterRecalledEvent` from the public
methods (`base.py:348-407`), so a spy written against the hooks would count correctly and silently
skip emission — the trap `turso_backend.py:384-388` documents — leaving every graph assertion in the
file quietly vacuous.

## What this module deliberately does not do

**No live-thread recall, though the lever exists.** `recall` and `search` accept an explicit
`coordinator` and `thread_id` (`base.py:348-407`), and the event may be appended before or
independent of any trace — it "waits in the log" (`events.py:390-394`). So a binder *could* emit
into a live `MethodThread`'s log directly and give a multi-cycle conversation a per-cycle recall
with a preserved edge, without going through `trace` at all. That is a real and interesting second
mechanism, and it is future work rather than an omission. It needs its own decision about what a
recall means when a cycle is forked or replayed, and answering that badly is worse than not
answering it. Recorded here so the next reader knows the lever is there and knows it was left
alone on purpose.

**No `run` or `__call__` wrapper.** Only `trace` builds the graph. A `run` variant would be a
retrieval whose edge is thrown away — the failure this module exists to prevent, offered as an API.

**No consolidation, no optimizer coupling.** `Recall` performs the forward half and stops. The
caller holds the `Result`, hands `binder.backend` to `build_graph_from_result`, and drives
`TextGradOptimizer` itself — which groups by `(id(backend), name)` and merges `meta["results"]` into
the `retrieved=` argument of `backend.consolidate` (`textgrad.py:269-298`). Owning the backward pass
here would mean deciding how many rounds, what feedback, and which trace to keep, and those are the
loop's decisions. `gated.py` drew the same line at "no score, no optimizer, no memory"; this module
draws it at "no backward pass".

**One binder, one backend.** `source` is a field path on *a* schema. A binder over several stores
would have to disambiguate them in the marker, which would put storage topology into the agent's
signature. Two stores means two binders over the same agent, and they compose without either one
knowing about the other.
