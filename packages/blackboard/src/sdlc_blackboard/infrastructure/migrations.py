"""Migration runner (hexagonal-arch-stack.md §5: dbmate, plain SQL).

Two entry points:
- ``run_dbmate`` shells out to the dbmate binary (the canonical path; the ``migrate``
  CLI command and CI use this).
- ``apply_sql_files`` is a dependency-free asyncpg fallback that executes the
  ``-- migrate:up`` section of each migration in order, used by integration tests and
  when the dbmate binary is unavailable.

Both accept a plain ``postgresql://`` DSN. ``to_dbmate_url`` strips a SQLAlchemy
``+driver`` suffix (testcontainers returns ``postgresql+psycopg2://``), which dbmate
and asyncpg both reject (research-stack.yaml).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def to_dbmate_url(url: str) -> str:
    """Normalize a DSN for dbmate/asyncpg: drop any ``+driver`` and ensure sslmode."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    query = parts.query or "sslmode=disable"
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


def run_dbmate(database_url: str, *, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """Apply pending migrations via the dbmate binary (no schema.sql dump)."""
    dbmate = shutil.which("dbmate")
    if dbmate is None:
        raise RuntimeError("dbmate not found on PATH; install it via `mise install` or dbmate docs")
    # S603: every argument is a trusted constant or a normalized DSN, never
    # attacker-controlled; the executable is a resolved absolute path.
    subprocess.run(  # noqa: S603
        [
            dbmate,
            "--url",
            to_dbmate_url(database_url),
            "--migrations-dir",
            str(migrations_dir),
            "--no-dump-schema",
            "--wait",
            "migrate",
        ],
        check=True,
    )


def _extract_up_sql(migration_text: str) -> str:
    """Return the ``-- migrate:up`` section (everything before ``-- migrate:down``)."""
    up_marker = "-- migrate:up"
    down_marker = "-- migrate:down"
    start = migration_text.find(up_marker)
    if start == -1:
        return migration_text
    body = migration_text[start + len(up_marker) :]
    down = body.find(down_marker)
    if down != -1:
        body = body[:down]
    return body.strip()


def _load_up_sections(migrations_dir: Path) -> list[str]:
    """Read + parse every migration's up-section synchronously (local file I/O)."""
    sections: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        up_sql = _extract_up_sql(path.read_text())
        if up_sql:
            sections.append(up_sql)
    return sections


async def apply_sql_files(database_url: str, *, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """Fallback runner: execute each migration's up-section in filename order.

    Files are read synchronously up front (small local files), so the async body
    only performs the database round-trips.
    """
    sections = _load_up_sections(migrations_dir)
    dsn = to_dbmate_url(database_url)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        for up_sql in sections:
            await conn.execute(up_sql)
    finally:
        await conn.close()
