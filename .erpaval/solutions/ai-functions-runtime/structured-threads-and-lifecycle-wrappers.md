# ai-functions runtime: STRUCTURED threads work; lifecycle wrappers must not trust local state

**Category**: ai-functions-runtime
**Tags**: ai-functions, threads, structured, lifecycle, fork, seed_from, testing
**Modules**: src/pneuma/method.py
**Session**: session-6450cb (2026-08-07)

## Lessons (pinned rev e47dc94e, all verified by execution)

1. **STRUCTURED AIFunctions are fully thread-capable.** `handle.run(*args, **kwargs)`
   forwards typed kwargs verbatim (handle.py:79-116 → worker PromptRequest →
   per-cycle template render); history lives on the coordinator's event log and is
   rebuilt every cycle (`reconstruct_messages`), so multi-turn works for any input
   shape. The STR_PROMPT restriction exists ONLY in the LLM-facing tools
   (`send_message` target gate, ai_thread/tools.py:172) and the CLI. Design
   consequence: typed agents join teams as tools; `notify()` is their inbound channel.

2. **A thread wraps one Spawnable.** Cross-method continuity is
   `coordinator.spawn(fn_b, seed_from=thread_a_id)` — a log copy at spawn time,
   the same mechanism fork() uses. Do not try to host two signatures on one thread.

3. **A lifecycle wrapper's local liveness flag WILL desync from the runtime.**
   Anything holding the raw handle can terminate the thread behind the wrapper's
   back; then the wrapper's `retire()` hits `ThreadNotFoundError` — which is a
   `KeyError`, not a `RuntimeError`, so it sails past the handlers callers write.
   Suppress it in teardown paths (idempotence must hold against the runtime, not
   just the object) and keep explicit guards for the fail-loud ops.

4. **Guard-naming must follow the published tool name.** `@ai_method(name=...)`
   renames the tool (`fn.config.name` wins over `{owner}.{method}`); error messages
   and thread_names must use the published name or callers hunt for a thread absent
   from the schema they read. Derive once at spawn, thread it through fork.

5. **Testing traps.** `ScriptedModel` is `@final` (compose, don't subclass — wrap a
   `strands.Model` that records `stream`'s messages then delegates);
   `RuntimeHarness.agent_messages` only captures threads spawned THROUGH the
   harness, not through a caller's coordinator; `compile_ai_method` resolves
   annotations via `typing.get_type_hints` against module globals, so test fixture
   output types must be module-level, never function-local; `AIFunction.spawn()`
   (argless) builds a private coordinator per call — always spawn onto the caller's.

6. **Upstream gap.** `Coordinator.fork()` takes no `thread_name`; a fork's log name
   degrades to the bare prompt_fn name. Candidate upstream contribution.
