"""`Trajectory`: one durable row per team run — the trajectory plane learning loops consume.

Every future learning/evolution loop needs rollout evidence: what the team was asked, who
was on it, what happened on the wire, and what came out. This hook writes exactly that, one
row per run, to a libSQL file. The contract is **write-on-every-path**: `on_teardown` runs
inside `core.Team.run`'s `finally` (`core.py`, the teardown loop), so a run that faulted
mid-flight still lands as a row — with `outcome='faulted'` and the fault's repr — and a
trajectory table with a gap is a bug, never an expected state.

Serialization is `json.dumps(default=repr)`: lossy but total. A transcript entry may carry
a pydantic model or an exception object; `repr` renders both rather than refusing the row,
and the consumer that needs structure re-parses what it recognises. The trade is
deliberate — a row that exists with an approximate field beats a row that vanished because
one entry was unserialisable.

**Persistence failures raise.** The strands-teams `EventLog` swallowed `OSError`, and an
audit plane that drops rows on a full disk reads as coverage — every downstream consumer
sees "no trajectory" and concludes "no run". So a failed write propagates: `core.py`
collects teardown errors and re-raises the first only when nothing else is already
propagating, which is exactly the loudness a durable plane owes its consumers.

Connection discipline follows `memory/turso_backend.py` (`connect`): `turso.connect` — the
library side's driver; `libsql` is application-only, enforced by `tests/library/
test_boundary.py` — with WAL + `synchronous=NORMAL` set per connection, a module-level
`SCHEMA` of `CREATE TABLE IF NOT EXISTS` statements split on `;`, idempotent init. Connections are
open-write-commit-close per run — nothing held between runs — so concurrent teams sharing
one file coordinate through WAL and tests need no lifecycle management. Reads go through
`_fetch_rows`, which closes its cursor in `try/finally`: `memory/embedding.py`'s
`fetch_rows` documents (measured) that a GC'd cursor holding an unfinalized SELECT
silently discards pending writes, and this module keeps that discipline even though its
reads and writes never share a connection — the convention is cheaper than the debugging
session that re-derives it.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import turso

from ..core import Accept, Workspace

__all__ = ["SCHEMA", "Trajectory", "read_trajectories"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS trajectories (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  request     TEXT NOT NULL,
  answer      TEXT,
  outcome     TEXT NOT NULL,
  fault       TEXT,
  members     TEXT NOT NULL,
  transcript  TEXT NOT NULL,
  hooks_data  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trajectories_outcome ON trajectories(outcome);
"""

_COLUMNS = (
    "id",
    "started_at",
    "finished_at",
    "request",
    "answer",
    "outcome",
    "fault",
    "members",
    "transcript",
    "hooks_data",
)

_UNSET: Any = object()
"""Sentinel distinguishing "no answer ever reached this hook" from a `None` answer."""


def _connect(path: Path) -> turso.Connection:
    """Open the trajectory database in WAL mode — the `turso_backend.connect` discipline.

    WAL is set on every open because the pragma is per-connection for readers even though
    the mode itself persists in the file header.
    """
    connection = turso.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _init_schema(connection: turso.Connection) -> None:
    for statement in filter(str.strip, SCHEMA.split(";")):
        connection.execute(statement)
    connection.commit()


def _fetch_rows(
    connection: turso.Connection, sql: str, args: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    """Run a SELECT and return every row, always finalizing the statement.

    The cursor closes in `try/finally` — `memory/embedding.py`'s `fetch_rows` measured a
    GC'd unfinalized SELECT cursor silently discarding pending writes on its connection,
    with the symptom surfacing statements away from the cause. Every read in this module
    goes through here; none constructs a bare cursor.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql, args)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def read_trajectories(path: Path | str) -> list[dict[str, Any]]:
    """Every persisted trajectory row on `path`, oldest first, as plain dicts.

    The consumer seam: learning loops and tests read here rather than writing their own
    SQL, so the cursor discipline above covers them too. Initialises the schema first, so
    reading a path no run has written yet returns `[]` rather than raising — a trajectory
    plane with zero rows is a fact, not a fault.
    """
    connection = _connect(Path(path))
    try:
        _init_schema(connection)
        rows = _fetch_rows(
            connection,
            f"SELECT {', '.join(_COLUMNS)} FROM trajectories ORDER BY id",
        )
        return [dict(zip(_COLUMNS, row, strict=True)) for row in rows]
    finally:
        connection.close()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Trajectory:
    """The trajectory plane as a hook: observe the run, persist one row on teardown.

    Args:
        path: The libSQL file. File-based only — `:memory:` is refused at construction
            (repo convention): an in-memory trajectory plane vanishes with the process,
            which is the opposite of the durability this hook exists to provide, and each
            open-write-close cycle would see a *different* empty database anyway.

    Per-run scratch (start time, member names, the latest answer) lives on the hook
    instance, not in `work.data` — so `hooks_data` carries only what *other* hooks
    recorded and this hook never has to excise its own bookkeeping from the row. State
    resets by workspace identity (the `worklog.py` / `hiring.py` pattern), so one hook
    instance on a `Team` that runs twice never bleeds run 1's answer into run 2's row.
    """

    def __init__(self, path: Path | str) -> None:
        text = str(path)
        if ":memory:" in text or "mode=memory" in text:
            raise ValueError(
                f"Trajectory(path={text!r}) is an in-memory database; the trajectory "
                f"plane exists to outlive the process — give it a file path."
            )
        self.path = Path(path)
        self._run: Workspace | None = None
        self._started_at: str | None = None
        self._member_names: list[str] = []
        self._answer: Any = _UNSET

    # ── Per-run state ──

    def _reset_if_new_run(self, work: Workspace) -> None:
        """A new workspace is a new run: drop the old scratch before anything reads it.

        Compared by identity because the workspace *is* the run. Without this, a fault
        early in run 2 would persist run 1's answer and start time on run 2's row.
        """
        if self._run is not work:
            self._run = work
            self._started_at = None
            self._member_names = []
            self._answer = _UNSET

    # ── The hook surface ──

    def on_assemble(self, work: Workspace) -> None:
        """Members are live, the lead has not run: stamp the start and the cast."""
        self._reset_if_new_run(work)
        self._started_at = _utcnow()
        self._member_names = [member.name for member in work.members]

    def on_answer(self, work: Workspace, answer: Any) -> Accept:
        """Record the latest answer seen and wave it through — an observer, never a gate.

        Pure-observe + `Accept()` is what makes this hook compatible with both answer-loop
        semantics: under the current per-hook sequential loop, wire `Trajectory` *after*
        any reviewing hooks and the answer it sees is the one those hooks settled; under
        restart-chain semantics, a full final pass revisits every `on_answer` hook, so the
        last answer this observer sees is the final answer regardless of position. On a
        bare team (no reviewing hooks) `core._answer_loop` still consults every hook that
        defines `on_answer`, so a Trajectory-only team gets exactly one call, carrying the
        lead's first — and final — answer.
        """
        self._reset_if_new_run(work)
        self._answer = answer
        return Accept()

    def on_teardown(self, work: Workspace) -> None:
        """Write the row — on every path, faulted runs included.

        `core.Team.run` calls this in its `finally`, so `sys.exception()` here is the
        fault in flight when there is one and `None` on the clean path. A write failure
        raises (see the module header): the core collects it and re-raises only when
        nothing else propagates, so a vanished trajectory is always loud.
        """
        self._reset_if_new_run(work)
        error = sys.exception()
        finished_at = _utcnow()
        answered = self._answer is not _UNSET
        row = (
            self._started_at or finished_at,  # a pre-assembly fault still gets a row
            finished_at,
            work.request,
            str(self._answer) if answered else None,
            "completed" if answered and error is None else "faulted",
            repr(error) if error is not None else None,
            json.dumps(self._member_names or [m.name for m in work.members], default=repr),
            json.dumps(work.transcript, default=repr),
            json.dumps(work.data, default=repr),
        )
        connection = _connect(self.path)
        try:
            _init_schema(connection)
            connection.execute(
                "INSERT INTO trajectories "
                "(started_at, finished_at, request, answer, outcome, fault, members, "
                "transcript, hooks_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            connection.commit()
        finally:
            connection.close()

    def __repr__(self) -> str:
        return f"<Trajectory path={str(self.path)!r}>"
