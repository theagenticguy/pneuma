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
    SqliteMemoryBackend,
    VectorExtensionUnavailable,
    load_vector_extension,
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
    "load_vector_extension",
    "pack_vector",
    "sqlite_connect",
    "unpack_vector",
]
