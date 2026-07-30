"""Embed text with Cohere Embed v4 on Bedrock, cached in the same libSQL file.

Two facts drive the design, both measured rather than assumed.

The first is that an embedding call is cheap but not free: 0.37s for a two-text
batch against Bedrock in us-east-1. A training loop recalls once per decision
per case per round, so an uncached backend would pay that latency on the hot
path of every step. The cache turns the steady state into a local blob read.

The second is that a cache keyed by anything other than the text itself is a
correctness bug waiting for the optimizer. Consolidation *rewrites* entries, so
an entry id is not a stable key for its content: keying on the id would serve
the pre-rewrite vector for post-rewrite text, and the failure would be silent
because a vector search always returns something ranked. So the key is
`sha256(text)`, and staleness is structurally impossible rather than managed —
a rewritten entry has a different digest, misses the cache, and is re-embedded.
The old vector stays behind harmlessly, addressed by a digest nothing points at.

`input_type` is part of the key because Cohere v4 embeds asymmetrically:
`search_document` and `search_query` produce different vectors for the same
string, and mixing them silently degrades ranking. Sharing one cache row
between them would be exactly that bug.

`vector32` is the wire format. It is little-endian float32 packed end to end,
which `struct.pack` produces byte-for-byte — verified by asking the database
`SELECT vector32('[1.0,0.0,0.0]') = ?` against a packed blob and getting 1. So
nothing here calls `vector32`; blobs are bound as parameters, which also keeps
the text out of SQL.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    import turso

# Cohere Embed v4 on Bedrock. The `global.` prefix is an inference profile; the
# bare `cohere.embed-v4:0` also resolves. 1536 dims, verified live.
DEFAULT_MODEL_ID = "global.cohere.embed-v4:0"
DEFAULT_REGION = "us-east-1"
EMBED_DIMENSIONS = 1536

DOCUMENT = "search_document"
QUERY = "search_query"


CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_embedding_cache (
  digest     TEXT NOT NULL,
  input_type TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  dims       INTEGER NOT NULL,
  embedding  BLOB NOT NULL,
  PRIMARY KEY (digest, input_type, model_id)
);
"""


def digest_of(text: str) -> str:
    """Content address for a piece of text: the cache key that cannot go stale."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_rows(
    connection: turso.Connection, sql: str, args: Sequence[Any] = ()
) -> list[tuple[Any, ...]]:  # pyright: ignore[reportExplicitAny]
    """Run a SELECT and return every row, always finalizing the statement.

    Not a convenience. `pyturso` 0.7.2 **silently discards pending uncommitted
    writes** when a `Cursor` holding an unfinalized SELECT is garbage collected.
    Reproduced minimally: open a cursor, `fetchone()` without exhausting it,
    `INSERT OR REPLACE` on the connection, then let the cursor fall out of
    scope. The insert is gone, `commit()` reports success, and no exception is
    raised anywhere.

    It cost this backend a real bug before it was found. A read-modify-write
    counter — read `next_id`, write `next_id + 1`, insert the row — allocated
    the same id forever, because the reading cursor died after the counter
    write and took it with it. The symptom surfaced as a `UNIQUE constraint`
    failure on the *entry* table, three statements away from the cause.

    `Cursor.close()` finalizes the active statement (lib.py:539) while
    `__del__` does not, so closing explicitly is the fix. Every read in this
    package goes through this function; none constructs a bare cursor. Verified
    over eight read-modify-write cycles with and without WAL.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql, args)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def fetch_one(
    connection: turso.Connection, sql: str, args: Sequence[Any] = ()
) -> tuple[Any, ...] | None:  # pyright: ignore[reportExplicitAny]
    """Run a SELECT and return its first row, or None. See :func:`fetch_rows`."""
    rows = fetch_rows(connection, sql, args)
    return rows[0] if rows else None


def pack_vector(values: Sequence[float]) -> bytes:
    """Pack floats into Turso's `vector32` blob format (little-endian float32)."""
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack a `vector32` blob back to floats."""
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class Embedder(Protocol):
    """What the backend needs from an embedding provider.

    A protocol rather than a class so a test can substitute a deterministic
    embedder and assert retrieval *ordering* without a network call. The
    distinction matters for what the suite can prove offline: ranking logic,
    cache behaviour, and the discrimination guard are all testable against a
    fake; only whether Cohere's semantics actually separate a realistic corpus
    needs the live model.
    """

    @property
    def model_id(self) -> str:
        """Identifier recorded in the cache key, so switching models re-embeds."""
        ...

    @property
    def dimensions(self) -> int:
        """Vector width. Turso raises on a dimension mismatch, so this is checked."""
        ...

    def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        """Embed a batch. `input_type` is `search_document` or `search_query`."""
        ...


class BedrockCohereEmbedder:
    """Cohere Embed v4 via `bedrock-runtime.invoke_model`.

    The boto3 client is built lazily so importing this module — and therefore
    importing the backend — never requires credentials. A test suite without
    AWS access can import everything and skip only the tests that embed.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        region_name: str = DEFAULT_REGION,
        client: Any | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> None:
        self._model_id = model_id
        self._region_name = region_name
        self._client = client

    @property
    def model_id(self) -> str:
        """The Bedrock model id, recorded in every cache key."""
        return self._model_id

    @property
    def dimensions(self) -> int:
        """Cohere Embed v4 output width, measured live at 1536."""
        return EMBED_DIMENSIONS

    def _runtime(self) -> Any:  # pyright: ignore[reportExplicitAny]
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region_name)
        return self._client

    def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input in order."""
        if input_type not in (DOCUMENT, QUERY):
            raise ValueError(f"input_type must be {DOCUMENT!r} or {QUERY!r}, got {input_type!r}")
        if not texts:
            return []
        response = self._runtime().invoke_model(
            modelId=self._model_id,
            body=json.dumps(
                {
                    "texts": list(texts),
                    "input_type": input_type,
                    "embedding_types": ["float"],
                }
            ),
        )
        payload = json.loads(response["body"].read())
        vectors: list[list[float]] = payload["embeddings"]["float"]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embedding provider returned {len(vectors)} vectors for {len(texts)} texts; "
                "a silently truncated batch would misalign every entry's vector."
            )
        return vectors


class EmbeddingCache:
    """Content-addressed embedding store living in the caller's libSQL database.

    Colocated with the entries it serves for the same reason the whole backend
    is: one file that holds evidence, learned parameters, and the vectors that
    retrieve them is an artifact you can hand to somebody. It also lets the
    top-k query be a single JOIN, so ranking happens in the database rather
    than in Python over a fully materialized corpus.
    """

    def __init__(self, connection: turso.Connection, embedder: Embedder) -> None:
        self._connection = connection
        self._embedder = embedder
        self.calls = 0
        """Provider round-trips made. A test asserts the cache actually caches."""
        self.texts_embedded = 0
        """Texts sent to the provider, cache misses only."""

    @property
    def embedder(self) -> Embedder:
        """The provider, exposed so callers can read its model id and dimensions."""
        return self._embedder

    def ensure(self, texts: Sequence[str], input_type: str) -> dict[str, str]:
        """Embed whatever is missing, and return `{text: digest}` for every input.

        Batches all misses into one provider call. Returns digests rather than
        vectors: callers use them as join keys against the cache table, so the
        vectors never travel through Python on the retrieval path.
        """
        by_digest = {digest_of(t): t for t in texts}
        if not by_digest:
            return {}

        placeholders = ",".join("?" for _ in by_digest)
        rows = fetch_rows(
            self._connection,
            f"SELECT digest FROM memory_embedding_cache "  # noqa: S608 — placeholders only
            f"WHERE input_type = ? AND model_id = ? AND digest IN ({placeholders})",
            (input_type, self._embedder.model_id, *by_digest),
        )
        present = {str(row[0]) for row in rows}
        missing = [(d, t) for d, t in by_digest.items() if d not in present]

        if missing:
            vectors = self._embedder.embed([t for _, t in missing], input_type)
            self.calls += 1
            self.texts_embedded += len(missing)
            expected = self._embedder.dimensions
            payload: list[tuple[str, str, str, int, bytes]] = []
            for (dgst, _), vector in zip(missing, vectors, strict=True):
                if len(vector) != expected:
                    raise RuntimeError(
                        f"Embedder {self._embedder.model_id!r} returned {len(vector)} dims, "
                        f"expected {expected}. Turso rejects a dimension mismatch at query "
                        "time, so this is caught at write time where the cause is visible."
                    )
                payload.append(
                    (dgst, input_type, self._embedder.model_id, expected, pack_vector(vector))
                )
            self._connection.executemany(
                "INSERT OR REPLACE INTO memory_embedding_cache "
                "(digest, input_type, model_id, dims, embedding) VALUES (?, ?, ?, ?, ?)",
                payload,
            )
            self._connection.commit()

        return {t: d for d, t in by_digest.items()}

    def vector(self, text: str, input_type: str) -> bytes:
        """Return one text's cached blob, embedding it first if absent."""
        self.ensure([text], input_type)
        row = fetch_one(
            self._connection,
            "SELECT embedding FROM memory_embedding_cache "
            "WHERE digest = ? AND input_type = ? AND model_id = ?",
            (digest_of(text), input_type, self._embedder.model_id),
        )
        if row is None:
            raise RuntimeError(f"Embedding for {text[:40]!r} vanished immediately after write.")
        return bytes(row[0])
