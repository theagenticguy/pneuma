# ERPAVal lessons index

Lessons learned from prior ERPAVal sessions. Claude reads this at
session start and greps `.erpaval/solutions/**` for relevant
lessons before starting work.

## By category

### ai-functions-runtime

- [Restart-chain answer loops, verdict tiers, library-side DB driver](solutions/ai-functions-runtime/restart-chain-and-boundary-drivers.md) — a later hook's Revise must re-run earlier gates (budgets outside the walk); verdict tokens need a two-tier parse and fixtures relied on the containment bug; library modules import turso not libsql (boundary-derived); docstring pins drift — make them executable
- [STRUCTURED threads work; lifecycle wrappers must not trust local state](solutions/ai-functions-runtime/structured-threads-and-lifecycle-wrappers.md) — send_message gate is tool-side only; ThreadNotFoundError is a KeyError; ScriptedModel is @final; fixture types must be module-level
- [Gate protocols, fault-wrapping, and fork beams](solutions/ai-functions-runtime/gate-protocol-and-fork-beam.md) — async-admitting protocol attrs block sync subclass methods; wrap every hook on the validation path; fork drops pending notify; measure a guard by breaking it
- [Recall injection: ambient-scope trap and marker limits](solutions/ai-functions-runtime/recall-injection-and-marker-traps.md) — retrieval under a live thread_scope silently kills the gradient edge (wrap in no_thread_scope); no upstream drop/auto-fill for Annotated params; refuse duplicate markers and positional-only marked slots; BM25 needs N>=4 to rank
- [Hooks, budgets, and introspection-safe gates](solutions/ai-functions-runtime/hooks-budgets-and-introspection-safe-gates.md) — a new hook makes a loop's cosmetic off-by-ones material (re-audit boundary conditions); wiring gates raise AttributeError so hasattr/getmembers answer; a guard's test can be satisfied by a coincidental fallback; ai_methods walks the MRO
- [Orchestrator state lifetimes and tool races](solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md) — per-run state resets per run (type(self.x)() preserves subclasses); the default tool executor is concurrent so reserve-before-await; release-then-unregister for teardown tools; delivery claims need a verified wire; dict-keyed aggregation drops name collisions; cross-file line cites go stale mid-session; probes must script models by construction
- [Suffix replay and cache economics](solutions/ai-functions-runtime/suffix-replay-and-cache-economics.md) — replay is vacuous when the edit is consumed at decision 0; trace() tears threads down so nothing survives to fork; Bedrock caches nothing without an explicit cachePoint (CacheConfig seam); fork beams stay cache-friendly only because branches run serially

### architecture-patterns

- [Hooks over phases for orchestrators](solutions/architecture-patterns/hooks-over-phases-for-orchestrators.md) — a ~460-line core owning the composed config_hook, the Accept/Revise loop, and teardown; features as list entries; grading opt-in via review members; strands silently drops bad tool names (wire tests only); team learning via deferred emission against the surviving event log; packet-before-code is what makes agents resumable

### verification

- [TLC liveness: WF kills only stuttering, cycles are real findings](solutions/verification/tlc-liveness-fairness-semantics.md) — why liveness is opt-in; SF not WF for loop-toggled exits; safety masks liveness
- [TLC can exit 0 after printing an Error](solutions/verification/tlc-exit-zero-error-trap.md) — success line is the gate; probe the binary before pinning parsers to documented exit codes
- [Truncation must dominate positive evidence](solutions/verification/truncation-must-dominate-positive-evidence.md) — a truncated sweep withholds, never settles "works"; a witnessed violation settles even under truncation; write the could-not-tell test first

## Recent additions

- 2026-08-11 · session-fc7f24 · roadmap P0-P2: restart-chain loop, verdict tiers, contract tests, Trajectory/Squad/Expedition (7/7 tasks, 918 tests green)
- 2026-08-10 · session-94adc2 · hooks-over-phases lesson from the team rebuild (4/4 tasks, 881 tests green, live TextGrad step verified)
- 2026-08-09 · session-b84f7e · suffix-replay/cache-economics + truncation-asymmetry lessons from the paper-takeaways build (6/6 tasks, 817 tests green)
- 2026-08-07 · session-9df6ea · orchestrator state-lifetime/race lesson from the Team build (kernel complete, 5/5)
- 2026-08-07 · session-1b17c3 · hooks/budgets/gates lesson from the ProcessAgent build
- 2026-08-07 · session-c8116b · recall-injection lesson (ambient scope, marker limits, BM25 fixture floor)
- 2026-08-07 · session-5abb9e · two verification lessons from the opt-in liveness work
