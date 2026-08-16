# `memory/sqlite_backend.py` — design rationale

Why there is a second memory backend, what actually differs from the first, and the two
decisions a reader would otherwise have to reverse-engineer from a SQL string. The module
docstring states the invariants and the landmines; this file carries the arguments.
Read [turso_backend.md](turso_backend.md) first — everything about *why* addressable
entries, two learning channels, and a measured discrimination guard exist is argued there
and is not repeated.

## Why this exists

`TursoMemoryBackend` earns its engine. The audit database in `casestudy.eventlog` is libSQL,
and putting learned parameters in the same file is the point: the event log, the mined
model, its verification verdict, the runs executed under it, and the parameters those runs
learned become one artifact somebody can be handed.

That argument does not transfer to a caller who has no libSQL file. For them `pyturso` is a
dependency, an alpha-version driver with a known write-loss defect, and a second SQL dialect
to keep in their head — all to get `vector_distance_cos` over a few dozen entries.
`sqlite3` is in the standard library and `sqlite-vec` is one small shared object with no
transitive dependencies, so this backend is the same three capabilities with nothing to
install and nothing to hold. It also removes a real risk from the library layer: with two
independent implementations of one contract, a driver-specific defect shows up as a
disagreement between them rather than as behaviour nobody can compare to anything.

`memory/__init__.py` exports both. The choice is an import, not a flag, because a
`backend="sqlite"` string argument is the kind of thing a caller sets once and then reasons
about wrongly for a year.

## The backends are one contract, and the contract is enforced three ways

"Drop-in sibling" is a claim, so it is checked rather than asserted.

**Same behaviour.** `tests/library/test_sqlite_memory.py` ports every assertion in
`test_turso_memory.py` — id monotonicity across delete and reopen, ranking ASC with `k`
honoured, content-addressed cache invalidation, the four-link `meta["results"]` chain,
byte-identical unretrieved entries, the numeric channel's convergence and bounds, three-valued
discrimination, tool-name parity. Ported rather than parametrised over both backends,
because each suite also carries facts true of one engine only (a `vector32()` equality and a
cursor-GC regression guard there; NULL-first ordering and a thread-affinity guard here), and
folding them together would either skip half the cases per backend or make every test carry
a driver switch.

**Same objects, not same-shaped objects.** `Retrieved`, `Discrimination`, and
`CeilingNotSeparable` are imported from `turso_backend`, and
`test_probe_retrieval_returns_the_same_verdict_type_as_the_turso_backend` asserts type
*identity*. A caller reading `discriminates` must not have to know which backend produced
the verdict, and two copies of a three-valued verdict would drift — a separation threshold
changed in one and not the other, with nothing able to see it. Same reasoning for the
consolidation prompts (nothing in them is driver-specific) and for `_TRUST_FRACTION` /
`_EXPLORE_DECAY` / `_EXCLUSIVE_EPSILON`, each justified by a measured bug at its definition.

**Same numbers.** `_numeric_update` is a copy — it must be, since it is a method on a class
that cannot inherit from a frozen file — so
`test_both_backends_propose_the_same_number_from_the_same_history` runs twelve rounds
through both and compares step by step. This is not belt-and-braces. Changing the port's
decay exponent from `len(history) - 1` to `len(history)` left the convergence test and the
trust-region test *both passing*, because a search that decays a round early still converges
and still starts inside the trust region. Only the cross-backend comparison caught it, at
round 0. A ported algorithm needs its original as the oracle; its properties are not enough.

One thing is deliberately *not* shared, and `SCHEMA` says so at its definition: the DDL is a
separate literal even though the two are byte-identical today. Importing the Turso module's
schema would make a Turso-side column change a silent change to this backend's storage, and
the whole premise here is that the two engines are not the same engine.

## The stored file is portable, and that is the strong form of the claim

`test_a_turso_written_database_ranks_under_this_backend` writes a corpus with
`TursoMemoryBackend`, closes it, reopens the same file with `SqliteMemoryBackend`, and ranks
without re-embedding — entry ids, positions, scalars, and the embedding cache all intact, one
provider call for the query alone. So "drop-in" means the artifact is portable, not merely
that the classes have matching method names.

What makes it work is that the blob format is genuinely shared rather than coincidentally
compatible. `sqlite_vec.serialize_float32`, `embedding.pack_vector`, and Turso's `vector32()`
all produce little-endian float32 packed end to end, verified all three ways, so this module
reuses `pack_vector` and adds no second packer.

What does *not* travel is a calibrated `distance_ceiling`, and `calibrate_ceiling`'s
docstring says so. Both metrics are cosine distance over the same bytes, but they do not
agree to the last bit: an identical float32 pair measures 0.0 under `vec_distance_cosine`
and 4.47e-08 under `vector_distance_cos`. The ceiling is a property of the corpus and the
embedder rather than of the SQL function, so it should be re-derived by measurement anyway —
which is the same conclusion `turso_backend.md` reaches by a different route. `_search` now
records `distance_metric` in the recall meta so an event log holding rows from both backends
cannot invite the mistake.

## The NULL-handling decision

This is the one genuinely new retrieval hazard, and it comes from a pair of facts that are
each harmless alone.

`vec_distance_cosine` returns **NULL** — not an error, not 1.0 — for a zero-magnitude
vector, because cosine is undefined without a direction. And SQLite orders NULL **first**
under `ORDER BY ... ASC`. Together, a single degenerate cached vector is returned as the
*nearest* hit. Turso has neither half: measured, `vector_distance_cos` on a zero vector
returns 1.0, which sorts last and is the honest answer.

Both facts are asserted directly in
`test_a_zero_magnitude_vector_yields_a_null_distance_not_an_error`, as properties of the
database rather than of this module, so a `sqlite-vec` upgrade that starts raising or
returning a number shows up as a failing test rather than as a filter nobody can justify.

Three decisions follow.

**Every ranking query carries `AND distance IS NOT NULL`.** Removing that clause was tried:
the degenerate entry came back as hit zero and `float(None)` raised `TypeError` in the row
mapper, three frames from anything explanatory — and *only* because the mapper happens to be
strict about types. A mapper that passed the value through would have produced a top-ranked
entry at `distance=None`, silently, which is precisely the failing-soft shape this whole
backend is designed against.

**`degenerate_entries` exists next to `unranked_entries`.** Two methods rather than one
because they are different situations with different fixes: an unranked entry has no cached
vector and re-embedding fixes it; a degenerate entry *has* a vector and re-embedding will
produce the same one. It is therefore invisible to the missing-vector check while being just
as absent from every result, and a search that silently returns fewer entries than the
corpus holds is this backend's characteristic failure.

**A degenerate *query* vector raises rather than returning `[]`.** With the filter in place,
a zero-magnitude query makes every distance NULL and the search returns an empty list —
indistinguishable from "this corpus has nothing relevant". Those are unrelated findings and
collapsing them is the same defect `detect/`'s three-valued verdicts exist to prevent, so
`_require_rankable_query` tests the query vector's distance to itself and refuses, naming the
embedder. Removing it was tried too: `[]` came back and every caller-visible signal was
identical to a legitimate miss.

The rankability test is the vector's distance to *itself*, which reads oddly and is the
cheapest available oracle: NULL there means zero magnitude, by the same definition that
makes the whole problem exist.

The inner join stays. A NULL *blob* still raises (`Error reading 1st vector: ... found
NULL`), so an entry with no cached vector cannot be scored at all and no filter can rescue
it — the same conclusion Turso reached from a different error message.
`test_a_null_blob_still_raises_which_is_why_the_join_is_inner` records it so the join is not
relaxed to a LEFT JOIN on the theory that the NULL filter now covers that case.

## Rejected: the `vec0` virtual table

`sqlite-vec` ships `vec0`, a virtual table with a real KNN index, and it works: `CREATE
VIRTUAL TABLE v USING vec0(embedding float[1536] distance_metric=cosine)`, then `WHERE
embedding MATCH ? AND k = 5`. It joins to ordinary tables, accepts a text primary key, and
supports partition keys — all probed, all fine. It is still the wrong choice here, for four
reasons in ascending order of importance.

**The corpus is tiny.** A learned playbook holds entries a person could read: six in the
measured live corpus, a few dozen at the top end. An exact scan over that is microseconds,
and an approximate index over it is a data structure defending against a cost nobody pays.

**It duplicates state that must not diverge.** `memory_embedding_cache` is
content-addressed by `sha256(text)`, which is what makes a rewritten entry re-embed
automatically and staleness structurally impossible rather than managed. A `vec0` table is a
second copy of every vector, keyed by rowid, that has to be kept in step with the cache
through every `update_entry`, `remove_entry`, wholesale `_save`, and cross-actor share. Every
one of those is a place the two can drift, and a drifted vector index fails by returning
plausible neighbours — the failure mode this file spends its whole budget on.

**Approximate answers make the discrimination measurement unsound.** `probe_retrieval`'s
verdict is a margin between two distance distributions, and `calibrate_ceiling` derives a
threshold from the *worst* relevant hit and the *closest* control hit. Both are extremes,
and an index that may omit a true neighbour perturbs exactly the extremes. A ceiling
calibrated against approximate distances would be applied to approximate distances and might
well be self-consistent, which is worse than being wrong: the guard would agree with itself
while both halves were measuring something other than the corpus.

**An exact scan is honest about what it computes, and cheaply auditable.** Thirteen tests in
the new suite assert on distances, four of them by binding hand-packed vectors and reading
the metric's answer straight back — including the two that pin NULL-first ordering, which is
the behaviour the whole retrieval path is shaped around. That kind of assertion is available
only because the ranking query is an ordinary scan a reader can evaluate by hand; against a
`MATCH ... AND k = ?` index the same claims would be measured through the thing under test.

The trigger for revisiting this is a corpus large enough that scan latency shows up in a
decision loop's profile, not a corpus that feels big. At that point the honest change is a
`vec0` table *derived* from the cache with a rebuild step and a test that the two agree —
not an index maintained incrementally alongside it.

## Two landmines that are the driver's, not the design's

**`check_same_thread=False` is mandatory.** `MemoryBackend.recall` / `.query` / `.search`
each run their `_*` hook inside `asyncio.to_thread`, so the *first awaited recall* touches
the connection from a worker thread and stock `sqlite3` raises `ProgrammingError: SQLite
objects created in a thread can only be used in that same thread`. Every synchronous test
passes without the flag, which is why `test_an_awaited_search_crosses_the_thread_boundary`
exists rather than the property being covered incidentally. A borrowed connection must have
been opened the same way, and the `connection=` docstring says so, because this backend
cannot retrofit the flag onto a handle somebody else opened.

**The `pyturso` write-loss defect does not exist here, so its discipline is not needed.**
`turso_backend.md` documents at length why every read there goes through
`embedding.fetch_rows`: `pyturso` 0.7.2 silently discards pending uncommitted writes when a
`Cursor` holding an unfinalized SELECT is garbage collected, which broke the entry-id
counter and surfaced as a `UNIQUE constraint` failure three statements away. On stdlib
`sqlite3` the same shape is safe — measured, eight read-modify-write allocations with a
deliberately leaked unexhausted cursor each cycle produced ids 1 through 8, and a write
following a leaked cursor was still visible after `commit()`.
`test_a_leaked_cursor_does_not_discard_a_pending_write` is the mirror of the Turso suite's
`test_dropping_a_cursor_mid_select_discards_writes`: one asserts the defect is still there,
the other that it is still absent, so neither claim rests on a changelog.

Reads still go through `fetch_rows` anyway. Not out of necessity — `EmbeddingCache` uses it,
and one read path across the package is easier to audit than two — and nothing in this module
depends on the close.

The entry-id counter is still persisted rather than derived from `MAX(entry_id)`, and that is
unrelated to the driver: a deleted maximum would hand its id straight back out, and a stale
id from an earlier forward pass would then resolve to somebody else's entry.

## Extension loading is re-disabled, on purpose

`load_vector_extension` sets `enable_load_extension(True)`, loads, and closes the flag again
in a `finally`. An open flag lets any later SQL string reaching this connection load a shared
object from disk, and a memory backend is a place where SQL is assembled around
model-authored text. `test_the_extension_is_loaded_but_loading_is_left_disabled` asserts both
halves — `vec_version()` resolves, and `load_extension(...)` is refused with `not
authorized` — so "we re-disable it" is a checked property rather than a comment. Leaving the
flag open makes that test fail with a different error (`cannot open shared object file`),
which is how the check was confirmed to have teeth.

Loading happens on a borrowed connection too, since the extension is per-connection state and
a caller who opened the file themselves has no reason to have loaded it. It is idempotent, so
the borrow path does not have to know whether it already happened.

`enable_load_extension` is a CPython compile-time option
(`--enable-loadable-sqlite-extensions`) and some distro builds ship without it. That case
raises `VectorExtensionUnavailable` at connect time, naming the build and pointing at
`TursoMemoryBackend`, because the alternative is `no such function: vec_distance_cosine` on
the first retrieval — long after the cause, and indistinguishable from a missing package.

## `sqlite-vec` is a library-layer dependency, and the boundary still holds

`tests/library/test_boundary.py` forbids the library half from importing `polars`, `libsql`,
or `pm4py` — the measurable form of "the library needs no dataframe engine and no
process-mining package". `sqlite-vec` was *not* added to that forbidden set, and the AST
check and the subprocess import blocker both pass unchanged, because it is not that kind of
dependency: one small shared object, no transitive requirements, and required by a library
capability rather than by an application one. The property the boundary test protects is
unaffected.
