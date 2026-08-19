"""The artifact plane: immutable revisions, one branch per proposer, merges only at the lead.

pneuma versions every agent's *conversation* — threads fork, the event log replays, a
`Trajectory` row outlives the run. Nothing versioned the *documents* a team works on, so each
member's evidence stayed private and the lead was the single integration point: whatever it
failed to fold into its answer was lost, and two members editing one design were invisible to
each other until one answer contradicted the other. This module is the missing plane.
Revisions are immutable and content-addressed — `revision_id` digests content *and* lineage,
so an identical re-proposal is the same row rather than a second one — every proposal lands on
its author's own branch parented at `main`'s head, and `main` moves only by fast-forward or by
an explicit merge whose non-overlap was proven. Rationale: `docs/design/artifacts.md`.

Four things break if edited carelessly.

**Stdlib `sqlite3`, not `turso`.** `hooks/trajectory.py` uses `turso` because
`memory/turso_backend.py` already did; this module needs no vector search and no replica, so
it takes the interpreter's own driver and adds zero dependencies. The *discipline* is
trajectory's verbatim: WAL + `synchronous=NORMAL` per connection, a module-level `SCHEMA` of
`CREATE TABLE IF NOT EXISTS` split on `;`, idempotent init, cursors closed in `try/finally`
(`memory/embedding.py` measured a GC'd unfinalized SELECT cursor silently discarding pending
writes on its connection).

**`:memory:` keeps one connection; a file opens one per operation.** `Trajectory` refuses
`:memory:` because open-write-commit-close would see a fresh empty database every time. That
argument is about the *pattern*, so this store holds one connection for an in-memory
database's whole lifetime (the default, and what an offline test wants) and opens per
operation for a file path (what durability and WAL-coordinated concurrency want).

**Persistence failures raise; the model's mistakes are text.** A wrong path, an unknown
revision, a non-fast-forward: mistakes a model can fix, so they arrive as `ArtifactError` and
the hook renders them as `"error: ..."` tool text (`hooks/hiring.py`'s rule). A `sqlite3.Error`
never becomes text — a plane that drops writes on a full disk reads as agreement
(`hooks/trajectory.py`'s rule).

**A conflict is a row, never a lost write.** Overlapping edits are recorded in `conflicts` and
returned as a typed `Conflict`; nothing overwrites and nothing is silently dropped.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "MAIN",
    "SCHEMA",
    "ArtifactError",
    "ArtifactStore",
    "Conflict",
    "Revision",
    "SplitBrain",
    "split_brain",
    "three_way_merge",
]

MAIN = "main"
"""The one branch a reader may treat as the team's answer.

Named rather than inlined because three files agree on it (`hooks/artifacts.py` renders it
to the model, `split_brain` counts it as a branch like any other, and the fast-forward rule
is stated in terms of it), and a string literal repeated in three places is a rename away
from a plane that silently grows two mains.
"""

MEMORY = ":memory:"
"""The default path: a store that lives exactly as long as the process holding it."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
  path        TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
  revision_id     TEXT PRIMARY KEY,
  artifact_id     INTEGER NOT NULL,
  parent_revision TEXT,
  merged_from     TEXT,
  content         TEXT NOT NULL,
  digest          TEXT NOT NULL,
  author          TEXT NOT NULL,
  branch          TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  decides         TEXT,
  run_id          TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_artifact ON revisions(artifact_id, branch);
CREATE TABLE IF NOT EXISTS refs (
  artifact_id   INTEGER NOT NULL,
  branch        TEXT NOT NULL,
  head_revision TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (artifact_id, branch)
);
CREATE TABLE IF NOT EXISTS conflicts (
  conflict_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id       INTEGER NOT NULL,
  proposal_revision TEXT NOT NULL,
  head_revision     TEXT,
  ancestor_revision TEXT,
  mergeable         INTEGER NOT NULL,
  overlapping       TEXT NOT NULL,
  resolution        TEXT,
  detected_at       TEXT NOT NULL
);
"""

_REVISION_COLUMNS = (
    "revision_id",
    "parent_revision",
    "merged_from",
    "content",
    "digest",
    "author",
    "branch",
    "rationale",
    "decides",
    "run_id",
    "created_at",
)

MAX_DIFF_LINES = 60
"""Lines of each side's diff carried in a `Conflict`'s text, before an explicit truncation note.

Bounded because the text lands in the lead's model context and an unbounded diff of a
megafile would evict the conversation that asked for it. Truncation is *stated* rather than
silent — a diff that stopped early and did not say so is the same defect
`detect/discrimination.py` refuses on sweeps.
"""


# ── Failures the model can fix ──


class ArtifactError(Exception):
    """A mistake in what was asked of the plane, not a fault of the plane.

    Separate from `sqlite3.Error` on purpose, and that separation is the whole reason this
    class exists: `hooks/artifacts.py` catches exactly this and returns its message as tool
    text the model reads and acts on, while a storage fault propagates and takes the run
    down loudly. A single bare `except Exception` at the tool boundary would render a full
    disk as advice to the model.
    """


# ── What a revision is ──


@dataclass(frozen=True)
class Revision:
    """One immutable revision of one artifact.

    Attributes:
        revision_id: A digest over content *and* lineage (`_revision_id`), which is what
            makes an identical re-proposal the same row rather than a second one.
        path: The artifact's path. Carried on the revision because every consumer that
            holds a revision wants the document it belongs to and no consumer wants a
            second query to get it.
        parent_revision: The revision this one was written against — `main`'s head at
            proposal time, or `None` for the first revision of a new artifact.
        merged_from: The proposal a merge revision folded in, or `None`. The second parent,
            recorded as a fact without turning `parent_revision` into a general DAG.
        content: The whole document, not a patch. A patch stored without its base is a
            revision that cannot be read without replaying every ancestor.
        digest: `sha256` of the content alone, so two revisions that say the same thing
            share a digest — which is what lets `split_brain` tell "decided differently"
            from "decided the same way twice".
        author: Bound by the hook to the proposing member's name, never reported by the
            model (`hooks/worklog.py`'s attribution rule).
        branch: The proposer's own branch, or `MAIN` for a committed merge.
        rationale: Why this change. Free text, because the reason a document changed is
            exactly the thing a closed vocabulary cannot carry.
        decides: The design question this change settles, or `None`. Read only by
            `split_brain`; deliberately not the `Worklog`'s closed vocabulary.
        run_id: Which team run produced it, or `None`. The join back to the trajectory plane.
        created_at: ISO-8601 UTC. Outside the content address, so the same change proposed
            twice stays one revision.
    """

    revision_id: str
    path: str
    parent_revision: str | None
    merged_from: str | None
    content: str
    digest: str
    author: str
    branch: str
    rationale: str
    decides: str | None
    run_id: str | None
    created_at: str

    @property
    def short(self) -> str:
        """The first 12 hex characters — what the model reads and types back.

        A full 64-character digest in a tool description is a transcription error waiting to
        happen; `ArtifactStore.revision` resolves any unambiguous prefix, so the short form
        is the one the plane actually speaks.
        """
        return self.revision_id[:12]

    def __str__(self) -> str:
        return f"{self.short} on {self.branch!r} by {self.author} — {self.rationale}"


# ── What a collision is ──


@dataclass(frozen=True)
class Conflict:
    """Two revisions that changed the same document since their common ancestor.

    Returned by `ArtifactStore.commit` whenever a proposal is not a fast-forward, and by
    `ArtifactStore.merge` whenever the two sides' edits overlap. `mergeable` distinguishes
    the two situations and is the only thing the lead needs to decide what to do next:
    a `mergeable` conflict is a sibling that committed first and touched other lines, which
    `merge_change` lands; a non-`mergeable` conflict is two agents rewriting the same lines,
    which nothing may resolve automatically because whichever side lost would be a silent
    overwrite — Cursor's measured failure mode for agent swarms without a VCS (agents in
    conflict either overwrite each other or abandon the work).

    Attributes:
        path: The artifact.
        proposal: The proposal's `revision_id`, with its author and branch.
        head: `main`'s head at detection time, with its author. `None` only when the head
            was deleted from under the plane, which nothing in this module does.
        ancestor: The common ancestor both sides were written against, or `None` when the
            proposal's parent is not reachable from `main`'s head at all — a lineage this
            module never creates and therefore never merges.
        overlapping: One rendered line per overlapping region, base line numbers included.
            Empty exactly when `mergeable` is true.
        mergeable: Whether `merge_change` would land this proposal.
        diff: A three-way summary: what each side changed against the ancestor, bounded by
            `MAX_DIFF_LINES` per side with truncation stated.
        conflict_id: The `conflicts` row. Non-`None` once recorded, which is always, because
            a conflict the plane did not write down is a lost write wearing a verdict.
    """

    path: str
    proposal: str
    proposal_author: str
    proposal_branch: str
    head: str | None
    head_author: str | None
    ancestor: str | None
    overlapping: tuple[str, ...]
    mergeable: bool
    diff: str
    conflict_id: int | None = None

    def __str__(self) -> str:
        """The text the lead acts on — the whole conflict, and what to do about it.

        Rendered here rather than in the hook so the store's own consumers (a test, a
        report, a later coach) read the same words the lead read. Ends in the two moves
        that are actually available, because a conflict report with no next move is what
        makes an agent abandon the work.
        """
        head = f"main's head {self.head[:12]}" if self.head else "main (no head)"
        head_by = f" by {self.head_author}" if self.head_author else ""
        ancestor = self.ancestor[:12] if self.ancestor else "no common ancestor"
        lines = [
            f"conflict on {self.path}: proposal {self.proposal[:12]} "
            f"(branch {self.proposal_branch!r}, by {self.proposal_author}) is not a "
            f"fast-forward — {head}{head_by} moved since their common ancestor {ancestor}.",
        ]
        if self.mergeable:
            lines.append(
                "The two sides changed different lines, so merge_change can land this one "
                "without losing either edit."
            )
        else:
            lines.append("The two sides changed the SAME lines, so no merge is safe:")
            lines.extend(f"  overlap: {region}" for region in self.overlapping)
            lines.append(
                "Resolve it by deciding which change the team keeps: commit or merge one "
                "side, then ask the other's author to read the artifact again and propose "
                "against the new head. Do not restate one side's text as the other's — the "
                "author whose edit disappears is the one who knew why it was there."
            )
        lines.append(self.diff)
        return "\n".join(lines)


# ── The three-way merge, on stdlib difflib ──


@dataclass(frozen=True)
class _Hunk:
    """One side's change to base lines `[i1, i2)`, replaced by `lines`."""

    i1: int
    i2: int
    lines: tuple[str, ...]

    @property
    def span(self) -> tuple[int, int]:
        """The base region this hunk claims, with a pure insertion widened to one line.

        A pure insertion covers zero base lines, and a zero-width interval overlaps nothing
        — so two agents inserting different text at the same point, or one inserting where
        the other replaced, would both read as non-overlapping and one insertion would land
        at an arbitrary side of the other. Widening by one line makes both cases overlap,
        which is the safe direction: this module owes the team no silent overwrite, and it
        owes nobody a merge.
        """
        return (self.i1, max(self.i2, self.i1 + 1))

    def render(self, side: str) -> str:
        if self.i1 == self.i2:
            return f"{side} inserted at base line {self.i1 + 1}"
        if self.i2 - self.i1 == 1:
            return f"{side} changed base line {self.i1 + 1}"
        return f"{side} changed base lines {self.i1 + 1}-{self.i2}"


def _hunks(base: Sequence[str], side: Sequence[str]) -> list[_Hunk]:
    """Every non-equal opcode from base to one side, as hunks over base line indices."""
    matcher = difflib.SequenceMatcher(None, list(base), list(side), autojunk=False)
    return [
        _Hunk(i1, i2, tuple(side[j1:j2]))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _touching(left: _Hunk, right: _Hunk) -> bool:
    (a1, a2), (b1, b2) = left.span, right.span
    return a1 < b2 and b1 < a2


def three_way_merge(base: str, ours: str, theirs: str) -> tuple[str | None, tuple[str, ...]]:
    """Merge two independent edits of `base`, or refuse and say exactly where they collide.

    Returns `(merged_text, ())` when the two sides' hunks claim disjoint base regions, and
    `(None, overlapping)` when they do not. Line-based on `splitlines(keepends=True)`, so a
    document's missing trailing newline survives the round trip and a merge of two
    whitespace-only edits is still a merge of lines rather than of characters.

    Two hunks that are byte-identical are agreement, not collision: both members made the
    same edit, and refusing that would surface a conflict a reader cannot act on (there is
    nothing to choose between). Everything else that touches — including two insertions at
    one point — refuses. The asymmetry is deliberate and it is the module's whole safety
    argument: a wrongly refused merge costs the lead one turn, a wrongly accepted one loses
    an author's work with nothing raised.

    `difflib` rather than a real merge engine because the alternative is a dependency, and
    the only judgement a merge engine adds over `SequenceMatcher` opcodes is heuristics for
    resolving overlap — which is exactly the judgement this plane refuses to make.
    """
    if ours == theirs:
        return ours, ()  # both sides already agree; there is nothing to merge
    base_lines = base.splitlines(keepends=True)
    ours_hunks = _hunks(base_lines, ours.splitlines(keepends=True))
    theirs_hunks = _hunks(base_lines, theirs.splitlines(keepends=True))

    overlapping: list[str] = []
    for ours_hunk in ours_hunks:
        for theirs_hunk in theirs_hunks:
            if not _touching(ours_hunk, theirs_hunk):
                continue
            if (ours_hunk.i1, ours_hunk.i2, ours_hunk.lines) == (
                theirs_hunk.i1,
                theirs_hunk.i2,
                theirs_hunk.lines,
            ):
                continue  # the same edit twice is agreement
            overlapping.append(
                f"{ours_hunk.render('main')} / {theirs_hunk.render('the proposal')}"
            )
    if overlapping:
        return None, tuple(overlapping)

    # Deduplicate the identical hunks the loop above waved through, then splice in base
    # order. Every remaining hunk claims a disjoint region, so the cursor only ever moves
    # forward — the guard below states that rather than trusting it, because a cursor that
    # jumped backwards would silently delete the base lines between.
    hunks = {(h.i1, h.i2, h.lines) for h in (*ours_hunks, *theirs_hunks)}
    merged: list[str] = []
    cursor = 0
    for i1, i2, lines in sorted(hunks):
        if i1 < cursor:
            return None, (f"overlapping hunks survived the check at base line {i1 + 1}",)
        merged.extend(base_lines[cursor:i1])
        merged.extend(lines)
        cursor = i2
    merged.extend(base_lines[cursor:])
    return "".join(merged), ()


def _diff_summary(base: str, ours: str, theirs: str) -> str:
    """What each side changed against the ancestor, bounded and honest about the bound."""
    blocks = []
    for label, side in (("main", ours), ("the proposal", theirs)):
        diff = list(
            difflib.unified_diff(
                base.splitlines(),
                side.splitlines(),
                fromfile="ancestor",
                tofile=label,
                lineterm="",
                n=1,
            )
        )
        shown = diff[:MAX_DIFF_LINES]
        if len(diff) > MAX_DIFF_LINES:
            shown.append(f"... ({len(diff) - MAX_DIFF_LINES} more diff lines not shown)")
        body = "\n".join(shown) or "(no textual change)"
        blocks.append(f"--- what {label} changed ---\n{body}")
    return "\n".join(blocks)


# ── The store ──


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _revision_id(
    path: str, parent: str | None, content: str, author: str, branch: str, rationale: str
) -> str:
    """The content address: a digest over content *and* the lineage it was written against.

    Content alone would make two members proposing the same text one revision, losing the
    second author's attribution and its rationale. Lineage alone would be a counter. Both
    together give the property the plane wants: proposing the identical change twice is
    idempotent (the same row, one author, one reason), and every genuinely different
    proposal is a different row. `created_at` is deliberately excluded — including it would
    make the address a timestamp with extra steps.
    """
    payload = json.dumps(
        [path, parent, content, author, branch, rationale], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _connect(path: str) -> sqlite3.Connection:
    """Open the artifact database in WAL mode — `hooks/trajectory.py`'s discipline verbatim.

    WAL is set on every open because the pragma is per-connection for readers even though
    the mode persists in the file header. On `:memory:` it answers `memory` and changes
    nothing, which is a documented no-op rather than a failure worth branching on.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _init_schema(connection: sqlite3.Connection) -> None:
    for statement in filter(str.strip, SCHEMA.split(";")):
        connection.execute(statement)
    connection.commit()


def _fetch(
    connection: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    """Run a SELECT and return every row, always finalizing the statement.

    The cursor closes in `try/finally`: `memory/embedding.py`'s `fetch_rows` measured a GC'd
    unfinalized SELECT cursor silently discarding pending writes on its connection, with
    the symptom surfacing statements away from the cause. Every read here goes through this.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql, args)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


class ArtifactStore:
    """Content-addressed revisions of shared documents, with one branch per proposer.

    Args:
        path: `:memory:` (the default — an offline test wants a store that needs no
            filesystem) or a file path (durability, and WAL-coordinated concurrency between
            two teams sharing one plane). Unlike `Trajectory`, `:memory:` is *not* refused
            here: see the module header for why the two answers differ.

    The write model is the whole of the design:

    - `propose` appends an immutable revision on the author's own branch, parented at
      `main`'s head **at proposal time**. A member's second proposal is parented at main's
      head too, never at its own first — the branch is a one-deep staging slot, not a
      history the member rebases, and an author whose earlier proposal was superseded still
      has it on the record.
    - `commit` moves `main` **only** by fast-forward: the proposal's parent must be main's
      head. Anything else returns a `Conflict`.
    - `merge` lands a proven non-overlapping three-way merge as a new revision on `main`,
      authored by whoever merged, carrying `merged_from`. Overlap returns a `Conflict`.

    Rejected alternatives, both recorded in `docs/design/artifacts.md`: letting any member
    commit (the plane then has no integration point and last-writer-wins is the plane's
    semantics rather than a bug), and auto-merging everything (an overlap resolved by rule
    is one author's work deleted by a coin toss, which is the failure this plane exists to
    make visible).
    """

    def __init__(self, path: Path | str = MEMORY) -> None:
        self.path = str(path)
        # One connection for the lifetime of an in-memory store, because open-write-close
        # would see a different empty database on every operation; a file path gets a
        # connection per operation, so nothing is held between runs and two teams sharing
        # the file coordinate through WAL alone.
        self._shared: sqlite3.Connection | None = None
        if self.path == MEMORY or "mode=memory" in self.path:
            self._shared = _connect(self.path)
            _init_schema(self._shared)
        else:
            # Initialised here so a bad path is refused where the wirer is looking rather
            # than mid-run from inside a member's tool call.
            connection = _connect(self.path)
            try:
                _init_schema(connection)
            finally:
                connection.close()

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One unit of work: open, use, commit, close — or the shared in-memory connection.

        Committed only on the success path, so a raise leaves the plane exactly as it was.
        """
        if self._shared is not None:
            yield self._shared
            self._shared.commit()
            return
        connection = _connect(self.path)
        try:
            _init_schema(connection)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def close(self) -> None:
        """Release an in-memory store's connection. A no-op for a file-backed one."""
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    # ── Reads ──

    def paths(self) -> list[str]:
        """Every artifact path the plane knows, sorted."""
        with self._session() as connection:
            rows = _fetch(connection, "SELECT path FROM artifacts ORDER BY path")
        return [row[0] for row in rows]

    def head(self, path: str, branch: str = MAIN) -> Revision | None:
        """The newest revision on `branch`, or `None` when that branch has no head.

        A `None` head is an ordinary state — a brand-new artifact, or a path no member has
        proposed against — so it is returned rather than raised. An *unknown path* is a
        different thing and raises: see `read`.
        """
        with self._session() as connection:
            artifact_id = self._artifact_id(connection, path)
            if artifact_id is None:
                return None
            return self._head(connection, artifact_id, path, branch)

    def read(self, path: str, branch: str = MAIN) -> str:
        """The content of `branch`'s head, or `""` for an artifact with no revision yet.

        An unknown path raises `ArtifactError` naming the paths that do exist, rather than
        answering `""`: a member that mistyped a path and read an empty document would
        propose a whole new file over the top of the one it meant to edit, and the plane
        would have no way to tell that from a genuine new artifact.
        """
        with self._session() as connection:
            artifact_id = self._artifact_id(connection, path)
            if artifact_id is None:
                known = [
                    row[0] for row in _fetch(connection, "SELECT path FROM artifacts ORDER BY path")
                ]
                raise ArtifactError(
                    f"no artifact at {path!r}; the team's artifacts are: "
                    f"{', '.join(known) or '(none yet)'}"
                )
            head = self._head(connection, artifact_id, path, branch)
        return head.content if head is not None else ""

    def revision(self, revision_id: str) -> Revision:
        """One revision by id or by any unambiguous prefix of it.

        Prefixes are resolved because `Revision.short` is what the plane shows a model, and
        a tool that shows twelve characters and demands sixty-four is a transcription error
        the model cannot see. An ambiguous prefix raises rather than picking one.
        """
        with self._session() as connection:
            rows = _fetch(
                connection,
                f"SELECT a.path, {', '.join('r.' + c for c in _REVISION_COLUMNS)} "
                f"FROM revisions r JOIN artifacts a ON a.artifact_id = r.artifact_id "
                f"WHERE r.revision_id = ? OR r.revision_id LIKE ?",
                (revision_id, f"{revision_id}%"),
            )
        exact = [row for row in rows if row[1] == revision_id]
        if exact:
            return _revision_from(exact[0])
        if not rows:
            raise ArtifactError(f"no revision {revision_id!r} in this team's artifact store")
        if len(rows) > 1:
            found = ", ".join(sorted(row[1][:12] for row in rows))
            raise ArtifactError(f"revision {revision_id!r} is ambiguous; it matches {found}")
        return _revision_from(rows[0])

    def proposals(self, path: str) -> list[Revision]:
        """The head of every branch that `main` has not already absorbed, oldest first.

        Branch heads rather than every revision on every branch, because a superseded
        proposal is history and only the newest one on a branch is a thing the lead could
        land. The full history stays queryable in `revisions`; this is the lead's inbox.

        A branch head reachable from `main` is excluded, because "already landed" is not
        "pending": a committed proposal leaves its branch ref where it was (nothing rewrites
        history here), so a branch-only filter would show the lead the change it just
        committed and the seeded original as two proposals waiting on it — measured, and the
        first thing that broke the end-to-end test.
        """
        with self._session() as connection:
            artifact_id = self._artifact_id(connection, path)
            if artifact_id is None:
                return []
            rows = _fetch(
                connection,
                f"SELECT a.path, {', '.join('r.' + c for c in _REVISION_COLUMNS)} "
                f"FROM refs f "
                f"JOIN revisions r ON r.revision_id = f.head_revision "
                f"JOIN artifacts a ON a.artifact_id = r.artifact_id "
                f"WHERE f.artifact_id = ? AND f.branch != ? "
                f"ORDER BY r.created_at, r.revision_id",
                (artifact_id, MAIN),
            )
            head = self._head(connection, artifact_id, path, MAIN)
            landed = self._reachable(connection, head.revision_id) if head is not None else set()
        return [_revision_from(row) for row in rows if row[1] not in landed]

    def revisions(self, path: str | None = None) -> list[Revision]:
        """Every revision, oldest first, optionally for one path. The audit trail."""
        with self._session() as connection:
            sql = (
                f"SELECT a.path, {', '.join('r.' + c for c in _REVISION_COLUMNS)} "
                f"FROM revisions r JOIN artifacts a ON a.artifact_id = r.artifact_id"
            )
            args: tuple[Any, ...] = ()
            if path is not None:
                sql += " WHERE a.path = ?"
                args = (path,)
            rows = _fetch(connection, f"{sql} ORDER BY r.created_at, r.revision_id", args)
        return [_revision_from(row) for row in rows]

    def conflicts(self, path: str | None = None) -> list[dict[str, Any]]:
        """Every recorded conflict, oldest first, as plain dicts — the consumer seam.

        Conflicts are rows and not merely return values because a conflict a run reported
        and nobody wrote down is indistinguishable, one run later, from a change nobody
        ever proposed.
        """
        with self._session() as connection:
            sql = (
                "SELECT c.conflict_id, a.path, c.proposal_revision, c.head_revision, "
                "c.ancestor_revision, c.mergeable, c.overlapping, c.resolution, c.detected_at "
                "FROM conflicts c JOIN artifacts a ON a.artifact_id = c.artifact_id"
            )
            args: tuple[Any, ...] = ()
            if path is not None:
                sql += " WHERE a.path = ?"
                args = (path,)
            rows = _fetch(connection, f"{sql} ORDER BY c.conflict_id", args)
        return [
            {
                "conflict_id": row[0],
                "path": row[1],
                "proposal_revision": row[2],
                "head_revision": row[3],
                "ancestor_revision": row[4],
                "mergeable": bool(row[5]),
                "overlapping": tuple(json.loads(row[6])),
                "resolution": row[7],
                "detected_at": row[8],
            }
            for row in rows
        ]

    # ── Writes ──

    def propose(
        self,
        path: str,
        content: str,
        *,
        author: str,
        rationale: str,
        branch: str | None = None,
        decides: str | None = None,
        run_id: str | None = None,
    ) -> Revision:
        """Append one immutable revision on `branch` (the author's own by default).

        The parent is `main`'s head at this moment, which is what makes the fast-forward
        rule in `commit` meaningful: a proposal whose parent is no longer main's head is
        exactly a proposal written against a document that has since moved.

        Args:
            path: The artifact. Created on first proposal — a plane where a new document
                needs a separate create step has two ways to fail instead of one.
            content: The whole new document, not a patch.
            author: Bound by the caller (the hook binds the member's name), never taken
                from the model.
            rationale: Why. Refused empty: a revision whose reason is blank is the one the
                lead cannot weigh against a sibling's, and the refusal costs the model one
                turn where the missing reason costs the team the decision.
            branch: Defaults to `author`. Refused when it is `main`: `main` moves by commit
                and merge only, and a proposal that could write straight to it would make
                the lead's commit authority advisory.
            decides: The design question this change settles, or `None`. Free text; only
                `split_brain` reads it.
            run_id: Which run this came from.
        """
        if not rationale.strip():
            raise ArtifactError(
                f"a proposal for {path!r} needs a rationale; the lead weighs proposals "
                f"against each other and a blank reason is not weighable"
            )
        target = branch or author
        if target == MAIN:
            raise ArtifactError(
                f"{author!r} may not propose directly onto {MAIN!r}; propose on your own "
                f"branch and the lead commits it"
            )
        with self._session() as connection:
            artifact_id = self._ensure_artifact(connection, path)
            head = self._head(connection, artifact_id, path, MAIN)
            parent = head.revision_id if head is not None else None
            revision = Revision(
                revision_id=_revision_id(path, parent, content, author, target, rationale),
                path=path,
                parent_revision=parent,
                merged_from=None,
                content=content,
                digest=_digest(content),
                author=author,
                branch=target,
                rationale=rationale,
                decides=decides or None,
                run_id=run_id,
                created_at=_utcnow(),
            )
            self._insert_revision(connection, artifact_id, revision)
            self._set_head(connection, artifact_id, target, revision.revision_id)
        return revision

    def commit(self, revision_id: str) -> Revision | Conflict:
        """Fast-forward `main` onto `revision_id`, or record and return a `Conflict`.

        Fast-forward-or-conflict, never auto-merge: `commit` moves main only when the
        proposal was written against the head it is replacing, so a committed main is
        always a document some author actually read in full. Everything else is the lead's
        decision, taken through `merge` (clean) or by hand (overlapping).
        """
        revision = self.revision(revision_id)
        if revision.branch == MAIN:
            raise ArtifactError(
                f"{revision.short} is already on {MAIN!r}; there is nothing to commit"
            )
        with self._session() as connection:
            artifact_id = self._require_artifact(connection, revision.path)
            head = self._head(connection, artifact_id, revision.path, MAIN)
            head_id = head.revision_id if head is not None else None
            if revision.parent_revision == head_id:
                self._set_head(connection, artifact_id, MAIN, revision.revision_id)
                self._resolve(connection, revision.revision_id, "committed")
                return revision
        # Not a fast-forward: a sibling landed first. Built outside the session above so the
        # conflict's own row is written by one statement in its own unit of work.
        return self._conflict(revision, head)

    def merge(
        self,
        revision_id: str,
        *,
        author: str,
        rationale: str | None = None,
        run_id: str | None = None,
    ) -> Revision | Conflict:
        """Land a proven non-overlapping three-way merge on `main`, or return a `Conflict`.

        The merge revision's author is whoever merged, not the proposer: the merged text is
        a document neither side wrote and attributing it to the proposer would put words in
        an author's mouth. `merged_from` keeps the proposal on the record as the second
        parent, so the proposer's own revision is still there to read.

        A proposal that is already a fast-forward is committed rather than merged, because a
        merge revision over an unchanged head is a second row that says nothing.
        """
        revision = self.revision(revision_id)
        if revision.branch == MAIN:
            raise ArtifactError(f"{revision.short} is already on {MAIN!r}; nothing to merge")
        head = self.head(revision.path, MAIN)
        head_id = head.revision_id if head is not None else None
        if revision.parent_revision == head_id:
            return self.commit(revision.revision_id)
        if head is None:
            # Unreachable through this module's own writes (a proposal parented on something
            # implies a head existed), and stated rather than assumed because a merge with
            # no head to merge into would otherwise three-way against ""
            return self._conflict(revision, None)
        ancestor = self._ancestor(revision, head)
        if ancestor is None:
            return self._conflict(revision, head)
        merged, overlapping = three_way_merge(ancestor.content, head.content, revision.content)
        if merged is None:
            return self._conflict(revision, head, overlapping=overlapping)
        reason = rationale or (
            f"merge of {revision.short} by {revision.author} into {head.short}: the two "
            f"changed different lines"
        )
        with self._session() as connection:
            artifact_id = self._require_artifact(connection, revision.path)
            merge_revision = Revision(
                revision_id=_revision_id(
                    revision.path, head.revision_id, merged, author, MAIN, reason
                ),
                path=revision.path,
                parent_revision=head.revision_id,
                merged_from=revision.revision_id,
                content=merged,
                digest=_digest(merged),
                author=author,
                branch=MAIN,
                rationale=reason,
                # Deliberately NOT inherited from the proposal. A merge revision says the
                # same thing the proposal decided, in text neither side wrote, so copying
                # `decides` here would give `split_brain` two branches (`main` and the
                # proposer's) holding one question at two digests — a divergence the merge
                # just resolved, reported as one it caused. The provenance is `merged_from`.
                decides=None,
                run_id=run_id,
                created_at=_utcnow(),
            )
            self._insert_revision(connection, artifact_id, merge_revision)
            self._set_head(connection, artifact_id, MAIN, merge_revision.revision_id)
            self._resolve(connection, revision.revision_id, "merged")
        return merge_revision

    # ── Internals ──

    def _conflict(
        self,
        revision: Revision,
        head: Revision | None,
        *,
        overlapping: tuple[str, ...] = (),
    ) -> Conflict:
        """Build the typed conflict AND write its row, in that order, always.

        One seam for both callers (`commit`'s non-fast-forward and `merge`'s overlap) so the
        row and the returned value cannot drift apart. `mergeable` is recomputed here rather
        than passed in when the caller has not already measured the overlap, so a `Conflict`
        that says "merge_change can land this" is a statement the merge engine made.
        """
        ancestor = self._ancestor(revision, head) if head is not None else None
        base = ancestor.content if ancestor is not None else ""
        ours = head.content if head is not None else ""
        if overlapping:
            regions = overlapping
        else:
            merged, regions = three_way_merge(base, ours, revision.content)
            if merged is not None:
                regions = ()
        conflict = Conflict(
            path=revision.path,
            proposal=revision.revision_id,
            proposal_author=revision.author,
            proposal_branch=revision.branch,
            head=head.revision_id if head is not None else None,
            head_author=head.author if head is not None else None,
            ancestor=ancestor.revision_id if ancestor is not None else None,
            overlapping=tuple(regions),
            mergeable=not regions and head is not None and ancestor is not None,
            diff=_diff_summary(base, ours, revision.content),
        )
        with self._session() as connection:
            artifact_id = self._require_artifact(connection, revision.path)
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "INSERT INTO conflicts (artifact_id, proposal_revision, head_revision, "
                    "ancestor_revision, mergeable, overlapping, resolution, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        artifact_id,
                        conflict.proposal,
                        conflict.head,
                        conflict.ancestor,
                        int(conflict.mergeable),
                        json.dumps(list(conflict.overlapping)),
                        _utcnow(),
                    ),
                )
                conflict_id = int(cursor.lastrowid or 0)
            finally:
                cursor.close()
        return Conflict(**{**conflict.__dict__, "conflict_id": conflict_id})

    def _ancestor(self, revision: Revision, head: Revision) -> Revision | None:
        """The proposal's parent, but only when it is genuinely reachable from main's head.

        A proposal is parented on some earlier main head by construction, so its parent
        *is* the common ancestor — but "by construction" is the kind of claim that stops
        being true one refactor later, and a three-way merge against a base that is not a
        common ancestor produces plausible text with lines nobody wrote. So the parent chain
        from `head` (through `parent_revision` and `merged_from` both, because a merge has
        two parents) is walked, and an unreachable parent returns `None` — reported to the
        lead as a conflict with no common ancestor rather than merged optimistically.
        """
        if revision.parent_revision is None:
            return None
        with self._session() as connection:
            reachable = self._reachable(connection, head.revision_id)
        if revision.parent_revision not in reachable:
            return None
        return self.revision(revision.parent_revision)

    @staticmethod
    def _reachable(connection: sqlite3.Connection, revision_id: str) -> set[str]:
        """Every revision reachable from `revision_id` through both parent edges.

        `merged_from` is walked alongside `parent_revision` because a merge revision has two
        parents, and a walk that followed only the first would report a merged proposal as
        unreachable — which would make it read as still pending and as having no common
        ancestor, one run after the lead merged it.
        """
        seen: set[str] = set()
        frontier = [revision_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            rows = _fetch(
                connection,
                "SELECT parent_revision, merged_from FROM revisions WHERE revision_id = ?",
                (current,),
            )
            for row in rows:
                frontier.extend(parent for parent in row if parent)
        return seen

    @staticmethod
    def _artifact_id(connection: sqlite3.Connection, path: str) -> int | None:
        rows = _fetch(connection, "SELECT artifact_id FROM artifacts WHERE path = ?", (path,))
        return int(rows[0][0]) if rows else None

    def _require_artifact(self, connection: sqlite3.Connection, path: str) -> int:
        artifact_id = self._artifact_id(connection, path)
        if artifact_id is None:  # only reachable if a row vanished under a live revision
            raise ArtifactError(f"no artifact at {path!r}")
        return artifact_id

    def _ensure_artifact(self, connection: sqlite3.Connection, path: str) -> int:
        existing = self._artifact_id(connection, path)
        if existing is not None:
            return existing
        connection.execute(
            "INSERT INTO artifacts (path, created_at) VALUES (?, ?)", (path, _utcnow())
        )
        created = self._artifact_id(connection, path)
        if created is None:  # pragma: no cover — an INSERT that neither raised nor landed
            raise ArtifactError(f"artifact {path!r} did not persist")
        return created

    @staticmethod
    def _head(
        connection: sqlite3.Connection, artifact_id: int, path: str, branch: str
    ) -> Revision | None:
        rows = _fetch(
            connection,
            f"SELECT ?, {', '.join('r.' + c for c in _REVISION_COLUMNS)} "
            f"FROM refs f JOIN revisions r ON r.revision_id = f.head_revision "
            f"WHERE f.artifact_id = ? AND f.branch = ?",
            (path, artifact_id, branch),
        )
        return _revision_from(rows[0]) if rows else None

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection, artifact_id: int, revision: Revision
    ) -> None:
        """Write one revision, idempotently.

        `INSERT OR IGNORE` because the revision id *is* the content address: proposing the
        identical change against the identical parent twice is the same revision, and a
        second row would be the same document with two ids. The branch head still moves, so
        the second call is a no-op that leaves the plane exactly where the first did.
        """
        connection.execute(
            "INSERT OR IGNORE INTO revisions (revision_id, artifact_id, parent_revision, "
            "merged_from, content, digest, author, branch, rationale, decides, run_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.revision_id,
                artifact_id,
                revision.parent_revision,
                revision.merged_from,
                revision.content,
                revision.digest,
                revision.author,
                revision.branch,
                revision.rationale,
                revision.decides,
                revision.run_id,
                revision.created_at,
            ),
        )

    @staticmethod
    def _set_head(
        connection: sqlite3.Connection, artifact_id: int, branch: str, revision_id: str
    ) -> None:
        connection.execute(
            "INSERT INTO refs (artifact_id, branch, head_revision, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(artifact_id, branch) DO UPDATE SET "
            "head_revision = excluded.head_revision, updated_at = excluded.updated_at",
            (artifact_id, branch, revision_id, _utcnow()),
        )

    @staticmethod
    def _resolve(connection: sqlite3.Connection, proposal: str, resolution: str) -> None:
        """Close every open conflict row against `proposal`.

        A conflict that was later merged or committed must stop reading as open, or a report
        one run later shows a team stuck on a collision it actually resolved. The row is
        updated rather than deleted: the collision happened, and the plane's whole claim is
        that nothing about it is lost.
        """
        connection.execute(
            "UPDATE conflicts SET resolution = ? WHERE proposal_revision = ? AND "
            "resolution IS NULL",
            (resolution, proposal),
        )

    def __repr__(self) -> str:
        return f"<ArtifactStore path={self.path!r}>"


def _revision_from(row: tuple[Any, ...]) -> Revision:
    """One `(path, *_REVISION_COLUMNS)` row as a `Revision`. Every read goes through here."""
    path, *rest = row
    values = dict(zip(_REVISION_COLUMNS, rest, strict=True))
    return Revision(path=path, **values)  # type: ignore[arg-type]


# ── The split-brain probe ──


@dataclass(frozen=True)
class SplitBrain:
    """Did two branches settle the same design question differently?

    Three-valued in `detect/discrimination.py`'s style and for its reason: under a boolean,
    "the plane recorded decisions and none of them diverged" and "the plane recorded no
    decisions at all" collapse into one `False`, and a team nobody asked to declare what its
    changes decide would then read as a team that agreed.

        True   at least one question is settled differently on two branches. The finding.
        False  every recorded decision was examined and none diverged.
        None   the measurement could not be posed: nothing carried a decision.

    `withheld` is a tuple of named reasons rather than a flag, so a reader can tell "no
    member declared what its change decides" from any later reason a run might add.

    This is deliberately *not* `detect.discrimination.Discrimination`, for
    `memory.turso_backend.Discrimination`'s reason: the verdict's shape generalises, the
    measurement does not. There, `separating > 0` means the check works; here it means the
    team is split, so the polarity of every helper on that class (`idle`, `discriminates`)
    would read backwards. Reusing it would also make `team/` the first library package to
    import `detect/`, coupling two packages that today share nothing.

    Attributes:
        divergences: One `(path, question, {branch: content-digest})` per diverging
            question. The branches and their digests travel with the finding, because a
            split-brain verdict without the two branches leaves the reader re-deriving it.
        questions: How many distinct `(path, decides)` questions were recorded — the
            reference scale, not a strict denominator.
        contested: How many of those were decided on more than one branch, i.e. were ever
            in a position to diverge at all.
        decisions: How many revisions carried a `decides` at all.
        withheld: Named reasons the verdict cannot be settled.
    """

    divergences: tuple[tuple[str, str, dict[str, str]], ...] = ()
    questions: int = 0
    contested: int = 0
    decisions: int = 0
    withheld: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.questions, self.contested, self.decisions) < 0:
            raise ValueError(
                f"SplitBrain: counts must be non-negative, got questions={self.questions}, "
                f"contested={self.contested}, decisions={self.decisions}"
            )

    @property
    def diverged(self) -> bool | None:
        """Two branches decided one question differently? See the class docstring."""
        if self.divergences:
            return True
        if self.withheld:
            return None
        return False

    @property
    def settled(self) -> bool:
        """Whether the probe produced a verdict at all."""
        return self.diverged is not None

    def __str__(self) -> str:
        counted = f"{len(self.divergences)} of {self.questions} questions diverging"
        if self.diverged is None:
            return f"split-brain: UNSETTLED ({counted}); " + "; ".join(self.withheld)
        if not self.diverged:
            if not self.contested:
                return (
                    f"split-brain: NONE OBSERVED ({self.questions} questions, none decided "
                    f"on more than one branch — never in a position to diverge)"
                )
            return f"split-brain: NONE OBSERVED ({counted}, {self.contested} contested)"
        detail = "; ".join(
            f"{question!r} in {path} decided differently on {', '.join(sorted(branches))}"
            for path, question, branches in self.divergences
        )
        return f"split-brain: DIVERGENCE CONFIRMED ({counted}): {detail}"


def _question_key(decides: str) -> str:
    """Two members naming one question must land on one key, or nothing ever diverges.

    `decides` is free text a model wrote — the alternative was a closed vocabulary, refused
    because the design questions a team circles are exactly what a vocabulary fixed in
    advance cannot name. The cost is that "which store backs the plane" and "Which store
    backs the plane?" are the same question typed twice, and keying on the raw string would
    make every divergence read as two uncontested questions (measured: the first split-brain
    test found `contested == 0` with two rival documents in front of it). So the key folds
    case, collapses whitespace, and drops trailing punctuation. Nothing more: a normaliser
    that stemmed or dropped stopwords would start merging questions a team really does hold
    apart, and a false *merge* here manufactures a divergence that is not there.
    """
    return " ".join(decides.split()).casefold().rstrip("?.!:")


def split_brain(store: ArtifactStore) -> SplitBrain:
    """Probe the plane for two branches settling one design question differently.

    A probe, not enforcement. Nothing here refuses a proposal, closes a branch, or grades a
    run: a team may legitimately hold two answers to one question for a while, and the whole
    value of recording it is that the lead can see it *before* an answer ships built on both.
    Divergence is measured on `(path, decides)` and read off content digests, so the same
    decision reached twice in the same words is agreement and a decision reached differently
    is the finding — the identity `Revision.digest` exists for.

    A question decided on only one branch counts as an examined question but cannot diverge,
    and `contested` reports how many were ever in a position to. That emptiness belongs to
    the subject rather than to this function's own bound, so it is a finding (`False`), not
    an abstention — `detect/discrimination.py` states the rule and the one case that inverts
    it.

    A divergence the lead already merged still reports, and that is the intended reading
    rather than staleness: `merge` lands two *line-disjoint* edits, which resolves the
    document and says nothing about the question. Two members who answered "which store" in
    different paragraphs produce a clean merge whose text now asserts both answers, and that
    is precisely the state worth surfacing. Only the merge revision's own `decides` is
    dropped (see `merge`), so `main` is never counted as a third voice on a question it only
    integrated.
    """
    decided = [r for r in store.revisions() if r.decides]
    if not decided:
        return SplitBrain(
            withheld=(
                "no revision recorded what it decides, so no question could be compared "
                "across branches",
            )
        )
    questions: dict[tuple[str, str], dict[str, str]] = {}
    for revision in decided:
        assert revision.decides is not None  # narrowed by the filter above
        key = (revision.path, _question_key(revision.decides))
        # Branch → the digest of what that branch last said about this question. A branch
        # that revised its own answer is one voice, not two.
        questions.setdefault(key, {})[revision.branch] = revision.digest
    divergences = tuple(
        (path, question, dict(branches))
        for (path, question), branches in questions.items()
        if len(branches) > 1 and len(set(branches.values())) > 1
    )
    return SplitBrain(
        divergences=divergences,
        questions=len(questions),
        contested=sum(1 for branches in questions.values() if len(branches) > 1),
        decisions=len(decided),
    )
