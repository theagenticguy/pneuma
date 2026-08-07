# A new hook makes old off-by-ones material; gates must be introspection-safe; a guard's test can be satisfied by a coincidental fallback

**Category**: ai-functions-runtime
**Tags**: processagent, interpreter, hooks, max-steps, attributeerror, hasattr, guards, mro
**Modules**: src/pneuma/process/agent.py, src/pneuma/process/interpreter.py, src/pneuma/casestudy/handlers.py
**Session**: session-1b17c3 (2026-08-07)

## Lessons (all verified by execution)

1. **Adding a side-effecting hook to a loop upgrades that loop's cosmetic bugs to material
   ones — audit the loop's edges when you add the hook.** interpreter.run's terminal check
   sat at the top of the loop, so terminal-reached-on-the-last-budgeted-step raised a false
   "exceeded N steps"; harmless for years, until on_enter meant the terminal state's handler
   FIRED and filed paperwork on a run reported as failed (and live.py counts ProcessError as
   blocked — a corrupted experimental split at exactly the budget the experiment uses).
   The fix is a terminality re-check after the loop; the general rule is: a hook's arrival
   changes which existing behaviors are observable, so re-review the host loop's boundary
   conditions as part of the hook's review.

2. **A "you forgot to wire X" gate must raise AttributeError, not RuntimeError.** hasattr,
   getattr(obj, name, default), and inspect.getmembers suppress only AttributeError; any
   other type makes capability probes and debugger variable panes explode with the gate's
   message in place of what they were reporting. Same guidance text, right type: probes
   answer "absent", direct calls still fail loudly naming the fix. (Corollary already
   learned in this repo for __repr__; the property getter is the same trap one attribute
   over.)

3. **A guard's test can pass with the guard deleted when a coincidental fallback produces
   the same observable.** The choose-as-handler collision test originally asserted an error
   naming state+method — which the signature-bind TypeError fallback also produced, so the
   test was a comment. The break-the-guard step is what caught it. Sharpen with (a) an
   exact-type assertion the fallback fails, and (b) the SILENT variant of the failure (an
   arguments_for that binds cleanly: one model turn spent on a phantom decision, run
   completes, nothing says so) with a zero-turn model. Rule extension: break the guard AND
   check the test fails for the reason the guard exists, not for a neighbor's reason.

4. **ai_methods() walks the MRO, so any base-class @ai_method leaks into every subclass's
   published tool set.** A subclass whose contract pins an exact capability set must filter
   (by the base's declared constant, not by restating names) — the decorator stays the
   single source of truth, and the base's private adapters (a decider) do not become
   advertised services.

5. **Two waves, two integration conflicts the first wave's report missed** (frozen-oracle
   constructor signature; the MRO leak above). The packet rule that saved both: "read the
   oracle first and record the constraint before writing code", and Wave 1's handoff facts
   are labeled "verify against the actual files". Handoffs state intentions; oracles state
   contracts.
