# Suffix replay is vacuous when the edited parameter is consumed at decision 0; Bedrock caches nothing without an explicit cachePoint

**Category**: ai-functions-runtime
**Tags**: replay, fork, seed-from, prompt-caching, bedrock, cache-config, trace, thread-lifetime
**Modules**: src/pneuma/model.py, src/pneuma/gated.py, src/pneuma/casestudy/minelearn.py, src/pneuma/casestudy/harnesslearn.py
**Session**: session-b84f7e (2026-08-09)

## Lessons (all verified by execution)

1. **Counterfactual suffix replay (fork at the decision an edit alters, replay only the
   suffix) requires weak edit↔trajectory coupling — check WHERE the parameter is consumed
   before building the replay path.** minelearn's toolkit is executed at sandbox setup and
   advertised in the first prompt preamble; harnesslearn's coverage_weight parameterises the
   gate at every swept threshold and opens the propose prompt. Both alter decision 0, so the
   suffix IS the whole trajectory (Shepherd 2605.10913's admitted limit). Worse: minelearn's
   scratch rehearsal is model-free, so replay would SPEND cycles scratch never spends. The
   shipped design: `replay_decision()` detects the coupling, records `rehearsal_path` +
   `rehearsal_fallback_reason` on every Attempt/Round, and falls back — an honest negative
   with the detection machinery kept.

2. **`AIFunction.trace` tears its thread down, so nothing run through trace leaves a
   recorded thread to fork.** `fork()` and `spawn(seed_from=)` on a dead id both raise
   ThreadNotFoundError (a KeyError). Any replay-from-history design must capture the thread
   BEFORE trace returns, or use MethodThread lifecycles it owns.

3. **Bedrock Converse caches nothing for Anthropic models unless the request carries an
   explicit `{"cachePoint": {"type": "default"}}` block.** Byte-identical prefixes earn zero
   reuse by default. The strands seam: `BedrockConfig.cache_config = CacheConfig(strategy=
   "auto")` (model.py:53 via opus5) makes `_format_bedrock_messages` inject the cachePoint on
   the last user message. Measured: k=2 beam branches read ~22.3k cache tokens each (~99% of
   the prefix); a seeded replay thread read 20,060 — caching compounds into every log-copy
   path (fork, seed_from), not just beams.

4. **Fork beams are cache-friendly by construction only because branches run serially and
   history is reconstructed deterministically** (propose_k gated.py:400-401; reconstruction
   has no timestamps/uuids). Parallelising branch cycles would race the cache write and
   forfeit the ~90% input discount on branches 2..k.
