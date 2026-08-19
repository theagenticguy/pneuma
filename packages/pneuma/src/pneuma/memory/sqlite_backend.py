"""A `MemoryBackend` over stdlib `sqlite3` + `sqlite-vec`: the Turso backend, no server.

A drop-in sibling of `TursoMemoryBackend` — same constructor shape, same schema shape, same
public surface — for callers who want vector recall out of a file `sqlite3` already opens.
Everything about *why* addressable entries, two learning channels, and a measured
discrimination guard exist is argued once on `TursoMemoryBackend`; this file is about the
three things the driver swap actually changes. Rationale: `docs/design/sqlite_backend.md`.

**`check_same_thread=False` is mandatory, and it is this file's landmine.**
`MemoryBackend.recall` / `.query` / `.search` all run their `_*` hook inside
`asyncio.to_thread`, so every public recall touches the connection from a worker thread.
Stock `sqlite3.connect` refuses: `ProgrammingError: SQLite objects created in a thread can
only be used in that same thread`. It fails on the *first* awaited recall, which reads as a
broken backend rather than as a connection flag, so `sqlite_connect` sets the flag and a
test asserts an awaited `search` crosses the boundary.

**`vec_distance_cosine` returns NULL for a zero-magnitude vector, and SQLite sorts NULL
first under ASC.** So a degenerate cached vector becomes the *top hit* at `distance=None`,
which `float(None)` then rejects with a `TypeError` from the row mapper — three frames from
anything explanatory, and only because the mapper happens to be strict. Every ranking query
therefore carries `AND distance IS NOT NULL`, and `degenerate_entries` makes the dropped set
countable for the same reason `unranked_entries` does. Turso has no such case: measured,
`vector_distance_cos` on a zero vector returns 1.0. A NULL *blob* still raises here (`Error
reading 1st vector: ... found NULL`), so retrieval is an inner join exactly as it is there.

**The `pyturso` cursor-GC write-loss defect does not exist here, so its discipline is not
needed.** Measured: eight read-modify-write counter cycles with a deliberately leaked,
unexhausted cursor per cycle allocated ids 1..8, and a write following a leaked cursor was
still visible after `commit()`. Reads still go through `embedding.fetch_rows` — not out of
necessity, but because `EmbeddingCache` uses it and one read path is easier to audit than
two. Nothing in this module depends on the close.

Blob format is shared, not merely compatible: `sqlite_vec.serialize_float32`,
`embedding.pack_vector`, and Turso's `vector32()` produce byte-identical little-endian
float32, verified. So `memory_embedding_cache` rows written by either backend rank in the
other, and this module reuses `pack_vector` rather than adding a second packer.

Ranking is an ordinary table scan ordered ASC, not a `vec0` virtual table. See the design
doc for why an exact scan is the honest choice at these entry counts.

**Hybrid retrieval lives here and nowhere else, because only this driver can rank
lexically.** `turso_backend.py` has no FTS path and its docstring says why: Turso's FTS
exists, but `fts_score()` returns 0.0 for *every* matching row, so it can select a matching
set and cannot order it — the ABC's `k` would take an arbitrary subset. Stdlib `sqlite3`
ships FTS5 with a real `bm25()`: measured on this build (SQLite 3.53.1, `ENABLE_FTS5`),
`bm25(memory_entry_fts)` returns distinct negative scores that order by match strength. So
`hybrid_entries` and `_search` fuse a vector channel with a lexical one, and the Turso
sibling keeps a pure-vector `_search`. That is a capability difference between two engines,
not a design difference between two backends, and it is the one place the "drop-in sibling"
claim is deliberately asymmetric.

**Fusion is Reciprocal Rank Fusion, not a weighted score blend, and that is a defect-class
decision rather than a preference.** Cosine distance is on [0, 2] and small-is-better;
`bm25()` is unbounded, negative, and corpus-dependent. Any `alpha * vector + (1 - alpha) *
lexical` needs a normalisation and an `alpha`, and neither is measured — they are fitted
constants sitting in a ranking path where a wrong value fails soft, which is exactly the
defect class `pneuma.detect` exists to catch and the same argument
`calibrate_ceiling` makes about a guessed `distance_ceiling`. RRF reads only each channel's
*rank*, so it needs no shared scale and has one constant (`RRF_K = 60`) whose effect is a
monotone reweighting of ranks rather than a threshold.

**The lexical index is a second copy of the entry text, kept honest by being countable.**
The design doc rejects a `vec0` table partly because it duplicates state that can diverge;
`memory_entry_fts` duplicates state too. The difference is auditability: a drifted *vector*
fails by returning plausible neighbours nobody can check, while a drifted *text* copy is
directly comparable to its source in one query. `fts_drift` is that query, and it sits
beside `unranked_entries` and `degenerate_entries` for the same reason they exist.

**Sync is manual, not by trigger, and two facts force it.** `init_schema` splits `SCHEMA`
on `;` and runs the statements one at a time — deliberately, because `executescript` issues
an implicit `COMMIT` that would commit a borrowed connection's in-flight transaction — and a
`CREATE TRIGGER ... BEGIN ...; ...; END` body contains semicolons, so the split hands SQLite
`OperationalError: incomplete input` (measured). More decisively, a trigger cannot index
rows that predate it: the portability property this backend rests on lets
`TursoMemoryBackend` write a corpus that is then reopened here, and those `memory_entry`
rows exist with no FTS rows at all. A backfill is required either way, and once
`init_schema` reconciles the index there is nothing left for a trigger to buy.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec
from ai_functions.memory.base import DynamicToolProvider, MemoryBackend, ParameterMeta
from strands.tools.decorator import (
    tool as _strands_tool,  # pyright: ignore[reportUnknownVariableType]
)

from .embedding import (
    CACHE_SCHEMA,
    DOCUMENT,
    QUERY,
    BedrockCohereEmbedder,
    Embedder,
    EmbeddingCache,
    digest_of,
    fetch_one,
    fetch_rows,
)

# Backend-independent pieces, imported rather than forked. The verdict types are a
# *contract*: a caller that reads `Discrimination.discriminates` must get the same
# three-valued object from either backend, and two copies would drift silently — the
# separation threshold in one and not the other, and no test would notice. Same for the
# consolidation prompts (nothing in them is driver-specific) and the numeric constants,
# each of which is justified by a measured bug recorded at its definition. This costs
# nothing: `pneuma.memory.__init__` already imports `turso_backend`, so `turso` is on the
# import path of anybody who reaches this module through the package.
from .turso_backend import (
    _EXCLUSIVE_EPSILON,
    _EXPLORE_DECAY,
    _TRUST_FRACTION,
    CeilingNotSeparable,
    Discrimination,
    EntryToolProvider,
    Retrieved,
    _answer_over_value,
    _bullets,
    _edit_entries,
    _format_numeric,
    _nested_attr,
    _numeric_type,
    _rewrite_code,
    _rewrite_value,
)

if TYPE_CHECKING:
    from ai_functions.types.graph import GradFeedback
    from pydantic import BaseModel
    from strands.models import Model
    from strands.types.tools import AgentTool


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memory_entry (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  entry_id   TEXT NOT NULL,
  position   INTEGER NOT NULL,
  value      TEXT NOT NULL,
  digest     TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_entry_order ON memory_entry(actor_id, param, position);

CREATE TABLE IF NOT EXISTS memory_scalar (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param)
);

CREATE TABLE IF NOT EXISTS memory_counter (
  actor_id TEXT NOT NULL,
  param    TEXT NOT NULL,
  next_id  INTEGER NOT NULL,
  PRIMARY KEY (actor_id, param)
);

CREATE TABLE IF NOT EXISTS memory_score_observation (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  value      REAL NOT NULL,
  score      REAL NOT NULL,
  rationale  TEXT NOT NULL DEFAULT '',
  observed_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param, seq)
);

{CACHE_SCHEMA}

CREATE VIRTUAL TABLE IF NOT EXISTS memory_entry_fts USING fts5(
  value,
  actor_id UNINDEXED,
  param    UNINDEXED,
  entry_id UNINDEXED,
  tokenize = 'unicode61'
);
"""
"""The first four tables are identical in shape to `turso_backend.SCHEMA`, deliberately as a
separate literal; `memory_entry_fts` has no Turso counterpart and cannot have one.

Every type in the shared part (`TEXT`, `REAL`, `INTEGER`, `PRIMARY KEY`, `CREATE INDEX IF
NOT EXISTS`) is plain SQLite, so those DDLs happen to be byte-identical today. They are not
shared, because importing the Turso module's schema would make a Turso-side change to a
column a silent change to this backend's storage — and the whole reason this file exists is
that the two engines are not the same engine.

`memory_entry_fts` is the lexical half of hybrid retrieval, and three choices in it are
load-bearing:

**Contentless-adjacent by convention, not by `content=''`.** FTS5 offers a true external-
content table (`content='memory_entry'`), which stores no text of its own and reads the
original row by rowid. It is rejected: it binds the index to `memory_entry.rowid`, an
implicit alias on a table whose primary key is the composite `(actor_id, param, entry_id)`.
Nothing in this module ever reads that rowid, so nothing would notice it changing — and
measured, `VACUUM` after a delete leaves surviving rowids alone *this* time, which is worse
than a guarantee because it invites the assumption. Every joinable identity in this file is
already `(actor_id, param, entry_id)`, so the index carries those three columns
`UNINDEXED` and joins on them. The cost is one duplicated copy of the entry text; `fts_drift`
makes the duplication auditable rather than trusted.

**`UNINDEXED` on the three key columns.** Without it an actor id or a parameter name becomes
a searchable term, so a query mentioning `guidance` matches every entry of the `guidance`
parameter at a bm25 score indistinguishable from a real content match. They are stored for
the join and excluded from the index.

**`tokenize='unicode61'`, which is the default, written out.** The default is not a detail
here: it folds diacritics and splits on Unicode categories, so `café` and `价格 上涨` tokenize
(verified) rather than collapsing to nothing. Naming it means a future switch to `porter` or
`trigram` — each of which would change what `bm25()` scores and therefore what the fused
ranking returns — is a visible edit rather than a silent inherit.
"""


RRF_K = 60
"""The rank-smoothing constant in Reciprocal Rank Fusion: `1 / (RRF_K + rank)`.

60 is the value from Cormack, Clarke & Buettcher's original RRF paper and it is a *shape*
parameter, not a fitted threshold — which is the whole reason a constant is tolerable here at
all. Raising it flattens the contribution curve so the two channels approach equal weight
per rank; lowering it sharpens the premium on being first. Every value orders the fused list
somehow, and no value can make the fusion return nothing or admit a hit neither channel
found, so a wrong `RRF_K` degrades ranking quality rather than silently changing what the
result *means*. Contrast a normalisation-and-`alpha` blend, where the same wrongness makes
one channel's scores dominate arithmetically while every observable signal stays identical.

Exposed as a module constant rather than a default argument so the value a fused ranking used
is nameable from a test and from the result meta.
"""

_FTS_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
"""Runs of word characters, underscore excluded: the tokens a query contributes to MATCH.

Deliberately the same *class* of split `unicode61` performs rather than an attempt to
reproduce it exactly. It does not have to match: a token this finds that FTS5 would have
folded away simply matches nothing, and `fts_entries` treats a zero-hit query as an honest
lexical miss.
"""


def fts_match_expression(query: str) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression: `"tok" OR "tok" OR ...`.

    Query text here is *model-authored or user-authored prose*, and MATCH is a query language
    of its own — not a string. Passing prose through raw is both a crash and a correctness
    problem, and every case below was measured against this build rather than read off the
    documentation:

        MATCH ''            -> OperationalError: fts5: syntax error near ""
        MATCH 'AND'         -> OperationalError: fts5: syntax error near "AND"
        MATCH 'NEAR('       -> OperationalError: fts5: syntax error near ""
        MATCH '-x'          -> OperationalError: no such column: x
        MATCH 'a:b'         -> OperationalError: no such column: a

    So a bare `AND` in an entry query, an unbalanced paren from a truncated prompt, or a
    colon in `note: check this` each raise from inside a ranking path — and the last two
    raise `no such column`, which reads as a schema bug rather than as untrusted input. Note
    what the parameter placeholder does *not* protect: binding stops SQL injection into the
    statement, and MATCH parsing happens afterwards on the bound value.

    Tokenising and re-quoting rather than escaping in place, because the two obvious cheaper
    fixes are both wrong. Wrapping the whole query in one quoted literal (`'"' + query + '"'`)
    parses fine but means a *phrase* search: measured, `"he said ""AND"" loudly"` returns 0
    rows against a corpus containing every one of those words, so the lexical channel would
    silently contribute nothing for any query longer than an exact substring. And escaping
    only `"` leaves `-x`, `a:b`, and `(` still raising.

    Each token becomes its own quoted literal, which makes FTS5 treat operator words as
    ordinary terms — verified, `"a" OR "AND"` matches — and the tokens are OR-ed because a
    disjunction is the recall-shaped choice: an AND over a nine-word question would require
    every word present and the channel would be empty in exactly the cases hybrid retrieval
    exists to catch. Ranking within the matched set is `bm25()`'s job, and it already
    rewards a row matching more of the query.

    Returns:
        A MATCH expression, or `""` when the query holds no indexable token at all
        (punctuation only, say). Empty is returned rather than a placeholder expression
        because `fts_entries` must be able to tell "the lexical channel cannot run" from
        "it ran and matched nothing" — collapsing those is the same failing-soft mistake
        `_require_rankable_query` refuses on the vector side.
    """
    tokens = _FTS_TOKEN.findall(query)
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def reciprocal_rank_fusion(channels: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked id lists into `{entry_id: rrf_score}`, higher is better.

    Reads each channel's *rank position only* — never its score — which is the point.
    `vec_distance_cosine` yields [0, 2] where small is better, `bm25()` yields unbounded
    negatives where small is also better but on a scale that moves with corpus size and term
    frequency; measured on a three-entry corpus, bm25 scores ranged over five orders of
    magnitude (-1.45 to -9.9e-07) for the same query. There is no normalisation of those two
    that is not a fitted constant, so ranks are used and the scales never meet.

    A hit present in both channels accumulates both terms, so agreement between an
    independent semantic ranking and an independent lexical one is what promotes an entry —
    which is the actual retrieval claim hybrid search makes.

    Args:
        channels: One ranked list of entry ids per channel, best first. An empty
            channel contributes nothing, which is how a fallback is expressed:
            fusing over one channel returns that channel's own order.
        k: Rank smoothing; see `RRF_K`.

    Returns:
        `{entry_id: score}` over the union of the channels. Unordered on purpose —
        ties are real and frequent (any two ids appearing at the same rank in
        single-channel lists of the same length tie exactly), so the caller breaks
        them explicitly rather than inheriting dict order.
    """
    fused: dict[str, float] = {}
    for ranking in channels:
        for position, entry_id in enumerate(ranking):
            fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (k + position + 1)
    return fused


class VectorExtensionUnavailable(RuntimeError):
    """This Python's `sqlite3` cannot load extensions, so `sqlite-vec` cannot be used.

    `enable_load_extension` is a compile-time option (`--enable-loadable-sqlite-extensions`)
    and several distro CPython builds ship without it. Raised at connect time, with the
    build named, because the alternative is `no such function: vec_distance_cosine` on the
    first retrieval — long after the cause, and indistinguishable from a missing package.
    """


def sqlite_connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite database with `sqlite-vec` loaded, WAL, and `synchronous=NORMAL`.

    Named `sqlite_connect` rather than `connect` because `memory.connect` is already the
    Turso opener and a caller picking a backend should not be able to grab the wrong one by
    autocomplete.

    `check_same_thread=False` is required, not tuning. See the module docstring: the base
    class dispatches every `_recall` / `_query` / `_search` through `asyncio.to_thread`, so
    the connection is used from a worker thread on the first awaited recall.

    WAL for the reason `casestudy.eventlog.connect` sets it — a training loop reads
    parameters while a writer appends traces to the same file — and verified here:
    `PRAGMA journal_mode=WAL` returns `('wal',)` and `-wal` / `-shm` files appear.
    `PRAGMA` does not open a transaction under the default `isolation_level`, so setting
    both pragmas before any DDL leaves nothing uncommitted.
    """
    connection = sqlite3.connect(str(path), check_same_thread=False)
    load_vector_extension(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def load_vector_extension(connection: sqlite3.Connection) -> None:
    """Load `sqlite-vec` onto a connection, then re-disable extension loading.

    Called for a borrowed connection too, since the extension is per-connection state and a
    caller who opened the file themselves has no reason to have loaded it. Idempotent —
    verified that a second `sqlite_vec.load` on the same connection is a no-op — so the
    borrow case does not have to know whether it already happened.

    Loading is left *disabled* afterwards: an open `enable_load_extension(True)` lets any
    later SQL string reaching this connection load a shared object from disk, and a memory
    backend is a place where SQL is assembled around model-authored text.
    """
    if not hasattr(connection, "enable_load_extension"):
        raise VectorExtensionUnavailable(
            "This Python's sqlite3 was built without loadable-extension support, so "
            "sqlite-vec cannot be loaded and vector recall is impossible. Use "
            "TursoMemoryBackend, or a CPython built with "
            "--enable-loadable-sqlite-extensions."
        )
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


class SqliteMemoryBackend(MemoryBackend):
    """Memory over stdlib `sqlite3` + `sqlite-vec` + FTS5: addressable entries, hybrid recall.

    Behaviourally the same object as `TursoMemoryBackend` — the two pass one shared test
    contract — so most of the surface below is stated by reference rather than re-argued. The
    one deliberate asymmetry is the lexical channel: `_search` here fuses a vector ranking
    with a `bm25()` one, and the Turso sibling cannot, because its `fts_score()` returns 0.0
    for every matching row. See the module docstring.

    ## Public surface

    Storage and identity:
        `path`, `connection`, `backend_id`, `close`, `init_schema`

    Entries (list parameters), keyed by never-reused monotonic ids:
        `list_entries`, `add_entry`, `update_entry`, `remove_entry`

    Retrieval, three ways, and the choice between them is not cosmetic:
        `hybrid_entries` — vector + `bm25()`, fused by RRF. What `_search` uses.
        `search_entries` — the vector channel alone. What `probe_retrieval`,
            `calibrate_ceiling`, and the shared `EntryToolProvider` use, because a
            distance-distribution verdict cannot be measured through a fused ranking.
        `fts_entries` — the lexical channel alone, mostly for auditing which channel
            produced a hit.

    Numeric parameters, learned from `GradFeedback.score`:
        `numeric_value`, `observations`

    Retrieval quality, because an embedding backend fails soft:
        `probe_retrieval`, `calibrate_ceiling`, `distance_ceiling` (`None` = no cap;
        applies to the vector channel only, since `bm25()` has no calibratable scale)

    Countable retrieval gaps, every one of which a bare `search` hides:
        `unranked_entries` — no cached vector, so the inner join drops them
        `degenerate_entries` — zero-magnitude vector, so `vec_distance_cosine` is NULL
        `fts_drift` — the lexical index disagrees with `memory_entry` (missing / stale /
            orphaned), repaired by `reindex_fts`

    Inherited from `MemoryBackend` and *not* overridden, deliberately: `recall`, `query`,
    `search`, `consolidate`, `save`, `fetch`, `delete`. Overriding those instead of the
    `_*` hooks skips `ParameterRecalledEvent` emission, and the parameter then vanishes
    from the optimizer graph with no error at all.

    Args:
        schema: The Pydantic memory schema.
        actor_id: Namespace within the database; several actors share one file.
        path: Database file. Ignored when `connection` is supplied.
        model: Model for the consolidation and query AI functions.
        embedder: Embedding provider. Defaults to Cohere Embed v4 on Bedrock,
            constructed lazily so importing this module needs no credentials.
        distance_ceiling: Drop hits further than this. `None` (the default) caps
            nothing; derive a value with `calibrate_ceiling` instead of picking one.
        connection: An existing `sqlite3` connection to share. It is not closed by
            `close()` — the owner closes it — and `sqlite-vec` is loaded onto it, since
            the extension is per-connection and a borrowed handle will not have it.
            It must have been opened with `check_same_thread=False`; an awaited recall
            otherwise raises `ProgrammingError` from a worker thread.
    """

    def __init__(
        self,
        schema: type[BaseModel],
        actor_id: str,
        path: Path | str | None = None,
        model: Model | str | None = None,
        embedder: Embedder | None = None,
        distance_ceiling: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(schema, actor_id)
        if connection is None and path is None:
            raise ValueError("SqliteMemoryBackend needs either a path or a connection.")

        self.path = Path(path) if path is not None else None
        self._owns_connection = connection is None
        if connection is None:
            self.connection = sqlite_connect(self.path)  # pyright: ignore[reportArgumentType]
        else:
            self.connection = connection
            load_vector_extension(connection)
        self.distance_ceiling = distance_ceiling

        self._edit_entries_fn = _edit_entries.replace(model=model)
        self._rewrite_value_fn = _rewrite_value.replace(model=model)
        self._rewrite_code_fn = _rewrite_code.replace(model=model)
        self._answer_fn = _answer_over_value.replace(model=model)

        self.init_schema()
        self.cache = EmbeddingCache(self.connection, embedder or BedrockCohereEmbedder())  # pyright: ignore[reportArgumentType]
        self._seed_defaults()

    # ── Storage setup ──

    def init_schema(self) -> None:
        """Create the memory tables if absent, then reconcile the FTS index against them.

        Statements are split and run one at a time rather than through
        `executescript`, which issues an implicit `COMMIT` first and would therefore
        commit a borrowed connection's in-flight transaction on the way past. That split is
        also why FTS sync is manual rather than trigger-driven: a `CREATE TRIGGER ... BEGIN
        ...; ...; END` body contains semicolons, so splitting on `;` hands SQLite
        `OperationalError: incomplete input`. Measured, not inferred.

        `reindex_fts` runs on every open, and it is not merely defensive. Two supported
        arrangements produce `memory_entry` rows with no FTS row at all, and both are
        properties this backend advertises: a database written by `TursoMemoryBackend` and
        reopened here has no `memory_entry_fts` table until this call creates it, and a
        `connection=` handle somebody else wrote entries into never went through this
        module's write path. A trigger could not have covered either, since it cannot index
        rows that predate it. It is a no-op — one comparison query, no writes — once the
        index agrees.
        """
        for statement in filter(str.strip, SCHEMA.split(";")):
            self.connection.execute(statement)
        self.connection.commit()
        self.reindex_fts()

    def _seed_defaults(self) -> None:
        """Write each parameter's schema default on first use of this actor.

        Without this a fresh actor's scalar read returns the empty string where the schema
        promised a seed value. Existing rows are never touched, so a reopen preserves what
        was learned rather than reseeding over it.
        """
        defaults = self.schema()
        for name in self._leaf_parameter_names():
            value = _nested_attr(defaults, name)
            if self._is_list_field(name):
                if not self._entry_rows(name):
                    for item in value or []:
                        self.add_entry(name, str(item))
            elif not self._scalar_present(name):
                self._write_scalar(name, "" if value is None else str(value))
        self.connection.commit()

    def close(self) -> None:
        """Commit, and close the connection only when this backend owns it.

        A borrowed connection is left open: its owner is still using it, and closing
        somebody else's handle would break the colocation the `connection=` argument exists
        for.
        """
        self.connection.commit()
        if self._owns_connection:
            self.connection.close()

    # ── Parameter classification ──

    def _is_numeric_field(self, name: str) -> bool:
        """Whether this parameter is an `int` or `float` learned from scores.

        Tolerates `Optional[int]` and `Annotated[...]`, because a schema author writing a
        harness parameter will reasonably use either and misclassifying one routes it to
        the text path, where a model is asked to rewrite a number.
        """
        return _numeric_type(self._resolve_field(name).annotation) is not None

    def _numeric_bounds(self, name: str) -> tuple[float | None, float | None]:
        """Read `Ge`/`Gt`/`Le`/`Lt` from the field's metadata as a search domain.

        Ported from `turso_backend.TursoMemoryBackend._numeric_bounds`, which carries the
        full argument: the schema's own constraints are the only trustworthy domain because
        they are what Pydantic will actually enforce, and `gt`/`lt` are *exclusive*, so an
        exclusive bound is pulled inward by `_EXCLUSIVE_EPSILON` (a whole unit for an `int`,
        where `gt=0` means the smallest legal value is 1 and a fractional epsilon would
        round back onto the forbidden endpoint). Reading them as inclusive was a measured
        bug: `Field(0.5, gt=0.0, lt=1.0)` under repeated zero scores proposed exactly 1.0.

        Identical here because bounds come from the schema, not from the database. Sharing
        the constant rather than the code is the part that matters — two epsilons could
        drift, and the drift would surface as a validation error in one backend only.
        """
        is_integer = _numeric_type(self._resolve_field(name).annotation) is int
        margin = 1.0 if is_integer else _EXCLUSIVE_EPSILON
        low: float | None = None
        high: float | None = None
        for marker in self._resolve_field(name).metadata:
            for attribute, inward in (("ge", 0.0), ("gt", margin)):
                bound = getattr(marker, attribute, None)
                if bound is not None:
                    candidate = float(bound) + inward
                    low = candidate if low is None else max(low, candidate)
            for attribute, inward in (("le", 0.0), ("lt", margin)):
                bound = getattr(marker, attribute, None)
                if bound is not None:
                    candidate = float(bound) - inward
                    high = candidate if high is None else min(high, candidate)
        return low, high

    # ── Entry storage ──

    def _require_list(self, name: str) -> None:
        if not self._is_list_field(name):
            raise TypeError(
                f"Entry operations are only supported for list parameters, but '{name}' is not one."
            )

    def _entry_rows(self, name: str) -> list[tuple[str, str]]:
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT entry_id, value FROM memory_entry WHERE actor_id = ? AND param = ? "
            "ORDER BY position, entry_id",
            (self.actor_id, name),
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    def _entry_exists(self, name: str, entry_id: str) -> bool:
        return (
            fetch_one(
                self.connection,  # pyright: ignore[reportArgumentType]
                "SELECT 1 FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
                (self.actor_id, name, entry_id),
            )
            is not None
        )

    def list_entries(self, name: str) -> dict[str, str]:
        """Return `{entry_id: value}` for a list parameter, in position order."""
        self._require_list(name)
        return dict(self._entry_rows(name))

    def _alloc_id(self, name: str) -> str:
        """Allocate the next entry id: monotonic per parameter, never reused.

        Never reused is what makes a narrow gradient survive a round. An id recorded in the
        forward pass's event log must still name the same logical entry when `consolidate`
        runs, and it will have survived saves, deletes, other consolidations, and a reopen
        by then.

        This read-modify-write is the shape that broke under `pyturso`, whose GC discarded
        the counter increment when the reading cursor died. Not so here — measured over
        eight cycles with a leaked cursor each time — but the counter is still persisted
        rather than derived from `MAX(entry_id)`, because a deleted maximum would hand its
        id straight back out.
        """
        row = fetch_one(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT next_id FROM memory_counter WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        current = int(row[0]) if row is not None else 1
        self.connection.execute(
            "INSERT OR REPLACE INTO memory_counter (actor_id, param, next_id) VALUES (?, ?, ?)",
            (self.actor_id, name, current + 1),
        )
        return str(current)

    def _next_position(self, name: str) -> int:
        row = fetch_one(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT COALESCE(MAX(position), -1) FROM memory_entry WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        return (int(row[0]) if row is not None else -1) + 1

    # ── Lexical index sync ──
    #
    # Every write to `memory_entry` is paired with the matching write to
    # `memory_entry_fts` *before* the `commit()` that publishes it, so the two are
    # never both visible and disagreeing. Manual rather than trigger-driven for the
    # reasons `init_schema` and the module docstring give; `fts_drift` is the check
    # that the pairing was not missed anywhere, and it is asserted after every write
    # path in the test suite rather than argued here.

    def _fts_delete(self, name: str, entry_id: str) -> None:
        """Drop one entry's row from the lexical index. Uncommitted."""
        self.connection.execute(
            "DELETE FROM memory_entry_fts WHERE actor_id = ? AND param = ? AND entry_id = ?",
            (self.actor_id, name, entry_id),
        )

    def _fts_insert(self, name: str, entry_id: str, value: str) -> None:
        """Add one entry's row to the lexical index. Uncommitted."""
        self.connection.execute(
            "INSERT INTO memory_entry_fts (value, actor_id, param, entry_id) VALUES (?, ?, ?, ?)",
            (value, self.actor_id, name, entry_id),
        )

    def _fts_replace(self, name: str, entry_id: str, value: str) -> None:
        """Re-index one entry: delete then insert, because FTS5 has no upsert on a join key.

        Delete-then-insert rather than `UPDATE ... SET value = ?`, which FTS5 does support.
        The reason is that the delete is unconditional, so an entry that somehow acquired two
        index rows converges to one instead of updating both and staying doubled — and a
        doubled row would score the entry twice in `bm25()`, promoting it for no reason a
        reader could see.
        """
        self._fts_delete(name, entry_id)
        self._fts_insert(name, entry_id, value)

    def _fts_delete_param(self, name: str) -> None:
        """Drop every index row for one parameter, for a wholesale replace. Uncommitted."""
        self.connection.execute(
            "DELETE FROM memory_entry_fts WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )

    def fts_drift(self) -> list[tuple[str, str, str]]:
        """Rows where `memory_entry` and `memory_entry_fts` disagree, as `(param, id, why)`.

        The lexical index is a second copy of the entry text, and the design doc rejects a
        `vec0` vector table partly *because* it would duplicate state that can diverge. This
        duplication is accepted on one condition: that the divergence is countable, in the
        same spirit as `unranked_entries` and `degenerate_entries`. Three ways they can
        disagree, and they need different fixes, so they are reported separately:

            `missing`   — an entry with no index row, so it is invisible to the lexical
                          channel while ranking normally on the vector one. This is what a
                          Turso-written database looks like before `reindex_fts`.
            `stale`     — an index row whose text is not the entry's current text. The
                          serious one: the entry still matches, on the *old* words, so
                          retrieval keeps working and keeps being wrong. A rewritten entry
                          that skipped `_fts_replace` looks exactly like this.
            `orphaned`  — an index row for an entry that no longer exists. It contributes a
                          lexical hit whose join to `memory_entry` finds nothing, so it
                          silently shrinks the candidate set.

        Should always be empty. It is a method rather than an assertion so a caller who
        borrowed the connection and wrote entries through it can find out.
        """
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.param, e.entry_id, 'missing' FROM memory_entry e "
            "LEFT JOIN memory_entry_fts f "
            "  ON f.actor_id = e.actor_id AND f.param = e.param AND f.entry_id = e.entry_id "
            "WHERE e.actor_id = ? AND f.entry_id IS NULL "
            "UNION ALL "
            "SELECT e.param, e.entry_id, 'stale' FROM memory_entry e "
            "JOIN memory_entry_fts f "
            "  ON f.actor_id = e.actor_id AND f.param = e.param AND f.entry_id = e.entry_id "
            "WHERE e.actor_id = ? AND f.value <> e.value "
            "UNION ALL "
            "SELECT f.param, f.entry_id, 'orphaned' FROM memory_entry_fts f "
            "LEFT JOIN memory_entry e "
            "  ON e.actor_id = f.actor_id AND e.param = f.param AND e.entry_id = f.entry_id "
            "WHERE f.actor_id = ? AND e.entry_id IS NULL "
            "ORDER BY 1, 2, 3",
            (self.actor_id, self.actor_id, self.actor_id),
        )
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    def reindex_fts(self) -> int:
        """Rebuild this actor's lexical index from `memory_entry`; return rows reconciled.

        `memory_entry` is the single source of truth and the index is derived, so a
        disagreement is repaired in one direction only — there is no case where the index is
        right and the entry is wrong. Called by `init_schema` on every open, which is what
        makes the two arrangements that cannot be trigger-covered work: a database written by
        `TursoMemoryBackend` (no FTS table existed) and a borrowed connection somebody else
        wrote entries into.

        Scoped to this actor, and deliberately not to the whole file. Several actors share
        one database, and reindexing another actor's entries from this backend would be a
        write outside the namespace every other statement in this module confines itself to.
        A rebuild that reached across actors would also make two backends opening the same
        file at once each undo the other's in-flight state.

        Returns 0 when the index already agrees, which is the normal case and costs one
        comparison query.
        """
        drift = self.fts_drift()
        if not drift:
            return 0
        for param, entry_id, _ in drift:
            self._fts_delete(param, entry_id)
        for param, entry_id, _ in drift:
            row = fetch_one(
                self.connection,  # pyright: ignore[reportArgumentType]
                "SELECT value FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
                (self.actor_id, param, entry_id),
            )
            if row is not None:
                self._fts_insert(param, entry_id, str(row[0]))
        self.connection.commit()
        return len(drift)

    def add_entry(self, name: str, value: str) -> str:
        """Append an entry and return its stable id."""
        self._require_list(name)
        entry_id = self._alloc_id(name)
        self.connection.execute(
            "INSERT INTO memory_entry "
            "(actor_id, param, entry_id, position, value, digest, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.actor_id,
                name,
                entry_id,
                self._next_position(name),
                value,
                digest_of(value),
                time.time(),
            ),
        )
        self._fts_replace(name, entry_id, value)
        self.connection.commit()
        return entry_id

    def update_entry(self, name: str, entry_id: str, value: str) -> bool:
        """Replace an entry's text, keeping its id. False when the id is unknown.

        The digest moves with the text, which is what makes the embedding cache
        self-invalidating: the rewritten entry no longer matches any cached vector, so the
        next search re-embeds it. A cache keyed by entry id would serve the pre-rewrite
        vector for post-rewrite text, and the mistake would be invisible because a vector
        search always returns something ranked.

        The lexical index gets the same treatment for the same reason, and this is the write
        path a consolidation rewrite takes. Content addressing handles the vector side
        *structurally* — a new digest cannot hit an old cache row — but the FTS index is keyed
        by entry id, so nothing about its layout makes staleness impossible and
        `_fts_replace` has to be called. Skipping it leaves the entry matching on its
        pre-rewrite words: retrieval keeps working, keeps returning the entry, and returns it
        for the wrong reasons. `fts_drift` reports that as `stale`.
        """
        self._require_list(name)
        if not self._entry_exists(name, entry_id):
            return False
        self.connection.execute(
            "UPDATE memory_entry SET value = ?, digest = ?, updated_at = ? "
            "WHERE actor_id = ? AND param = ? AND entry_id = ?",
            (value, digest_of(value), time.time(), self.actor_id, name, entry_id),
        )
        self._fts_replace(name, entry_id, value)
        self.connection.commit()
        return True

    def remove_entry(self, name: str, entry_id: str) -> bool:
        """Delete an entry by id. The id is retired, never reused.

        The index row goes with it. Left behind it would be an `orphaned` row: a lexical hit
        whose join to `memory_entry` finds nothing, so the fused candidate set shrinks with
        no signal that it did.
        """
        self._require_list(name)
        if not self._entry_exists(name, entry_id):
            return False
        self.connection.execute(
            "DELETE FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
            (self.actor_id, name, entry_id),
        )
        self._fts_delete(name, entry_id)
        self.connection.commit()
        return True

    # ── Retrieval: two channels, fused ──

    def embed_pending(self, name: str) -> int:
        """Embed every entry of `name` that has no cached vector; return the count.

        Idempotent, and the only place entry text reaches the provider. Called by
        `search_entries`, but exposed so a caller can pay the embedding cost up front
        rather than inside a decision loop.
        """
        self._require_list(name)
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.value FROM memory_entry e "
            "LEFT JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND c.digest IS NULL",
            (DOCUMENT, self.cache.embedder.model_id, self.actor_id, name),
        )
        pending = sorted({str(row[0]) for row in rows})
        if pending:
            self.cache.ensure(pending, DOCUMENT)
        return len(pending)

    def search_entries(self, name: str, query: str, k: int = 5) -> list[Retrieved]:
        """Return the top-k entries by `vec_distance_cosine ASC`. The vector channel alone.

        Kept pure-vector deliberately, even though `_search` is hybrid by default. Three
        callers need a single-metric ranking and would be made unsound by a fused one:
        `probe_retrieval`, whose whole verdict is a margin between two *distance*
        distributions; `calibrate_ceiling`, which derives a distance threshold from that
        margin; and `EntryToolProvider`, imported from `turso_backend` and shared with a
        backend that has no lexical channel at all. `hybrid_entries` is the fused path, and
        `_search` routes through it.

        Ranking happens in the database over a JOIN against the embedding cache, so no
        corpus is materialized in Python. It is an ordinary table scan with an ORDER BY,
        not a `vec0` KNN index; at the entry counts a playbook holds an exact scan is both
        simpler and honest about what it computes, and `docs/design/sqlite_backend.md`
        argues the rejection.

        Two filters that Turso's query does not need, and both are load-bearing:

        The JOIN is *inner* because `vec_distance_cosine` raises on a NULL blob rather than
        returning NULL, so an entry with no cached vector cannot be scored. Same as Turso.
        `unranked_entries` counts what the join drops.

        `AND distance IS NOT NULL` is new here. `vec_distance_cosine` returns NULL — not an
        error, not 1.0 as Turso does — for a zero-magnitude vector, and SQLite orders NULL
        *first* under ASC. Without the filter a degenerate vector is returned as the
        nearest hit; `float(None)` then raises `TypeError` in the row mapper below, which
        is luck rather than design. `degenerate_entries` counts what this drops.

        A query whose own vector is degenerate raises instead of returning `[]`, because an
        empty result reads as "the corpus has nothing relevant" when the truth is "nothing
        could be ranked at all" — the failing-soft confusion this whole backend is designed
        against.

        `distance_ceiling`, when set, drops hits beyond it, including all of them. An
        honest empty result is the point: an agent handed the best of a bad set cannot tell
        it apart from good advice.
        """
        self._require_list(name)
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        if not self._entry_rows(name):
            return []

        self.embed_pending(name)
        query_vector = self.cache.vector(query, QUERY)
        self._require_rankable_query(query, query_vector)

        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.entry_id, e.value, vec_distance_cosine(c.embedding, ?) AS distance "
            "FROM memory_entry e "
            "JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND distance IS NOT NULL "
            "ORDER BY distance ASC LIMIT ?",
            (query_vector, DOCUMENT, self.cache.embedder.model_id, self.actor_id, name, k),
        )
        hits = [
            Retrieved(entry_id=str(row[0]), value=str(row[1]), distance=float(row[2]))
            for row in rows
        ]
        if self.distance_ceiling is not None:
            hits = [h for h in hits if h.distance <= self.distance_ceiling]
        return hits

    def _require_rankable_query(self, query: str, vector: bytes) -> None:
        """Refuse a query vector `vec_distance_cosine` cannot score against anything.

        The test is the vector's distance to itself: NULL means zero magnitude, since
        cosine is undefined without a direction. Raising here rather than returning `[]`
        keeps "no relevant entries" and "no ranking was possible" distinguishable, which is
        the distinction the `Discrimination` guard exists to protect.

        Still a refusal on this path, and that is not an inconsistency with
        `hybrid_entries`, which degrades to the lexical channel in the same situation.
        `search_entries` is single-channel by contract — `probe_retrieval` and
        `calibrate_ceiling` need it to be — so here there genuinely is no ranking left, and
        the distinction the raise protects is genuinely at risk.
        """
        if not self._query_vector_is_rankable(vector):
            raise ValueError(
                f"The embedding for query {query[:60]!r} has zero magnitude, so "
                "vec_distance_cosine cannot rank anything against it and every distance "
                f"would be NULL. Embedder {self.cache.embedder.model_id!r} returned a "
                "degenerate vector; an empty result list would have hidden that."
            )

    def fts_entries(self, name: str, query: str, k: int = 5) -> list[tuple[str, str, float]]:
        """Return the top-k entries by `bm25()` ASC, as `(entry_id, value, bm25_score)`.

        The lexical channel, and the capability that has no Turso counterpart: `bm25()`
        returns real, distinct, order-bearing scores where Turso's `fts_score()` returns 0.0
        for every matching row. Measured on this build over a three-entry corpus, one query
        scored -1.4506, -1.07e-06, and -9.86e-07 — three ranks, and a range spanning six
        orders of magnitude, which is the concrete reason `reciprocal_rank_fusion` reads ranks
        and not scores.

        **`bm25()` is negative and ASCending is best-first**, which is the opposite sign
        convention from most BM25 implementations and is easy to get backwards. SQLite's FTS5
        negates the score specifically so that `ORDER BY rank` (an alias for `bm25()` on the
        table) is best-first, matching every other `ORDER BY ... ASC` in this module.
        `test_bm25_is_negative_so_ascending_is_best_first` pins it as a property of the
        database, so a future FTS5 that flips the sign fails there rather than silently
        inverting the lexical channel — which would fail soft, since an inverted ranking still
        returns k plausible entries.

        Three shapes in the SQL that were established by probing and each of which fails
        differently:

        The MATCH target is the **unaliased table name**. `FROM memory_entry_fts f ... WHERE f
        MATCH ?` raises `OperationalError: no such column: f`, and so does `bm25(f)`; both the
        match operand and the `bm25()` argument must be the literal table name even when the
        rest of the query is aliased.

        `bm25(memory_entry_fts)` is aliased to `bm25_score` and the ORDER BY names the alias,
        not a second call. FTS5 auxiliary functions are only valid in a full-text query, and
        repeating the call is a second evaluation of a nontrivial function per row.

        The **actor and parameter filters sit on the FTS table**, not only on the joined
        entry table. They are the same rows either way, but filtering the virtual table lets
        FTS5 drop non-matching rows before the join instead of after — and, more importantly,
        makes the namespace confinement visible in the same clause as the MATCH.

        Returns `[]` for a query with no indexable token, and for one whose tokens are all
        absent from the corpus. Those are *both* honest lexical misses and the caller treats
        them identically; what neither of them is, is a crash — see `fts_match_expression`
        for the five MATCH inputs that used to be one.
        """
        self._require_list(name)
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        expression = fts_match_expression(query)
        if not expression:
            return []
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.entry_id, e.value, bm25(memory_entry_fts) AS bm25_score "
            "FROM memory_entry_fts "
            "JOIN memory_entry e "
            "  ON e.actor_id = memory_entry_fts.actor_id "
            "  AND e.param = memory_entry_fts.param "
            "  AND e.entry_id = memory_entry_fts.entry_id "
            "WHERE memory_entry_fts MATCH ? "
            "  AND memory_entry_fts.actor_id = ? AND memory_entry_fts.param = ? "
            "ORDER BY bm25_score ASC, e.entry_id ASC LIMIT ?",
            (expression, self.actor_id, name, k),
        )
        return [(str(row[0]), str(row[1]), float(row[2])) for row in rows]

    def hybrid_entries(
        self, name: str, query: str, k: int = 5, overfetch: int = 3
    ) -> tuple[list[Retrieved], list[str]]:
        """Fuse the vector and lexical channels by RRF; return `(hits, channels_used)`.

        The retrieval `_search` uses, and it strictly dominates the vector channel it
        replaces: the embeddings are identical, the vector candidate list is over-fetched
        rather than truncated, and the lexical channel can only add candidates a cosine
        ranking missed. An exact-term match that the embedder happens to place far away — a
        rare identifier, a case number, a spelled-out rule name — is the case a bag-of-vectors
        model reliably loses and BM25 reliably wins.

        ## Fusion

        Both channels are fetched at `overfetch * k` (bounded below by `k`) and fused by
        `reciprocal_rank_fusion`, which reads rank positions only. Over-fetching is what makes
        the fusion mean anything: at exactly `k` per channel, an entry ranked k+1 by vector
        and 1 by BM25 could never be promoted, so the fusion would only ever reorder what the
        vector channel already returned. Three is a shape choice with the same character as
        `RRF_K` — larger costs a longer candidate list and cannot change what the fusion
        *means*.

        ## Deterministic tiebreaking, and why it needs saying

        RRF ties are not an edge case, they are the common case. Two entries at the same rank
        in two single-channel lists tie exactly, and with one channel running *every* pair of
        adjacent hits has a distinct score only because the ranks differ — so any fallback to
        dict insertion order decides real winners. `detect/objective.py` carries the measured
        version of this defect: on a 21^3 metric grid, 21 points tied for smallest
        `edge_share` while scoring anywhere from 0.0 to 0.9744, and which one became "the
        answer" was decided by `itertools.product`'s iteration order. Its fix and this one are
        the same fix — tiebreak on a *measured* quantity, then on a stable identifier:

            (-rrf_score, bm25_score, distance, entry_id)

        `bm25_score` first among the tiebreakers because it is the requested rerank and it is
        the quantity RRF deliberately discarded, so it is exactly the information available to
        break a rank tie. `distance` next, for a tie among entries the lexical channel did not
        score. `entry_id` last, so the order is total: with it, the same corpus and query
        return the same list on every run and in every process, which is what makes a
        retrieval assertion in a test a claim about ranking rather than about a hash seed.
        Entries missing from a channel sort last within it (`+inf`), never first.

        ## Honest degradation

        `channels` names which rankings actually produced the hits, and it travels into
        `_search`'s meta so a reader auditing an event log can tell a fused result from a
        fallback. Four cases:

            `["vector", "fts"]`  both ran and both matched — the normal case.
            `["vector"]`         no lexical hit (every query term absent, or the query has no
                                 indexable token). Pure vector, unchanged behaviour.
            `["fts"]`            the query embedding is degenerate but terms matched. This is
                                 a **behaviour fix**: `search_entries` raises here, and
                                 rightly, because it has only the one channel. With a working
                                 lexical ranking, refusing would discard a real result to
                                 protect a distinction that is no longer at risk.
            raise                both channels are unavailable. Only then, and the message
                                 names both reasons, because `[]` here would read as "this
                                 corpus has nothing relevant" when the truth is "nothing could
                                 be ranked at all" — the failing-soft collapse the whole
                                 backend is designed against.

        `distance_ceiling` is applied to the vector channel and to nothing else. BM25 has no
        ceiling and cannot be given one by analogy: the scores are unbounded, negative, and
        move with corpus size and term frequency, so there is no value a caller could
        calibrate and no `calibrate_ceiling` that could derive one. A hit the lexical channel
        found and the vector channel placed beyond the ceiling therefore survives — correctly,
        since the ceiling is a statement about *cosine* confidence and says nothing about an
        exact term match. Both facts are in the result meta so the asymmetry is auditable
        rather than surprising.

        Returns:
            `(hits, channels)`. `hits` are `Retrieved`, so a caller cannot tell a fused
            result from a vector one by type. `distance` is the cosine distance where the
            vector channel scored the entry, and `inf` for a lexical-only hit — an honest
            "this metric did not rank it" rather than a fabricated number, and readable as
            such by the `distance_ceiling` comparison, which `inf` fails.

        Raises:
            ValueError: `k < 1`, or neither channel could rank.
        """
        self._require_list(name)
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        if overfetch < 1:
            raise ValueError(f"overfetch must be >= 1, got {overfetch}")
        if not self._entry_rows(name):
            return [], []

        candidates = max(k * overfetch, k)

        lexical = self.fts_entries(name, query, k=candidates)
        lexical_ranking = [entry_id for entry_id, _, _ in lexical]
        bm25_scores = {entry_id: score for entry_id, _, score in lexical}

        self.embed_pending(name)
        query_vector = self.cache.vector(query, QUERY)
        vector_rankable = self._query_vector_is_rankable(query_vector)
        vector: list[Retrieved] = []
        if vector_rankable:
            vector = self._vector_channel(name, query_vector, candidates)
        elif not lexical:
            # Both channels are out, and only now is a raise the honest answer. Naming both
            # reasons because a caller seeing only the embedding half would re-embed and try
            # again against a corpus that also had no lexical hit.
            raise ValueError(
                f"Neither channel can rank query {query[:60]!r} for '{name}': the embedding "
                f"from {self.cache.embedder.model_id!r} has zero magnitude, so every "
                "vec_distance_cosine is NULL, and no query term appears in the FTS index "
                "either. An empty result list would have read as an irrelevant corpus."
            )

        distances = {hit.entry_id: hit.distance for hit in vector}
        values = {hit.entry_id: hit.value for hit in vector}
        values.update({entry_id: value for entry_id, value, _ in lexical})

        fused = reciprocal_rank_fusion([[hit.entry_id for hit in vector], lexical_ranking], k=RRF_K)
        ordered = sorted(
            fused,
            key=lambda entry_id: (
                -fused[entry_id],
                bm25_scores.get(entry_id, float("inf")),
                distances.get(entry_id, float("inf")),
                entry_id,
            ),
        )

        hits = [
            Retrieved(
                entry_id=entry_id,
                value=values[entry_id],
                distance=distances.get(entry_id, float("inf")),
            )
            for entry_id in ordered
        ]
        if self.distance_ceiling is not None:
            # Vector-channel-only, as the docstring argues. A lexical-only hit carries
            # `distance=inf`, so it must be exempted explicitly rather than compared: `inf <=
            # ceiling` is False, and letting the comparison stand would silently make the
            # ceiling drop every hit BM25 found alone.
            hits = [
                hit
                for hit in hits
                if hit.entry_id not in distances or hit.distance <= self.distance_ceiling
            ]

        channels = [
            *(["vector"] if vector else []),
            *(["fts"] if lexical_ranking else []),
        ]
        return hits[:k], channels

    def _vector_channel(self, name: str, query_vector: bytes, k: int) -> list[Retrieved]:
        """The vector half of `hybrid_entries`, with no ceiling applied.

        Factored out of `search_entries` rather than called through it, for one reason that
        matters: `search_entries` applies `distance_ceiling` to its own result, and applying
        it here would drop candidates *before* fusion. A hit the ceiling excludes on cosine
        grounds may still be the top lexical hit, and pre-filtering it would make the fused
        ranking depend on the ceiling in a way `hybrid_entries`'s "vector channel only"
        promise denies. The ceiling is applied once, at the end, to the fused list.
        """
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.entry_id, e.value, vec_distance_cosine(c.embedding, ?) AS distance "
            "FROM memory_entry e "
            "JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND distance IS NOT NULL "
            "ORDER BY distance ASC, e.entry_id ASC LIMIT ?",
            (query_vector, DOCUMENT, self.cache.embedder.model_id, self.actor_id, name, k),
        )
        return [
            Retrieved(entry_id=str(row[0]), value=str(row[1]), distance=float(row[2]))
            for row in rows
        ]

    def _query_vector_is_rankable(self, vector: bytes) -> bool:
        """Whether `vec_distance_cosine` can score anything against this query vector.

        The predicate half of `_require_rankable_query`, split out because `hybrid_entries`
        needs to *branch* on the answer rather than refuse on it. Same oracle, stated once:
        the vector's distance to itself is NULL exactly when its magnitude is zero.
        """
        row = fetch_one(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT vec_distance_cosine(?, ?)",
            (vector, vector),
        )
        return row is not None and row[0] is not None

    def unranked_entries(self, name: str) -> list[str]:
        """Entry ids with no cached vector, which the inner join silently cannot return.

        Should always be empty after `embed_pending`. It exists because the alternative to
        counting this is a join quietly shrinking the candidate set, which reads exactly
        like a corpus that never had the entry in it.
        """
        self._require_list(name)
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.entry_id FROM memory_entry e "
            "LEFT JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND c.digest IS NULL",
            (DOCUMENT, self.cache.embedder.model_id, self.actor_id, name),
        )
        return [str(row[0]) for row in rows]

    def degenerate_entries(self, name: str) -> list[str]:
        """Entry ids whose cached vector has zero magnitude, so it cannot be ranked.

        The `sqlite-vec` half of `unranked_entries`, and the reason it is a separate
        method: an entry here *has* a vector and still cannot be scored, so it is invisible
        to the missing-vector check while being just as absent from every result. Both
        counts are exposed because a search that silently returns fewer entries than the
        corpus holds is the shape of this backend's characteristic failure.
        """
        self._require_list(name)
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT e.entry_id FROM memory_entry e "
            "JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? "
            "  AND vec_distance_cosine(c.embedding, c.embedding) IS NULL",
            (DOCUMENT, self.cache.embedder.model_id, self.actor_id, name),
        )
        return [str(row[0]) for row in rows]

    # ── Retrieval discrimination ──

    def probe_retrieval(
        self,
        name: str,
        relevant: Sequence[tuple[str, str]],
        controls: Sequence[str] = (),
        k: int = 3,
    ) -> Discrimination:
        """Measure whether retrieval separates answerable queries from unrelated ones.

        The guard against this backend's characteristic failure, and the same measurement
        `TursoMemoryBackend.probe_retrieval` makes — it returns the same `Discrimination`
        class, so a caller cannot tell which backend produced a verdict. Three parts:
        relevant `(query, expected_entry_id)` probes counted at top-1 and within the top k;
        self-retrieval, where an entry's own text failing to retrieve it is an index defect
        and sets `discriminates` False outright; and control queries this corpus does not
        answer, whose distances are the null distribution the relevant ones must beat.
        Without controls there is no separation and the verdict is `None`, because a
        measurement that cannot fail is not a measurement.

        Ignores `distance_ceiling` throughout: the ceiling is derived from this measurement,
        so applying it here would be circular.

        **Measures the vector channel, not the hybrid one**, and that is a soundness
        requirement rather than an oversight. Every quantity in the verdict is a cosine
        distance: `separation` is a difference of two distance means, `worst_relevant` and
        `best_control` are distance extremes, and `calibrate_ceiling` turns them into a
        distance threshold. A fused ranking's top hit can be a lexical-only hit carrying
        `distance=inf`, which would put an infinity into every one of those and make the
        verdict meaningless — and worse, would make it *look* decisive. So this calls
        `search_entries`. The consequence is worth stating plainly: a `discriminates` verdict
        here is a claim about the embedding, and a corpus whose embedding does not
        discriminate may still retrieve well through `_search`. That is not the guard being
        wrong; the failure it exists to catch is a *useless embedding*, and the lexical
        channel does not make a useless embedding useful.
        """
        self._require_list(name)
        ceiling, self.distance_ceiling = self.distance_ceiling, None
        try:
            probes: list[tuple[str, str, str, float]] = []
            hits = recalled = 0
            for query, expected in relevant:
                found = self.search_entries(name, query, k=k)
                if not found:
                    probes.append((query, expected, "", float("inf")))
                    continue
                ids = [h.entry_id for h in found]
                hits += ids[0] == expected
                recalled += expected in ids
                probes.append((query, expected, ids[0], found[0].distance))

            control_rows: list[tuple[str, str, float]] = []
            for query in controls:
                found = self.search_entries(name, query, k=1)
                if found:
                    control_rows.append((query, found[0].entry_id, found[0].distance))

            failures = [
                entry_id
                for entry_id, value in self._entry_rows(name)
                if (own := self.search_entries(name, value, k=1)) and own[0].entry_id != entry_id
            ]
        finally:
            self.distance_ceiling = ceiling

        return Discrimination(
            relevant=tuple(probes),
            controls=tuple(control_rows),
            hits=hits,
            recalled=recalled,
            self_retrieval_failures=tuple(failures),
        )

    def calibrate_ceiling(
        self,
        name: str,
        relevant: Sequence[tuple[str, str]],
        controls: Sequence[str],
        k: int = 3,
        margin: float = 0.5,
    ) -> float:
        """Derive a `distance_ceiling` from measurement, or refuse.

        The ceiling sits between the furthest relevant hit and the closest control hit, at
        `margin` of the way across the gap. When those distributions overlap there is no
        such point, and this raises rather than returning a midpoint that would either drop
        real hits or admit unrelated ones. A threshold nobody measured is a silent cap.

        Note what is *not* transferable between backends: a value calibrated against Turso
        cannot be copied here. Both metrics are cosine distance on the same blobs, but the
        ceiling is a property of the corpus and the embedder, not of the SQL function, and
        `vec_distance_cosine` and `vector_distance_cos` do not agree to the last bit —
        measured, an identical float32 pair is 0.0 here and 4.47e-08 there.

        Derived from, and applicable to, the **vector channel only** — inherited from
        `probe_retrieval`, which explains why. There is deliberately no BM25 counterpart: bm25
        scores are unbounded, negative, and move with corpus size and term frequency, so
        "furthest relevant" and "closest control" are not comparable across two queries let
        alone across two corpora, and any number derived from them would be the fitted
        constant this whole retrieval path refuses. `hybrid_entries` therefore lets a
        lexical-only hit past the ceiling, which is the honest reading: the ceiling is a
        statement about cosine confidence and says nothing about an exact term match.

        Raises:
            CeilingNotSeparable: The distributions overlap, or one side was not
                measured, so no threshold is justified.
        """
        if not 0.0 <= margin <= 1.0:
            raise ValueError(f"margin must be in [0, 1], got {margin}")
        report = self.probe_retrieval(name, relevant, controls, k=k)
        worst, best = report.worst_relevant, report.best_control
        if worst is None or best is None:
            raise CeilingNotSeparable(
                "Calibration needs both relevant probes and control queries; "
                f"got {len(report.relevant)} and {len(report.controls)}."
            )
        if worst >= best:
            raise CeilingNotSeparable(
                f"No threshold separates relevant from unrelated for '{name}': the furthest "
                f"relevant hit is {worst:.4f} and the closest unrelated hit is {best:.4f}. "
                "Any ceiling either drops real hits or admits noise. Split entries that "
                "bundle several points, or add control queries closer to the domain."
            )
        return worst + margin * (best - worst)

    # ── Numeric parameters, learned from scores ──

    def numeric_value(self, name: str) -> float:
        """Current value of a numeric parameter, as a float."""
        if not self._is_numeric_field(name):
            raise TypeError(f"'{name}' is not a numeric parameter.")
        return float(self._read_scalar(name) or 0.0)

    def observations(self, name: str) -> list[tuple[float, float, str]]:
        """Every `(value, score, rationale)` recorded for a numeric parameter.

        The search's memory, and the audit trail for why a harness parameter holds the
        value it does. Persisted, so a reopened database resumes the search instead of
        restarting it.
        """
        rows = fetch_rows(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT value, score, rationale FROM memory_score_observation "
            "WHERE actor_id = ? AND param = ? ORDER BY seq",
            (self.actor_id, name),
        )
        return [(float(r[0]), float(r[1]), str(r[2])) for r in rows]

    def _record_observation(self, name: str, value: float, score: float, rationale: str) -> None:
        row = fetch_one(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM memory_score_observation "
            "WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        self.connection.execute(
            "INSERT INTO memory_score_observation "
            "(actor_id, param, seq, value, score, rationale, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.actor_id,
                name,
                int(row[0]) if row is not None else 1,
                value,
                score,
                rationale,
                time.time(),
            ),
        )

    def _numeric_update(self, name: str, current: float, score: float) -> float:
        """Propose the next value of a numeric parameter from the scores so far.

        Ported verbatim in behaviour from `turso_backend.TursoMemoryBackend._numeric_update`
        — the origin, and where the full argument for each factor lives. It is
        backend-independent by construction: the only thing it reads from storage is
        `observations`, and the only thing it writes is a return value. Both backends must
        propose the *same* next value from the same history, so the shared test contract
        asserts the numeric properties against both rather than trusting the copy.

        In short: a deterministic one-dimensional trust-region search over the domain the
        schema declares. Exploit by bisecting toward a previously tried value that scored
        better; otherwise explore by `span * _TRUST_FRACTION * (1 - score) *
        _EXPLORE_DECAY ** trials`. `1 - score` leaves a well-served value nearly still,
        `_EXPLORE_DECAY ** trials` converges instead of oscillating, and `_TRUST_FRACTION`
        caps the first step — omitting it was a measured bug where `Field(20, ge=1, le=100)`
        scoring 0.2 proposed 99, which is a jump to the boundary rather than a search.

        Rounding happens before clamping, not after: rounding an already-clamped value can
        step back outside the bound (0.5 rounds to 1.0 under `lt=1.0`), and a proposal
        outside its declared domain fails validation somewhere the cause is invisible.
        """
        low, high = self._numeric_bounds(name)
        span = (high - low) if (low is not None and high is not None) else max(abs(current), 1.0)

        history = self.observations(name)
        means: dict[float, list[float]] = {}
        for value, observed, _ in history:
            means.setdefault(value, []).append(observed)
        averaged = {v: sum(s) / len(s) for v, s in means.items()}

        best_value = max(averaged, key=lambda v: (averaged[v], -abs(v - current)))
        if averaged[best_value] > score and best_value != current:
            proposal = current + 0.5 * (best_value - current)
        else:
            step = (
                span
                * _TRUST_FRACTION
                * (1.0 - score)
                * (_EXPLORE_DECAY ** max(len(history) - 1, 0))
            )
            worst_value = min(averaged, key=lambda v: (averaged[v], abs(v - current)))
            direction = 1.0 if worst_value == current else (1.0 if current > worst_value else -1.0)
            proposal = current + direction * step

        if _numeric_type(self._resolve_field(name).annotation) is int:
            proposal = float(round(proposal))
        if low is not None:
            proposal = max(proposal, low)
        if high is not None:
            proposal = min(proposal, high)
        return proposal

    # ── Scalar storage ──

    def _scalar_present(self, name: str) -> bool:
        return (
            fetch_one(
                self.connection,  # pyright: ignore[reportArgumentType]
                "SELECT 1 FROM memory_scalar WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            is not None
        )

    def _read_scalar(self, name: str) -> str:
        row = fetch_one(
            self.connection,  # pyright: ignore[reportArgumentType]
            "SELECT value FROM memory_scalar WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        return str(row[0]) if row is not None else ""

    def _write_scalar(self, name: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO memory_scalar (actor_id, param, value, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (self.actor_id, name, value, time.time()),
        )

    # ── Abstract storage contract ──

    def _save(self, name: str, value: Any) -> None:  # pyright: ignore[reportExplicitAny]
        if self._is_list_field(name):
            # A wholesale replace retires the old entries. The counter is monotonic, so the
            # retired ids are never handed out again and a stale id from an earlier forward
            # pass resolves to nothing rather than to somebody else's entry.
            #
            # The lexical index is cleared for the same parameter in the same breath, before
            # any re-add. Deleting the entries alone would leave every old row `orphaned`,
            # and since ids are never reused those rows would never be overwritten by a
            # later insert either — they would accumulate across saves, each one a lexical
            # hit that joins to nothing.
            self.connection.execute(
                "DELETE FROM memory_entry WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            self._fts_delete_param(name)
            for item in value or []:
                self.add_entry(name, str(item))
        else:
            if self._is_procedural(name):
                ast.parse(str(value))
            self._write_scalar(name, "" if value is None else str(value))
        self.connection.commit()

    def _recall(self, name: str) -> tuple[Any, ParameterMeta]:  # pyright: ignore[reportExplicitAny]
        """Return a parameter's full value.

        The list case returns every entry, which is a full recall by definition. Note what
        it does *not* return: per-entry ids in the meta. A full recall's gradient is about
        the whole parameter, so handing consolidation a retrieval context here would claim
        a narrowness the forward pass did not have.
        """
        if self._is_list_field(name):
            return [value for _, value in self._entry_rows(name)], {}
        raw = self._read_scalar(name)
        if self._is_numeric_field(name):
            numeric = _numeric_type(self._resolve_field(name).annotation)
            return (numeric(float(raw or 0.0)) if numeric else raw), {}
        return raw, {}

    def _query(self, name: str, query: str) -> tuple[str, ParameterMeta]:
        value, _ = self._recall(name)
        content = "\n".join(f"- {v}" for v in value) if isinstance(value, list) else str(value)
        return self._answer_fn.run_sync(value=content, query=query), {}

    def _search(
        self,
        name: str,
        query: str,
        k: int = 5,
        **kwargs: Any,  # pyright: ignore[reportExplicitAny]
    ) -> tuple[list[str], ParameterMeta]:
        """Return the top-k entry texts by fused hybrid ranking, with the ids in
        `meta["results"]`.

        `meta["results"]` is the whole mechanism for narrow gradients: it travels into the
        recall event, onto the reconstructed `ParameterNode`, and back out as
        `consolidate`'s `retrieved=`, so consolidation edits exactly the entries this
        forward pass read. `distances` rides along so a caller can audit retrieval quality
        from the event log alone, after the fact, without re-running anything. Both are
        unchanged in shape by the hybrid path — the gradient chain depends on them and the
        contract is upstream's, not this file's.

        `distance_metric` is recorded alongside the embedding model because the numbers are
        only comparable within one SQL function. A log holding rows from both backends
        would otherwise invite a threshold derived under one to be applied to the other. The
        hybrid path adds three more fields for the same auditability reason, and they matter
        most when read *after* the fact:

            `channels`      which rankings produced these hits: `["vector", "fts"]`,
                            `["vector"]`, or `["fts"]`. Without it a fallback is invisible,
                            and a fallback is the one case where the hits mean something
                            different from what the caller asked for.
            `rrf_k`         the fusion constant these ranks were fused under, so a log row
                            is interpretable if the default ever moves.
            `lexical_metric` names `bm25`, in the same spirit as `distance_metric` and for a
                            sharper reason: it is the field that says this row came from a
                            backend whose engine can rank lexically at all. A `TursoMemory
                            Backend` row never carries it, because `fts_score()` cannot.

        `distances` holds `inf` for a hit only the lexical channel scored. That is deliberate
        and it is why the value is not rounded blindly: `inf` is an honest "cosine did not
        rank this" and round-trips through JSON as `Infinity`, where a substituted 0.0 would
        claim a perfect vector match and a substituted 2.0 would claim a worst-case one.
        """
        del kwargs
        hits, channels = self.hybrid_entries(name, query, k=k)
        return [h.value for h in hits], {
            "results": {h.entry_id: h.value for h in hits},
            "distances": {
                h.entry_id: (h.distance if h.distance == float("inf") else round(h.distance, 6))
                for h in hits
            },
            "distance_ceiling": self.distance_ceiling,
            "embedding_model": self.cache.embedder.model_id,
            "distance_metric": "vec_distance_cosine",
            "lexical_metric": "bm25",
            "channels": channels,
            "rrf_k": RRF_K,
        }

    def _consolidate(
        self,
        name: str,
        feedback: list[GradFeedback],
        retrieved: dict[str, str] | None = None,
        **kwargs: Any,  # pyright: ignore[reportExplicitAny]
    ) -> None:
        """Fold gradients into a parameter, over whichever channel applies.

        Routing is unchanged from the Turso backend, because it is a property of the
        parameter's type rather than of the store: a numeric parameter reads `score` (and
        with no score channel returns without writing, since rewriting a number from
        feedback text alone would be invention); a list parameter reads `text` agentically,
        editing only the entries `retrieved` names; a scalar or `Procedural` parameter
        reads `text` and is rewritten whole, the `Procedural` case through a post-condition
        that re-parses the result so a gradient cannot leave unparseable code in the store.
        """
        del kwargs
        texts = [g.text for g in feedback]

        if self._is_numeric_field(name):
            scores = [g.score for g in feedback if g.score is not None]
            if not scores:
                return
            score = min(max(sum(scores) / len(scores), 0.0), 1.0)
            current = self.numeric_value(name)
            self._record_observation(name, current, score, " | ".join(texts))
            proposal = self._numeric_update(name, current, score)
            annotation = self._resolve_field(name).annotation
            self._write_scalar(name, _format_numeric(annotation, proposal))
            self.connection.commit()
            return

        if self._is_procedural(name):
            updated = self._rewrite_code_fn.run_sync(
                value=self._read_scalar(name),
                feedback=texts,
                description=self._get_description(name),
            )
            self._write_scalar(name, updated)
            self.connection.commit()
            return

        if self._is_list_field(name):
            self._consolidate_entries(name, texts, retrieved)
            return

        updated = self._rewrite_value_fn.run_sync(
            value=self._read_scalar(name),
            feedback=texts,
            description=self._get_description(name),
        )
        self._write_scalar(name, updated)
        self.connection.commit()

    def _consolidate_entries(
        self, name: str, feedback: list[str], retrieved: dict[str, str] | None
    ) -> None:
        """Agentic entry editing, scoped to the entries the forward pass retrieved.

        Values are re-read from the store rather than taken from `retrieved`, because an
        entry may have been rewritten by an earlier consolidation in the same round and the
        agent must edit what is there now. Ids that no longer resolve are dropped. With no
        usable retrieval context the full entry set is shown, which is the honest fallback:
        a gradient whose forward pass we cannot localize should not pretend to be narrow.

        `EntryToolProvider` is reused from `turso_backend` rather than reimplemented. Its
        `backend` parameter is annotated `TursoMemoryBackend`, which this file cannot widen
        — `turso_backend.py` is frozen — but it only ever calls `search_entries`,
        `add_entry`, `update_entry`, and `remove_entry`, which both backends expose with
        identical signatures. `test_the_shared_entry_tool_provider_drives_this_backend`
        makes the reuse a checked property rather than an assumption about a docstring.
        """
        from ai_functions.optimizer._formatting import to_yaml

        entries = self.list_entries(name)
        scoped = {i: entries[i] for i in (retrieved or {}) if i in entries} or entries
        fn = self._edit_entries_fn.replace(tools=[EntryToolProvider(self, name)])  # pyright: ignore[reportArgumentType]
        fn.run_sync(
            retrieved=to_yaml(scoped),
            feedback=_bullets(feedback),
            description=self._get_description(name),
        )
        self.connection.commit()

    def _delete(self, name: str) -> None:
        """Reset a parameter to its schema default, and forget its score history.

        The observations go too, because `_numeric_update` reads them: keeping them would
        let a reset value be pulled straight back toward the pre-reset best on the next
        gradient, which is not a reset.
        """
        field_info = self._resolve_field(name)
        if field_info.is_required():
            raise ValueError(
                f"Cannot delete required parameter '{name}': it has no schema default."
            )
        default = field_info.get_default(call_default_factory=True)
        self._save(name, default)
        if self._is_numeric_field(name):
            self.connection.execute(
                "DELETE FROM memory_score_observation WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            self.connection.commit()

    # ── Tool provider ──

    def tool_provider(self, *names: str, operations: set[str] | None = None) -> DynamicToolProvider:
        """Extend the base tools with entry-id CRUD for list parameters.

        Same tool names as `JSONMemoryBackend` and `TursoMemoryBackend` — `add_to_<name>`,
        `update_<name>`, `delete_from_<name>` on top of the base `recall_` / `query_` /
        `search_` / `save_` / `delete_` — so an agent written against either keeps working
        when the backend is swapped underneath it.
        """
        ops = operations or {"recall", "query", "search", "save", "delete", "add", "update"}
        provider = super().tool_provider(*names, operations=ops)
        extra: list[AgentTool] = []
        for name in names:
            if not self._is_list_field(name):
                continue
            description = self._get_description(name) or name
            safe = name.replace("/", "_")
            if "add" in ops:
                extra.append(
                    _strands_tool(
                        name=f"add_to_{safe}", description=f"Add a new entry to: {description}"
                    )(self._entry_add_tool(name))
                )
            if "update" in ops:
                extra.append(
                    _strands_tool(
                        name=f"update_{safe}",
                        description=f"Update an entry by entry_id in: {description}",
                    )(self._entry_update_tool(name))
                )
            if "delete" in ops:
                extra.append(
                    _strands_tool(
                        name=f"delete_from_{safe}",
                        description=f"Delete an entry by entry_id from: {description}",
                    )(self._entry_delete_tool(name))
                )
        return DynamicToolProvider(provider.tools + extra)

    def _entry_add_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _add(value: str) -> str:
            """Add a new entry to this list.

            Args:
                value: The text content of the new entry.
            """
            return f"Added with entry_id={self.add_entry(name, value)}"

        return _add

    def _entry_update_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _update(entry_id: str, value: str) -> str:
            """Update an existing entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to update.
                value: The new text content.
            """
            if not self.update_entry(name, entry_id, value):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Updated entry_id={entry_id}"

        return _update

    def _entry_delete_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _delete(entry_id: str) -> str:
            """Delete an entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to delete.
            """
            if not self.remove_entry(name, entry_id):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Deleted entry_id={entry_id}"

        return _delete

    def __str__(self) -> str:
        """Human-readable dump of every parameter, for a report or a log."""
        lines: list[str] = []
        for name in self._leaf_parameter_names():
            if self._is_list_field(name):
                lines.append(f"{name}:")
                lines += [f"  [{i}] {v}" for i, v in self._entry_rows(name)]
            else:
                lines.append(f"{name}: {self._read_scalar(name)}")
        return "\n".join(lines)
