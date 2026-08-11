"""`Worklog`: typed discoveries members post, fanned back to everyone else at step boundaries.

The legacy worklog as a hook. `tools_for_member` gives every member a `post_discovery` tool
whose `source` is wired to the member's name — attribution an audit can trust is attribution
the model cannot spoof. A post appends to the durable log (`hooks_data["worklog"]`) and fans
the rendered text to every *other* registered channel through `notify`, which appends to a
thread's log without starting a cycle — so a teammate sees the discovery at its own next
model call and is never interrupted mid-thought. Evidence for the feature: AgentRadio
(arXiv 2607.28430) measured passive awareness at +10.5 points net, concentrated on
cross-cutting tasks — and a team's members hold disjoint evidence by design.

**`post` reserves before it awaits** (the hiring seam's lesson): the entry is appended in
the same synchronous stretch that builds it, and only then is any `send` awaited — the tool
executor is concurrent (`strands/agent/agent.py:462`), so two posts in one assistant turn
interleave, and an append on the far side of an await could drop one. **The fan-out itself
is concurrent** (`asyncio.gather`): the deliveries run side by side, so one slow channel no
longer holds the posting member's tool return behind every other teammate's. **One channel
failing never stops the rest**: each delivery runs under its own handler (`_deliver` records
and swallows, so gather never sees an exception) and a dead teammate becomes `failed[name]`
on the entry rather than a run-ending fault. A consequence of the concurrency: `delivered`
is a set-like membership record — names land in completion order, not registration order,
and the order carries no meaning.

**Registration replays.** A channel opened late — the lead's, a hire's — receives every
prior entry on registration, so a discovery posted during the briefing phase reaches the
lead's *first* context and a helper hired *because* of an obstacle is not the one teammate
who never heard of it. `on_hire` is the seam the `Hiring` hook calls for exactly that.

Per-run state is per run: the entries list lives on the run's own `Workspace.data`, and the
channel map resets whenever this hook sees a new workspace — one hook instance on a `Team`
that runs twice must not replay run 1's discoveries into run 2's threads
(`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ai_functions.types import CustomEvent, ThreadContext
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from ..core import Workspace
from ..members import Recruit

__all__ = ["DISCOVERY_KINDS", "Worklog"]

DISCOVERY_KINDS = ("bears-on-teammate", "contradicts-plan", "obstacle", "dead-end")
"""The four things worth interrupting nobody about.

A closed vocabulary rather than free text, which is the worklog's whole difference from a
chat channel: typed payloads are this library's standing bet everywhere free text was the
alternative (`method.py`'s header). A kind the model invents is refused as text, so the
model picks a real one and posts again.
"""


class Worklog:
    """The discovery log and its fan-out, as one hook.

    No constructor arguments: what a worklog is — the vocabulary, the rendering, the
    delivery discipline — is the feature, and a team that wants it differently subclasses.
    """

    def __init__(self) -> None:
        self._run: Workspace | None = None
        self._channels: dict[str, Callable[[str], Awaitable[None]]] = {}

    # ── Per-run state ──

    def _entries(self, work: Workspace) -> list[dict[str, Any]]:
        """The durable record, on the run's own workspace — published as
        `hooks_data["worklog"]` and created lazily because a member may post during another
        hook's `on_assemble`, before this hook's own has run."""
        self._reset_if_new_run(work)
        return work.data.setdefault("worklog", [])

    def _reset_if_new_run(self, work: Workspace) -> None:
        """A new workspace is a new run: drop the old channel map before anything uses it.

        Compared by identity because the workspace *is* the run. Without this, a post early
        in run 2 would fan into run 1's retired threads and record their predictable
        failures on run 2's log.
        """
        if self._run is not work:
            self._run = work
            self._channels = {}

    # ── The rendering (one seam, two readers: fan-out and replay) ──

    def render(self, entry: Mapping[str, Any]) -> str:
        """The text a teammate reads: attributed, kind first, one block."""
        return (
            f"[team worklog] {entry['source']} flagged {entry['kind']}: {entry['body']}\n"
            f"This is awareness, not an instruction — weigh it against what you alone know."
        )

    # ── Channels ──

    async def register(
        self, work: Workspace, name: str, send: Callable[[str], Awaitable[None]]
    ) -> None:
        """Open a channel, and replay every prior entry into it.

        The replay is what makes registration order not matter: whoever joins late reads
        what the team already flagged, delivered exactly as a live post would be — the
        entry's `delivered`/`failed` record does not distinguish the two, because both
        answer "who saw this".
        """
        self._reset_if_new_run(work)
        self._channels[name] = send
        for entry in list(self._entries(work)):
            if entry["source"] == name or name in entry["delivered"] or name in entry["failed"]:
                continue
            await self._deliver(entry, name, send)

    async def post(self, work: Workspace, kind: str, body: str, source: str) -> dict[str, Any]:
        """Append one discovery and fan it to every channel except the poster's own.

        The poster is excluded because it already knows: an echo would spend a slot in its
        next context restating what it just said. The append happens before any await — see
        the module header.
        """
        entry: dict[str, Any] = {
            "kind": kind,
            "body": body,
            "source": source,
            "delivered": [],
            "failed": {},
        }
        self._entries(work).append(entry)  # reserved before any await
        await asyncio.gather(
            *(
                self._deliver(entry, name, send)
                for name, send in list(self._channels.items())
                if name != source
            )
        )
        return entry

    async def _deliver(
        self, entry: dict[str, Any], name: str, send: Callable[[str], Awaitable[None]]
    ) -> None:
        try:
            await send(self.render(entry))
        except Exception as error:  # noqa: BLE001 — one dead teammate must not stop the rest
            entry["failed"][name] = repr(error)
        else:
            entry["delivered"].append(name)

    # ── The hook surface ──

    async def on_assemble(self, work: Workspace) -> None:
        """Open a channel per member that can receive, and the lead's.

        The lead's handle exists before any hook runs (`Workspace.lead`'s contract), and
        its thread has not cycled yet — so registering here, with replay, is what lands a
        briefing-time discovery in the lead's *first* model context. A member without a
        reachable `notify` (a test spy, a foreign recruit shape) simply gets no channel: a
        member that cannot receive is a fact, not a fault.
        """
        self._reset_if_new_run(work)
        for member in work.members:
            notify = _notify_of(member)
            if notify is not None:
                await self.register(work, member.name, notify)
        lead_notify = getattr(work.lead, "notify", None)
        if callable(lead_notify):
            await self.register(work, "lead", lead_notify)

    def tools_for_member(
        self, work: Workspace, member: Recruit, ctx: ThreadContext
    ) -> list[AgentTool]:
        """One `post_discovery` per member cycle, the poster bound by the wire.

        `member.name` is bound here rather than taken as a tool parameter, so the `source`
        on every entry is the name the team wired and not a name the model chose.
        """
        return [self._discovery_tool(work, ctx, member.name)]

    # ── The seams the Hiring hook calls (per-hook convention, not core surface) ──

    async def on_hire(self, work: Workspace, recruit: Recruit, handle: Any) -> None:
        """A hire joins the fan-out: replay first, so it knows what the team flagged."""
        notify = getattr(handle, "notify", None)
        if callable(notify):
            await self.register(work, recruit.name, notify)

    def on_dismiss(self, work: Workspace, recruit: Recruit) -> None:
        """A dismissed hire's channel closes with its thread, so later posts do not record
        a predictable failure against a teammate the team already agreed is gone."""
        self._reset_if_new_run(work)
        self._channels.pop(recruit.name, None)

    # ── The tool ──

    def _discovery_tool(self, work: Workspace, ctx: ThreadContext, poster: str) -> AgentTool:
        """Failures are text: a wrong kind is a mistake the model can fix, so it reads the
        refusal — which rides back as a successful tool result — and posts again."""
        kinds = ", ".join(DISCOVERY_KINDS)
        worklog = self

        @strands_tool(
            name="post_discovery",
            description=(
                "Flag a discovery your teammates should see at their next step. Use it when "
                "you find something that bears on a teammate's work, contradicts the current "
                "plan, is an obstacle, or marks a dead end nobody should re-explore. Your "
                "teammates read it as context, not as an interruption. kind must be one of: "
                f"{kinds}."
            ),
        )
        async def post_discovery(kind: str, body: str) -> str:
            if kind not in DISCOVERY_KINDS:
                return f"error: no such kind {kind!r}; pick one of: {kinds}"
            entry = await worklog.post(work, kind, body, poster)
            ctx.on_event(
                CustomEvent(
                    kind="team.discovery",
                    payload={
                        "source": poster,
                        "discovery": kind,
                        "delivered": list(entry["delivered"]),
                        "failed": sorted(entry["failed"]),
                    },
                )
            )
            reached = ", ".join(entry["delivered"]) or "nobody yet"
            return f"posted {kind}; reached {reached}"

        return post_discovery


def _notify_of(member: Recruit) -> Callable[[str], Awaitable[None]] | None:
    """A member's inbound side channel, duck-typed off whichever handle shape it holds.

    `Recruit` guarantees three verbs and a handle is not one of them, so this walks the two
    shapes the library ships — `Member.thread` (a `MethodThread`) and the demo `Agent`'s
    `handle` — and gives up quietly on anything else. Both properties raise when unspawned;
    at `on_assemble` time the cast is live, and a raise here still just means "no channel".
    """
    for attribute in ("thread", "handle"):
        try:
            handle = getattr(member, attribute)
        except AttributeError, RuntimeError:
            continue
        notify = getattr(handle, "notify", None)
        if callable(notify):
            return notify
    return None
