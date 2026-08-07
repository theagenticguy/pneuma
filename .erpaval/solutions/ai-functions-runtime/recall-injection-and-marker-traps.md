# Recall injection: the ambient-scope trap, and what an Annotated marker can and cannot do

**Category**: ai-functions-runtime
**Tags**: recall, parameterview, thread-scope, annotated, markers, gradient, bm25, testing
**Modules**: src/pneuma/recall.py, src/pneuma/casestudy/learning.py
**Session**: session-c8116b (2026-08-07)

## Lessons (pinned rev e47dc94e, all verified by execution)

1. **A retrieval performed under an active `thread_scope` silently destroys the gradient
   edge for any later trace.** `recall`/`search` with no explicit ids emit their
   `ParameterRecalledEvent` against the ambient scope and mark the view `emitted=True`
   (base.py:275-291); `AIFunction.trace`'s flush then no-ops ("one logical recall, one
   event") and the traced thread's log carries nothing — `graph.parameters == []`, no
   error anywhere. The runtime opens a scope for EVERY executing cycle
   (runtime/worker.py:613), so any binder/wrapper that retrieves-for-injection and might
   run inside a capability body or gate MUST wrap retrieval in `no_thread_scope()`
   (public export, `ai_functions.types`). The critic found this; every offline test had
   passed because none traced from inside a cycle. Regression test: trace under an
   explicit `thread_scope` and assert the node survives.

2. **An `Annotated` marker can declare, but nothing upstream can hide or auto-fill a
   parameter.** `load_tools` copies the full prompt_fn signature verbatim
   (ai_function.py:412-442) — no drop, no fill. So a "framework supplies this" marker
   must keep the parameter in the tool schema (peers supply it themselves; only the
   training-loop binder fills from memory). A schema-stripping marker is an upstream
   change wearing a library's clothes.

3. **Marker detection must refuse conflicting duplicates, not resolve first-wins.**
   `Annotated` metadata is ordered; two distinct markers (merge artifact, or a union
   marked on both sides) means which store the parameter reads from depends on
   annotation order. Collect all, dedupe identical, raise on >1 distinct.

4. **Keyword injection cannot fill a positional-only parameter — refuse at wiring time,
   before the retrieval is spent.** Otherwise the failure is a TypeError about Python
   calling mechanics, three frames from the wiring mistake, after the backend round-trip
   (and its recall-event side effects) already happened. General rule restated: a guard
   that raises after spending what it protects is half a guard; assert with a spy that
   the refusal precedes the spend.

5. **BM25 cannot rank a two-document corpus.** `BM25Okapi` IDF is
   `log((N - df + 0.5)/(df + 0.5))` = `log(1.0)` = 0 for a term in one of two docs, so
   every score is 0.0 and "retrieval" is insertion order. A query-selection test needs
   N>=4 with disjoint vocabulary or it proves nothing. (JSONMemoryBackend's `_search`
   does carry `{"results": {entry_id: value}}` — usable for narrow-gradient tests
   without an embedder, json_backend.py:381-403.)

6. **Read retrieved ids off `Result.inputs` matched by `view.name == marker.source`**
   (the backend field name, not the Python parameter name), and read the source from the
   marker (`binder.bound(method)[param].source`) so one declaration governs both the
   retrieval and the bookkeeping — a renamed field then breaks loudly instead of
   accumulating an empty set forever.
