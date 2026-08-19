"""asyncpg connection pool + unit of work (handoff §9, hexagonal-arch-stack.md §3).

The pool registers a pool-wide jsonb codec (research-stack.yaml): asyncpg rejects a
raw ``dict`` for a jsonb column unless a codec is registered, so we encode with orjson
and decode with orjson on ``pg_catalog.jsonb``. This centralizes JSON handling in the
adapter — repositories pass and receive plain ``dict``/``list`` values.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType

import asyncpg
import orjson
from asyncpg.pool import PoolConnectionProxy

#: A transaction handle is either a raw connection or a pooled proxy — both expose the
#: same query surface asyncpg repositories use.
type PgConn = asyncpg.Connection | PoolConnectionProxy


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register the jsonb codec on every pooled connection.

    ``schema="pg_catalog"`` is required for the builtin jsonb type (asyncpg's docs
    default of ``"public"`` is wrong for builtins — verified in research-stack.yaml).
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda value: orjson.dumps(value).decode(),
        decoder=orjson.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda value: orjson.dumps(value).decode(),
        decoder=orjson.loads,
        schema="pg_catalog",
    )


class Postgres:
    """Owns the asyncpg pool lifecycle (APP scope in the DI container)."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 20,
        command_timeout: float = 30.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=self._command_timeout,
            init=_init_connection,
        )

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Postgres pool has not started")
        return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[PgConn]:
        async with self.pool.acquire() as connection, connection.transaction():
            yield connection

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[PgConn]:
        """A pooled connection with no surrounding transaction (read-side queries)."""
        async with self.pool.acquire() as connection:
            yield connection


class UnitOfWork:
    """Owns transaction scope; ``begin()`` yields one connection bound to one txn.

    Every mutating command runs inside a single ``begin()`` so the domain mutation,
    the event append, the outbox row, and the processed-command record commit atomically.
    """

    def __init__(self, postgres: Postgres) -> None:
        self._postgres = postgres

    def begin(self) -> _TxnCtx:
        return _TxnCtx(self._postgres)


class _TxnCtx:
    """Concrete async-context-manager returned by UnitOfWork.begin().

    Delegates to ``Postgres.transaction()`` (acquire + BEGIN), committing on clean
    exit and rolling back on exception.
    """

    def __init__(self, postgres: Postgres) -> None:
        self._ctx = postgres.transaction()

    async def __aenter__(self) -> PgConn:
        return await self._ctx.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return await self._ctx.__aexit__(exc_type, exc, tb)
