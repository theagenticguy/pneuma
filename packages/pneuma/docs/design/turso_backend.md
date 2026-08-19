# `memory/turso_backend.py` — design rationale

Why the Turso memory backend is shaped the way it is. The module docstring states the
invariants and the numbers; this file carries the arguments.

## Three capabilities that happen to be one object

A drop-in alternative to `JSONMemoryBackend` for a learning loop.

**Targeted recall.** A single prose parameter is recalled whole or not at all, so a
gradient about one piece of advice is routed to a blob containing all of it and the
consolidating model rewrites whatever it likes. Splitting that parameter into addressable
entries and retrieving the few that bear on the current decision makes the gradient land
on those entries and no others. That is the whole point of `ParameterMeta["results"]`, and
"Narrow gradients" below is the mechanism.

**One artifact.** This project's audit database is already libSQL (see
`casestudy.eventlog`). Putting learned parameters in the same engine means the event log,
the mined model, its verification verdict, the runs executed under it, and the parameters
the runs learned are one file somebody can be handed.

**Two learning channels.** `GradFeedback` carries `text` and `score`. A text-rewriting
backend ignores the score and a score-learning host ignores the text; because
`MemoryBackend` already satisfies `ParameterHost`, one `_consolidate` gets both. Text
entries are rewritten. Numeric parameters — a support threshold, a step budget, anything an
agent might propose for its own harness — are moved by the score, using the search in
`_numeric_update`. No separate host class.

## Retrieval is vector search, and FTS could not have replaced it

Measured with Cohere Embed v4 vectors over six agent-guidance entries, by
`tests/library/test_turso_memory.py::test_live_retrieval_discriminates_on_a_real_playbook`, which
prints exactly this:

    query                                            top-1  distance
    "I am at a state I have seen before, what now"    OK     0.6155
    "which of these two moves ends the case"          OK     0.6541
    "can I file the appeal now"                       OK     0.5388
    "the customer still owes money"                   OK     0.6210
    "keeps going around in circles between steps"     MISS   0.7219

    4/5 top-1, 5/5 recalled at k=2
    relevant mean 0.6303 against control mean 0.8683, separation +0.2381

Four of five, and the fifth is why `learning.TOP_K` is greater than one: the correct entry
is retrieved, just not first. The failing probe is kept in the test rather than removed,
because choosing the questions is how a retrieval measurement flatters itself.

Turso's FTS does exist (`CREATE INDEX ... USING fts (...)` behind
`experimental_features='index_method'`, Tantivy-backed) but `fts_score()` returns 0.0 for
every matching row, so it cannot rank and the ABC's `k` would select an arbitrary subset.
`test_turso_fts_cannot_rank_...` records that, and it needs no credentials because it is a
property of the database. Nothing here depends on FTS.

Two database behaviours here were established by probing rather than taken from
documentation. The match syntax is `WHERE column MATCH ?` (the `fts_match(index_name, ...)`
form does not parse, "no such column"), and `vector_distance_cos` raises
`Conversion error: Invalid vector type` on a NULL blob rather than returning NULL, which is
why every retrieval query is an inner join.

## The defect this file is designed against

An embedding backend fails soft. It always returns something ranked, so "retrieval returned
garbage" and "retrieval worked" are the same observation from the outside. Shipping that
unguarded rebuilds, inside the memory layer, exactly the vacuity problem `pneuma.detect`
exists to catch: the loop would report healthy rounds while learning from advice that had
nothing to do with the decision.

So retrieval quality is *measured*, by `probe_retrieval`, and the measurement is a
discrimination test rather than a smoke test. It asks whether relevant queries land closer
than deliberately unrelated ones, and reports the margin between the two distributions.
When they overlap it says so and refuses to certify, in the same three-valued spirit as
`detect.vacuity`: not knowing is not a pass. See
[discrimination.md](discrimination.md) for where that shape is and is not shared.
`test_probe_retrieval_detects_a_useless_embedding` is the proof it has teeth — against a
constant embedder, `search` still returns a full ranked list of the requested length, so
every smoke test passes and only the separation reveals there is no signal.

`distance_ceiling` is off by default and it is not a default anybody should guess. On the
corpus above the gap is real but narrow, and it is a property of that corpus rather than of
the embedding model: `calibrate_ceiling` on the same five probes returns 0.7757, which no
one would have picked. So the ceiling is derived from measurement, and refused outright when
the distributions overlap. A ceiling set by taste is a silent cap.

## Narrow gradients

The chain, and every link is load-bearing:

1. `search` returns a `ParameterView` whose `meta["results"]` is `{entry_id: value}` for
   the retrieved entries only.
2. `MemoryBackend.search` merges that into the `ParameterRecalledEvent`'s meta.
3. `build_graph` copies the event's meta onto the `ParameterNode`.
4. `TextGradOptimizer.consolidate` merges `meta["results"]` across the consolidation group
   and passes it as `retrieved=`.
5. `_consolidate` shows the consolidating agent *only* those entries and hands it CRUD
   tools keyed by entry id.

Entry ids come from a persisted monotonic counter and are never reused, so an id recorded
during the forward pass still names the same logical entry at consolidation time, across
saves, deletes, and reopens.

Two sharp edges the library documents and this file honours. A `ParameterView` is
single-use — "one logical recall, one event" — so callers must recall per call, not once
per batch. And a gradient target is discovered in *call arguments*, so a view must be
passed as a handle: `f"{view}"` computes the same prompt with the edge silently dropped.

## Which channel a parameter type reads, and why

`_consolidate` routes by parameter type, so each type reads only the channel it can
actually inform:

- A **numeric** parameter reads `score`. Asking a model to rewrite a support threshold
  produces a number with a justification and no evidence; the score is a measurement of how
  the current value performed, which is what a search over values can use. The text is kept
  as the observation's rationale, so the artifact records why. No score channel means no
  evidence about this value, and `_consolidate` returns without writing — rewriting from
  the text alone would be invention, and a loop cannot tell invention from learning.
- A **list** parameter reads `text`, agentically, editing only the entries `retrieved`
  names.
- A **scalar or `Procedural`** parameter reads `text` and is rewritten whole. A
  `Procedural` one goes through a post-condition that re-parses the result
  (`_check_valid_python`), so a gradient can never leave unparseable code in the store.

That post-condition placement mirrors the constraint `casestudy/harnesslearn.py` records
for its proposal gate: a validating call is a check the loop can forget, while a
post-condition cannot be skipped, and its failure is a retry with the reason rather than a
lost round. The second constraint there is on parameter naming — a post-condition whose
first parameter shares a name with an `ai_function` parameter binds to the argument rather
than to `self`, so a validator reading `self` must not collide.

## The `pyturso` write-loss gotcha

Every read in this package goes through `memory.embedding.fetch_rows`, and none constructs
a bare cursor. That is not a style preference. `pyturso` 0.7.2 **silently discards pending
uncommitted writes** when a `Cursor` holding an unfinalized SELECT is garbage collected.
Reproduced minimally: open a cursor, `fetchone()` without exhausting it, `INSERT OR
REPLACE` on the connection, then let the cursor fall out of scope. The insert is gone,
`commit()` reports success, and no exception is raised anywhere.

The symptom lands far from the cause. A read-modify-write counter — read `next_id`, write
`next_id + 1`, insert the row — allocated the same id forever, because the reading cursor
died after the counter write and took it with it. That surfaced as a `UNIQUE constraint`
failure on the *entry* table, three statements away from the cause.

`Cursor.close()` finalizes the active statement (lib.py:539) while `__del__` does not, so
closing explicitly is the fix. Verified over eight read-modify-write cycles with and
without WAL. The full statement lives on `memory/embedding.py:fetch_rows`.

WAL is set in `connect` for the same practical reason `casestudy.eventlog.connect` sets it:
a training loop reads parameters while the interpreter writes run traces to the same file,
and WAL lets the readers proceed without blocking the writer. Verified against `pyturso`
0.7.2 — `PRAGMA journal_mode=WAL` returns `('wal',)` and a `-wal` file appears next to the
database.
