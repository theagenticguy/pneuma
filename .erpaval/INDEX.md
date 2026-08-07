# ERPAVal lessons index

Lessons learned from prior ERPAVal sessions. Claude reads this at
session start and greps `.erpaval/solutions/**` for relevant
lessons before starting work.

## By category

### ai-functions-runtime

- [STRUCTURED threads work; lifecycle wrappers must not trust local state](solutions/ai-functions-runtime/structured-threads-and-lifecycle-wrappers.md) — send_message gate is tool-side only; ThreadNotFoundError is a KeyError; ScriptedModel is @final; fixture types must be module-level
- [Gate protocols, fault-wrapping, and fork beams](solutions/ai-functions-runtime/gate-protocol-and-fork-beam.md) — async-admitting protocol attrs block sync subclass methods; wrap every hook on the validation path; fork drops pending notify; measure a guard by breaking it
- [Recall injection: ambient-scope trap and marker limits](solutions/ai-functions-runtime/recall-injection-and-marker-traps.md) — retrieval under a live thread_scope silently kills the gradient edge (wrap in no_thread_scope); no upstream drop/auto-fill for Annotated params; refuse duplicate markers and positional-only marked slots; BM25 needs N>=4 to rank

### verification

- [TLC liveness: WF kills only stuttering, cycles are real findings](solutions/verification/tlc-liveness-fairness-semantics.md) — why liveness is opt-in; SF not WF for loop-toggled exits; safety masks liveness
- [TLC can exit 0 after printing an Error](solutions/verification/tlc-exit-zero-error-trap.md) — success line is the gate; probe the binary before pinning parsers to documented exit codes

## Recent additions

- 2026-08-07 · session-c8116b · recall-injection lesson (ambient scope, marker limits, BM25 fixture floor)
- 2026-08-07 · session-5abb9e · two verification lessons from the opt-in liveness work
