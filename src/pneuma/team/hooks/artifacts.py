"""`Artifacts`: members propose changes to shared documents; only the lead lands them.

The worklog gave members a way to *tell* each other something. This hook gives them a way to
*change* something together. `tools_for_member` grants `read_artifact` and `propose_change`;
`tools_for_lead` grants `read_artifact`, `list_proposals`, `commit_change` and `merge_change`.
The author on every proposal is bound here to the member's name — attribution an audit can
trust is attribution the model cannot spoof, the `worklog.py` rule applied to writes, where
it matters more: a worklog entry with the wrong source misleads a reader, a revision with the
wrong author sends the lead to the wrong agent to resolve a collision.

**The lead holds sole commit authority**, and that is the asymmetry the hook exists to
express. A member's proposal lands on a branch named after it and changes nothing anyone else
reads; `main` moves only when the lead commits (fast-forward) or merges (proven
non-overlapping). Rejected alternative — every member commits to `main` — is recorded in
`docs/design/artifacts.md`: the plane then has no integration point, last-writer-wins becomes
its semantics rather than a bug, and there is nowhere for a conflict to be *seen*.

**Collisions are text the lead can act on, never a silent overwrite.** `commit_change` on a
proposal whose parent is no longer main's head returns the rendered `Conflict`: both
revisions, their common ancestor, a bounded three-way diff, and the two moves actually
available. Cursor's measured failure for agent swarms without a VCS is that agents in
conflict overwrite each other or abandon the work; a conflict rendered with no next move
produces the second, so `Conflict.__str__` always ends in one.

**Every failure a model can fix is text** (`hiring.py`'s rule): an unknown path, a blank
rationale, a stale proposal all ride back as `"error: ..."` strings inside a *successful*
tool result, so the model reads the problem and retries. `ArtifactError` is caught at the
tool boundary; `sqlite3.Error` is not, because a full disk is not a mistake the model made
and an artifact plane that renders it as advice has silently stopped persisting.

**Per-run bookkeeping is per run, the store is not.** `hooks_data["artifacts"]` is a fresh
list on each new `Workspace` (compared by identity — the workspace *is* the run, the
`worklog.py` / `trajectory.py` pattern), so run 2's report never carries run 1's proposals.
The `ArtifactStore` deliberately survives: a file-backed plane outliving the run is the whole
point of versioning the artifact rather than the conversation, and a store reset per run
would make every commit a fast-forward over an empty document.
"""

from __future__ import annotations

from typing import Any

from ai_functions.types import CustomEvent, ThreadContext
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from ..artifacts import MAIN, ArtifactError, ArtifactStore, Conflict, Revision
from ..core import Workspace
from ..members import Recruit

__all__ = ["Artifacts"]


class Artifacts:
    """The artifact plane as a hook: propose on your own branch, the lead lands it.

    Args:
        store: The plane. Defaults to a fresh in-memory `ArtifactStore` — an offline test
            and a single-run script both want a store that needs no filesystem, and a caller
            wanting durability passes `ArtifactStore(path)`. Shared deliberately across runs
            of one hook instance (see the module header).
        run_id: Stamped onto every revision this hook writes, or `None`. The join back to
            `Trajectory`'s rows: given both planes, a reader can ask which run produced a
            revision without either plane knowing about the other.
        seed: `{path: content}` written to `main` before the lead's first cycle, authored by
            `origin`. The team's starting document — without it every member's first
            proposal creates a rival new artifact and there is nothing to collide over.
            Written only when the path has no revision yet, so a file-backed store's second
            run does not overwrite what the first one landed.
        origin: The author recorded for seeded revisions. Not a member name, so a reader can
            tell "this is where the document started" from "a member wrote this".
    """

    def __init__(
        self,
        store: ArtifactStore | None = None,
        *,
        run_id: str | None = None,
        seed: dict[str, str] | None = None,
        origin: str = "origin",
    ) -> None:
        self.store = store if store is not None else ArtifactStore()
        self.run_id = run_id
        self.seed = dict(seed or {})
        self.origin = origin
        self._run: Workspace | None = None

    # ── Per-run state ──

    def log(self, work: Workspace) -> list[dict[str, Any]]:
        """This run's record, published as `hooks_data["artifacts"]`, in order.

        Created lazily as well as in `on_assemble`, because a member may propose during
        another hook's `on_assemble` — before this hook's own has run — and a proposal
        missing from the report is exactly the lost write the plane promises never to have.
        """
        self._reset_if_new_run(work)
        return work.data.setdefault("artifacts", [])

    def _reset_if_new_run(self, work: Workspace) -> None:
        """A new workspace is a new run: republish an empty log before anything appends.

        Compared by identity because the workspace *is* the run. The list lives on
        `work.data`, so the reset is really about the key existing early; the guard is kept
        explicit anyway, because the next field added to this hook will be per-run and the
        pattern is what makes that safe (`worklog.py`, `trajectory.py`, `hiring.py`).
        """
        if self._run is not work:
            self._run = work
            work.data["artifacts"] = []

    def _record(self, work: Workspace, action: str, **fields: Any) -> None:
        self.log(work).append({"action": action, **fields})

    # ── The hook surface ──

    def on_assemble(self, work: Workspace) -> None:
        """Publish the log and write the seed — before the lead's first cycle.

        Seeding here rather than in `__init__` so the seeded revisions land on the run's own
        record, and so a hook constructed once and run twice does not re-seed over a
        document its first run changed (each path is written only when it has no revision).
        """
        self._reset_if_new_run(work)
        for path, content in self.seed.items():
            if self.store.head(path, MAIN) is not None:
                continue
            revision = self.store.propose(
                path,
                content,
                author=self.origin,
                rationale="the document as the team received it",
                branch=f"{self.origin}-seed",
                run_id=self.run_id,
            )
            landed = self.store.commit(revision.revision_id)
            if isinstance(landed, Conflict):  # pragma: no cover — no head, so always a FF
                raise RuntimeError(f"seeding {path!r} conflicted with an empty plane: {landed}")
            self._record(work, "seed", path=path, revision=landed.revision_id, digest=landed.digest)

    def tools_for_member(
        self, work: Workspace, member: Recruit, ctx: ThreadContext
    ) -> list[AgentTool]:
        """Read plus propose, with the author bound to this member's name.

        Bound here rather than taken as a tool parameter for the same reason `post_discovery`
        binds `source`: the plane's attribution has to be the team's wiring, not the model's
        claim. No commit tool: a member that could land its own change would make the lead's
        authority advisory, and the branch a proposal sits on would mean nothing.
        """
        return [self._read_tool(work, member.name), self._propose_tool(work, ctx, member.name)]

    def tools_for_lead(self, work: Workspace, ctx: ThreadContext) -> list[AgentTool]:
        """Read, see the pending proposals, and land them — commit or merge.

        No `propose_change` for the lead: the lead's writes are integrations of what members
        proposed, and a lead that could also propose would have a branch of its own to
        commit from, which is `main` moving by one party's decision through two doors.
        """
        return [
            self._read_tool(work, "lead"),
            self._proposals_tool(work),
            self._commit_tool(work, ctx),
            self._merge_tool(work, ctx),
        ]

    # ── The tools ──

    def _read_tool(self, work: Workspace, reader: str) -> AgentTool:
        """`read_artifact`, shared by members and the lead — one seam, one rendering.

        Reads `main` by default and any branch by name, because the lead resolving a
        collision needs to see the proposal's side as a document and not only as a diff.
        """
        store = self.store

        @strands_tool(
            name="read_artifact",
            description=(
                "Read one of the team's shared documents at its current state. Pass the "
                "document's path. Read before you propose: your proposal is recorded against "
                "the version you read, and the team can see when two changes were written "
                "against the same version. Pass branch to read a teammate's pending proposal "
                f"instead of the agreed version ({MAIN})."
            ),
        )
        async def read_artifact(path: str, branch: str = MAIN) -> str:
            try:
                content = store.read(path, branch)
            except ArtifactError as error:
                return f"error: {error}"
            head = store.head(path, branch)
            if head is None:
                return (
                    f"{path} on branch {branch!r} has no revision yet; it is empty. Propose "
                    f"its first version if that is the work."
                )
            self._record(
                work, "read", path=path, branch=branch, reader=reader, revision=head.revision_id
            )
            return f"{path} at revision {head.short} (by {head.author}):\n{content}"

        return read_artifact

    def _propose_tool(self, work: Workspace, ctx: ThreadContext, author: str) -> AgentTool:
        """`propose_change`, the author bound by the wire.

        `decides` is offered as an optional parameter and never required: a member forced to
        name the design question its change settles would invent one, and `split_brain`
        reading invented questions would report divergences nobody has. Left unset, the probe
        says it could not tell — which is the honest answer.
        """
        store = self.store
        run_id = self.run_id

        @strands_tool(
            name="propose_change",
            description=(
                "Propose a change to one of the team's shared documents. Pass the path, the "
                "COMPLETE new content of the document (not a patch, not an excerpt — "
                "whatever you send becomes the whole document), and a one-sentence rationale "
                "saying why the change is right. Your proposal goes on your own branch and "
                "changes nothing anyone else reads until your lead commits it, so proposing "
                "is safe and cannot destroy a teammate's work. If this change settles a "
                "design question the team has been circling, say which one in decides — that "
                "is how the team notices two people answering it differently."
            ),
        )
        async def propose_change(
            path: str, new_content: str, rationale: str, decides: str = ""
        ) -> str:
            try:
                revision = store.propose(
                    path,
                    new_content,
                    author=author,
                    rationale=rationale,
                    decides=decides or None,
                    run_id=run_id,
                )
            except ArtifactError as error:
                return f"error: {error}"
            self._record(
                work,
                "propose",
                path=path,
                author=author,
                branch=revision.branch,
                revision=revision.revision_id,
                parent=revision.parent_revision,
                rationale=rationale,
                decides=revision.decides,
                digest=revision.digest,
            )
            ctx.on_event(
                CustomEvent(
                    kind="team.artifact_proposed",
                    payload={
                        "path": path,
                        "author": author,
                        "branch": revision.branch,
                        "revision": revision.short,
                    },
                )
            )
            return (
                f"proposed {revision.short} on branch {revision.branch!r} for {path}. Your "
                f"lead decides whether it lands; tell it the revision id {revision.short}."
            )

        return propose_change

    def _proposals_tool(self, work: Workspace) -> AgentTool:
        """`list_proposals`: the lead's inbox, one line per pending branch head.

        Rendered rather than returned as data because the lead is a model: the id it has to
        type back, who wrote it, and why, in the order they arrived.
        """
        store = self.store

        @strands_tool(
            name="list_proposals",
            description=(
                "List every change your team members have proposed for one document and are "
                "waiting on you to land. Pass the document's path. Each line gives the "
                "revision id you pass to commit_change or merge_change."
            ),
        )
        async def list_proposals(path: str) -> str:
            try:
                store.read(path)  # raises on an unknown path, so a typo is not "no proposals"
            except ArtifactError as error:
                return f"error: {error}"
            pending = store.proposals(path)
            self._record(
                work, "list_proposals", path=path, pending=[r.revision_id for r in pending]
            )
            if not pending:
                return f"no member has proposed a change to {path} yet"
            head = store.head(path)
            agreed = f"{head.short} by {head.author}" if head is not None else "(none yet)"
            lines = [f"{path}: agreed version is {agreed}. Pending proposals:"]
            lines.extend(
                f"  {r.short} by {r.author} on branch {r.branch!r} — {r.rationale}"
                + (f" [decides: {r.decides}]" if r.decides else "")
                + (
                    ""
                    if head is None or r.parent_revision == head.revision_id
                    else " (written against an older version — merge_change, not commit_change)"
                )
                for r in pending
            )
            return "\n".join(lines)

        return list_proposals

    def _commit_tool(self, work: Workspace, ctx: ThreadContext) -> AgentTool:
        """`commit_change`: fast-forward `main`, or hand the lead the whole collision."""
        store = self.store

        @strands_tool(
            name="commit_change",
            description=(
                "Land a member's proposed change as the team's agreed version of a document. "
                "Pass the document's path and the revision id from list_proposals. This "
                "works when the proposal was written against the version that is still "
                "agreed. If a teammate's change landed first, you get the collision back in "
                "full — both versions, what each changed, and whether merge_change can land "
                "this one without losing either edit. Nothing is ever overwritten silently."
            ),
        )
        async def commit_change(path: str, revision_id: str) -> str:
            try:
                revision = store.revision(revision_id)
            except ArtifactError as error:
                return f"error: {error}"
            if revision.path != path:
                return (
                    f"error: revision {revision.short} belongs to {revision.path}, not "
                    f"{path!r}; commit it against its own document or pick another revision"
                )
            try:
                outcome = store.commit(revision.revision_id)
            except ArtifactError as error:
                return f"error: {error}"
            return self._render_outcome(work, ctx, outcome, verb="commit")

        return commit_change

    def _merge_tool(self, work: Workspace, ctx: ThreadContext) -> AgentTool:
        """`merge_change`: land a proven non-overlapping merge, refuse an overlapping one.

        The merge's author is the lead's own name on the wire — the merged text is a document
        neither side wrote, and attributing it to the proposer would put words in an author's
        mouth. `Revision.merged_from` keeps the proposal readable as the second parent.
        """
        store = self.store
        run_id = self.run_id

        @strands_tool(
            name="merge_change",
            description=(
                "Land a member's proposal that was written against an older version, by "
                "combining it with the changes that landed since. Pass the document's path "
                "and the revision id. This succeeds only when the two sides changed "
                "different parts of the document; when they changed the same lines you get "
                "the overlap back and you must decide which change the team keeps — no "
                "automatic merge will choose for you, because the author whose edit "
                "disappeared is the one who knew why it was there."
            ),
        )
        async def merge_change(path: str, revision_id: str) -> str:
            try:
                revision = store.revision(revision_id)
            except ArtifactError as error:
                return f"error: {error}"
            if revision.path != path:
                return (
                    f"error: revision {revision.short} belongs to {revision.path}, not "
                    f"{path!r}; merge it against its own document or pick another revision"
                )
            try:
                outcome = store.merge(
                    revision.revision_id, author=self._lead_name(work), run_id=run_id
                )
            except ArtifactError as error:
                return f"error: {error}"
            return self._render_outcome(work, ctx, outcome, verb="merge")

        return merge_change

    # ── Rendering, shared by both landing tools ──

    def _render_outcome(
        self,
        work: Workspace,
        ctx: ThreadContext,
        outcome: Revision | Conflict,
        *,
        verb: str,
    ) -> str:
        """One seam for both landing tools, so the record and the text cannot drift.

        A `Conflict` comes back as `Conflict.__str__` verbatim rather than as `"error: ..."`,
        deliberately: it is not the lead's mistake, it is a fact about the plane the lead now
        has to decide about, and prefixing it as an error would invite the model to retry the
        same commit rather than read the diff.
        """
        if isinstance(outcome, Conflict):
            self._record(
                work,
                "conflict",
                path=outcome.path,
                proposal=outcome.proposal,
                head=outcome.head,
                ancestor=outcome.ancestor,
                mergeable=outcome.mergeable,
                overlapping=list(outcome.overlapping),
                conflict_id=outcome.conflict_id,
                attempted=verb,
            )
            ctx.on_event(
                CustomEvent(
                    kind="team.artifact_conflict",
                    payload={
                        "path": outcome.path,
                        "proposal": outcome.proposal[:12],
                        "mergeable": outcome.mergeable,
                        "attempted": verb,
                    },
                )
            )
            return str(outcome)
        self._record(
            work,
            verb,
            path=outcome.path,
            revision=outcome.revision_id,
            author=outcome.author,
            merged_from=outcome.merged_from,
            digest=outcome.digest,
            decides=outcome.decides,
        )
        ctx.on_event(
            CustomEvent(
                kind="team.artifact_committed",
                payload={
                    "path": outcome.path,
                    "revision": outcome.short,
                    "author": outcome.author,
                    "how": verb,
                },
            )
        )
        landed = "merged into" if outcome.merged_from else "committed to"
        return (
            f"{landed} {MAIN}: {outcome.path} is now at revision {outcome.short}. Every "
            f"member reading {outcome.path} from here on sees this version."
        )

    @staticmethod
    def _lead_name(work: Workspace) -> str:
        """The lead's name for a merge revision's author, degrading to a literal.

        `work.team.lead` is an `AIFunction` and carries `.name`; a `Workspace` built by a
        test around a stub team may carry neither, and a merge that raised because the
        *attribution string* was unavailable would be this hook failing at the one job it
        does have — so the fallback is a name a reader can still act on.
        """
        lead = getattr(getattr(work, "team", None), "lead", None)
        return str(getattr(lead, "name", None) or "lead")

    def __repr__(self) -> str:
        return f"<Artifacts store={self.store.path!r} seeded={sorted(self.seed)!r}>"
