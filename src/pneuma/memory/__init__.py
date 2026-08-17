"""Memory backends for pneuma's learning loops, over one SQLite-family file.

Two interchangeable backends over the same schema and the same embedding cache:
`TursoMemoryBackend` on `pyturso`, for colocation with `casestudy.eventlog`'s libSQL audit
database, and `SqliteMemoryBackend` on stdlib `sqlite3` + `sqlite-vec`. They pass one shared
behavioural contract (`tests/library/test_turso_memory.py`,
`tests/library/test_sqlite_memory.py`) and share the `Retrieved` / `Discrimination` /
`CeilingNotSeparable` verdict types, so a caller reading a distance or a discrimination
verdict does not need to know which one produced it.

`connect` opens a Turso database; `sqlite_connect` opens a `sqlite3` one with the vector
extension loaded. Two names rather than one dispatcher, so picking the wrong engine is a
visible import rather than an argument nobody reads.

One capability is *not* shared, and it is an engine difference rather than a design one:
`SqliteMemoryBackend._search` is hybrid — a vector ranking fused with an FTS5 `bm25()` one by
Reciprocal Rank Fusion — while `TursoMemoryBackend._search` is pure vector, because Turso's
`fts_score()` returns 0.0 for every matching row and so cannot order anything. Both still
return the same `Retrieved` objects and the same `meta["results"]` shape, so the gradient
chain is unaffected; the sqlite backend's meta carries `channels` and `lexical_metric` on top,
which is how a reader of an event log tells the two apart. `reciprocal_rank_fusion` and
`fts_match_expression` are exported because they are the two pieces of that path with
behaviour worth testing without a database.
"""

from .embedding import (
    DEFAULT_MODEL_ID,
    DOCUMENT,
    EMBED_DIMENSIONS,
    QUERY,
    BedrockCohereEmbedder,
    Embedder,
    EmbeddingCache,
    digest_of,
    pack_vector,
    unpack_vector,
)
from .sqlite_backend import (
    RRF_K,
    SqliteMemoryBackend,
    VectorExtensionUnavailable,
    fts_match_expression,
    load_vector_extension,
    reciprocal_rank_fusion,
    sqlite_connect,
)
from .turso_backend import (
    CeilingNotSeparable,
    Discrimination,
    EntryToolProvider,
    Retrieved,
    TursoMemoryBackend,
    connect,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "DOCUMENT",
    "EMBED_DIMENSIONS",
    "QUERY",
    "RRF_K",
    "BedrockCohereEmbedder",
    "CeilingNotSeparable",
    "Discrimination",
    "Embedder",
    "EmbeddingCache",
    "EntryToolProvider",
    "Retrieved",
    "SqliteMemoryBackend",
    "TursoMemoryBackend",
    "VectorExtensionUnavailable",
    "connect",
    "digest_of",
    "fts_match_expression",
    "load_vector_extension",
    "pack_vector",
    "reciprocal_rank_fusion",
    "sqlite_connect",
    "unpack_vector",
]
