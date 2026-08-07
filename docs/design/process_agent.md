# `process/agent.py` — design rationale

Why the work inside a state is dispatched from a hook in the interpreter rather than from the
decider or from the finished trace, why the handler seam is two methods and not one, and why a
handler fault is the one thing in this package that stops a verified run. The module docstring
states the shape; this file carries the arguments, the measurements, and the alternatives that
lost.

This is the first design doc for the `process` package, so it also records what the interpreter
gained — one keyword argument, in the file whose header says "hand-written, reviewed once".

## The promise that was open for four modules

`State.agent_method` has said this since the IR was written (`ir.py:178`):

> `agent_method` names the `@ai_method` the interpreter dispatches to.

The interpreter never dispatched to it. The field's only reader was
`casestudy/handlers.handler_for` (`handlers.py:155-159`), which does not use it as a name at all:
it checks the field for `None` as an opt-in gate and then looks the real handler up in a
`dict[str, tuple[str, dict]]` keyed by `state.name`. And `handlers.dispatch` was never called
from inside a run — `tests/app/test_casestudy.py:377-418` calls it directly with a hand-built
`State`, which is the only place in the repo where a per-state handler has ever executed.

So the pipeline had a real asymmetry. The decision *between* states was verified end to end —
mined to IR, model-checked by TLC, driven by an `@ai_method`, every proposal validated against
the skeleton — and the work *within* a state was a field name and a leaf module. `ProcessAgent`
closes it, and the closing is one hook plus one resolution rule.

## Why the hook is in the interpreter, and why it is `on_enter`

Two designs avoid touching `interpreter.py` at all. Both are wrong, and each is wrong in a way
that a test would not necessarily catch.

### Rejected: dispatch from inside the decider

The decider already runs during the walk and already knows the state it is standing in, so
calling `dispatch(state)` at the top of `decide` needs no upstream change whatsoever.

It would silently skip most of the process. `_elicit` returns immediately when a state has one
enabled transition, without consulting the decider at all (`interpreter.py:171-172`) — deliberate
cost control, and pinned by `tests/library/test_process.py:803-814`, which asserts that walking
the claims process consults the agent exactly once, at `Escalated`. In this file's own `corridor()`
fixture no state branches, so a decider-hosted dispatcher would do *nothing* on a process whose
middle state names a handler. In `filing()`, three of the four occupied non-terminal states are
deterministic. On a mined municipal process the ratio is worse: the mined models are mostly
corridors with a handful of real branches, which is exactly why the fast path was written.

The failure is also invisible in the shape a test would naturally take. A run whose work never
happened returns the same `Run`, reaches the same terminal state, and has the same `path`. Only a
count of model calls or a recording hook shows the difference — which is why every "this state is
free" test in `tests/library/test_process_agent.py` counts calls rather than checking a return
value.

### Rejected: dispatch afterwards over `Run.steps`

The `Run` records every step with its source state, so a caller can walk `run.steps` after
`interpreter.run` returns and dispatch each state's handler. No upstream change, and the states
are all there — including the deterministic ones.

It gets the order wrong, and order is the point. What a handler produces is what the next decision
is made from: a check that finds a document missing is the reason the model should route to
`request_more` rather than `approve`. Running every handler after the walk means every decision
was made without any of the work that was supposed to inform it, which turns the process into a
random walk with paperwork attached. The `Run` would look identical.

It also cannot express a handler that fails. A fault discovered in the post-pass has already been
preceded by a completed run and a terminal state; there is nothing to stop.

### So: `on_enter`, and only that

`interpreter.run` gained one keyword-only parameter:

```python
on_enter: Callable[[str], Awaitable[None]] | None = None
```

called once per state the run *occupies* — the initial state included, after that state's
invariant check, before anything is decided from it. `None` leaves the walk byte-identical, and
`tests/library/test_process.py` is the oracle for that: 52 tests over the same interpreter,
unmodified, green.

Three properties of the placement are load-bearing and each has a test:

**The initial state counts.** A process whose first state does the intake work would otherwise
skip it, and the skip is silent. `test_on_enter_sees_every_state_the_run_occupies_including_the_initial_one`
asserts `["A", "B", "C"]` over a three-state corridor.

**Once per visit, not once per name.** A rework loop that returns to a state returns to its work.
`test_on_enter_fires_once_per_visit_so_a_cycle_works_twice` asserts `["A", "B", "C", "B", "D"]`
over the cycle `test_process.py:820`'s `revisiting()` models.

**Inside the `_HISTORY` scope.** The initial-state call sits after `_HISTORY.set(...)` rather than
beside the pre-loop invariant check, so a handler can call `interpreter.history()` and read the
same path a decider standing in that state would. Outside the scope it would read `[]`, and a
handler prompt that wanted to say where the case has been would say nothing, with no error.
`test_on_enter_sees_the_same_history_a_decider_standing_there_would` asserts
`[["A"], ["A", "B"], ["A", "B", "C"]]`.

**Why the hook takes a state name and nothing else.** Whoever installs it holds the `Process` the
run is walking, so the `State` object is one dictionary lookup away — and `ProcessAgent.work` does
that lookup once, outside the hook, because `Process.state_map` rebuilds its dict on every access
(`ir.py:277-279`) and the hook runs at every step. Passing the `State`, the variables, or the
partial trace would widen the surface of the one file in this package that is meant to stay fixed,
in order to save that lookup. The narrow signature is also what keeps the hook honest about what
it is: a notification that a state was entered, not a second decision point.

**And one interpreter change the hook forced into the open: terminal-at-exact-budget is
completion.** The loop's terminal check sits at the top, so a run landing on a terminal state with
its *last* budgeted transition used to execute the step and then raise `exceeded N steps` — a
message that was factually false but, pre-hook, merely cosmetic. The hook made it material:
adversarial review measured the terminal state's handler firing, its paperwork landing on the case
file, and the run still reported as failed — and `live.py:174` counts `ProcessError` as `blocked`,
so at exactly the budget the experiment runs with (`max_steps=12`), a legitimately completed case
would corrupt the completed/blocked split. `interpreter.run` now re-checks terminality after the
loop; `test_a_run_that_lands_terminal_on_its_last_budgeted_step_has_completed` fails with the
re-check removed (measured) and the frozen `test_process.py` passes unchanged, since no prior test
pinned the false raise.

## Why the handler seam is two methods plus a recorder

```python
def handler_for(self, state: State) -> tuple[str, dict[str, Any]] | None
def arguments_for(self, state: State) -> dict[str, Any]
def on_result(self, state: State, result: Any) -> Awaitable[None] | None
```

`handler_for`'s default honours `agent_method` literally: it names an `@ai_method` on this agent,
or the state is free. That is the promise from `ir.py:178`, and honouring it is what lets a process
and an agent be wired together by the IR alone — no table, no registration, no per-process code.

**Why an unrecognised name returns None rather than raising.** This is the half that would be easy
to get wrong in the strict direction. Every mined state carries `agent_method='handle'`
(`casestudy/miner.py:125`, `casestudy/aimine.py:372`), a placeholder that resolves to no method on
any agent in this repo, and `tests/app/test_casestudy.py:418` pins it as correctly `None`. A base
that raised on an unrecognised name would make every mined process unrunnable by the class built
to run mined processes. So "names nothing I can do" and "names nothing" are the same free state,
and both are asserted with a zero-turn model rather than by checking a return value.

**Why `arguments_for` is separate rather than folded into `handler_for`.** The two overrides answer
different questions and are wanted separately:

- An agent whose states name their methods correctly and only needs per-state arguments overrides
  `arguments_for` and inherits the resolution rule it agrees with (`ArgumentClerk` in the tests).
- An agent whose mapping does not come from `agent_method` at all overrides `handler_for` and
  ignores `arguments_for` entirely (`TableClerk`).

Folding them together would force the first case to restate the resolution it wanted. Keeping both
costs one method with a two-line body.

**`handler_for`'s signature is `casestudy.handlers.handler_for`'s, deliberately.** Same parameter,
same return type, same `None` meaning (`handlers.py:155`). The mined-activity mapping — a table
keyed by state name, with `agent_method` as the opt-in gate — is expressible as an override without
the library knowing that tables exist, which is what keeps `process/agent.py` on the library side
of `tests/library/test_boundary.py`. `test_handler_for_can_be_overridden_with_a_table_the_library_knows_nothing_about`
proves it from the library side, with a table that *disagrees* with `agent_method` on two states so
the override is demonstrably doing the resolving.

**Why `on_result` may be `async`.** A sync-only hook would turn `async def on_result` into a silent
no-op: the coroutine is created, never awaited, the paperwork is never written, the run still
reports a completed case, and the only trace is a `RuntimeWarning` at garbage collection. That is
the fail-soft class this package's guards exist to remove, and one `inspect.isawaitable` check
makes it unrepresentable. The `Clerk` fixture's `on_result` is `async` for exactly that reason —
written in the shape that would expose the mistake — and a separate test covers the sync one.

## Why a handler fault stops the run, and why that is not `gated.py`'s fault-wrapping

Every hook `dispatch` calls is the caller's code, and any of them can be wrong. A dispatcher that
swallowed the exception and kept walking would return a `Run` that reached a terminal state with
the work inside it missing — a report of a completed case that did not happen, which is worse than
a crash by exactly the amount a reader trusts the report. So every fault is re-raised as
`HandlerFailed` naming the state, the method, and which part broke, with the original attached.

Measured by breaking it. Replacing the raise with `return None`:

    FAILED ...::test_a_handler_that_raises_fails_the_run_naming_the_state_and_the_method
    E  Failed: DID NOT RAISE HandlerFailed
    FAILED ...::test_a_handler_fault_is_not_catchable_as_a_process_refusal
    E  Failed: DID NOT RAISE HandlerFailed

**`HandlerFailed` is deliberately not a `ProcessError`.** The interpreter's three failures all mean
the process refused to continue, and callers branch on that: `casestudy/live.py:172-175` catches
`ProcessError` and counts the case as `blocked`, which is an experimental result about whether the
guardrail bit. A bug in a handler is not a result about the process, and inheriting from
`ProcessError` would launder one into the other — a code fault arriving in a published number as
evidence about a control. There is a test asserting the non-subclassing directly, because the
relationship is the kind a later "tidy up the exception hierarchy" commit removes.

**Wrapped for context, not re-dressed as a verdict, and the difference from `gated.py` is *where
the code runs*.** Every hook a `GatedProposer` calls runs inside a post-condition validator, where
the runtime turns any exception into the text of a `[VALIDATION ERROR]` user turn the next attempt
reads (`ai_thread.py:640-664`) — so a bug there is indistinguishable from a considered refusal and
burns every retry on something the model cannot fix. `docs/design/gated.md` argues that at length
and it is right. None of it applies here: a handler runs its own cycle to completion and there is
no model waiting to be told anything, so the exception's only job is to reach the caller with a
traceback. `recall.py` drew the same line for retrieval and stated the qualifier — *every callable
the framework re-raises must be fault-wrapped* — and dispatch is not on that path.

**`handler_for` is fault-wrapped too, which is the lesson from the gate lift.** `gated.py` wrapped
the gate and left the hook added beside it (`candidate_of`) outside the wrap, and a typoed override
surfaced as a raw `AttributeError` burning `max_attempts` retries. Resolution here runs before the
model call, so there is nothing to burn — the wrap buys the state's name on the traceback, which is
the only thing that makes a mapping bug findable in a run of a dozen states. Assume any new hook
has this edge.

**And the interpreter does not soften a hook fault either.** `on_enter`'s exception propagates out
of `run` unchanged, and `test_a_raising_on_enter_stops_the_run_and_propagates_unchanged` asserts
both halves: the exception type survives, and the run did not reach the state after the one that
failed.

## The one guard, and the measurement that rewrote its test

`work()` refuses, before anything is compiled or spent, a process whose state names the decider as
its per-state handler. `choose` is an `@ai_method` on this agent, so the default `handler_for`
resolves `agent_method="choose"` to it and the state would dispatch the decision-maker as work.

The guard-break is worth recording in full, because the first version of the test **passed with the
guard deleted** — the guard-break's real job.

Under the *default* `arguments_for`, dispatching `choose` dies on the signature bind with or
without the guard. Measured:

    HandlerFailed: collide-agent: the handler 'choose' for state 'Start' raised, ...:
      TypeError: ProcessAgent.choose() missing 3 required positional arguments:
      'state', 'options', and 'facts'

That fallback happens to name the same state and the same method, and it also spends nothing — so
every assertion the original test made was satisfied by the absence of the thing under test. It was
a comment.

The case the guard exists for is the *silent* one: an `arguments_for` that happens to supply
`choose`'s parameters. Measured with the guard removed and one turn available:

    RUN COMPLETED: End ['Finish']
    MODEL CALLS: 1                       (a turn spent on a phantom decision)
    on_result RECEIVED: [('Start', Choice(transition='Finish', reason='because'))]

A real model call, spent, returning a transition name the interpreter never sees, at a state that
then also gets a genuine decision. The run completes and the waste is invisible.

So the test was rewritten onto that shape (`SelfChoosingClerk`) with two assertions that carry the
weight: `type(raised.value) is RuntimeError`, which the `HandlerFailed` fallback fails; and a
zero-turn model, so a late refusal reaches the model and dies as `ScriptExhausted` instead. Both
now fail with the guard removed, and a second test keeps the loud shape as the negative control:

    E  AssertionError: the refusal must be a wiring error, not a report of something that ran:
       HandlerFailed: ... ScriptExhausted: agent requested turn 1 but script has only 0 turns
    E  assert <class 'pneuma.process.agent.HandlerFailed'> is RuntimeError

This is `recall.py`'s rule restated: *a guard that raises after spending what it protects is half a
guard*, and its corollary discovered here — a guard whose test is satisfied by a coincidental
fallback is no guard at all.

**Why only the declared `agent_method` is scanned.** An override of `handler_for` that returns the
decider's name is arbitrary code and not reachable from a wiring-time check. The boundary matches
the one `arguments_for` draws: this class refuses what it can see before spending anything, and
lets runtime binding fail loudly on its own. Which is the reason there is no second guard checking
that `arguments_for` supplies a handler's required parameters — that failure is a `TypeError` from
the signature bind, naming the method, immediately, and a wiring-time check would buy a marginally
better message for a failure impossible to miss. `gated._check_no_collision` checks only the first
parameter for the same reason: narrow guards over silent failures, nothing over loud ones.

## Why nothing is compiled at wiring time, and how overrides reach everything

`work(**overrides)` forwards its keywords to *every* `compiled` call the run makes — the decider's
and each handler's. One keyword makes a whole process scriptable offline, decisions and work
together, which is the seam `tests/app/test_casestudy.py:542-570`'s `_scripted_choose` monkeypatch
had to work around: that helper rebinds `compiled` on the instance because there was no other way
to reach a decider built outside the caller's control.

Both paths still work, deliberately. `ProcessAgent.decider` compiles inside itself and `dispatch`
compiles per call, and both go through `self.compiled` rather than `compile_ai_method` — the
argument `MethodAgent.spawn` makes for the same choice (`method.py:407-411`) and `Recall.trace`
repeats: a function compiled in `__init__` would have captured the real model before any test
binding happened, and the failure mode is the worst available in an offline suite, a test that
reaches the network instead of failing. There is a test for each entry point.

## What the `Navigator` refactor did and did not change

`Navigator` is now a `ProcessAgent` that declares no handlers, which is exactly what it always was:
a decider. The class body is one `__init__` that calls `super().__init__` and restores the
published name.

Everything published stayed put. The prompt text is byte-identical, `max_attempts=2` and the
`Choice` output type are unchanged, `compiled("choose")` is still `STRUCTURED`, the tool is still
published as `{process}-navigator.choose`, and `Choice` is still importable from
`pneuma.process.agent_driver`. `tests/library/test_process.py:765-800` (the rejection loop through
a `ScriptedModel`) and `:949-953` (the STRUCTURED pin) are the oracle and both pass against an
unmodified file.

The name matters more than it looks. `_owner_name` (`method.py:61-68`) makes it the compiled tool's
prefix and the subject of every lifecycle error message, and `casestudy/live.py` writes a live
decision log per arm — renaming the agent mid-study would split one arm's rows across two names.

`decider(facts)` was kept even though a grep found it has **zero callers** anywhere in `src/` or
`tests/`: `live.py` builds its own `make_decider` (`live.py:127-166`) with a step counter and a
capture sink. It was kept because `work` needs precisely that adapter, so keeping it costs nothing,
and because it is the seam `live.py` migrates onto when someone wants the step counter as an
override rather than a closure. It gained a `**overrides` keyword; the positional call is unchanged.

## What this module deliberately does not do

**No memory, no recall, no optimizer.** `casestudy/learning.py` stays on the raw `Decide` contract
and that is the right place for it. Its decider retrieves a playbook per decision, traces the call,
and harvests retrieved ids off `Result.inputs` (`learning.py:312-332`) — it needs the `Result` of
each decision, and a `Run` does not carry one. Making `work` return traces would mean deciding how
many to keep, which one is the gradient's, and what happens on a rejection loop where one decision
produced two cycles; those are the training loop's decisions, and `docs/design/recall.md` draws the
same line at "no backward pass". A future `ProcessAgent` subclass can compose `Recall` in an
overridden `decider` without this class knowing.

**No `Team` orchestration.** A `ProcessAgent`'s capabilities are `AIFunction`s like any
`MethodAgent`'s, so `agents()` already hands them to a peer as typed tools. Turning a process into
a team — one agent per state, handoffs at transitions — is a different mechanism with its own
questions about who owns the walk, and answering them badly is worse than not answering them.

**No trace capture on the `Run`.** Handler results are the subclass's to keep via `on_result`.
Putting them on the `Run` would mean editing the fixed interpreter's own data structure to carry
this class's payload, which is the direction the whole design is trying not to go.

**No validation that a handler's arguments match its signature.** See the guard section: that
failure is already loud.
