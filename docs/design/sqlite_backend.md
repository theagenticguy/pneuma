# `memory/sqlite_backend.py` — design rationale

Why there is a second memory backend, what actually differs from the first, the one capability
it has that the first cannot, and the decisions a reader would otherwise have to
reverse-engineer from a SQL string. The module docstring states the invariants and the
landmines; this file carries the arguments.
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

Two things are deliberately *not* shared. `SCHEMA` says so at its definition: the shared DDL is
a separate literal even though the four common tables are byte-identical today, because
importing the Turso module's schema would make a Turso-side column change a silent change to
this backend's storage, and the whole premise here is that the two engines are not the same
engine. And `_search` is now hybrid here and pure vector there — see "Hybrid retrieval" below.
That asymmetry is *not* a hole in the contract: `Retrieved`, `meta["results"]`, and
`meta["distances"]` keep their shape, so every ported assertion still holds and the gradient
chain does not know the difference. What the contract never promised is that two engines rank
identically, and it could not — Turso's `fts_score()` returns 0.0 for every matching row, so
there is no lexical ranking to port even if the code were shareable.

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

The lexical index does not travel either, and unlike the ceiling that is a repairable absence
rather than a caveat. A Turso-written file arrives with `memory_entry` rows and no
`memory_entry_fts` table at all, so `init_schema` creates it and `reindex_fts` backfills this
actor's entries on the first open. `test_a_turso_written_corpus_is_indexed_lexically_on_first_open`
asserts the reopened corpus ranks on both channels rather than silently falling back to one.
That backfill is also the reason FTS sync is manual rather than trigger-driven: a trigger cannot
index rows that predate it. See "Hybrid retrieval" below.

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

## Hybrid retrieval: FTS5 + vector, fused by RRF

This is the one capability that does **not** transfer to the Turso sibling, and it is an engine
difference rather than a design difference. `turso_backend.md` records why that backend has no
FTS path: Turso's full-text index exists, is Tantivy-backed, and `fts_score()` returns 0.0 for
*every* matching row — so it can select a matching set and cannot order it, and the ABC's `k`
would take an arbitrary subset. Stdlib `sqlite3` ships FTS5 with a real `bm25()`. Measured on
this build (SQLite 3.53.1, `ENABLE_FTS5`), one query over three entries scored -1.4506,
-1.07e-06, and -9.86e-07: three distinct ranks, ordered by match strength.
`test_bm25_is_negative_so_ascending_is_best_first` pins that as a property of the database, and
it is the direct counterpart of `test_turso_fts_cannot_rank_...` one file over. So hybrid
retrieval lives here because only this driver can rank lexically, and `_search` is hybrid by
default while the Turso sibling's stays pure vector.

`search_entries` is deliberately still pure vector. Three callers need a single-metric ranking
and a fused one would make them unsound — `probe_retrieval`, whose verdict is a margin between
two *distance* distributions; `calibrate_ceiling`, which turns that margin into a distance
threshold; and `EntryToolProvider`, which is imported from `turso_backend` and shared with a
backend that has no lexical channel. `hybrid_entries` is the fused path.

### Why an exact-term match is worth a second index at all

The rejection of `vec0` above argues that at these entry counts an exact cosine scan is both
cheap and honest. That argument is about *speed*, and it says nothing about *recall*. A
sentence embedder compresses a sentence to a few hundred floats, so a rare literal token — an
identifier, a case number, a spelled-out rule name — contributes almost nothing to the vector,
and a query containing it lands nowhere near the entry that holds it. No amount of exactness in
the distance calculation recovers a token the representation dropped.

`test_hybrid_retrieval_finds_a_lexical_hit_the_vector_channel_misses` makes that concrete with
a `TopicOnly` embedder, which sees only a topic word and is blind to everything else. Every
same-topic entry sits at distance 0.0 from every same-topic query, so the entry holding
`ORD-4471` is at 1.0 and unreachable in the vector top-3 no matter how many query terms it
matches verbatim. The lexical channel retrieves it. The pure-vector baseline is asserted in the
same test, because without it the claim would pass against a corpus where vector search already
worked.

`BagOfWords`, the double the rest of the suite uses, cannot serve here — it embeds token counts,
so its cosine ordering already approximates BM25 and the two channels agree on nearly every
query. A test that hybrid beats vector needs a corpus where they disagree, which is why there
are two doubles.

### RRF, not a weighted blend, and this is a defect-class decision

Cosine distance is on [0, 2], small-is-better. `bm25()` is unbounded, negative, and
corpus-dependent — measured, six orders of magnitude of range on a three-entry corpus. Any
`alpha * vector + (1 - alpha) * lexical` needs both a normalisation and an `alpha`, and neither
is measured. They would be fitted constants sitting in a ranking path where a wrong value fails
*soft*: the search still returns k plausible entries. That is precisely the defect class
`pneuma.detect` exists to catch, and it is the same argument `calibrate_ceiling` makes when it
refuses to invent a `distance_ceiling`.

Reciprocal Rank Fusion reads each channel's **rank position only**, so the two scales never
meet. It has one constant, `RRF_K = 60`, and the reason a constant is tolerable there is that it
is a *shape* parameter rather than a threshold: every value orders the fused list somehow, no
value can make the fusion return nothing or admit a hit neither channel found, so a wrong
`RRF_K` degrades ranking quality rather than changing what the result means.
`test_fusion_reads_ranks_not_scores` asserts that, and it also runs the mutant's arithmetic
inline on the real measured numbers — under `1 / (k + score)`, a bm25 hit at -1e-06 and a
perfect cosine match at 0.0 are indistinguishable, while on a larger corpus (bm25 -2.48) the
lexical term dominates outright. Neither behaviour is a ranking; both look like one.

**Over-fetching is what makes the fusion rule operative**, and writing the test for it taught
the sharper version of the claim. The obvious story — "over-fetch reaches a hit ranked k+1 by
vector and 1 by BM25" — is not the binding one, because a top-ranked lexical hit is already in
the k-length lexical list. The binding one is *agreement*: RRF's whole mechanism is that an
entry both channels endorse outscores an entry only one does, and an overlap between two lists
is only visible if the lists are long enough to contain it. Both channels therefore fetch
`3 * k` candidates. `test_over_fetching_is_what_makes_agreement_detectable` shows an entry
ranked second in both channels and first in neither winning at `overfetch=3` and losing at
`overfetch=1`, same corpus and same k.

A related property is worth stating because it surprised the tests twice: RRF promotes on
agreement, so a query whose terms are *shared* across the corpus buys less promotion than a
query whose terms discriminate. For `"procedure question about rule ORD-4471"` the exact-term
hit comes back second, not first, because the entry at vector rank 1 is also at lexical rank 3
and two endorsements beat one — `1/61 + 1/63 > 1/61`. That is the fusion working. Both cases are
asserted, so the behaviour is documented rather than discovered later as a bug.

### Deterministic tiebreaking, and a limit found by mutating

RRF ties are structural, not an edge case. Any two entries at the same rank in two
single-channel lists tie exactly, and a flat vector channel makes every same-topic pair tie. So
a fallback to dict iteration order would decide real winners — which is the defect
`detect/objective.py` records in its measured form: on a 21^3 metric grid, 21 points tied for
smallest `edge_share` while scoring from 0.0 to 0.9744, and which one became "the emptiest
answer" was `itertools.product`'s iteration order. The fix there and here is the same fix, and
`hybrid_entries` cites it: tiebreak on a measured quantity, then on a stable identifier.

    (-rrf_score, bm25_score, distance, entry_id)

`bm25_score` first among the tiebreakers because it is the requested rerank and it is exactly
the information RRF discarded on purpose, so it is the sharpest thing left. `entry_id` last, so
the order is total.

Two things about testing this are worth recording, both found by mutating.

**The obvious test was toothless.** The first draft ran the same search five times, then re-ran
it in subprocesses under three `PYTHONHASHSEED` values, and asserted one stable answer. It
passed with the entire tiebreak deleted, and it had to: CPython dicts iterate in *insertion*
order and hash seeding does not touch that, so the seed loop measured nothing. The real mutant
is swapping the channel lists, which changes the insertion sequence —
`test_an_rrf_tie_is_not_decided_by_which_channel_was_fused_first` does that, on a corpus where
insertion order says one entry and bm25 says another.

**And the explicit key is not provably load-bearing.** Deleting the tiebreak *and* fusing
lexical-first passes every test, and measured over 3000 random channel pairs that combination is
behaviourally identical to the explicit key — Python's sort is stable, so lexical-first
insertion preserves lexical order inside a tie, which is what ordering on `bm25` does. The key
stays anyway, and the reason is not coverage: the equivalence rests on two coincidences a reader
cannot see (dict insertion order, sort stability) and would break silently the moment a
channel's fetch order stopped matching its score order. That is a claim about legibility, and it
is not one a test can make. Recorded here rather than left for somebody to "simplify".

### The index is derived state, so its drift is countable

`memory_entry_fts` is a second copy of the entry text, and the `vec0` rejection below turns
partly on not duplicating state that can diverge. The difference that makes this duplication
acceptable is auditability: a drifted *vector* fails by returning plausible neighbours nobody
can check, while a drifted *text* copy is directly comparable to its source in one query.
`fts_drift` is that query, and it sits beside `unranked_entries` and `degenerate_entries` for
the same reason they exist. It reports three kinds separately because they need different fixes
— `missing` costs lexical recall, `stale` returns the entry for words it no longer contains,
`orphaned` shrinks the candidate set through a join that finds nothing — and `reindex_fts`
repairs in one direction only, since there is no case where the index is right and the entry is
wrong.

**Sync is manual rather than trigger-driven**, and two facts force it. `init_schema` splits
`SCHEMA` on `;` and runs the statements one at a time — deliberately, because `executescript`
issues an implicit `COMMIT` that would commit a borrowed connection's in-flight transaction —
and a `CREATE TRIGGER ... BEGIN ...; ...; END` body contains semicolons, so the split hands
SQLite `OperationalError: incomplete input`. Measured, not inferred. More decisively, a trigger
cannot index rows that predate it, and two *supported* arrangements produce exactly that: a
database written by `TursoMemoryBackend` and reopened here (the portability property the section
above rests on) has no FTS table at all until `init_schema` creates one, and a `connection=`
handle somebody else wrote entries through never went via this module. A backfill is required
either way, so `init_schema` calls `reindex_fts` on every open and a trigger would buy nothing.
`test_a_turso_written_corpus_is_indexed_lexically_on_first_open` is the guard, and the failure it
prevents is a quiet one: the lexical channel would be permanently empty for a ported corpus, and
`_search` would report `channels == ["vector"]` forever with nobody reading it.

The index is **not** an FTS5 external-content table (`content='memory_entry'`), which would
store no duplicate text. That form binds the index to `memory_entry.rowid`, an implicit alias on
a table whose primary key is the composite `(actor_id, param, entry_id)`. Nothing in this module
reads that rowid, so nothing would notice it changing — and measured, `VACUUM` after a delete
leaves surviving rowids alone *this* time, which is worse than a guarantee because it invites the
assumption. Every joinable identity in this file is already the composite key, so the index
carries those three columns `UNINDEXED` and joins on them. `UNINDEXED` matters on its own:
without it an actor id or a parameter name becomes a searchable term, so a query mentioning
`guidance` would match every entry of the `guidance` parameter at a score indistinguishable from
a real content match.

### FTS5 query safety: MATCH is a language, and binding does not protect it

Query text here is model-authored or user-authored prose, and it goes into `MATCH`. Every one of
these was measured on this build rather than read off the documentation:

    MATCH ''      -> OperationalError: fts5: syntax error near ""
    MATCH 'AND'   -> OperationalError: fts5: syntax error near "AND"
    MATCH 'NEAR(' -> OperationalError: fts5: syntax error near ""
    MATCH '-x'    -> OperationalError: no such column: x
    MATCH 'a:b'   -> OperationalError: no such column: a

So a bare `AND` in an entry query, an unbalanced paren from a truncated prompt, or a colon in
`note: check this` each raise from inside a ranking path — and the last two raise `no such
column`, which reads as a schema bug and sends a reader to the DDL. Note where the danger is
*not*: binding the parameter stops SQL injection into the statement, and MATCH parsing happens
afterwards on the bound value. The placeholder is no protection here at all.

`fts_match_expression` tokenises the query and re-quotes each token: `"tok" OR "tok" OR ...`.
Two cheaper fixes were tried and both are wrong. Escaping only `"` leaves `-x`, `a:b`, and `(`
still raising. And wrapping the whole query in one quoted literal parses fine but means a
*phrase* search — measured, `"he said ""AND"" loudly"` returns zero rows against a corpus holding
all four words — so the lexical channel would silently contribute nothing for any query that is
not an exact substring. That mutant never raises, which makes it the worse of the two, and
`test_an_operator_word_is_matched_as_a_term_not_parsed_as_an_operator` runs it inline so its
silence is visible.

Tokens are OR-ed rather than AND-ed because a disjunction is the recall-shaped choice: an AND
over a nine-word question would require every word present, and the channel would be empty in
exactly the cases hybrid retrieval exists to catch. Ranking within the matched set is `bm25()`'s
job and it already rewards a row matching more of the query.

### Honest degradation, and one behaviour this fixes

`meta["channels"]` names which rankings produced the hits, so a fallback is auditable from an
event log rather than invisible. Four cases:

| channels | when | behaviour |
| --- | --- | --- |
| `["vector", "fts"]` | both ran and matched | the normal case |
| `["vector"]` | no query term is in the index, or the query has no indexable token | pure vector, unchanged |
| `["fts"]` | the query embedding is degenerate but terms matched | pure lexical — see below |
| raise | neither channel can rank | names both reasons |

The `["fts"]` case is a **behaviour fix**, and it is the one place hybrid retrieval changes an
answer rather than adding one. `search_entries` raises on a zero-magnitude query vector, and
rightly: it has one channel, every distance is NULL, and `[]` would read as "this corpus has
nothing relevant" — the argument in "The NULL-handling decision" above. `hybrid_entries` has two
channels, so in exactly that case the refusal would be discarding a real, rankable result to
protect a distinction that is no longer at risk. BM25 needs no embedding. The two paths
deliberately disagree, and `test_a_degenerate_query_embedding_falls_back_to_the_lexical_channel`
asserts both halves in one test because the disagreement is the finding.

Only when *both* channels are out does it raise, and the message names both reasons — naming
only the embedding would invite a caller to re-embed and retry against a corpus that also had no
lexical hit. Returning `[]` there was tried, and every caller-visible signal was identical to a
legitimate miss, which is guard 3's argument one level up. The dual failure is reachable from a
plain input rather than only a constructed one: `""` under `BagOfWords` is both a MATCH syntax
error and the zero vector.

A lexical-only hit carries `distance=inf`, not a substituted number. `inf` reads as "cosine did
not rank this", where 0.0 would claim a perfect vector match and 2.0 a worst-case one. That
choice has one consequence worth spelling out: **`distance_ceiling` applies to the vector channel
only.** A hit the vector channel scored is capped normally, even when it is also the top lexical
hit — the ceiling is a claim about the embedding and the embedding made one. A hit the vector
channel never scored is exempt, because `inf <= ceiling` is False and a plain comparison would
silently drop every lexical-only hit. There is deliberately no BM25 counterpart to calibrate:
bm25 scores are unbounded, negative, and move with corpus size and term frequency, so "far" has
no fixed meaning and any threshold would be the fitted constant this whole path refuses.

For the same reason, `probe_retrieval` and `calibrate_ceiling` measure the **vector channel**.
Every quantity in a `Discrimination` verdict is a cosine distance, and a fused top hit carrying
`inf` would put an infinity into all of them and make the verdict meaningless while making it
*look* decisive. The consequence is worth stating plainly: a `discriminates` verdict is a claim
about the embedding, and a corpus whose embedding does not discriminate may still retrieve well
through `_search`. That is not the guard being wrong — the failure it exists to catch is a
useless embedding, and a lexical channel does not make a useless embedding useful.

### The gradient contract is unchanged

`meta["results"]` and `meta["distances"]` keep their shape and meaning through the hybrid path,
because the four-link chain that makes a gradient narrow is a contract with the library and not
with this file. The hybrid path adds `channels`, `rrf_k`, and `lexical_metric` alongside the
existing `distance_metric`, for the same auditability reason `distance_metric` exists:
`lexical_metric` is the field that says a row came from the backend whose engine can rank
lexically, and a `TursoMemoryBackend` row never carries it.

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

That argument is in obvious tension with `memory_entry_fts`, which *does* duplicate the entry
text and *is* maintained incrementally through those same four write paths. The tension is real
and the resolution is auditability, not consistency: a drifted vector cannot be compared to
anything, because the only way to check a cached embedding is to re-embed and pay the provider,
while a drifted text copy is `f.value <> e.value` in one query. So `fts_drift` exists and there
is no `vec_drift` that could. If a future `vec0` table came with an equally cheap oracle, this
argument would need re-weighing on that basis rather than on duplication as such — and the
closing paragraph of this section already points at the shape that would have one.

**Approximate answers make the discrimination measurement unsound.** `probe_retrieval`'s
verdict is a margin between two distance distributions, and `calibrate_ceiling` derives a
threshold from the *worst* relevant hit and the *closest* control hit. Both are extremes,
and an index that may omit a true neighbour perturbs exactly the extremes. A ceiling
calibrated against approximate distances would be applied to approximate distances and might
well be self-consistent, which is worse than being wrong: the guard would agree with itself
while both halves were measuring something other than the corpus.

**An exact scan is honest about what it computes, and cheaply auditable.** A dozen-plus tests in
the suite assert on distances, four of them by binding hand-packed vectors and reading
the metric's answer straight back — including the two that pin NULL-first ordering, which is
the behaviour the whole retrieval path is shaped around. That kind of assertion is available
only because the ranking query is an ordinary scan a reader can evaluate by hand; against a
`MATCH ... AND k = ?` index the same claims would be measured through the thing under test.
The hybrid tests lean on it harder still: every fusion assertion states the exact distances and
bm25 scores the two channels produced, and that is only writable because both channels' rankings
are computable by hand from the corpus.

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

Hybrid retrieval adds **no dependency at all**, which is part of why FTS5 is the right lexical
engine here rather than a vendored BM25 or an external index. FTS5 is compiled into the same
stdlib `sqlite3` the backend already opens — verified on this build, `PRAGMA compile_options`
reports `ENABLE_FTS5` — so the second channel costs one virtual table in a file the caller
already has. A pure-Python BM25 would have meant materializing the corpus in Python on every
search, which is the thing the vector channel's in-database ranking exists to avoid.
