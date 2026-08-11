"""`Squad` — a whole `Team` as a single `Recruit`, so teams nest.

This is how multi-loop composition stays out of the core: `core.Team` never learns about
other teams, and "no cross-team messaging" holds *by construction* because the inner team is
reachable only as the outer lead's typed tool. The outer lead calls the squad the way it
calls any member — one `request: str`, one answer back — and everything behind that wire
(the inner lead, its members, its hooks, its answer loop) is the inner team's business. No
new verbs, no new channel, no registry of teams anywhere.

**Each ask is one full team run.** `ask` calls `Team.run` from scratch: inner members spawn
fresh, hooks fire, the teardown `finally` retires everybody. So a `Squad` is *stateless
across asks* — unlike a `Member`, whose live `MethodThread` accrues history across the
lead's calls to it, a squad remembers nothing between questions. Memory across asks is the
outer lead's context (it saw both answers), never the squad's. That is a deliberate
tradeoff: statelessness is what makes the adapter this small, and a caller who wants
continuity holds the outer lead, not the squad. The most recent run's audit trail survives
as `last_run` for callers (an Expedition, a test) that want `hooks_data` or the transcript.

**Asks are serialized with an `asyncio.Lock`.** Two concurrent `Team.run` calls on one
`Team` instance are unsafe: hooks carry per-run mutable state keyed by workspace identity,
and two live workspaces over one hook list would cross their streams. The lock makes an
outer lead that fires two squad calls in one cycle safe at the cost of running them in
sequence.

**A squad has no inbound side channel — documented fact, not oversight.** The worklog's
`_notify_of` walks `("thread", "handle")` looking for a live handle to deliver lateral
discoveries to, and gives up quietly on anything else; `Squad` deliberately exposes neither
attribute (enforced by `__slots__`), because there is no thread to deliver to between asks
and no single thread during one. A nested team that wants a worklog carries its own, inside.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .core import Team, TeamRun

__all__ = ["Squad"]


class _SquadHandle:
    """The minimal spawn handle: the skeleton reads only `.id` (the `Recruit` contract)."""

    __slots__ = ("id",)

    def __init__(self, ident: str) -> None:
        self.id = ident


class Squad:
    """A `Team` adapted to the `Recruit` protocol: name, spawn, ask, retire.

    Args:
        team: The inner team. Its lead answers every `ask`; its whole cast spawns and
            retires per ask.
        name: Required and explicit, because a `Team` has no name of its own and the outer
            team's duplicate-member guard (`core._check_no_duplicate_member_names`) keys on
            `.name` — an implicit name would collide invisibly.

    `spawn` performs no work beyond remembering the coordinator and parent for later asks:
    no thread exists until a question arrives, so the handle it returns carries a synthetic
    id (`squad:{name}`). The real threads appear per ask — the inner team's lead spawns as a
    *child of the outer lead* because `parent_id` chains through `Team.run`, so the whole
    nested run lands as one subtree in one event log.
    """

    __slots__ = ("team", "name", "last_run", "_coordinator", "_parent_id", "_spawned", "_lock")

    def __init__(self, team: Team, name: str) -> None:
        self.team = team
        self.name = name
        self.last_run: TeamRun | None = None
        self._coordinator: Any = None
        self._parent_id: Any = None
        self._spawned = False
        self._lock = asyncio.Lock()

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        self._coordinator = coordinator
        self._parent_id = parent_id
        self._spawned = True
        return _SquadHandle(f"squad:{self.name}")

    async def ask(self, request: str) -> Any:
        """One full inner-team run for `request`; the inner lead's answer comes back.

        Serialized: see the module docstring for why two concurrent runs of one `Team`
        instance are unsafe. The completed `TeamRun` is kept on `last_run`.
        """
        if not self._spawned:
            raise RuntimeError(
                f"{self.name}: not spawned (or already retired); await "
                f"spawn(coordinator, parent_id=...) before ask() — a team does this for its "
                f"members automatically, so a direct caller must too"
            )
        async with self._lock:
            run = await self.team.run(request, self._coordinator, parent_id=self._parent_id)
            self.last_run = run
            return run.answer

    async def retire(self) -> None:
        """Idempotent no-op beyond forgetting the coordinator and parent.

        Each ask already tore its own run down (`Team.run`'s unconditional `finally`), so
        there is no thread to kill here — only the stored wiring to clear, so a retired
        squad refuses further asks instead of running against a dead coordinator.
        `last_run` survives retirement: the audit trail outlives the wiring.
        """
        self._coordinator = None
        self._parent_id = None
        self._spawned = False

    def __repr__(self) -> str:
        return f"<Squad {self.name!r} team={self.team!r}>"
