"""The stdlib-`sqlite3` + `sqlite-vec` + FTS5 memory backend: one shared contract, seven guards.

Three jobs, and keeping them apart is the point of the section headings below.

**The shared contract.** Everything `tests/library/test_turso_memory.py` asserts about
behaviour — monotonic never-reused ids, ranking by cosine distance ASC, a content-addressed
cache that re-embeds a rewritten entry, the `meta["results"]` chain that makes a gradient
narrow, the numeric trust-region search, three-valued discrimination — is asserted again
here against `SqliteMemoryBackend`. Ported rather than parametrised over both backends: the
Turso module's fixtures encode Turso-only facts (a `vector32()` equality, a cursor-GC
regression guard) and folding them into one parametrised suite would either skip half the
cases per backend or make each test carry a driver switch. Two readable suites that agree on
the claims beat one that hedges. Where a claim is *only* provable jointly — that both
backends propose the same number from the same score history — the assertion is here and
says so.

**The three new guards**, each of which was broken deliberately once and observed to fail:

1. `vec_distance_cosine` returns NULL for a zero-magnitude vector and SQLite sorts NULL
   first, so a degenerate entry becomes the top hit. Removing `AND distance IS NOT NULL`
   makes `test_a_degenerate_vector_is_not_returned_as_the_nearest_hit` fail with the
   `TypeError` from `float(None)`.
2. `check_same_thread=False`. The base class runs every `_recall` / `_query` / `_search`
   inside `asyncio.to_thread`. Dropping the flag makes
   `test_an_awaited_search_crosses_the_thread_boundary` fail with `ProgrammingError`.
3. A query vector with zero magnitude must raise, not return `[]`. Dropping
   `_require_rankable_query` makes `test_a_degenerate_query_vector_raises_...` fail, because
   the search returns an empty list that reads exactly like an irrelevant corpus.

**The hybrid retrieval path**, which is this backend's own capability rather than a port:
`_search` fuses the vector ranking with an FTS5 `bm25()` one by RRF, because only this
driver's FTS can rank at all (Turso's `fts_score()` is 0.0 for every row). Four more guards,
each also broken deliberately and observed to fail:

4. The lexical index must not drift from `memory_entry`. Dropping `_fts_replace` from
   `update_entry` makes `test_a_rewritten_entry_re_indexes_lexically` fail — and the failure
   is the *interesting* one: the entry still retrieves, on its pre-rewrite words.
5. RRF must fuse on ranks, never on the raw scores. Fusing `1 / (k + score)` instead makes
   `test_fusion_reads_ranks_not_scores` fail, because a bm25 score of -1e-06 and a cosine
   distance of 1.0 are six orders of magnitude apart and the lexical channel dominates
   arithmetically.
6. Query text must be escaped into a MATCH expression. Passing it through raw makes
   `test_a_query_with_fts5_operators_is_not_a_syntax_error` fail with `fts5: syntax error`
   or, worse, `no such column: x`, which reads as a schema bug rather than as untrusted input.
7. A fallback must not mask a dual failure. Returning `[]` when both channels are out makes
   `test_both_channels_unavailable_raises_rather_than_returning_nothing` fail, which is guard
   3's argument one level up.

Nothing here needs credentials or a network. The live retrieval measurement — whether Cohere
Embed v4 semantically separates a real playbook — is a property of the embedding model, not
of the driver, and is measured once in the Turso module rather than duplicated here.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types.events import ParameterRecalledEvent
from ai_functions.types.graph import GradFeedback
from pydantic import BaseModel, Field

from pneuma.memory import (
    RRF_K,
    CeilingNotSeparable,
    SqliteMemoryBackend,
    digest_of,
    fts_match_expression,
    pack_vector,
    reciprocal_rank_fusion,
    sqlite_connect,
    unpack_vector,
)
from pneuma.memory.embedding import DOCUMENT, QUERY, fetch_one, fetch_rows

# ── Fixtures and doubles ──


class BagOfWords:
    """Deterministic embedder: L2-normalised token-count vectors.

    The same double the Turso suite uses, and for the same reason: cosine over token
    counts ranks by lexical overlap, which is the *wrong* retrieval model. Every offline
    assertion here is about plumbing — does the id reach consolidation, does the cache
    invalidate, does a NULL distance get filtered — and a fake that cannot be mistaken for
    a real embedding keeps those assertions honest.
    """

    model_id = "bagofwords:v1"

    def __init__(self, dimensions: int = 128) -> None:
        self._dimensions = dimensions
        self.calls = 0
        self.texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        assert input_type in (DOCUMENT, QUERY)
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            self.texts.append(text)
            vector = [0.0] * self._dimensions
            for token in text.lower().replace(".", " ").replace(",", " ").split():
                vector[_stable_bucket(token) % self._dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


def _stable_bucket(token: str) -> int:
    """Hash a token without `hash()`, whose salt varies per interpreter run."""
    total = 0
    for character in token:
        total = (total * 131 + ord(character)) % 1_000_003
    return total


class Constant:
    """An embedder with no signal at all: every text gets the same unit vector.

    Search still returns a full ranked list against this, which is the whole reason
    `probe_retrieval` exists rather than a smoke test on `search`.
    """

    model_id = "constant"
    dimensions = 4

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        del input_type
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class Zeros:
    """An embedder that returns the zero vector — degenerate, not merely uninformative.

    Not a contrived double. A truncated or all-out-of-vocabulary text through a real
    provider can produce this, and `vec_distance_cosine` answers NULL rather than raising,
    so it is the input that makes NULL-first ordering reachable in production.
    """

    model_id = "zeros"
    dimensions = 4

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        del input_type
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


TOPICS = ("procedure", "timing", "money")


class TopicOnly:
    """An embedder that sees only the topic word and is blind to every other token.

    The double the hybrid tests need, and it is not a strawman — it is a caricature of the
    failure a real embedding model actually has. A sentence embedder compresses a sentence to
    a few hundred floats, so a rare literal token (an identifier, a case number, a
    spelled-out rule name) contributes almost nothing to the vector and a query containing it
    lands nowhere near the entry that holds it. `TopicOnly` makes that exact loss
    *reproducible*: every entry on one topic is at cosine distance 0.0 from every query on
    that topic, so ordering inside a topic is a tie the vector channel cannot break, and an
    entry on a different topic is unreachable no matter how many query terms it contains
    verbatim.

    `BagOfWords` cannot serve here, and that is the point of adding a second double: it
    embeds token counts, so its cosine ordering *already* approximates BM25 and the two
    channels agree on almost every query. A test that hybrid beats vector needs a corpus
    where they disagree.
    """

    model_id = "topiconly:v1"
    dimensions = len(TOPICS)

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Any, input_type: str) -> list[list[float]]:
        assert input_type in (DOCUMENT, QUERY)
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            vector = [1.0 if topic in text.lower() else 0.0 for topic in TOPICS]
            if not any(vector):
                vector = [1.0] * len(TOPICS)  # no topic named: equidistant from all of them
            norm = math.sqrt(sum(x * x for x in vector))
            vectors.append([x / norm for x in vector])
        return vectors


class CaptureFn:
    """Stand-in for a consolidation `AIFunction`: records kwargs, returns a fixed string.

    Preferred over a `ScriptedModel` for the same reason the Turso suite gives: the real
    consolidators are structured `@ai_function[str]`s, so scripting them would force
    `structured_output=False` and exercise a path production never takes. What these tests
    are about — which entries were shown, what the store held afterwards — is visible
    either way.
    """

    def __init__(self, returns: str = "done") -> None:
        self.returns = returns
        self.kwargs: dict[str, Any] | None = None
        self.tools: Any = None
        self.calls = 0

    def replace(self, **overrides: Any) -> CaptureFn:
        self.tools = overrides.get("tools", self.tools)
        return self

    def run_sync(self, **kwargs: Any) -> str:
        self.calls += 1
        self.kwargs = kwargs
        return self.returns


class Advice(BaseModel):
    """A playbook-shaped schema: entries, a scalar, and two numeric parameters."""

    guidance: list[str] = Field(default_factory=list, description="Advice entries.")
    summary: str = Field("nothing yet", description="A scalar note.")
    threshold: int = Field(20, ge=1, le=100, description="Support threshold.")
    ratio: float = Field(0.5, ge=0.0, le=1.0, description="A fractional knob.")


def _backend(tmp_path: Path, **kwargs: Any) -> SqliteMemoryBackend:
    kwargs.setdefault("embedder", BagOfWords())
    return SqliteMemoryBackend(Advice, actor_id="nav", path=tmp_path / "mem.db", **kwargs)


ENTRIES = (
    "never revisit a state this case has already passed through",
    "prefer the transition whose target ends the case",
    "an appeal may only follow a fine that was already sent",
)


def _seeded(tmp_path: Path) -> tuple[SqliteMemoryBackend, list[str]]:
    memory = _backend(tmp_path)
    return memory, [memory.add_entry("guidance", text) for text in ENTRIES]


# ── sqlite-vec primitives ──


def test_pack_vector_is_exactly_what_sqlite_vec_serializes(tmp_path: Path) -> None:
    """`pack_vector` and `sqlite_vec.serialize_float32` produce the same bytes.

    The assumption the whole retrieval path rests on, and the reason this module reuses
    `embedding.pack_vector` instead of adding a second packer. It is also what makes an
    embedding cache written by the Turso backend rankable here: both are little-endian
    float32 packed end to end. If it ever stopped holding, distances would still be
    computed and still be ordered, and they would be ordered by garbage.
    """
    values = [1.0, 0.0, -2.5]
    assert pack_vector(values) == sqlite_vec.serialize_float32(values)
    assert unpack_vector(pack_vector(values)) == pytest.approx(values)

    memory = _backend(tmp_path)
    row = fetch_one(
        memory.connection,
        "SELECT vec_distance_cosine(?, ?)",
        (pack_vector(values), sqlite_vec.serialize_float32(values)),
    )
    assert row is not None and row[0] == pytest.approx(0.0, abs=1e-6)
    memory.close()


def test_cosine_distance_is_zero_for_identical_and_one_for_orthogonal(tmp_path: Path) -> None:
    """`vec_distance_cosine` is cosine distance, so ASCending order is nearest-first."""
    memory = _backend(tmp_path)
    same = fetch_one(
        memory.connection,
        "SELECT vec_distance_cosine(?, ?)",
        (pack_vector([1.0, 0.0]), pack_vector([1.0, 0.0])),
    )
    orthogonal = fetch_one(
        memory.connection,
        "SELECT vec_distance_cosine(?, ?)",
        (pack_vector([1.0, 0.0]), pack_vector([0.0, 1.0])),
    )
    assert same is not None and orthogonal is not None
    # Exactly 0.0 here, where Turso's `vector_distance_cos` returns 4.47e-08 for the same
    # pair. Recorded because it is why a calibrated ceiling is not portable between them.
    assert same[0] == pytest.approx(0.0, abs=1e-6)
    assert orthogonal[0] == pytest.approx(1.0, abs=1e-6)
    memory.close()


def test_a_zero_magnitude_vector_yields_a_null_distance_not_an_error(tmp_path: Path) -> None:
    """The engine behaviour the `IS NOT NULL` filter exists for, asserted directly.

    Two facts, and the pairing is what makes the defect reachable: `vec_distance_cosine`
    returns NULL rather than raising for a zero-magnitude vector, and SQLite orders NULL
    *first* under `ORDER BY ... ASC`. Turso does neither — measured, `vector_distance_cos`
    on the same pair returns 1.0 — so this is the one retrieval hazard that is genuinely
    new in this backend.

    Asserted as a property of the database so that a `sqlite-vec` upgrade which starts
    raising, or returning a number, shows up here rather than as a filter nobody can
    justify any more.
    """
    memory = _backend(tmp_path)
    row = fetch_one(
        memory.connection,
        "SELECT vec_distance_cosine(?, ?)",
        (pack_vector([0.0, 0.0]), pack_vector([1.0, 0.0])),
    )
    assert row is not None and row[0] is None, "a zero vector stopped producing NULL"

    memory.connection.execute("CREATE TABLE probe (id TEXT, v BLOB)")
    memory.connection.execute("INSERT INTO probe VALUES ('zero', ?)", (pack_vector([0.0, 0.0]),))
    memory.connection.execute("INSERT INTO probe VALUES ('near', ?)", (pack_vector([1.0, 0.0]),))
    ordered = fetch_rows(
        memory.connection,
        "SELECT id, vec_distance_cosine(v, ?) AS d FROM probe ORDER BY d ASC",
        (pack_vector([1.0, 0.0]),),
    )
    assert [row[0] for row in ordered] == ["zero", "near"], "NULL stopped sorting first"
    memory.close()


def test_a_null_blob_still_raises_which_is_why_the_join_is_inner(tmp_path: Path) -> None:
    """A missing vector cannot be scored at all, so retrieval joins rather than outer-joins.

    Same conclusion as Turso reached for a different reason (there the error is
    `Conversion error: Invalid vector type`). Recorded so the inner join is not relaxed to
    a LEFT JOIN on the theory that the NULL filter now covers it — it does not, because
    this raises before any filter runs.
    """
    memory = _backend(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="found NULL"):
        fetch_one(
            memory.connection,
            "SELECT vec_distance_cosine(?, ?)",
            (None, pack_vector([1.0, 0.0])),
        )
    memory.close()


def test_the_extension_is_loaded_but_loading_is_left_disabled(tmp_path: Path) -> None:
    """`sqlite-vec` works, and the connection cannot be asked to load anything else.

    An open `enable_load_extension(True)` lets any later SQL reaching this connection load
    a shared object from disk, and a memory backend assembles SQL around model-authored
    text. So the flag is closed again after loading, and this asserts both halves —
    otherwise "we re-disable it" is a comment.
    """
    memory = _backend(tmp_path)
    assert fetch_one(memory.connection, "SELECT vec_version()") is not None
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        memory.connection.execute("SELECT load_extension('nonexistent_object')")
    memory.close()


def test_wal_is_enabled_so_a_reader_does_not_block_the_writer(tmp_path: Path) -> None:
    """WAL for the reason `casestudy.eventlog.connect` sets it, asserted not assumed."""
    memory = _backend(tmp_path)
    row = fetch_one(memory.connection, "PRAGMA journal_mode")
    assert row is not None and row[0] == "wal"
    assert (tmp_path / "mem.db-wal").exists()
    memory.close()


def test_a_leaked_cursor_does_not_discard_a_pending_write(tmp_path: Path) -> None:
    """The `pyturso` write-loss defect is absent here, which is why no discipline guards it.

    `test_turso_memory.py` asserts the defect still *exists* on that driver, so that
    `fetch_rows`'s explicit close is not removed as redundant. This is the mirror claim: on
    stdlib `sqlite3` the same shape is safe, and eight read-modify-write allocations with a
    deliberately leaked unexhausted cursor per cycle produce distinct ids. It exists so the
    module docstring's "the discipline is unneeded here" is a checked property rather than
    an inference from a different driver's changelog.
    """
    memory = _backend(tmp_path)

    def leak_a_cursor() -> None:
        cursor = memory.connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM memory_entry")
        cursor.fetchone()  # active, unexhausted; the cursor dies on return

    leak_a_cursor()
    memory.connection.execute(
        "INSERT OR REPLACE INTO memory_scalar VALUES ('nav', 'summary', 'survived', 0.0)"
    )
    memory.connection.commit()
    assert memory._read_scalar("summary") == "survived"

    ids = [memory.add_entry("guidance", f"entry number {i}") for i in range(8)]
    assert ids == [str(i) for i in range(1, 9)], "the id counter lost an increment"
    memory.close()


# ── Entry storage and id stability ──


def test_entry_ids_are_never_reused_across_delete_and_reopen(tmp_path: Path) -> None:
    """A retired id is never handed out again, which is what makes a stale id safe.

    `meta["results"]` records ids during the forward pass and `consolidate` resolves them a
    round later. If an id could be reused, a gradient about a deleted entry would land on
    whatever entry inherited its id — a wrong edit with no error.
    """
    memory, ids = _seeded(tmp_path)
    assert memory.remove_entry("guidance", ids[1]) is True
    assert memory.remove_entry("guidance", ids[1]) is False, "a retired id resolves to nothing"
    after_delete = memory.add_entry("guidance", "added after a delete")
    assert after_delete not in ids
    memory.close()

    reopened = _backend(tmp_path)
    assert reopened.add_entry("guidance", "added after a reopen") not in {*ids, after_delete}
    assert set(reopened.list_entries("guidance")) == {ids[0], ids[2], after_delete, "5"}
    reopened.close()


def test_save_replaces_entries_and_retires_their_ids(tmp_path: Path) -> None:
    """A wholesale `save` retires the old ids rather than renumbering onto them."""
    memory, ids = _seeded(tmp_path)
    memory.save("guidance", ["only this now"])
    fresh = memory.list_entries("guidance")
    assert list(fresh.values()) == ["only this now"]
    assert not set(fresh) & set(ids)
    memory.close()


def test_schema_defaults_seed_on_first_open_and_survive_a_reopen(tmp_path: Path) -> None:
    """Defaults are written once; a reopen preserves learned values instead of reseeding."""

    class Seeded(BaseModel):
        notes: list[str] = Field(default_factory=lambda: ["seed one", "seed two"], description="d")
        label: str = Field("seed label", description="d")

    first = SqliteMemoryBackend(
        Seeded, actor_id="s", path=tmp_path / "s.db", embedder=BagOfWords()
    )
    assert list(first.list_entries("notes").values()) == ["seed one", "seed two"]
    first.save("label", "learned label")
    first.remove_entry("notes", next(iter(first.list_entries("notes"))))
    first.close()

    second = SqliteMemoryBackend(
        Seeded, actor_id="s", path=tmp_path / "s.db", embedder=BagOfWords()
    )
    assert list(second.list_entries("notes").values()) == ["seed two"], "reseeded on reopen"
    assert second.fetch("label") == "learned label"
    second.close()


def test_actors_are_isolated_in_one_file(tmp_path: Path) -> None:
    """Two actors share the database without seeing each other's entries."""
    path = tmp_path / "shared.db"
    a = SqliteMemoryBackend(Advice, actor_id="a", path=path, embedder=BagOfWords())
    b = SqliteMemoryBackend(Advice, actor_id="b", path=path, embedder=BagOfWords())
    a.add_entry("guidance", "belongs to a")
    b.add_entry("guidance", "belongs to b")
    assert list(a.list_entries("guidance").values()) == ["belongs to a"]
    assert list(b.list_entries("guidance").values()) == ["belongs to b"]
    a.close()
    b.close()


def test_backend_id_names_this_backend_not_the_turso_one(tmp_path: Path) -> None:
    """`backend_id` is what `build_graph` matches a recall event back to.

    It must differ from `TursoMemoryBackend:nav`, or two backends over the same actor in
    one process would claim each other's recall events and gradients would be routed to
    the wrong store with nothing raising.
    """
    memory = _backend(tmp_path)
    assert memory.backend_id == "SqliteMemoryBackend:nav"
    memory.close()


def test_entry_operations_reject_a_scalar_parameter(tmp_path: Path) -> None:
    """Entry CRUD is list-only; a scalar raises rather than silently doing nothing."""
    memory = _backend(tmp_path)
    with pytest.raises(TypeError, match="list parameters"):
        memory.list_entries("summary")
    with pytest.raises(TypeError, match="list parameters"):
        memory.search_entries("summary", "anything")
    with pytest.raises(TypeError, match="list parameters"):
        memory.degenerate_entries("summary")
    memory.close()


def test_a_backend_needs_a_path_or_a_connection(tmp_path: Path) -> None:
    """Neither is a construction error, not a database quietly opened somewhere."""
    del tmp_path
    with pytest.raises(ValueError, match="path or a connection"):
        SqliteMemoryBackend(Advice, actor_id="nav", embedder=BagOfWords())


def test_a_shared_connection_is_not_closed_and_gets_the_extension(tmp_path: Path) -> None:
    """Colocation requires the backend not to close a borrowed handle — and to equip it.

    Two claims in one test because they are one arrangement. The caller owns the
    connection, so `close()` must leave it usable. But the vector extension is
    *per-connection* state, so a handle the caller opened has no `vec_distance_cosine` and
    retrieval over it would fail with `no such function` — hence `load_vector_extension` on
    the borrowed connection too, exercised here by searching through it.
    """
    connection = sqlite3.connect(tmp_path / "audit.db", check_same_thread=False)
    connection.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO audit_events VALUES (1)")
    connection.commit()

    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", connection=connection, embedder=BagOfWords()
    )
    memory.add_entry("guidance", "a parameter beside the evidence")
    assert memory.search_entries("guidance", "a parameter", k=1), "the borrowed handle cannot rank"
    memory.close()

    # Still usable, and both kinds of row are in the one file.
    assert fetch_one(connection, "SELECT COUNT(*) FROM audit_events") == (1,)
    assert fetch_one(connection, "SELECT COUNT(*) FROM memory_entry") == (1,)
    connection.close()


def test_sqlite_connect_opens_a_ready_connection_a_caller_can_share(tmp_path: Path) -> None:
    """The public opener, so a caller colocating evidence does not have to know the pragmas."""
    connection = sqlite_connect(tmp_path / "own.db")
    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", connection=connection, embedder=BagOfWords()
    )
    memory.add_entry("guidance", "colocated")
    assert memory.search_entries("guidance", "colocated", k=1)
    memory.close()
    assert fetch_one(connection, "PRAGMA journal_mode") == ("wal",)
    connection.close()


# ── Thread affinity: the guard the base class's `to_thread` demands ──


async def test_an_awaited_search_crosses_the_thread_boundary(tmp_path: Path) -> None:
    """`check_same_thread=False` is required, and this is the test that proves it.

    `MemoryBackend.recall` / `.query` / `.search` each run their `_*` hook inside
    `asyncio.to_thread`, so the very first awaited recall touches the connection from a
    worker thread. Without the flag, stock `sqlite3` raises `ProgrammingError: SQLite
    objects created in a thread can only be used in that same thread` — verified by
    removing the flag, at which point this test and every other awaited one in this module
    fail. The synchronous tests all keep passing, which is why the flag needs its own
    assertion rather than being covered incidentally.
    """
    memory, ids = _seeded(tmp_path)
    view = await memory.search("guidance", "revisit a state already passed through", k=2)
    assert ids[0] in view.meta["results"]

    recalled = await memory.recall("guidance")
    assert list(recalled.value) == list(ENTRIES)

    # And directly, so the failure mode is named rather than inferred from a coroutine.
    assert await asyncio.to_thread(memory.numeric_value, "threshold") == 20.0
    memory.close()


# ── Retrieval ──


async def test_search_ranks_by_distance_and_honours_k(tmp_path: Path) -> None:
    """Top-k ordering is `vec_distance_cosine` ascending, and `k` is respected."""
    memory, ids = _seeded(tmp_path)
    hits = memory.search_entries("guidance", "revisit a state already passed through", k=2)
    assert len(hits) == 2
    assert hits[0].entry_id == ids[0]
    assert hits[0].distance < hits[1].distance
    memory.close()


def test_search_rejects_a_k_below_one(tmp_path: Path) -> None:
    """`k=0` is a caller bug, and an empty list would look like an empty corpus."""
    memory, _ = _seeded(tmp_path)
    with pytest.raises(ValueError, match="k must be >= 1"):
        memory.search_entries("guidance", "anything", k=0)
    memory.close()


async def test_search_on_an_empty_parameter_returns_nothing_without_embedding(
    tmp_path: Path,
) -> None:
    """An empty corpus returns `[]` and never calls the provider.

    Worth asserting because the alternative — embedding the query anyway — costs a round
    trip per decision in exactly the state where there is nothing to retrieve.
    """
    embedder = BagOfWords()
    memory = _backend(tmp_path, embedder=embedder)
    assert (await memory.search("guidance", "anything")).value == []
    assert embedder.calls == 0
    memory.close()


def test_distance_ceiling_drops_far_hits_including_all_of_them(tmp_path: Path) -> None:
    """A ceiling can return nothing, and that honest emptiness is the point.

    An agent handed the best of a bad set cannot tell it from good advice. Returning
    nothing at least lets a caller say so.
    """
    memory, _ = _seeded(tmp_path)
    assert memory.search_entries("guidance", "revisit a state", k=3), "unbounded finds something"
    memory.distance_ceiling = 0.0
    assert memory.search_entries("guidance", "completely unrelated words", k=3) == []
    memory.close()


def test_embedding_cache_is_content_addressed_so_a_rewrite_re_embeds(tmp_path: Path) -> None:
    """The staleness property, and the reason the cache key is the text not the id.

    Keying on the entry id would serve the pre-rewrite vector for post-rewrite text. The
    failure would be silent, because a vector search always returns something ranked — so
    the cache would degrade retrieval and every observable signal would look identical.
    """
    embedder = BagOfWords()
    memory = _backend(tmp_path, embedder=embedder)
    entry_id = memory.add_entry("guidance", "original wording of the advice")

    memory.search_entries("guidance", "original wording", k=1)
    after_first = embedder.calls
    assert after_first > 0

    memory.search_entries("guidance", "original wording", k=1)
    assert embedder.calls == after_first, "an unchanged corpus and query must not re-embed"

    memory.update_entry("guidance", entry_id, "completely different wording now")
    assert memory.embed_pending("guidance") == 1, "the rewritten entry must re-embed"
    assert memory.embed_pending("guidance") == 0, "and only once"

    # The digest moved with the text, which is what made the cache miss.
    row = fetch_one(
        memory.connection,
        "SELECT digest FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
        ("nav", "guidance", entry_id),
    )
    assert row is not None and row[0] == digest_of("completely different wording now")
    memory.close()


def test_document_and_query_embeddings_are_cached_separately(tmp_path: Path) -> None:
    """Cohere v4 embeds asymmetrically, so one cache row per text is a ranking bug."""
    memory = _backend(tmp_path)
    text = "identical text embedded both ways"
    memory.cache.ensure([text], DOCUMENT)
    memory.cache.ensure([text], QUERY)
    rows = fetch_rows(
        memory.connection,
        "SELECT input_type FROM memory_embedding_cache WHERE digest = ?",
        (digest_of(text),),
    )
    assert {row[0] for row in rows} == {DOCUMENT, QUERY}
    memory.close()


def test_unranked_entries_makes_a_missing_vector_countable(tmp_path: Path) -> None:
    """An entry with no vector is invisible to the inner join, so it is counted."""
    memory, ids = _seeded(tmp_path)
    memory.embed_pending("guidance")
    assert memory.unranked_entries("guidance") == []

    memory.connection.execute("DELETE FROM memory_embedding_cache")
    memory.connection.commit()
    assert set(memory.unranked_entries("guidance")) == set(ids)
    memory.close()


# ── The NULL-distance guard: this backend's own failure mode ──


def test_a_degenerate_vector_is_not_returned_as_the_nearest_hit(tmp_path: Path) -> None:
    """A zero-magnitude vector scores NULL, and NULL sorts first, so it must be filtered.

    The guard `AND distance IS NOT NULL` exists for. Broken deliberately in development by
    removing that clause: the degenerate entry came back as hit zero with `distance=None`
    and `float(None)` raised `TypeError` from the row mapper, three frames from any
    explanation — and only because the mapper happens to be strict about types. A mapper
    that passed the value through would have produced a top-ranked entry at distance
    `None`, silently.

    Constructed by writing one entry's cache row as a zero vector directly rather than by
    swapping embedders, so the other two entries keep real vectors and the assertion is
    about *ordering* rather than about an empty result.
    """
    memory, ids = _seeded(tmp_path)
    memory.embed_pending("guidance")
    digest = fetch_one(
        memory.connection,
        "SELECT digest FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
        ("nav", "guidance", ids[1]),
    )
    assert digest is not None
    memory.connection.execute(
        "UPDATE memory_embedding_cache SET embedding = ? WHERE digest = ? AND input_type = ?",
        (pack_vector([0.0] * BagOfWords().dimensions), digest[0], DOCUMENT),
    )
    memory.connection.commit()

    hits = memory.search_entries("guidance", "prefer the transition that ends the case", k=3)
    assert ids[1] not in [h.entry_id for h in hits], "the degenerate entry was ranked"
    assert len(hits) == 2, "the other two entries must still rank"
    assert all(isinstance(h.distance, float) for h in hits)
    assert hits[0].distance <= hits[1].distance

    # And it is countable rather than merely absent, which is the whole point of exposing it.
    assert memory.degenerate_entries("guidance") == [ids[1]]
    assert memory.unranked_entries("guidance") == [], "it has a vector; it just cannot be scored"
    memory.close()


def test_a_degenerate_query_vector_raises_rather_than_returning_nothing(tmp_path: Path) -> None:
    """"Nothing is relevant" and "nothing could be ranked" must not be the same answer.

    With a zero-magnitude *query* vector every distance is NULL, so the filtered search
    returns `[]` — which reads exactly like a corpus with no relevant entry. That is the
    failing-soft confusion the whole discrimination guard exists to prevent, so it raises
    and names the embedder instead. Broken deliberately by removing
    `_require_rankable_query`: the search returned `[]` and every caller-visible signal was
    identical to a legitimate miss.
    """
    memory = _backend(tmp_path, embedder=Zeros())
    memory.add_entry("guidance", "an entry whose vector is degenerate too")
    with pytest.raises(ValueError, match="zero magnitude"):
        memory.search_entries("guidance", "any query at all", k=3)
    memory.close()


# ── The lexical channel: FTS5 and bm25, which Turso cannot do ──


def test_bm25_is_negative_so_ascending_is_best_first(tmp_path: Path) -> None:
    """The engine behaviour the whole lexical channel's ordering rests on, asserted directly.

    Two facts as properties of the database rather than of this module, because getting either
    backwards fails *soft*: an inverted lexical ranking still returns k plausible entries.

    First, `bm25()` returns real, distinct, order-bearing scores — which is precisely what
    Turso's `fts_score()` does not do (0.0 for every matching row) and is therefore the reason
    hybrid retrieval exists in this backend and not in that one.
    `test_turso_fts_cannot_rank_...` is the other half of this pair, one file over.

    Second, FTS5 *negates* the score so that ASCending is best-first, matching every other
    `ORDER BY ... ASC` in the module. So the strongest match is the most negative number,
    which is the opposite of most BM25 implementations. Recorded so a future FTS5 that stops
    negating fails here rather than silently inverting retrieval.

    The score range is asserted too: measured, one query over three entries spans six orders
    of magnitude. That is the concrete fact that makes any linear blend of bm25 with cosine
    distance a fitted constant, and therefore the fact `reciprocal_rank_fusion` exists for.
    """
    memory, ids = _seeded(tmp_path)
    scored = memory.fts_entries("guidance", "appeal fine already sent", k=3)
    assert scored, "the lexical channel found nothing in a corpus containing the words"
    scores = [score for _, _, score in scored]

    assert all(score < 0.0 for score in scores), "bm25 stopped being negative"
    assert scores == sorted(scores), "bm25 ASC stopped being best-first"
    assert scored[0][0] == ids[2], "the entry holding every query term did not rank first"
    assert len(set(scores)) == len(scores), "bm25 stopped discriminating between matches"
    assert abs(scores[0]) > abs(scores[-1]) * 100, (
        f"the bm25 range collapsed, so the incommensurable-scales argument for RRF would "
        f"need re-measuring: {scores}"
    )
    memory.close()


def test_a_query_with_fts5_operators_is_not_a_syntax_error(tmp_path: Path) -> None:
    """MATCH is a query language, so prose going into it must be escaped, not passed through.

    Guard 6, and the mutant is one line: `MATCH ?` bound to `query` instead of to
    `fts_match_expression(query)`. Every input below then raises from inside a ranking path,
    and the last three are the nasty ones — `no such column: x` reads as a schema bug rather
    than as untrusted input, so a reader would look in the DDL.

    Measured on this build, raw:

        ''      -> OperationalError: fts5: syntax error near ""
        'AND'   -> OperationalError: fts5: syntax error near "AND"
        'NEAR(' -> OperationalError: fts5: syntax error near ""
        '-x'    -> OperationalError: no such column: x
        'a:b'   -> OperationalError: no such column: a

    Note where the danger is not: binding the parameter stops SQL injection into the
    *statement*, and MATCH parsing happens afterwards, on the bound value. So the placeholder
    is no protection at all here — which is exactly why this needs a test rather than a
    comment.

    Asserted through `hybrid_entries` rather than `fts_entries` alone, because the property
    that matters is that a hostile query still *retrieves* — the vector channel must carry it
    — rather than merely not crashing.
    """
    memory, _ = _seeded(tmp_path)
    hostile = [
        "AND",
        "already AND",
        'he said "AND" loudly',
        "NEAR(appeal fine)",
        "-appeal",
        "note: check the appeal",
        "((unbalanced",
        "^caret *star",
        "!!! ???",
        "\\\\\\",
        "café Ünïcode",
        "价格 上涨",
    ]
    for query in hostile:
        assert isinstance(memory.fts_entries("guidance", query, k=2), list), query
        hits, channels = memory.hybrid_entries("guidance", query, k=2)
        assert channels, f"no channel ranked {query!r}"
        assert "vector" in channels, f"the vector channel must carry {query!r}"
        assert len(hits) <= 2

    # Two token-free queries are deliberately not in that list, and finding out why was the
    # useful part of writing this test. `""` and `"   "` are each a syntax error to raw MATCH
    # *and* embed to the zero vector under `BagOfWords` (no tokens, so no buckets set), so
    # they are genuine dual-channel failures and `hybrid_entries` raises — guard 7 firing on
    # a real input rather than a constructed one, which is a better argument for the guard
    # than the constructed case. What belongs here is that the *escaping* handles them
    # without a syntax error.
    for token_free in ("", "   "):
        assert fts_match_expression(token_free) == ""
        with pytest.raises(ValueError, match="Neither channel"):
            memory.hybrid_entries("guidance", token_free, k=2)

    # And the escaping is a pure function, so its output is assertable without a database.
    assert fts_match_expression('he said "AND" loudly') == '"he" OR "said" OR "AND" OR "loudly"'
    assert fts_match_expression("!!! ???") == "", "a token-free query must report itself as such"
    assert fts_match_expression('a "quoted" bit') == '"a" OR "quoted" OR "bit"'
    memory.close()


def test_an_operator_word_is_matched_as_a_term_not_parsed_as_an_operator(
    tmp_path: Path,
) -> None:
    """`AND` in a query must find the entry containing the word `AND`, not restructure it.

    Not-crashing is the weak half of guard 6. The strong half is that the escaping keeps the
    query *meaning* what it said: an entry whose text literally contains `AND` is found by a
    query containing `AND`, which is what a caller searching stored prose expects.

    Also pins the rejected cheap fix. Wrapping the whole query in one quoted literal parses
    fine and turns every query into a phrase search: measured, `"he said ""AND"" loudly"`
    returns zero rows against a corpus holding all four words. That mutant leaves the lexical
    channel silently contributing nothing rather than raising, which is the worse failure and
    the reason `fts_match_expression` tokenises.

    The second entry deliberately contains no `and`, which took a failed run to notice and is
    worth recording: `unicode61` folds case, so a first draft whose other entry read "appeals
    and fines" matched the query `AND` too — correctly, since `and` is an ordinary English
    word there. The corpus has to isolate the term for the assertion to be about operator
    handling rather than about English.
    """
    memory = _backend(tmp_path)
    target = memory.add_entry("guidance", "escalate when the operator AND the reviewer disagree")
    memory.add_entry("guidance", "unrelated advice concerning appeals, fines, penalties")

    found = memory.fts_entries("guidance", "AND", k=3)
    assert [entry_id for entry_id, _, _ in found] == [target], (
        "an operator word was not searched as a term"
    )
    assert found[0][2] < 0.0, "a term match through an operator word scored nothing"

    # The rejected cheap fix, run directly so its silence is visible rather than argued.
    # One quoted literal is a *phrase* search: it parses, and it only matches when the
    # query's tokens appear consecutively in that exact order. So a real question about the
    # entry finds nothing, while the entry's own text verbatim finds it — which is the shape
    # that makes the mutant dangerous. It never raises; the lexical channel just goes quiet
    # for every query that is not an exact substring.
    def phrase_match(text: str) -> list[tuple[Any, ...]]:
        return fetch_rows(
            memory.connection,
            "SELECT entry_id FROM memory_entry_fts "
            "WHERE memory_entry_fts MATCH ? AND actor_id = ?",
            ('"' + text.replace('"', '""') + '"', "nav"),
        )

    assert phrase_match('when does the operator "AND" the reviewer escalate') == [], (
        "one-quoted-literal escaping now behaves as a term search, so the tokenising "
        "argument in fts_match_expression needs re-measuring"
    )
    assert phrase_match("escalate when the operator") == [(target,)], (
        "the mutant is not a phrase search either, so its failure mode is not what is claimed"
    )
    # The same question through the real escaping does find the entry, which is the contrast.
    assert [i for i, _, _ in memory.fts_entries("guidance", "when does the operator escalate")] == [
        target
    ]
    memory.close()


def test_the_lexical_index_does_not_leak_across_actors_or_parameters(tmp_path: Path) -> None:
    """`UNINDEXED` on the key columns, and the namespace filters, both asserted.

    Two mutants in one test because they fail the same way — extra hits from outside the
    namespace, at a bm25 score indistinguishable from a real content match.

    Without `UNINDEXED` on `actor_id` / `param` / `entry_id`, those values become searchable
    terms, so a query mentioning `guidance` matches every entry of the `guidance` parameter.
    Without the `memory_entry_fts.actor_id = ?` filter, one actor's entries rank in another's
    search — and the actors-are-isolated property is one this backend advertises.

    The actor ids are deliberately long nonsense words rather than `"a"` and `"b"`. A first
    draft used the short ones and the `UNINDEXED` assertion passed for the wrong reason: `a`
    appears in the entry *text*, so a query for it matches whether or not the key column is
    indexed. A probe term has to be absent from every value to say anything about the index.
    """
    path = tmp_path / "shared.db"
    a = SqliteMemoryBackend(Advice, actor_id="navigatorzz", path=path, embedder=BagOfWords())
    b = SqliteMemoryBackend(Advice, actor_id="reviewerzz", path=path, embedder=BagOfWords())
    a.add_entry("guidance", "the appeal belongs to the first actor")
    b.add_entry("guidance", "the appeal belongs to the second actor")

    assert [value for _, value, _ in a.fts_entries("guidance", "appeal", k=5)] == [
        "the appeal belongs to the first actor"
    ], "the lexical channel crossed the actor namespace"

    # None of these three terms appears in any entry value, so a hit could only come from an
    # indexed key column.
    assert a.fts_entries("guidance", "navigatorzz", k=5) == [], "actor_id is indexed as a term"
    assert a.fts_entries("guidance", "guidance", k=5) == [], "param is indexed as a term"
    assert a.fts_entries("guidance", "1", k=5) == [], "entry_id is indexed as a term"
    a.close()
    b.close()


# ── FTS sync: the index is derived state, so its drift is countable ──


def test_a_rewritten_entry_re_indexes_lexically(tmp_path: Path) -> None:
    """Guard 4, and its broken form is the reason `fts_drift` reports `stale` separately.

    Consolidation rewrites entry text, so this is the write path that matters most. The
    embedding cache handles its half *structurally* — content addressing means a new digest
    cannot hit an old cache row, which
    `test_embedding_cache_is_content_addressed_so_a_rewrite_re_embeds` asserts. The FTS index
    has no such property: it is keyed by entry id, so nothing about its layout prevents
    staleness and `_fts_replace` has to be called.

    Broken deliberately by removing `_fts_replace` from `update_entry`. The failure is worth
    describing because it is not a crash and not an empty result: the entry keeps retrieving,
    on its *pre-rewrite* words. So a query about text that no longer exists anywhere in the
    store returns a confident hit, and a query about the new text returns nothing from the
    lexical channel — retrieval degrades while every count and every result length stays
    plausible. That is why the assertion is two-sided (old words gone, new words found) rather
    than just checking the new text is present.
    """
    memory, ids = _seeded(tmp_path)
    assert memory.fts_drift() == []

    assert [i for i, _, _ in memory.fts_entries("guidance", "appeal", k=3)] == [ids[2]]
    memory.update_entry("guidance", ids[2], "a rewritten note about xyzzy tokens instead")

    assert memory.fts_drift() == [], "the index did not follow the rewrite"
    assert memory.fts_entries("guidance", "appeal", k=3) == [], (
        "the pre-rewrite words still match, so the entry retrieves for reasons that are gone"
    )
    assert [i for i, _, _ in memory.fts_entries("guidance", "xyzzy", k=3)] == [ids[2]]
    memory.close()


def test_every_entry_write_path_keeps_the_index_in_step(tmp_path: Path) -> None:
    """Add, update, remove, wholesale save, consolidate, delete: `fts_drift` empty after each.

    Manual sync means one missed call is one silently un-indexed entry, so the check is run
    after *every* write path rather than after the one being demonstrated. Enumerated
    explicitly rather than checked once at the end, because a later path can hide an earlier
    one's mistake — a wholesale `save` clears the whole parameter and would paper over a
    missed `add_entry`.

    `_save`'s wholesale replace is the subtle one: ids are never reused, so an orphaned index
    row left by a save is never overwritten by a later insert either. They would accumulate
    across saves, each one a lexical hit whose join finds nothing.
    """
    memory, ids = _seeded(tmp_path)
    assert memory.fts_drift() == [], "after add_entry"

    memory.update_entry("guidance", ids[0], "updated text for the first entry")
    assert memory.fts_drift() == [], "after update_entry"

    memory.remove_entry("guidance", ids[1])
    assert memory.fts_drift() == [], "after remove_entry"

    memory.save("guidance", ["wholesale one", "wholesale two"])
    assert memory.fts_drift() == [], "after a wholesale save"
    assert len(fetch_rows(memory.connection, "SELECT entry_id FROM memory_entry_fts", ())) == 2, (
        "the retired ids' index rows survived the save"
    )

    memory.delete("guidance")
    assert memory.fts_drift() == [], "after delete resets to the schema default"

    # And through the shared tool provider, which is how a consolidating agent writes.
    from pneuma.memory import EntryToolProvider

    provider = EntryToolProvider(memory, "guidance")  # type: ignore[arg-type]
    tools = {t.tool_name: t for t in asyncio.run(provider.load_tools())}
    tools["add_entry"]._tool_func(value="added through the agent tool")  # type: ignore[attr-defined]
    assert memory.fts_drift() == [], "after a tool-driven add"
    memory.close()


def test_fts_drift_names_all_three_disagreements(tmp_path: Path) -> None:
    """The audit method has teeth: each way the index can diverge is reported as its own kind.

    Constructed by corrupting the index directly, because the write paths are correct — which
    is the point. `fts_drift` is the check that they *stay* correct, so it has to be able to
    see a divergence that no supported operation produces.

    Three kinds rather than one boolean, because they need different fixes and they fail
    differently: `missing` costs lexical recall, `stale` returns the entry for words it no
    longer contains, `orphaned` shrinks the candidate set through a join that finds nothing.
    """
    memory, ids = _seeded(tmp_path)

    memory.connection.execute(
        "DELETE FROM memory_entry_fts WHERE actor_id = ? AND entry_id = ?", ("nav", ids[0])
    )
    memory.connection.execute(
        "UPDATE memory_entry_fts SET value = 'not what the entry says' "
        "WHERE actor_id = ? AND entry_id = ?",
        ("nav", ids[1]),
    )
    memory.connection.execute(
        "INSERT INTO memory_entry_fts (value, actor_id, param, entry_id) "
        "VALUES ('a row for an entry that never existed', 'nav', 'guidance', '999')"
    )
    memory.connection.commit()

    assert memory.fts_drift() == [
        ("guidance", ids[0], "missing"),
        ("guidance", ids[1], "stale"),
        ("guidance", "999", "orphaned"),
    ]

    assert memory.reindex_fts() == 3
    assert memory.fts_drift() == [], "the repair did not converge"
    assert memory.reindex_fts() == 0, "reindex is not idempotent"
    assert [i for i, _, _ in memory.fts_entries("guidance", "revisit", k=3)] == [ids[0]]
    memory.close()


def test_a_turso_written_corpus_is_indexed_lexically_on_first_open(tmp_path: Path) -> None:
    """The arrangement no trigger could have covered, which is why sync is manual.

    A trigger cannot index rows that predate it, and this backend advertises exactly that
    situation: `test_a_turso_written_database_ranks_under_this_backend` makes a
    Turso-written file reopening here a supported property. Those `memory_entry` rows arrive
    with no `memory_entry_fts` table in the file at all, so without the `reindex_fts` call in
    `init_schema` the lexical channel would be permanently empty for a ported corpus — and
    `_search` would silently fall back to `["vector"]` forever, which is a real degradation
    reported in the meta and noticed by nobody.

    Same argument covers a borrowed `connection=` somebody else wrote entries through.
    """
    from pneuma.memory import TursoMemoryBackend

    path = tmp_path / "portable.db"
    written = TursoMemoryBackend(Advice, actor_id="nav", path=path, embedder=BagOfWords())
    ids = [written.add_entry("guidance", text) for text in ENTRIES]
    written.embed_pending("guidance")
    written.close()

    reopened = SqliteMemoryBackend(Advice, actor_id="nav", path=path, embedder=BagOfWords())
    assert reopened.fts_drift() == [], "the ported corpus was not backfilled into the index"
    assert [i for i, _, _ in reopened.fts_entries("guidance", "appeal fine sent", k=3)] == [ids[2]]

    _, channels = reopened.hybrid_entries("guidance", "appeal fine sent", k=2)
    assert channels == ["vector", "fts"], "a ported corpus fell back to one channel"
    reopened.close()


# ── Fusion: RRF over ranks, deterministic, and honest about which channel ran ──


def test_fusion_reads_ranks_not_scores() -> None:
    """Guard 5, asserted on the pure function so the claim needs no database.

    RRF's whole justification is that it never touches a score, and the mutant is to fuse on
    them — `1 / (k + score)` or any normalise-then-blend. The two lists below are the measured
    shape of the problem: cosine distances live on [0, 2] and bm25 scores ranged over six
    orders of magnitude on a three-entry corpus (-1.45 to -9.9e-07). Under any linear mix the
    bm25 term either dominates or vanishes depending on corpus size, and the `alpha` that
    fixes it for one corpus is a fitted constant nobody measured — the defect class
    `pneuma.detect` exists to catch.

    Three properties, and the third is the one a score-based fusion loses:

    Rank position alone decides the contribution, so the same ranks fuse identically whatever
    scores produced them. Agreement across channels accumulates, so an entry both channels
    ranked outscores an entry only one did — that is the actual retrieval claim hybrid search
    makes. And ties are *exact*, not nearly-equal, which is why the caller has to break them
    on something measured rather than on dict order.
    """
    vector = ["a", "b", "c"]
    lexical = ["c", "d"]

    fused = reciprocal_rank_fusion([vector, lexical])
    assert set(fused) == {"a", "b", "c", "d"}, "fusion is over the union of the channels"

    # `c` is rank 3 in one channel and rank 1 in the other; `a` is rank 1 in one only.
    assert fused["c"] == pytest.approx(1 / (RRF_K + 3) + 1 / (RRF_K + 1))
    assert fused["a"] == pytest.approx(1 / (RRF_K + 1))
    assert fused["c"] > fused["a"], "agreement between channels did not promote a hit"

    # Identical ranks fuse identically, whatever the scores behind them were.
    assert reciprocal_rank_fusion([vector, lexical]) == fused

    # Exact ties, not near-ties: `a` at rank 1 of one channel and `c` at rank 1 of the other.
    single = reciprocal_rank_fusion([["a"], ["c"]])
    assert single["a"] == single["c"], "same-rank hits must tie exactly, so ties need breaking"

    # One channel fuses to that channel's own order, which is how a fallback is expressed.
    alone = reciprocal_rank_fusion([vector, []])
    assert sorted(alone, key=lambda i: -alone[i]) == vector
    assert reciprocal_rank_fusion([[], []]) == {}

    # `k` is a shape parameter, not a threshold: it reweights and cannot reorder a single
    # channel or drop a hit. That is what makes a constant tolerable in a ranking path.
    for k in (1, 10, RRF_K, 1000):
        shaped = reciprocal_rank_fusion([vector, lexical], k=k)
        assert set(shaped) == set(fused)
        assert shaped["c"] > shaped["a"]

    # And the mutant's arithmetic, run inline against real measured numbers so its failure is
    # a demonstration rather than a claim. These are the actual values from a three-entry
    # corpus: cosine distances near 1.0, bm25 scores near -1e-06. Under `1 / (k + score)` the
    # bm25 term is ~1/60 and the cosine term is ~1/61 — indistinguishable — so the fusion is
    # decided by float noise, and on a larger corpus (bm25 -2.48) the lexical term becomes
    # 1/57.5 and dominates outright. Neither behaviour is a ranking; both look like one.
    def score_fusion(
        vector_distances: dict[str, float], bm25: dict[str, float]
    ) -> dict[str, float]:
        blended: dict[str, float] = {}
        for scores in (vector_distances, bm25):
            for entry_id, score in scores.items():
                blended[entry_id] = blended.get(entry_id, 0.0) + 1.0 / (RRF_K + score)
        return blended

    small = score_fusion({"a": 0.0, "b": 1.0}, {"c": -1.0e-06})
    assert small["c"] > small["a"], (
        "a single weak lexical hit does not outrank a perfect vector match under score "
        "fusion, so the incommensurable-scales argument needs re-measuring"
    )
    large = score_fusion({"a": 0.0}, {"c": -2.48})
    assert large["c"] / large["a"] > 1.04, "the corpus-size sensitivity of a score blend is gone"


def test_hybrid_retrieval_finds_a_lexical_hit_the_vector_channel_misses(
    tmp_path: Path,
) -> None:
    """The claim hybrid retrieval makes, measured: it strictly dominates the vector channel.

    "Strictly dominates" is a real claim and this is what makes it checkable. Identical
    embeddings, the vector candidate list over-fetched rather than truncated, so the lexical
    channel can only *add* candidates — and here it adds the one that matters.

    The corpus and the embedder are built so the two channels genuinely disagree. `TopicOnly`
    is blind to everything but the topic word, so `ORD-4471` contributes nothing to any
    vector and the entry holding it sits at distance 1.0 from a `procedure` query while three
    irrelevant same-topic entries sit at 0.0. Pure vector at k=3 therefore returns the three
    ties and never reaches it. This is a caricature of a real embedding failure, not an
    invented one: a rare literal token in a few-hundred-float sentence embedding is close to
    unrecoverable, which is exactly the case BM25 wins.

    The pure-vector baseline is asserted first, in the same test. Without it this would pass
    against a corpus where vector search already worked, and the dominance claim would be
    untested.

    Two queries, because a failed first draft taught the difference and it is the sharpest
    thing to know about RRF. The draft asserted that the exact-term hit came back *first* for
    `"procedure question about rule ORD-4471"`, and it came back second. That is RRF working
    correctly, not a bug: `procedure` is in three other entries too, so the target sits at
    lexical rank 1 but the entry at vector rank 1 is *also* at lexical rank 3, and two
    endorsements beat one. `1/61 + 1/63 > 1/61`. The lesson is that RRF promotes on
    *agreement*, so a query whose terms are shared across the corpus buys less promotion than
    a query whose terms discriminate — and the honest claim to test is reachability in the
    top-k plus first place when the lexical channel actually discriminates.
    """
    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", path=tmp_path / "topic.db", embedder=TopicOnly()
    )
    ids = [
        memory.add_entry("guidance", text)
        for text in (
            "procedure note: never revisit a state already passed through",
            "procedure note: prefer the transition that ends the case",
            "procedure note: escalate when the operator disagrees",
            "money note: rule ORD-4471 governs the penalty ledger",
        )
    ]

    # A query sharing a term with the rest of the corpus: promotion into the top-k, and the
    # entry endorsed by both channels stays ahead of the one endorsed by one.
    mixed = "procedure question about rule ORD-4471"
    vector_only = memory.search_entries("guidance", mixed, k=3)
    assert ids[3] not in [h.entry_id for h in vector_only], (
        "the vector channel already finds it, so this corpus does not test the claim"
    )
    assert {h.distance for h in vector_only} == {0.0}, "the same-topic tie is not reproducing"

    hits, channels = memory.hybrid_entries("guidance", mixed, k=3)
    assert channels == ["vector", "fts"]
    assert ids[3] in [h.entry_id for h in hits], (
        f"the exact-term match is still unreachable at k=3: {[h.entry_id for h in hits]}"
    )
    assert hits[0].entry_id == ids[0], "two channel endorsements did not beat one"
    assert next(h for h in hits if h.entry_id == ids[3]).distance == 1.0, (
        "the promoted hit lost its real cosine distance"
    )

    # A query whose terms discriminate: the exact-term match is the only lexical hit, so it
    # takes first place outright.
    sharp = "ORD-4471 penalty ledger"
    assert [i for i, _, _ in memory.fts_entries("guidance", sharp, k=9)] == [ids[3]]
    sharp_hits, _ = memory.hybrid_entries("guidance", sharp, k=3)
    assert sharp_hits[0].entry_id == ids[3], (
        f"a discriminating lexical hit was not promoted to first: "
        f"{[h.entry_id for h in sharp_hits]}"
    )
    memory.close()


def test_over_fetching_is_what_makes_agreement_detectable(tmp_path: Path) -> None:
    """At exactly k per channel, RRF cannot see the overlap it is supposed to reward.

    The reason `hybrid_entries` fetches `overfetch * k` candidates rather than `k`, and writing
    it taught the sharper version of the claim. The obvious story — "over-fetching reaches a
    hit ranked k+1 by vector and 1 by BM25" — is not the binding one, because a top-ranked
    lexical hit is already *in* the k-length lexical list and reaches the fusion either way.

    The binding one is agreement. RRF's whole mechanism is that an entry both channels endorse
    outscores an entry only one does, and an overlap between two lists is only visible if the
    lists are long enough to contain it. Over-fetch is therefore not a recall trick; it is what
    makes the fusion rule *operative* rather than decorative.

    Measured on the corpus below with `query = "procedure ledger"`:

        vector:  1 (0.00), 2 (0.00), 3 (0.29), 4 (0.42), 5 (1.00)
        lexical: 4, 3, 5, 1, 2

    Entry 3 is second in both channels and first in neither. At `overfetch=1, k=1` the fusion
    sees `[1]` and `[4]`, no overlap exists, and the answer is whichever wins the tiebreak — so
    the fused result is decided by a rerank rather than by agreement. At `overfetch=3` it sees
    `[1, 2, 3]` and `[4, 3, 5]`, entry 3 accumulates both terms, and it wins outright:
    `1/63 + 1/62 > 1/61`. Same corpus, same query, same k — only the candidate depth differs.
    """
    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", path=tmp_path / "overfetch.db", embedder=TopicOnly()
    )
    ids = [
        memory.add_entry("guidance", text)
        for text in (
            "procedure note: alpha",
            "procedure note: beta",
            "procedure timing note: gamma ledger",
            "procedure timing money note: delta ledger ledger ledger",
            "money note: epsilon ledger ledger",
        )
    ]
    query = "procedure ledger"
    agreed = ids[2]

    # The arrangement the docstring's arithmetic depends on: second in both, first in neither.
    assert [h.entry_id for h in memory.search_entries("guidance", query, k=5)][:3] == ids[:3]
    assert [i for i, _, _ in memory.fts_entries("guidance", query, k=5)][:2] == [ids[3], agreed]

    fused, _ = memory.hybrid_entries("guidance", query, k=1)
    assert [h.entry_id for h in fused] == [agreed], (
        "the doubly-endorsed entry did not win, so over-fetching is not reaching the overlap"
    )

    starved, _ = memory.hybrid_entries("guidance", query, k=1, overfetch=1)
    assert [h.entry_id for h in starved] != [agreed], (
        "overfetch=1 already sees the overlap, so the over-fetch is not load-bearing here"
    )

    with pytest.raises(ValueError, match="overfetch must be >= 1"):
        memory.hybrid_entries("guidance", "anything", k=1, overfetch=0)
    memory.close()


def test_an_rrf_tie_is_not_decided_by_which_channel_was_fused_first(tmp_path: Path) -> None:
    """Ties are broken on measured quantities, never on the order the channels were listed in.

    `detect/objective.py` carries the measured version of this defect: on a 21^3 metric grid
    21 points tied for smallest `edge_share` while scoring from 0.0 to 0.9744, so which one
    became "the emptiest answer" was decided by `itertools.product`'s iteration order. Its fix
    and this one are the same fix — tiebreak on a measured quantity, then on a stable
    identifier — and the comment at `hybrid_entries` cites it.

    **Writing this test the obvious way produced a toothless one, and that is worth recording
    because the obvious way is what a reader would reach for.** The first draft ran the same
    search five times, then re-ran it in subprocesses under three `PYTHONHASHSEED` values, and
    asserted one stable answer. It passed with the entire tiebreak deleted, and it had to: a
    `dict` in CPython iterates in *insertion* order, and hash seeding does not touch that. So
    the seed loop was measuring nothing at all, and "deterministic" was being confirmed by a
    property that holds whether or not the code is correct.

    What the fused order can actually depend on is the sequence `reciprocal_rank_fusion`
    inserts keys in — which is the order the channel lists are passed. Swapping
    `[vector, lexical]` to `[lexical, vector]` is therefore the real mutant, and it flips an
    unsorted tie while leaving a properly-broken one alone. That is the assertion below: the
    fusion is called both ways round on a corpus with a genuine RRF tie, and the answer must
    not move.

    The corpus is built so the tie is exact and the tiebreak is *visible*. Under `TopicOnly`
    all three entries sit at distance 0.0, so the vector channel is one flat tie in id order,
    while `"procedure ledger"` reverses the lexical order — entries 1 and 3 land at
    RRF 0.032266 each (rank 1+3 and rank 3+1), and bm25 puts 3 ahead. So insertion order says
    `1` and the measured tiebreak says `3`, and the two are distinguishable.

    One limit of this, found by mutating and worth stating rather than leaving for somebody to
    rediscover: deleting the tiebreak *and* swapping the channels to lexical-first passes every
    test here, and it is not a hole. Measured over 3000 random channel pairs, that combination
    is behaviourally identical to the explicit tiebreak — Python's sort is stable, so
    lexical-first insertion preserves lexical order inside an RRF tie, which is exactly what
    ordering on `bm25` does. The reason to keep the explicit key anyway is that the equivalence
    rests on two coincidences a reader cannot see (CPython dicts iterating in insertion order,
    and `sorted` being stable) and would break silently the moment either channel's fetch order
    stopped matching its score order. The explicit form says what it means; that is the whole
    claim, and it is not one a test can make.
    """
    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", path=tmp_path / "ties.db", embedder=TopicOnly()
    )
    ids = [
        memory.add_entry("guidance", text)
        for text in (
            "procedure note: aaa",
            "procedure note: bbb ledger",
            "procedure note: ccc ledger ledger",
        )
    ]
    query = "procedure ledger"

    vector = [h.entry_id for h in memory.search_entries("guidance", query, k=9)]
    lexical = [i for i, _, _ in memory.fts_entries("guidance", query, k=9)]
    assert vector == ids, "the vector channel is not the flat id-ordered tie this test needs"
    assert lexical == list(reversed(ids)), "the lexical channel does not reverse it"

    fused = reciprocal_rank_fusion([vector, lexical])
    assert fused[ids[0]] == pytest.approx(fused[ids[2]]), (
        f"the RRF tie this test rests on is gone: {fused}"
    )

    ordered = [h.entry_id for h in memory.hybrid_entries("guidance", query, k=3)[0]]
    assert ordered[0] == ids[2], (
        f"the tie was decided by insertion order rather than reranked on bm25: {ordered}"
    )

    # The mutant, run inline: fusing the channels the other way round changes the dict's
    # insertion order and must not change the answer.
    swapped = reciprocal_rank_fusion([lexical, vector])
    assert list(swapped) != list(fused), "the two fusions insert in the same order after all"
    assert swapped == fused, "RRF scores depend on channel order, which they must not"
    assert sorted(
        swapped,
        key=lambda i: (
            -swapped[i],
            dict((e, s) for e, _, s in memory.fts_entries("guidance", query, k=9))[i],
            i,
        ),
    ) == ordered, "the tiebreak is not independent of the fusion's insertion order"

    # Stable across repeated calls too, which is the weaker claim but the one a caller sees.
    assert {
        tuple(h.entry_id for h in memory.hybrid_entries("guidance", query, k=3)[0])
        for _ in range(5)
    } == {tuple(ordered)}
    memory.close()


def test_bm25_breaks_an_rrf_tie_before_the_entry_id_does(tmp_path: Path) -> None:
    """The rerank step: among entries tying on RRF, the better lexical match comes first.

    The tiebreak order `(-rrf, bm25, distance, entry_id)` is not arbitrary. `bm25` sits first
    among the tiebreakers because it is exactly the information RRF discarded on purpose — the
    fusion reads ranks, so the score is still available and unused, and it is the sharpest
    thing left to order a rank tie by.

    Asserted where the id order and the bm25 order *disagree*, which is the only arrangement
    that can tell the two apart. With `TopicOnly` every entry here is at distance 0.0, so the
    vector channel contributes one flat tie and the lexical channel decides. The entry
    mentioning the query terms twice is the stronger bm25 match and is deliberately given the
    *later* id, so a fallback to `entry_id` would put it second.
    """
    memory = SqliteMemoryBackend(
        Advice, actor_id="nav", path=tmp_path / "rerank.db", embedder=TopicOnly()
    )
    weak = memory.add_entry("guidance", "procedure note: the ledger is reviewed")
    strong = memory.add_entry("guidance", "procedure note: ledger ledger ledger audit")
    assert int(strong) > int(weak), "the stronger match must hold the later id"

    scored = dict((i, s) for i, _, s in memory.fts_entries("guidance", "ledger", k=2))
    assert scored[strong] < scored[weak], "bm25 does not prefer the repeated-term entry"

    hits, _ = memory.hybrid_entries("guidance", "ledger", k=2)
    assert [h.entry_id for h in hits] == [strong, weak], (
        "the RRF tie was broken by entry id rather than reranked on bm25"
    )
    memory.close()


# ── Honest degradation: which channel ranked, and when a raise is the only honest answer ──


def test_a_query_with_no_lexical_hit_falls_back_to_pure_vector(tmp_path: Path) -> None:
    """No matching term is a legitimate lexical miss, so the vector channel carries it alone.

    Two distinct ways to have no lexical hit, and they must behave identically because they
    are the same finding: the query's terms are absent from the index, or the query has no
    indexable token at all. Neither is an error — the semantic channel is precisely the one
    that handles a question sharing no vocabulary with its answer, which is why an embedding
    backend exists here in the first place.

    `channels == ["vector"]` in the meta is what makes the fallback auditable after the fact.
    Without it a caller reading an event log cannot tell a fused result from a degraded one,
    and a corpus that fell back on every query for a month would look exactly like one that
    never did.
    """
    memory, ids = _seeded(tmp_path)

    for query in ("kubernetes ingress certificate rotation", "!!! ???"):
        assert memory.fts_entries("guidance", query, k=3) == [], query
        hits, channels = memory.hybrid_entries("guidance", query, k=2)
        assert channels == ["vector"], f"{query!r} did not fall back cleanly"
        assert len(hits) == 2, "the vector channel must still rank the corpus"
        assert all(h.distance < float("inf") for h in hits), "a vector hit lost its distance"

    # And the fallback ranking is the vector channel's own, unperturbed.
    query = "revisit a state already passed through"
    memory.connection.execute("DELETE FROM memory_entry_fts WHERE actor_id = 'nav'")
    memory.connection.commit()
    hits, channels = memory.hybrid_entries("guidance", query, k=3)
    assert channels == ["vector"]
    assert [h.entry_id for h in hits] == [
        h.entry_id for h in memory.search_entries("guidance", query, k=3)
    ], "single-channel fusion reordered the vector ranking"
    assert ids[0] == hits[0].entry_id
    memory.close()


def test_a_degenerate_query_embedding_falls_back_to_the_lexical_channel(
    tmp_path: Path,
) -> None:
    """A behaviour *fix*, not merely a new path — and the one place hybrid changes an answer.

    `search_entries` raises on a zero-magnitude query vector, and rightly: it has one channel,
    every distance is NULL, and returning `[]` would read as "this corpus has nothing
    relevant". `hybrid_entries` has two channels, so in exactly this case the refusal would be
    discarding a real, rankable result to protect a distinction that is no longer at risk.
    BM25 does not need an embedding.

    So the two paths deliberately disagree here, and both are asserted in the same test
    because the disagreement is the finding. `channels == ["fts"]` records which ranking
    produced the hits, and the hits carry `distance=inf` — an honest "cosine did not rank
    this" rather than a fabricated 0.0 that would claim a perfect vector match.
    """
    memory = _backend(tmp_path, embedder=Zeros())
    ids = [memory.add_entry("guidance", text) for text in ENTRIES]

    with pytest.raises(ValueError, match="zero magnitude"):
        memory.search_entries("guidance", "appeal fine already sent", k=3)

    hits, channels = memory.hybrid_entries("guidance", "appeal fine already sent", k=3)
    assert channels == ["fts"], "the lexical channel did not carry a degenerate embedding"
    assert hits[0].entry_id == ids[2]
    assert all(h.distance == float("inf") for h in hits), (
        "an unranked-by-cosine hit was given a fabricated distance"
    )
    memory.close()


def test_both_channels_unavailable_raises_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    """Guard 7: the fallback must not become a way to hide a total retrieval failure.

    The argument is `_require_rankable_query`'s, one level up. With both channels out, `[]`
    is indistinguishable from "this corpus has nothing relevant" — and that collapse is the
    failing-soft defect the whole backend is designed against. Broken deliberately by
    returning `[]` instead of raising: the search came back empty and every caller-visible
    signal was identical to a legitimate miss.

    The message must name *both* reasons. Naming only the embedding invites a caller to
    re-embed and retry against a corpus that also had no lexical hit, which is a fix for a
    problem they only half have.
    """
    memory = _backend(tmp_path, embedder=Zeros())
    memory.add_entry("guidance", "an entry whose vector is degenerate too")

    with pytest.raises(ValueError) as raised:
        memory.hybrid_entries("guidance", "kubernetes ingress certificate rotation", k=3)
    message = str(raised.value)
    assert "zero magnitude" in message, "the embedding half of the failure is unnamed"
    assert "FTS index" in message, "the lexical half of the failure is unnamed"
    assert "zeros" in message, "the embedder is not named"

    # An empty corpus is *not* this case: nothing to rank is a legitimate empty answer, and
    # it must not be routed into the dual-failure raise. Asserted on a second actor rather
    # than a second file, since the corpus check is per-parameter-per-actor.
    empty = SqliteMemoryBackend(
        Advice, actor_id="unseeded", path=tmp_path / "mem.db", embedder=Zeros()
    )
    empty.save("guidance", [])
    assert empty.hybrid_entries("guidance", "anything", k=3) == ([], [])
    empty.close()
    memory.close()


def test_the_distance_ceiling_caps_the_vector_channel_only(tmp_path: Path) -> None:
    """`distance_ceiling` is a statement about cosine confidence, so BM25 is exempt.

    Not an oversight, and there is no BM25 counterpart to add. bm25 scores are unbounded,
    negative, and move with corpus size and term frequency, so "far" has no fixed meaning and
    `calibrate_ceiling` — which derives a threshold from a *distance* distribution — has
    nothing to work with. A ceiling invented for it would be the fitted constant this whole
    retrieval path refuses.

    Two halves, and the boundary between them is exactly where a first draft got it wrong.
    The ceiling applies to a hit *the vector channel scored*, whatever channel also found it —
    so an entry at distance 1.0 under a ceiling of 0.5 is dropped even when it is the top
    lexical hit, because the embedding did rank it and did say "far". What the ceiling cannot
    touch is a hit the vector channel never scored at all: those carry `distance=inf`, and
    `inf <= ceiling` is False, so a plain comparison would drop every one of them. Hence the
    filter tests `entry_id not in distances` instead. The lexical-only case is reachable
    through a degenerate query embedding, which is the arrangement below.
    """
    memory = SqliteMemoryBackend(
        Advice,
        actor_id="nav",
        path=tmp_path / "ceiling.db",
        embedder=TopicOnly(),
        distance_ceiling=0.5,
    )
    near = memory.add_entry("guidance", "procedure note: the usual advice")
    far = memory.add_entry("guidance", "money note: rule ORD-4471 governs the ledger")

    assert [h.entry_id for h in memory.search_entries("guidance", "procedure", k=5)] == [near], (
        "the ceiling is not excluding the far entry on the vector channel"
    )

    # Scored by the vector channel and beyond the ceiling, so dropped — even though it is the
    # strongest lexical hit. The ceiling is a claim about the embedding, and the embedding
    # made one.
    hits, channels = memory.hybrid_entries("guidance", "procedure ORD-4471", k=5)
    assert channels == ["vector", "fts"]
    assert [h.entry_id for h in hits] == [near], (
        "the ceiling stopped applying to a vector-scored hit"
    )

    # Never scored by the vector channel, so exempt. `Zeros` makes every distance NULL, the
    # vector channel returns nothing, and the surviving hits carry `inf` — which no ceiling
    # can judge and a naive comparison would discard entirely.
    lexical_only = SqliteMemoryBackend(
        Advice,
        actor_id="lex",
        path=tmp_path / "ceiling.db",
        embedder=Zeros(),
        distance_ceiling=0.5,
    )
    exempt = lexical_only.add_entry("guidance", "money note: rule ORD-4471 governs the ledger")
    surviving, lexical_channels = lexical_only.hybrid_entries("guidance", "ORD-4471", k=5)
    assert lexical_channels == ["fts"]
    assert [h.entry_id for h in surviving] == [exempt], (
        "the ceiling dropped a lexical-only hit it has no scale to judge"
    )
    assert surviving[0].distance == float("inf")
    lexical_only.close()

    # And the cap has not simply been disabled: tightened past every real distance, the
    # vector-scored hits all go.
    memory.distance_ceiling = -1.0
    capped, _ = memory.hybrid_entries("guidance", "procedure", k=5)
    assert capped == [], "the ceiling no longer caps the vector channel at all"
    assert far not in [h.entry_id for h in capped]
    memory.close()


async def test_search_meta_records_which_channels_ranked(tmp_path: Path) -> None:
    """The fused ranking reaches `_search`'s meta without changing the gradient contract.

    Two claims, and the first is the one the optimizer depends on: `results` and `distances`
    keep their shape and their meaning through the hybrid path, because
    `test_search_meta_carries_retrieved_entry_ids` and the three links after it are a contract
    with the library, not with this file.

    The second is the new auditability. `channels`, `rrf_k`, and `lexical_metric` are what let
    a reader of an event log tell a fused result from a fallback, know what constant fused it,
    and know the row came from the backend whose engine can rank lexically at all — a
    `TursoMemoryBackend` row never carries `lexical_metric`, because `fts_score()` cannot rank.

    `distances` holding `inf` for a lexical-only hit is asserted directly. It is deliberately
    not rounded to a number: `inf` reads as "cosine did not rank this", where a substituted
    0.0 would claim a perfect match.
    """
    memory, ids = _seeded(tmp_path)
    view = await memory.search("guidance", "appeal fine already sent", k=2)
    assert view.meta["channels"] == ["vector", "fts"]
    assert view.meta["rrf_k"] == RRF_K
    assert view.meta["lexical_metric"] == "bm25"
    assert view.meta["distance_metric"] == "vec_distance_cosine"
    assert set(view.meta["distances"]) == set(view.meta["results"])
    assert ids[2] in view.meta["results"]
    assert list(view.value) == list(view.meta["results"].values())
    memory.close()

    degenerate = SqliteMemoryBackend(
        Advice, actor_id="zeros", path=tmp_path / "mem.db", embedder=Zeros()
    )
    degenerate.add_entry("guidance", "an appeal may only follow a fine that was already sent")
    fallback = await degenerate.search("guidance", "appeal fine", k=1)
    assert fallback.meta["channels"] == ["fts"], "a fallback is invisible in the event log"
    assert list(fallback.meta["distances"].values()) == [float("inf")]
    degenerate.close()


# ── Retrieval discrimination: the guard against failing soft ──


def test_probe_retrieval_reports_separation_when_retrieval_works(tmp_path: Path) -> None:
    """Relevant queries must land closer than deliberately unrelated ones."""
    memory, ids = _seeded(tmp_path)
    report = memory.probe_retrieval(
        "guidance",
        relevant=[
            ("revisit a state already passed through", ids[0]),
            ("appeal follow a fine already sent", ids[2]),
        ],
        controls=[
            "kubernetes ingress certificate rotation",
            "sourdough starter hydration percentage",
        ],
        k=2,
    )
    assert report.discriminates is True
    assert report.hits == 2
    assert report.separation is not None and report.separation > 0
    assert report.self_retrieval_failures == ()
    assert "DISCRIMINATES" in str(report)
    memory.close()


def test_probe_retrieval_is_unmeasured_without_controls(tmp_path: Path) -> None:
    """No control queries means no null distribution, so the verdict is None.

    Three-valued on purpose, matching `detect.vacuity`. A measurement that cannot fail is
    not a measurement, and reporting it as a pass is the exact defect this project keeps
    finding.
    """
    memory, ids = _seeded(tmp_path)
    report = memory.probe_retrieval(
        "guidance", relevant=[("revisit a state", ids[0])], controls=[], k=1
    )
    assert report.discriminates is None
    assert report.separation is None
    assert "PARTIAL" in str(report)
    memory.close()


def test_probe_retrieval_is_unmeasured_with_no_probes_at_all(tmp_path: Path) -> None:
    """Zero probes is `None`, not `True`. Nothing was asked, so nothing was learned."""
    memory, _ = _seeded(tmp_path)
    report = memory.probe_retrieval("guidance", relevant=[], controls=["unrelated"], k=1)
    assert report.discriminates is None
    assert "UNMEASURED" in str(report)
    memory.close()


def test_probe_retrieval_detects_a_useless_embedding(tmp_path: Path) -> None:
    """A constant embedder cannot discriminate, and the probe says so.

    The failing-soft case made concrete. Every search still returns a full ranked list of
    the right length, so a smoke test on `search` passes completely. Only comparing
    relevant against control distances reveals there is no signal.
    """
    memory = _backend(tmp_path, embedder=Constant())
    ids = [memory.add_entry("guidance", text) for text in ENTRIES]

    assert len(memory.search_entries("guidance", "anything at all", k=3)) == 3, (
        "search still returns a full ranked list; that is why the probe is needed"
    )

    report = memory.probe_retrieval(
        "guidance",
        relevant=[("revisit a state already passed through", ids[0])],
        controls=["kubernetes ingress certificate rotation"],
        k=1,
    )
    assert report.discriminates is False
    assert report.separation == pytest.approx(0.0, abs=1e-6)
    memory.close()


def test_probe_retrieval_returns_the_same_verdict_type_as_the_turso_backend(
    tmp_path: Path,
) -> None:
    """One `Discrimination` class, not two that agree today.

    A caller that reads `discriminates` must not have to know which backend produced the
    report, and two copies of a three-valued verdict would drift — a separation threshold
    changed in one and not the other, with no test able to see it. Asserted by identity
    rather than by shape, because same-shaped is exactly what a drifted copy looks like.
    """
    from pneuma.memory import turso_backend

    memory, ids = _seeded(tmp_path)
    report = memory.probe_retrieval(
        "guidance", relevant=[("revisit a state", ids[0])], controls=["unrelated"], k=1
    )
    assert type(report) is turso_backend.Discrimination
    memory.close()


def test_calibrate_ceiling_derives_a_threshold_between_the_distributions(
    tmp_path: Path,
) -> None:
    """A ceiling is measured, and it sits strictly between the two distributions."""
    memory, ids = _seeded(tmp_path)
    ceiling = memory.calibrate_ceiling(
        "guidance",
        relevant=[
            ("revisit a state already passed through", ids[0]),
            ("appeal follow a fine already sent", ids[2]),
        ],
        controls=["kubernetes ingress certificate rotation"],
        k=2,
    )
    report = memory.probe_retrieval(
        "guidance",
        relevant=[("revisit a state already passed through", ids[0])],
        controls=["kubernetes ingress certificate rotation"],
        k=2,
    )
    assert report.worst_relevant is not None and report.best_control is not None
    assert report.worst_relevant <= ceiling <= report.best_control
    memory.close()


def test_calibrate_ceiling_refuses_when_the_distributions_overlap(tmp_path: Path) -> None:
    """No separable threshold means a raise, never an invented midpoint.

    Returning a plausible-looking number here would be the silent cap this project keeps
    finding, sitting in the retrieval path where nothing downstream could see it.
    """
    memory = _backend(tmp_path, embedder=Constant())
    ids = [memory.add_entry("guidance", text) for text in ENTRIES]
    with pytest.raises(CeilingNotSeparable, match="No threshold separates"):
        memory.calibrate_ceiling(
            "guidance",
            relevant=[("revisit a state", ids[0])],
            controls=["kubernetes ingress"],
            k=1,
        )
    memory.close()


def test_calibrate_ceiling_refuses_without_both_sides(tmp_path: Path) -> None:
    """Calibration needs both distributions; one side alone justifies nothing."""
    memory, ids = _seeded(tmp_path)
    with pytest.raises(CeilingNotSeparable, match="needs both"):
        memory.calibrate_ceiling("guidance", relevant=[("revisit", ids[0])], controls=[], k=1)
    memory.close()


def test_calibrate_ceiling_rejects_a_margin_outside_the_gap(tmp_path: Path) -> None:
    """`margin` places the ceiling inside the measured gap, so outside [0, 1] is a bug."""
    memory, ids = _seeded(tmp_path)
    with pytest.raises(ValueError, match=r"margin must be in \[0, 1\]"):
        memory.calibrate_ceiling("guidance", [("revisit", ids[0])], ["unrelated"], margin=1.5)
    memory.close()


# ── Narrow gradients: the crux, offline ──


async def test_search_meta_carries_retrieved_entry_ids(tmp_path: Path) -> None:
    """`search`'s `ParameterView.meta["results"]` names exactly the retrieved entries.

    Link one of the chain that makes a gradient narrow. `distance_metric` is asserted too:
    the numbers are only comparable within one SQL function, and a log holding rows from
    both backends would otherwise invite a ceiling derived under one to be reused under the
    other.
    """
    memory, ids = _seeded(tmp_path)
    view = await memory.search("guidance", "revisit a state already passed through", k=2)
    results = view.meta["results"]
    assert len(results) == 2
    assert set(results) <= set(ids)
    assert ids[0] in results
    assert list(view.value) == list(results.values())
    assert set(view.meta["distances"]) == set(results)
    assert view.meta["distance_metric"] == "vec_distance_cosine"
    assert view.meta["embedding_model"] == "bagofwords:v1"
    memory.close()


async def test_recall_event_carries_the_retrieved_ids(tmp_path: Path) -> None:
    """Link two: the ids reach the `ParameterRecalledEvent` in the thread's log."""
    from ai_functions.runtime import InMemoryCoordinator
    from ai_functions.types import ThreadId

    memory, ids = _seeded(tmp_path)
    coordinator = InMemoryCoordinator()
    thread_id = ThreadId("t-1")

    await memory.search(
        "guidance",
        "revisit a state already passed through",
        k=2,
        coordinator=coordinator,
        thread_id=thread_id,
    )

    events = [
        e
        for e in await coordinator.get_events(thread_id)
        if isinstance(e, ParameterRecalledEvent)
    ]
    assert len(events) == 1
    assert events[0].derivation == "search"
    assert events[0].meta["top_k"] == 2
    assert events[0].backend_id == "SqliteMemoryBackend:nav"
    assert ids[0] in events[0].meta["results"]
    memory.close()


async def test_traced_call_reconstructs_a_parameter_node_with_the_ids(tmp_path: Path) -> None:
    """Link three: `build_graph` puts `meta["results"]` on the `ParameterNode`.

    This also guards the two library sharp edges at once. The view is passed as a handle so
    `collect_nodes` finds it, and a *fresh* search happens per call because a
    `ParameterView` is single-use.
    """
    from ai_functions import ai_function

    memory, ids = _seeded(tmp_path)

    @ai_function[str](structured_output=False, coordinator_tools_enabled=False)
    def _decide(advice: list[str], state: str) -> str:
        """Pick a move at {state} given {advice}."""

    async with RuntimeHarness():
        compiled = _decide.replace(model=ScriptedModel([Turn(text="ok")] * 4))
        view = await memory.search("guidance", "revisit a state already passed through", k=2)
        traced = await compiled.trace(view, "S1")
        graph = await build_graph_from_result(traced, [memory])

    assert [p.name for p in graph.parameters] == ["guidance"]
    node = graph.parameters[0]
    assert node.derivation == "search"
    assert ids[0] in node.meta["results"]
    assert node.backend is memory
    memory.close()


async def test_interpolating_a_view_drops_the_gradient_edge(tmp_path: Path) -> None:
    """The silent failure the library docs warn about, asserted rather than trusted.

    `f"{view}"` computes an identical prompt and produces no parameter node. Nothing
    raises. Asserted for this backend too, because the property belongs to how a caller
    passes the view and a caller swapping backends keeps its own calling style.
    """
    from ai_functions import ai_function

    memory, _ = _seeded(tmp_path)

    @ai_function[str](structured_output=False, coordinator_tools_enabled=False)
    def _decide(advice: str, state: str) -> str:
        """Pick a move at {state} given {advice}."""

    async with RuntimeHarness():
        compiled = _decide.replace(model=ScriptedModel([Turn(text="ok")] * 4))
        view = await memory.search("guidance", "revisit a state", k=1)
        interpolated = await compiled.trace(f"{view}", "S1")
        graph = await build_graph_from_result(interpolated, [memory])

    assert graph.parameters == [], "an f-string view is not a gradient target"
    memory.close()


async def test_reusing_one_view_across_calls_yields_one_target(tmp_path: Path) -> None:
    """"One logical recall, one event": a reused view is a target only the first time.

    The bug that made a training loop report rounds while learning nothing.
    """
    from ai_functions import ai_function

    memory, _ = _seeded(tmp_path)

    @ai_function[str](structured_output=False, coordinator_tools_enabled=False)
    def _decide(advice: list[str], state: str) -> str:
        """Pick a move at {state} given {advice}."""

    async with RuntimeHarness():
        compiled = _decide.replace(model=ScriptedModel([Turn(text="ok")] * 8))
        reused = await memory.search("guidance", "revisit a state", k=1)
        first = await compiled.trace(reused, "S1")
        second = await compiled.trace(reused, "S2")
        fresh = await compiled.trace(await memory.search("guidance", "revisit a state", k=1), "S3")
        names = [
            [p.name for p in (await build_graph_from_result(r, [memory])).parameters]
            for r in (first, second, fresh)
        ]

    assert names == [["guidance"], [], ["guidance"]]
    memory.close()


async def test_consolidate_receives_the_retrieved_ids_from_the_optimizer(
    tmp_path: Path,
) -> None:
    """Link four: `TextGradOptimizer.consolidate` passes the ids as `retrieved=`.

    Driven through the real optimizer with hand-placed gradients, so the grouping and
    meta-merging code under test is the library's, not a re-implementation.
    """
    from ai_functions import TextGradOptimizer
    from ai_functions.types.graph import ParameterNode, ThreadNode

    memory, ids = _seeded(tmp_path)
    recorded: list[tuple[str, list[str], dict[str, str] | None]] = []
    memory._consolidate = lambda name, feedback, retrieved=None, **_: recorded.append(  # type: ignore[method-assign]
        (name, [g.text for g in feedback], retrieved)
    )

    node = ParameterNode(
        node_id="p",
        name="guidance",
        backend=memory,
        gradients=[GradFeedback(text="be sharper about revisits", score=0.3)],
        meta={"results": {ids[0]: ENTRIES[0]}},
    )
    root = ThreadNode(node_id="root", thread_id="root", parameters=[node])
    TextGradOptimizer().consolidate(root)

    assert len(recorded) == 1
    name, texts, retrieved = recorded[0]
    assert name == "guidance"
    assert texts == ["be sharper about revisits"]
    assert retrieved == {ids[0]: ENTRIES[0]}, "the gradient names one entry, not the corpus"
    memory.close()


def test_a_gradient_about_one_entry_leaves_the_others_untouched(tmp_path: Path) -> None:
    """The property the whole design exists for, asserted end to end offline.

    The consolidating agent is scripted to update the retrieved entry. What matters is that
    the other two entries come out **byte-identical**, including their ids: a whole-list
    rewrite would paraphrase advice no round measured, and a loop's completion-rate view
    cannot see that happening.
    """
    memory, ids = _seeded(tmp_path)
    before = memory.list_entries("guidance")

    script = ScriptedModel(
        [
            Turn(
                tool_calls=(
                    (
                        "update_entry",
                        {"entry_id": ids[0], "value": "never re-enter a visited state, ever"},
                    ),
                )
            ),
            Turn(text="done"),
        ]
    )
    memory._edit_entries_fn = memory._edit_entries_fn.replace(model=script)

    memory.consolidate(
        "guidance",
        [GradFeedback(text="the agent kept revisiting states", score=0.2)],
        retrieved={ids[0]: before[ids[0]]},
    )

    after = memory.list_entries("guidance")
    assert after[ids[0]] == "never re-enter a visited state, ever", "the target changed"
    assert after[ids[1]] == before[ids[1]], "an unretrieved entry must be byte-identical"
    assert after[ids[2]] == before[ids[2]], "an unretrieved entry must be byte-identical"
    assert set(after) == set(before), "no ids were retired or minted"
    memory.close()


def test_the_shared_entry_tool_provider_drives_this_backend(tmp_path: Path) -> None:
    """`EntryToolProvider` is reused from `turso_backend`, so the reuse is a checked property.

    Its `backend` parameter is annotated `TursoMemoryBackend` and this file cannot widen the
    annotation, because `turso_backend.py` is frozen. It only ever calls `search_entries`,
    `add_entry`, `update_entry`, and `remove_entry` — identical on both — but "identical
    today" is exactly the assumption that rots. So all four tools are driven against this
    backend and their effects read back from the store, rather than trusting a comment about
    a structural type.
    """
    from pneuma.memory import EntryToolProvider

    memory, ids = _seeded(tmp_path)
    provider = EntryToolProvider(memory, "guidance")  # type: ignore[arg-type]
    tools = {t.tool_name: t for t in asyncio.run(provider.load_tools())}
    assert set(tools) == {"search_entries", "add_entry", "update_entry", "delete_entry"}

    def call(name: str, **kwargs: Any) -> Any:
        return tools[name]._tool_func(**kwargs)  # type: ignore[attr-defined]

    found = call("search_entries", query="revisit a state already passed through", k=2)
    assert found[0]["entry_id"] == ids[0]
    assert float(found[0]["distance"]) < float(found[1]["distance"])

    added = call("add_entry", value="a tool-added entry")
    assert "entry_id=" in added
    call("update_entry", entry_id=ids[1], value="rewritten through the shared tool")
    call("delete_entry", entry_id=ids[2])

    entries = memory.list_entries("guidance")
    assert entries[ids[1]] == "rewritten through the shared tool"
    assert ids[2] not in entries
    assert "a tool-added entry" in entries.values()

    with pytest.raises(ValueError, match="not found"):
        call("update_entry", entry_id="does-not-exist", value="x")
    with pytest.raises(ValueError, match="not found"):
        call("delete_entry", entry_id="does-not-exist")
    memory.close()


def test_the_consolidator_is_only_shown_the_retrieved_entries(tmp_path: Path) -> None:
    """The scoping half of narrowness, which the byte-identity test cannot see.

    A mutation check found this gap in the Turso suite: replacing the scoping expression
    with "show the whole corpus" left every other test passing, because a *scripted* editor
    only touches what its script names regardless of what it was shown. With a real model
    the difference is the whole point, so the prompt's contents are asserted directly.
    """
    memory, ids = _seeded(tmp_path)
    capture = CaptureFn()
    memory._edit_entries_fn = capture  # type: ignore[assignment]

    memory.consolidate(
        "guidance",
        [GradFeedback(text="the agent kept revisiting states", score=0.2)],
        retrieved={ids[0]: ENTRIES[0]},
    )

    assert capture.kwargs is not None
    shown = capture.kwargs["retrieved"]
    assert ENTRIES[0] in shown
    assert ENTRIES[1] not in shown, "an unretrieved entry was shown to the editor"
    assert ENTRIES[2] not in shown, "an unretrieved entry was shown to the editor"
    assert capture.tools is not None, "the editor got no CRUD tools, so it can change nothing"
    memory.close()


def test_the_consolidator_sees_current_values_not_search_time_ones(tmp_path: Path) -> None:
    """Values are re-read from the store, because an earlier round may have edited them.

    Handing the editor the text `retrieved` carries would show it a stale entry and invite
    it to re-apply a change already made.
    """
    memory, ids = _seeded(tmp_path)
    memory.update_entry("guidance", ids[0], "the current, already-updated wording")
    capture = CaptureFn()
    memory._edit_entries_fn = capture  # type: ignore[assignment]

    memory.consolidate(
        "guidance",
        [GradFeedback(text="sharpen this")],
        retrieved={ids[0]: ENTRIES[0]},  # the search-time text, now stale
    )

    assert capture.kwargs is not None
    shown = capture.kwargs["retrieved"]
    assert "already-updated wording" in shown
    assert ENTRIES[0] not in shown, "the stale search-time text was shown"
    memory.close()


def test_consolidation_without_retrieval_context_sees_the_whole_corpus(
    tmp_path: Path,
) -> None:
    """With no `retrieved`, the fallback is honest: show everything.

    A gradient whose forward pass cannot be localized should not pretend to be narrow.
    Scoping it to an arbitrary subset would be a guess presented as evidence.
    """
    memory, ids = _seeded(tmp_path)
    capture = CaptureFn()
    memory._edit_entries_fn = capture  # type: ignore[assignment]

    memory.consolidate("guidance", [GradFeedback(text="general note")], retrieved=None)

    assert capture.kwargs is not None
    assert all(entry_id in capture.kwargs["retrieved"] for entry_id in ids)
    memory.close()


def test_a_stale_entry_id_is_dropped_rather_than_resolving_wrongly(tmp_path: Path) -> None:
    """An id deleted between forward pass and consolidation drops out safely."""
    memory, ids = _seeded(tmp_path)
    memory.remove_entry("guidance", ids[0])
    capture = CaptureFn()
    memory._edit_entries_fn = capture  # type: ignore[assignment]

    memory.consolidate(
        "guidance",
        [GradFeedback(text="about the deleted entry")],
        retrieved={ids[0]: ENTRIES[0]},
    )

    # Every id was stale, so the honest fallback shows the surviving corpus.
    assert capture.kwargs is not None
    shown = capture.kwargs["retrieved"]
    assert ids[1] in shown and ids[2] in shown
    memory.close()


# ── The score channel: numeric parameters ──


def test_a_score_moves_a_numeric_parameter_and_records_the_observation(
    tmp_path: Path,
) -> None:
    """The second channel, with no model call at all.

    `GradFeedback.score` alone moves the value. The text is kept as the rationale, so the
    artifact records why a harness parameter holds the value it does.
    """
    memory = _backend(tmp_path)
    assert memory.numeric_value("threshold") == 20.0

    memory.consolidate("threshold", [GradFeedback(text="too low, the model is noisy", score=0.2)])
    moved = memory.numeric_value("threshold")
    assert moved != 20.0
    assert memory.observations("threshold") == [(20.0, 0.2, "too low, the model is noisy")]
    memory.close()


def test_a_perfect_score_does_not_move_a_numeric_parameter(tmp_path: Path) -> None:
    """A value that served perfectly is left where it is.

    The property a "nudge it each round" rule lacks: without it the search would wander
    away from an optimum it had already found.
    """
    memory = _backend(tmp_path)
    memory.consolidate("ratio", [GradFeedback(text="ideal", score=1.0)])
    assert memory.numeric_value("ratio") == 0.5
    memory.close()


def test_a_numeric_parameter_stays_inside_its_schema_bounds(tmp_path: Path) -> None:
    """The declared domain is respected, so a proposal cannot fail validation."""
    memory = _backend(tmp_path)
    for _ in range(12):
        memory.consolidate("ratio", [GradFeedback(text="worthless", score=0.0)])
        assert 0.0 <= memory.numeric_value("ratio") <= 1.0
    for _ in range(12):
        memory.consolidate("threshold", [GradFeedback(text="worthless", score=0.0)])
        value = memory.numeric_value("threshold")
        assert 1.0 <= value <= 100.0
        assert value == int(value), "an int parameter stays integral"
    memory.close()


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_every_numeric_proposal_validates_against_its_own_schema(
    tmp_path: Path, score: float
) -> None:
    """Exclusive bounds are exclusive, so a proposal never fails its own validator.

    Regression guard for a bug found by probing on the Turso backend and inherited with the
    ported search. `gt`/`lt` read as inclusive made `Field(0.5, gt=0.0, lt=1.0)` under
    repeated zero scores propose exactly 1.0 on the fourth round — valid arithmetic,
    invalid data, and Pydantic raises a long way from the cause. Rounding order mattered
    too: rounding an already-clamped int could step back over the bound.

    The strongest available assertion is used deliberately: feed each proposal back through
    the schema. Anything weaker restates the clamping code rather than checking it against
    the thing that actually enforces the domain.
    """

    class Bounded(BaseModel):
        exclusive_float: float = Field(0.5, gt=0.0, lt=1.0, description="d")
        exclusive_int: int = Field(50, gt=0, lt=100, description="d")
        inclusive_int: int = Field(50, ge=1, le=100, description="d")

    for index, field in enumerate(Bounded.model_fields):
        memory = SqliteMemoryBackend(
            Bounded, actor_id=f"b{index}-{score}", path=tmp_path / "b.db", embedder=BagOfWords()
        )
        for round_index in range(20):
            memory.consolidate(field, [GradFeedback(text="observed", score=score)])
            value = memory.numeric_value(field)
            typed = value if field == "exclusive_float" else int(value)
            Bounded(**{field: typed})  # raises if the proposal left the domain
            assert value == memory.numeric_value(field), f"unstable read at {round_index}"
        memory.close()


def test_the_numeric_search_converges_under_a_constant_score(tmp_path: Path) -> None:
    """Steps shrink, so a constant score settles instead of oscillating forever.

    Without the decay the search would step the same distance every round and a loop would
    report a moving parameter that never lands anywhere.
    """
    memory = _backend(tmp_path)
    values = [memory.numeric_value("ratio")]
    for _ in range(8):
        memory.consolidate("ratio", [GradFeedback(text="mediocre", score=0.5)])
        values.append(memory.numeric_value("ratio"))
    steps = [abs(b - a) for a, b in zip(values[:-1], values[1:], strict=True)]
    assert steps[-1] < steps[0], f"steps did not shrink: {steps}"
    assert steps[-1] < 0.05, f"did not converge: {values}"
    memory.close()


def test_the_first_numeric_step_stays_inside_the_trust_region(tmp_path: Path) -> None:
    """A bad score proposes a step, not a jump to the far boundary.

    Regression guard for a measured bug. Without the trust region a `ge=1, le=100`
    parameter at 20 scoring 0.2 proposed 99: one sample at each end of the domain rather
    than a search over it.
    """
    memory = _backend(tmp_path)
    memory.consolidate("threshold", [GradFeedback(text="bad", score=0.2)])
    moved = memory.numeric_value("threshold")
    assert 20.0 < moved <= 20.0 + 0.25 * 99.0 + 1, f"first step left the trust region: {moved}"
    memory.close()


def test_the_numeric_search_bisects_back_toward_a_better_value(tmp_path: Path) -> None:
    """When a tried value scored better, the search steps toward it, not away."""
    memory = _backend(tmp_path)
    memory.consolidate("ratio", [GradFeedback(text="poor", score=0.2)])
    explored = memory.numeric_value("ratio")
    memory.consolidate("ratio", [GradFeedback(text="worse than before", score=0.05)])
    returned = memory.numeric_value("ratio")
    assert abs(returned - 0.5) < abs(explored - 0.5), (
        f"did not move back toward the better value: 0.5 -> {explored} -> {returned}"
    )
    memory.close()


def test_a_numeric_parameter_with_no_score_is_left_alone(tmp_path: Path) -> None:
    """No score means no evidence, so the value does not move.

    Rewriting a number from feedback text alone would be invention, and a loop cannot tell
    invention from learning.
    """
    memory = _backend(tmp_path)
    memory.consolidate("threshold", [GradFeedback(text="feels wrong", score=None)])
    assert memory.numeric_value("threshold") == 20.0
    assert memory.observations("threshold") == []
    memory.close()


def test_numeric_value_rejects_a_non_numeric_parameter(tmp_path: Path) -> None:
    """Asking for a float where the schema declares prose is a caller bug, not 0.0."""
    memory = _backend(tmp_path)
    with pytest.raises(TypeError, match="not a numeric parameter"):
        memory.numeric_value("summary")
    memory.close()


def test_numeric_observations_persist_across_a_reopen(tmp_path: Path) -> None:
    """A reopened database resumes the search rather than restarting it."""
    memory = _backend(tmp_path)
    memory.consolidate("threshold", [GradFeedback(text="first", score=0.3)])
    memory.close()

    reopened = _backend(tmp_path)
    assert len(reopened.observations("threshold")) == 1
    assert reopened.numeric_value("threshold") != 20.0
    reopened.close()


def test_delete_resets_a_numeric_parameter_and_clears_its_history(tmp_path: Path) -> None:
    """A reset drops the observations too; keeping them would bias a fresh search."""
    memory = _backend(tmp_path)
    memory.consolidate("threshold", [GradFeedback(text="x", score=0.1)])
    memory.delete("threshold")
    assert memory.numeric_value("threshold") == 20.0
    assert memory.observations("threshold") == []
    memory.close()


def test_delete_refuses_a_required_parameter(tmp_path: Path) -> None:
    """A reset needs a default to reset *to*; without one it raises rather than blanking.

    The schema is nested rather than flat, and that is the finding rather than a
    convenience. `_seed_defaults` instantiates `self.schema()` to read the defaults, so a
    *top-level* required field makes the backend unconstructible — a `ValidationError`
    before `delete` is reachable at all, on both backends. A required leaf under a field
    with a `default_factory` is the shape where the guard can actually fire: the outer
    default supplies the leaf, so the store seeds, and the leaf itself still has no default
    to reset to.
    """

    class Inner(BaseModel):
        must: str = Field(description="No default of its own, so nothing to reset to.")

    class Outer(BaseModel):
        inner: Inner = Field(default_factory=lambda: Inner(must="seeded"), description="d")

    memory = SqliteMemoryBackend(
        Outer, actor_id="r", path=tmp_path / "r.db", embedder=BagOfWords()
    )
    assert memory._resolve_field("inner/must").is_required()
    assert memory.fetch("inner/must") == "seeded", "the nested default seeded the store"
    with pytest.raises(ValueError, match="no schema default"):
        memory.delete("inner/must")
    memory.close()


def test_both_backends_propose_the_same_number_from_the_same_history(tmp_path: Path) -> None:
    """The numeric search is ported, so the port is checked against the original.

    The one claim that is only provable jointly, and the reason it belongs in this file:
    `_numeric_update` reads nothing from the database except `observations`, so both
    backends must propose the *same* value from the same score sequence.

    That the property-level tests do not cover this was measured, not assumed. Changing the
    port's decay exponent from `len(history) - 1` to `len(history)` — a real off-by-one in
    the shrink schedule — left `test_the_numeric_search_converges_under_a_constant_score`
    and `test_the_first_numeric_step_stays_inside_the_trust_region` both passing, because a
    search that decays a round early still converges and still starts inside the trust
    region. Only this test caught it, at round 0 (0.62 against 0.70). Twelve rounds are
    compared step by step so a divergence is located rather than merely detected.
    """
    from pneuma.memory import TursoMemoryBackend

    sqlite_memory = SqliteMemoryBackend(
        Advice, actor_id="cmp", path=tmp_path / "cmp-sqlite.db", embedder=BagOfWords()
    )
    turso_memory = TursoMemoryBackend(
        Advice, actor_id="cmp", path=tmp_path / "cmp-turso.db", embedder=BagOfWords()
    )
    try:
        scores = [0.2, 0.2, 0.7, 0.1, 0.9, 0.4, 0.4, 0.0, 1.0, 0.3, 0.6, 0.5]
        for index, score in enumerate(scores):
            for backend in (sqlite_memory, turso_memory):
                backend.consolidate("ratio", [GradFeedback(text="observed", score=score)])
                backend.consolidate("threshold", [GradFeedback(text="observed", score=score)])
            assert sqlite_memory.numeric_value("ratio") == pytest.approx(
                turso_memory.numeric_value("ratio")
            ), f"the float search diverged at round {index} (score {score})"
            assert sqlite_memory.numeric_value("threshold") == pytest.approx(
                turso_memory.numeric_value("threshold")
            ), f"the int search diverged at round {index} (score {score})"
        assert sqlite_memory.observations("ratio") == [
            (value, score, text) for value, score, text in turso_memory.observations("ratio")
        ]
    finally:
        sqlite_memory.close()
        turso_memory.close()


# ── The text channels ──


def test_a_scalar_parameter_is_rewritten_from_the_feedback_text(tmp_path: Path) -> None:
    """A plain scalar takes the text channel and is rewritten whole.

    Also asserts the schema description reaches the consolidator: it is where the author
    states how updates should merge, and dropping it would let a rewrite ignore the format
    the parameter is supposed to keep.
    """
    memory = _backend(tmp_path)
    capture = CaptureFn(returns="a fuller summary")
    memory._rewrite_value_fn = capture  # type: ignore[assignment]

    memory.consolidate("summary", [GradFeedback(text="say more", score=0.4)])

    assert capture.kwargs == {
        "value": "nothing yet",
        "feedback": ["say more"],
        "description": "A scalar note.",
    }
    assert memory.fetch("summary") == "a fuller summary"
    memory.close()


def test_a_procedural_parameter_stores_and_validates_code(tmp_path: Path) -> None:
    """`Procedural` is honoured: code is stored, and unparseable code is refused."""
    from ai_functions import Procedural

    class WithCode(BaseModel):
        helper: Procedural = Field(description="Mining helper functions.")

    memory = SqliteMemoryBackend(
        WithCode, actor_id="m", path=tmp_path / "code.db", embedder=BagOfWords()
    )
    assert memory._is_procedural("helper") is True

    memory.save("helper", "def mine(log):\n    return log\n")
    assert "def mine" in str(memory.fetch("helper"))

    with pytest.raises(SyntaxError):
        memory.save("helper", "def broken(:\n")
    assert "def mine" in str(memory.fetch("helper")), "the bad save did not corrupt the store"
    memory.close()


def test_procedural_consolidation_runs_through_the_code_rewriter(tmp_path: Path) -> None:
    """A gradient on a `Procedural` parameter rewrites code, not prose."""
    from ai_functions import Procedural

    class WithCode(BaseModel):
        helper: Procedural = Field(description="Mining helper functions.")

    memory = SqliteMemoryBackend(
        WithCode, actor_id="m", path=tmp_path / "code.db", embedder=BagOfWords()
    )
    memory.save("helper", "def mine(log):\n    return log\n")
    memory._rewrite_code_fn = CaptureFn(returns="def mine(log):\n    return sorted(log)\n")  # type: ignore[assignment]
    memory.consolidate("helper", [GradFeedback(text="sort the log first", score=0.5)])
    assert "sorted" in str(memory.fetch("helper"))
    memory.close()


def test_query_renders_entries_as_a_list_for_the_model(tmp_path: Path) -> None:
    """`query` reads the whole parameter, entries rendered as bullets not a repr.

    Asserted because `str(["a", "b"])` would hand the model `['a', 'b']`, which reads as a
    Python literal rather than as advice and is a needlessly worse prompt.
    """
    memory, _ = _seeded(tmp_path)
    capture = CaptureFn(returns="three")
    memory._answer_fn = capture  # type: ignore[assignment]

    assert memory._query("guidance", "how many entries?")[0] == "three"
    assert capture.kwargs is not None
    content = capture.kwargs["value"]
    assert content.startswith("- ")
    assert all(text in content for text in ENTRIES)
    memory.close()


def test_tool_provider_exposes_entry_crud_for_list_parameters(tmp_path: Path) -> None:
    """An agent gets the same tool names as the Turso and JSON backends.

    Name-for-name compatibility is what makes the backends swappable under an agent that
    was written against one of them: a renamed tool is a prompt-visible break with no type
    error anywhere.
    """
    memory = _backend(tmp_path)
    provider = memory.tool_provider("guidance", "summary")
    names = {t.tool_name for t in asyncio.run(provider.load_tools())}
    assert {
        "search_guidance",
        "add_to_guidance",
        "update_guidance",
        "delete_from_guidance",
    } <= names
    assert "search_summary" not in names, "search is list-only"
    assert {"save_summary", "delete_summary"} <= names
    memory.close()


def test_str_dumps_every_parameter_for_a_report(tmp_path: Path) -> None:
    """`str(backend)` is what a report or a log prints, so it shows entries with their ids."""
    memory, ids = _seeded(tmp_path)
    dump = str(memory)
    assert "guidance:" in dump
    assert f"[{ids[0]}] {ENTRIES[0]}" in dump
    assert "summary: nothing yet" in dump
    assert "threshold: 20" in dump
    memory.close()


# ── Cross-backend storage interop ──


def test_a_turso_written_database_ranks_under_this_backend(tmp_path: Path) -> None:
    """One file, either driver: the schema and the blob format are genuinely shared.

    Not a convenience claim. It is the evidence that "drop-in sibling" means the *stored
    artifact* is portable and not merely that the two classes have matching method names —
    so a project that started on Turso can be reopened here without re-embedding a corpus,
    and the ceiling caveat in `calibrate_ceiling` is about the metric alone.

    Entry ids, positions, and the embedding cache all come across, which is what makes it a
    real reopen: the ids a Turso-era event log recorded still resolve.
    """
    from pneuma.memory import TursoMemoryBackend

    path = tmp_path / "portable.db"
    written = TursoMemoryBackend(
        Advice, actor_id="nav", path=path, embedder=BagOfWords()
    )
    ids = [written.add_entry("guidance", text) for text in ENTRIES]
    assert written.embed_pending("guidance") == len(ENTRIES)
    written.save("summary", "written by the turso backend")
    written.close()

    embedder = BagOfWords()
    reopened = SqliteMemoryBackend(Advice, actor_id="nav", path=path, embedder=embedder)
    assert list(reopened.list_entries("guidance").values()) == list(ENTRIES)
    assert set(reopened.list_entries("guidance")) == set(ids)
    assert reopened.fetch("summary") == "written by the turso backend"

    hits = reopened.search_entries("guidance", "revisit a state already passed through", k=2)
    assert hits[0].entry_id == ids[0]
    assert embedder.calls == 1, "only the query was embedded; the cached document vectors ranked"
    assert reopened.unranked_entries("guidance") == []
    assert reopened.add_entry("guidance", "appended by the sqlite backend") not in ids
    reopened.close()
