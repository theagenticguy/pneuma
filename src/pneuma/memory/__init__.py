"""Memory backends for pneuma's learning loops, over the project's own libSQL file."""

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
    "TursoMemoryBackend",
    "connect",
    "digest_of",
    "pack_vector",
    "unpack_vector",
]
