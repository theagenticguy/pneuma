"""`Hiring`: the lead may create subagents — budgeted, audited, and always torn down.

Two layers, deliberately. `hiring_tools(roster, catalog, ...)` is the functional seam the
legacy module shipped: it builds a `config_hook` granting `hire`/`delegate`/`dismiss`
(+`hire_dynamic` when a synthesis factory is supplied) over a `Roster`, and it composes
outside any team — `demo/staffing.py` binds it straight onto a lead's own hook. The `Hiring`
class is that seam as a `TeamHook`: `tools_for_lead` rebuilds the tools each cycle, the
roster lives for one run, every hire is equipped with the sibling hooks' member tools before
it spawns and announced to them after (`on_hire`/`on_dismiss` — the worklog's channel seam),
and `on_teardown` retires whatever the lead never dismissed.

**A hire reserves its name and its headcount before it awaits anything.** The refusals and
the registration into `roster.hires` run in one synchronous stretch, and only then is the
spawn awaited — rolled back if it raises. Measured reason: the runtime's default tool
executor is concurrent (`strands/agent/agent.py:462`), so two `hire` calls in one assistant
turn interleave at the first genuine suspension; with the registration on the far side of
the await, both pass the cap and the second overwrite leaks a live thread nothing can reach
(`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).

**Every hire-side failure is text**, because a tool returning `"error: ..."` reaches the
model as a successful tool result whose content is that string — the model reads the problem
and fixes it. An exception would surface as a tool fault the model cannot act on.

**Dynamic hires are audited verbatim.** `hire_dynamic` records the instructions exactly as
written — nobody reviewed this prompt, so the audit trail is the safety story, and a
truncated log entry would be an audit of a different agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.types import CustomEvent, ThreadContext, ThreadId
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from ..core import Workspace
from ..members import DynamicAgent, Member, Recruit

__all__ = ["Hiring", "Roster", "hiring_tools"]


@dataclass
class Roster:
    """Who a team hired, keyed by the name the model chose, plus the evidence it was
    budgeted. The `log` is what `hooks_data["hiring"]` publishes and what a reader audits —
    every hire, delegation, dismissal and delegation *failure*, in order. Moved verbatim
    from the legacy module; `demo/staffing.Staff` still narrows it for its mandate hook."""

    hires: dict[str, Recruit] = field(default_factory=dict)
    thread_ids: dict[str, ThreadId] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def record(self, action: str, **fields: Any) -> None:
        self.log.append({"action": action, **fields})

    @property
    def headcount(self) -> int:
        return len(self.hires)


def hiring_tools(
    roster: Roster,
    catalog: Mapping[str, Callable[[str], Recruit]],
    *,
    max_hires: int = 4,
    equip: Callable[[Recruit], None] | None = None,
    hired: Callable[[Recruit, Any], Awaitable[None]] | None = None,
    dismissed: Callable[[Recruit], None] | None = None,
    dynamic: Callable[[str, str], Recruit] | None = None,
) -> Callable[[ThreadContext], ThreadKwargs]:
    """Build a `config_hook` granting hire/delegate/dismiss for one cycle.

    The runtime injects `list_threads`/`send_message` into every thread, so an agent can
    reach a thread that already exists — it cannot create one. `ThreadConfig` documents
    `config_hook` as the place to inject spawn-shaped tools (`config.py:166-185`) and
    nothing upstream ships them. This is that place, roster-agnostically.

    Args:
        roster: Where hires and the action log land. The caller keeps the reference, so a
            report outlives the lead's thread. Per run — the `Hiring` hook stands up a
            fresh one per workspace.
        catalog: Role name → factory, called as `factory(name)`. A plain mapping the caller
            supplies: what a team may hire is a property of that team.
        max_hires: Headcount cap. The runtime enforces no breadth limit of its own.
        equip: Called with each recruit *before* its spawn — the `Hiring` hook folds the
            sibling hooks' `tools_for_member` in here, so a hire carries the same worklog
            tool a cast member does.
        hired: Awaited with `(recruit, handle)` after a successful spawn — the seam the
            worklog's replayed channel rides in on.
        dismissed: Called after a successful dismissal, for the channel's closing.
        dynamic: Name + instructions → recruit, or `None` (the default and the
            recommendation). When supplied, `hire_dynamic` joins the wire: the lead writes
            a new subagent's instructions itself. Every hire discipline is shared — one
            budget, one reservation, one `delegate`/`dismiss` — because both kinds live in
            one roster; only the log's `action` distinguishes a reviewed-catalog hire from
            a synthesized one.

    Returns:
        A `config_hook` rebuilding the tools against each cycle's `ThreadContext` — which
        is what makes `parent_id=ctx.thread_id` correct: the hiring agent is the recorded
        parent, so cost attribution up the tree (`subtree_usage` walks `THREAD_SPAWNED`
        edges) is a free consequence of hiring correctly.
    """

    def hook(ctx: ThreadContext) -> ThreadKwargs:
        return {
            "tools": list(
                _hiring(ctx, roster, catalog, max_hires, equip, hired, dismissed, dynamic)
            )
        }

    return hook


def _hiring(
    ctx: ThreadContext,
    roster: Roster,
    catalog: Mapping[str, Callable[[str], Recruit]],
    max_hires: int,
    equip: Callable[[Recruit], None] | None,
    hired: Callable[[Recruit, Any], Awaitable[None]] | None,
    dismissed: Callable[[Recruit], None] | None,
    dynamic: Callable[[str, str], Recruit] | None,
) -> list[AgentTool]:
    """The tools, bound to one cycle's context. Failures are text throughout."""
    roles = "; ".join(sorted(catalog)) or "(none)"

    async def commission(name: str, recruit: Recruit) -> Any:
        """Reserve, spawn, roll back on failure — once, for both kinds of hire.

        Shared so the race discipline cannot drift between them: the reservation into
        `roster.hires` runs before the spawn await, and awaiting this coroutine executes
        its body synchronously up to that first genuine suspension — so the reservation
        sits in the same event-loop step as the caller's refusals. Rolled back if the
        spawn raises, so a failed hire holds neither the name nor a slot.
        """
        if equip is not None:
            equip(recruit)
        roster.hires[name] = recruit
        try:
            handle = await recruit.spawn(ctx.coordinator, parent_id=ctx.thread_id)
        except BaseException:
            del roster.hires[name]
            raise
        roster.thread_ids[name] = handle.id
        if hired is not None:
            await hired(recruit, handle)
        return handle

    @strands_tool(
        name="hire",
        description=(
            "Create a new subagent that reports to you and works only on what you give it. "
            "Choose a role from the catalog, give it a short unique name, and state its "
            f"mandate in one or two sentences. Available roles -- {roles}. Hiring only "
            "creates the subagent; call delegate to give it work."
        ),
    )
    async def hire(role: str, name: str, mandate: str) -> str:
        if role not in catalog:
            return f"error: no such role {role!r}; available: {sorted(catalog)}"
        if name in roster.hires:
            return f"error: you already have a subagent named {name!r}"
        if roster.headcount >= max_hires:
            return f"error: hiring cap reached ({max_hires}); dismiss someone first"

        # Built only after all three refusals, so a rejected hire spawns nothing.
        recruit = catalog[role](name)
        handle = await commission(name, recruit)
        roster.record("hire", role=role, name=name, mandate=mandate, thread_id=str(handle.id))
        ctx.on_event(
            CustomEvent(
                kind="team.hired",
                payload={"role": role, "name": name, "child_thread_id": str(handle.id)},
            )
        )
        return f"hired {name} as {role} (thread {handle.id})"

    @strands_tool(
        name="hire_dynamic",
        description=(
            "Create a new subagent by writing its instructions yourself, when NO catalog "
            "role fits the work. Prefer hire with a catalog role whenever one fits — "
            "catalog roles were reviewed and carry their own knowledge; an agent you "
            "synthesize knows only what your instructions say. Give it a short unique name, "
            "instructions that state who it is and how it should work, and its mandate in "
            "one or two sentences. Hiring only creates the subagent; call delegate to give "
            "it work."
        ),
    )
    async def hire_dynamic(name: str, instructions: str, mandate: str) -> str:
        if dynamic is None:  # unreachable when injected via `hook`; honest if called directly
            return "error: dynamic hiring is not enabled for this team"
        if not instructions.strip():
            return (
                "error: instructions are empty; a synthesized agent knows only what you "
                "write here, so state who it is and how it should work"
            )
        if name in roster.hires:
            return f"error: you already have a subagent named {name!r}"
        if roster.headcount >= max_hires:
            return f"error: hiring cap reached ({max_hires}); dismiss someone first"

        recruit = dynamic(name, instructions)
        # Instructions recorded VERBATIM — the audit trail is the safety story for a
        # prompt nobody reviewed.
        handle = await commission(name, recruit)
        roster.record(
            "hire_dynamic",
            name=name,
            instructions=instructions,
            mandate=mandate,
            thread_id=str(handle.id),
        )
        ctx.on_event(
            CustomEvent(
                kind="team.hired_dynamic",
                payload={"name": name, "child_thread_id": str(handle.id)},
            )
        )
        return f"hired {name} from your instructions (thread {handle.id})"

    @strands_tool(
        name="delegate",
        description=(
            "Give a request to a subagent you hired and wait for its answer. Pass the exact "
            "name you used when hiring. The subagent sees only your request and whatever its "
            "role already knows, so state everything it needs."
        ),
    )
    async def delegate(name: str, request: str) -> str:
        recruit = roster.hires.get(name)
        if recruit is None:
            return (
                f"error: you have not hired anyone named {name!r} (hired: {sorted(roster.hires)})"
            )
        try:
            answer = await recruit.ask(request)
        except Exception as error:  # noqa: BLE001 — the model can retry or re-scope
            roster.record("delegate_failed", name=name, error=repr(error))
            return f"error: {name} failed: {error}"
        roster.record("delegate", name=name, request=request, answer=str(answer))
        return str(answer)

    @strands_tool(
        name="dismiss",
        description=(
            "Terminate a subagent you hired when its work is finished. Its answers stay in "
            "your conversation; only the live thread ends."
        ),
    )
    async def dismiss(name: str) -> str:
        recruit = roster.hires.get(name)
        if recruit is None:
            return f"error: no subagent named {name!r}"

        # Retired first, unregistered only on success — a `pop` before the await drops the
        # roster's only reference, so a retire that raises leaves a live thread nothing can
        # reach. Left registered, the raise is retried by teardown, and retire is
        # idempotent, so the retry is free.
        await recruit.retire()

        del roster.hires[name]
        roster.thread_ids.pop(name, None)
        if dismissed is not None:
            dismissed(recruit)
        roster.record("dismiss", name=name)
        return f"dismissed {name}"

    # `hire_dynamic` exists on the wire only when a synthesis factory was supplied: an
    # absent tool cannot be called wrongly.
    if dynamic is not None:
        return [hire, hire_dynamic, delegate, dismiss]
    return [hire, delegate, dismiss]


class Hiring:
    """The hiring seam as a hook: per-run roster, sibling coordination, unconditional unwind.

    Args:
        catalog: Role name → `factory(name)`. `None` or empty means nothing is hireable
            from a catalog — meaningful when `dynamic` is the whole hiring story.
        dynamic: Whether the lead may synthesize a subagent inline (`hire_dynamic`). Off by
            default. The default recruit shape is a `DynamicAgent` behind the ordinary
            `Member` adapter — override `dynamic_recruit` to bind a model (an offline suite
            must make an unbound synthesized member unrepresentable).
        max_hires: Headcount cap, shared between catalog and dynamic hires — two caps would
            let a lead run 2x the intended team behind an innocent-looking flag.
    """

    def __init__(
        self,
        catalog: Mapping[str, Callable[[str], Recruit]] | None = None,
        *,
        dynamic: bool = False,
        max_hires: int = 3,
    ) -> None:
        if max_hires < 0:
            raise ValueError(
                f"Hiring(max_hires={max_hires}) is negative, which would silently behave "
                f"as 0 — pass 0 to forbid hiring, or a positive cap."
            )
        self.catalog = dict(catalog or {})
        self.dynamic = dynamic
        self.max_hires = max_hires
        self._run: Workspace | None = None
        self._roster = Roster()

    def dynamic_recruit(self, name: str, instructions: str) -> Recruit:
        """Name + instructions → the recruit `hire_dynamic` spawns.

        The library's own shape: a `DynamicAgent` behind the ordinary `Member` adapter, so
        the synthesized agent satisfies `Recruit`, joins the roster, gets the sibling
        equip, and is retired by every unwind path exactly as a catalog hire is.
        Overridable so a test binds a scripted model by construction.
        """
        return Member(DynamicAgent(name, instructions), "answer")

    def roster(self, work: Workspace) -> Roster:
        """This run's roster, fresh per workspace — every promise on it (the cap, the name
        reservation, the published log) is a promise about one run, and a roster that
        survived into the next would make all three quietly false there."""
        if self._run is not work:
            self._run = work
            self._roster = type(self._roster)()
            work.data["hiring"] = self._roster.log
        return self._roster

    def on_assemble(self, work: Workspace) -> None:
        """Stand the roster up early so `hooks_data["hiring"]` exists even on a run whose
        lead never hires — and so `on_teardown` after a pre-lead fault has one to walk."""
        self.roster(work)

    def tools_for_lead(self, work: Workspace, ctx: ThreadContext) -> list[AgentTool]:
        roster = self.roster(work)
        return hiring_tools(
            roster,
            self.catalog,
            max_hires=self.max_hires,
            equip=lambda recruit: self._equip(work, recruit),
            hired=lambda recruit, handle: self._announce(work, recruit, handle),
            dismissed=lambda recruit: self._farewell(work, recruit),
            dynamic=self.dynamic_recruit if self.dynamic else None,
        )(ctx)["tools"]  # type: ignore[return-value]

    async def on_teardown(self, work: Workspace) -> None:
        """Retire whatever the lead never dismissed. The core's finally retires the cast
        and the lead; the hires are this hook's own to release, and `retire` is idempotent
        so overlapping with an already-completed dismissal is harmless."""
        roster = self.roster(work)
        await asyncio.gather(
            *(hire.retire() for hire in list(roster.hires.values())), return_exceptions=True
        )

    # ── Sibling coordination: the hook-library convention on_hire/on_dismiss ──

    def _equip(self, work: Workspace, recruit: Recruit) -> None:
        """Fold every sibling hook's `tools_for_member` into the hire's own hook, before
        spawn — the same fold `core._equip_members` does for the cast, so a hire carries
        exactly the member tools a cast member does."""
        contributors = [
            h for h in work.team.hooks if getattr(h, "tools_for_member", None) is not None
        ]
        if not contributors:
            return
        equip = getattr(recruit, "equip", None)
        if not callable(equip):
            return

        def hook(ctx: ThreadContext, recruit: Recruit = recruit) -> ThreadKwargs:
            tools: list[AgentTool] = []
            for h in contributors:
                tools.extend(h.tools_for_member(work, recruit, ctx))
            return {"tools": tools}

        equip(hook)

    async def _announce(self, work: Workspace, recruit: Recruit, handle: Any) -> None:
        """Tell every sibling hook that carries `on_hire` — the worklog opens its channel
        (replay included) here, which is what makes a late hire read what the team already
        flagged."""
        for h in work.team.hooks:
            on_hire = getattr(h, "on_hire", None)
            if on_hire is not None and h is not self:
                await on_hire(work, recruit, handle)

    def _farewell(self, work: Workspace, recruit: Recruit) -> None:
        for h in work.team.hooks:
            on_dismiss = getattr(h, "on_dismiss", None)
            if on_dismiss is not None and h is not self:
                on_dismiss(work, recruit)
