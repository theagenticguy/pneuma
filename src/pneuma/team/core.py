"""`Team` — a lead over live members, with hooks for everything that is not that.

The old flat `team.py` (deleted in this rebuild) grew five phases, an oracle, a hiring seam, a
negotiation loop and a worklog into one class, and every new capability meant another field on
it. This rebuild inverts the shape: the core owns exactly what every team needs — spawn the
members, run the lead with the members as typed tools, retire everybody — and everything else
arrives as a `TeamHook`. A hook can contribute tools to the lead or the members, rewrite the
request, and review the answer through a bounded Accept/Revise loop the core drives. The bare
team is the whole API for the common case::

    team = Team(
        lead=chair.compiled("decide"),
        members=[Member(left, "read"), Member(right, "read")],
    )
    run = await team.run("who is right")
    print(run.answer)

**The one hard mechanical constraint.** The runtime resolves exactly one `config_hook` per
cycle and its `tools` patch *replaces* the compiled tools (`ai_thread.py:548-553`,
`config.py:166-185`, re-verified against the installed package). So the core owns the single
hook on each thread and folds every contribution into it: the lead's own hook and tools are
recomposed first, then the member tools, then every `tools_for_lead`; a member's own `tools=`
override is recomposed by `Member.equip`'s wrapper. Nothing else may set `config_hook` on a
thread the core manages — `Member.equip` refuses a member that carries its own.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ai_functions import AIFunction
from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.types import ThreadContext
from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from .members import Recruit

__all__ = ["Accept", "Revise", "Team", "TeamHook", "TeamRun", "Workspace"]


# ── The answer-loop verdicts ──


@dataclass(frozen=True)
class Accept:
    """A hook's verdict: the answer passes; the loop moves to the next hook."""


@dataclass(frozen=True)
class Revise:
    """A hook's verdict: re-run the lead with `feedback`, at most `cap` times for this hook.

    `cap` rides on the verdict rather than on the hook because the hook is the party that
    knows how much revision this particular finding is worth — and a hook that returns
    `Revise` forever must still terminate, so the core reads the cap off the *latest* verdict
    and stops when the rounds already spent reach it. Exhaustion is not an error: the last
    answer passes on and the transcript records that the cap, not the hook, ended the loop.
    """

    feedback: str
    cap: int = 2

    def __post_init__(self) -> None:
        if self.cap < 0:
            raise ValueError(
                f"Revise(cap={self.cap}) is negative, which would silently behave as 0 — "
                f"pass 0 to give feedback no rounds, or a positive cap."
            )


# ── What hooks see ──


@dataclass
class Workspace:
    """One run's shared state, handed to every hook method.

    A single argument rather than a growing signature, so a new field here breaks no existing
    hook. `data` is the cross-hook scratch space that `TeamRun.hooks_data` publishes;
    `transcript` is the audit trail the core itself writes member calls and revise rounds
    into, and hooks may append their own entries. `members` are live once `on_assemble` runs
    — spawning precedes every hook call. `lead` is the lead's live thread handle, set as soon
    as the lead's thread exists — before any hook runs — because a hook that opens a delivery
    channel to the lead (the worklog's fan-out) must open it before the lead's first cycle,
    or everything posted during assembly would reach every member and never the lead. The
    lead's thread is registered but *not running* until after the `on_request` fold.
    """

    team: Team
    request: str
    coordinator: Any
    members: Sequence[Recruit]
    data: dict[str, Any] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    lead: Any = None


class TeamHook(Protocol):
    """What a hook may implement. Every method is optional — absence means skip.

    Checked with `getattr(hook, name, None)` rather than `isinstance`, deliberately: a hook
    implements the two methods it needs and nothing else, and an introspection-safe absence
    check is what lets a debugger's `hasattr` probe never detonate a guard
    (`.erpaval/solutions/ai-functions-runtime/hooks-budgets-and-introspection-safe-gates.md`).

    `tools_for_lead` and `tools_for_member` are **synchronous**, because the runtime calls the
    one `config_hook` synchronously inside `_run_cycle` and documents "must not block"
    (`config.py:186-188`); the four lifecycle methods may be sync or async and the core awaits
    whichever it finds.
    """

    def on_assemble(self, work: Workspace) -> Any:
        """Members are live; the lead has not run. Briefing-style hooks start here."""
        ...

    def on_request(self, work: Workspace, request: str) -> str:
        """Rewrite the request the lead will be asked. Folded left across hooks in order."""
        ...

    def tools_for_lead(self, work: Workspace, ctx: ThreadContext) -> Sequence[AgentTool]:
        """Extra tools for the lead's cycle, rebuilt every cycle against `ctx`. Sync."""
        ...

    def tools_for_member(
        self, work: Workspace, member: Recruit, ctx: ThreadContext
    ) -> Sequence[AgentTool]:
        """Extra tools for one member's cycle, rebuilt every cycle against `ctx`. Sync."""
        ...

    def on_answer(self, work: Workspace, answer: Any) -> Accept | Revise:
        """Review the lead's answer. `Accept` passes it on; `Revise` re-runs the lead."""
        ...

    def on_teardown(self, work: Workspace) -> Any:
        """The run is over (successfully or not); the cast is still live. Clean up."""
        ...


# ── The report ──


class TeamRun(BaseModel):
    """Everything one run produced: the answer, the audit trail, and what hooks recorded.

    `answer` is `Any` because the lead's output type is the caller's choice. `transcript`
    carries the core's own record — one entry per member call (`kind="member"`) and per revise
    round (`kind="revise"` / `kind="revise_cap"`) — plus whatever hooks appended. `hooks_data`
    is `Workspace.data` verbatim. Both serialise away when empty, so a bare run's artifact
    stays one key and a hook-laden run's says exactly which hooks left something behind.
    """

    answer: Any
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    hooks_data: dict[str, Any] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _without_empty_keys(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if not self.transcript:
            data.pop("transcript", None)
        if not self.hooks_data:
            data.pop("hooks_data", None)
        return data


# ── The core ──


class Team:
    """A lead `AIFunction` over live members, extended by hooks and nothing else.

    Args:
        lead: The compiled function that answers the request. It runs on a live thread with
            every member available as a typed tool; its own `tools=` and `config_hook` are
            recomposed into the one hook the core installs, so a lead that already carries
            either loses nothing.
        members: The cast. Each is spawned as a child of the lead's thread before any hook
            runs and retired unconditionally in a `finally`. Names must be unique — each
            becomes a tool named after it, and two tools sharing a name shadow silently.
        hooks: `TeamHook`s, consulted in order everywhere order matters (request folding,
            tool contribution, the answer loop).
    """

    def __init__(
        self,
        lead: AIFunction[..., Any],
        members: Sequence[Recruit],
        hooks: Sequence[TeamHook] = (),
    ) -> None:
        self.lead = lead
        self.members = list(members)
        self.hooks = list(hooks)
        self._check_no_duplicate_member_names(self.members)

    async def run(
        self,
        request: str,
        coordinator: Any = None,
        *,
        parent_id: Any = None,
    ) -> TeamRun:
        """One full run: spawn, hook, lead, answer loop, teardown — always all of it.

        Args:
            request: What the team is asked. Hooks may rewrite it (`on_request`) before the
                lead sees it; the lead's first positional parameter is where it lands, which
                binds for a `STRUCTURED` lead as much as for a `STR_PROMPT` one.
            coordinator: The coordinator every thread spawns onto. `None` stands up a private
                `InMemoryCoordinator` + `LocalWorker` pair for this run and closes it in a
                `finally` — the convenience path for scripts; anything embedded in a larger
                runtime passes its own so the run lands in the caller's registry and log.
            parent_id: Thread to attribute the lead to. The members are always children of
                the lead, so the whole run is one subtree whatever the caller passes.
        """
        if coordinator is None:
            from ai_functions import InMemoryCoordinator, LocalWorker

            coordinator = InMemoryCoordinator()
            worker = LocalWorker(coordinator)
            await worker.register()
            try:
                return await self.run(request, coordinator, parent_id=parent_id)
            finally:
                await worker.close()

        work = Workspace(team=self, request=request, coordinator=coordinator, members=self.members)
        lead_handle: Any = None
        try:
            # The lead's thread exists first — not running, just registered — so every member
            # spawns as its child and the run is one subtree in one event log. The lead's one
            # config_hook is composed before the spawn, closing over `work` so per-cycle tool
            # contributions see the same state every other hook method does.
            lead_fn = self.lead.replace(config_hook=self._lead_hook(work))
            lead_handle = await coordinator.spawn(
                lead_fn, thread_name=f"{self.lead.name}-lead", parent_id=parent_id
            )
            work.lead = lead_handle

            self._equip_members(work)
            for member in self.members:
                await member.spawn(coordinator, parent_id=lead_handle.id)

            for hook in self.hooks:
                on_assemble = getattr(hook, "on_assemble", None)
                if on_assemble is not None:
                    await _maybe_await(on_assemble(work))

            for hook in self.hooks:
                on_request = getattr(hook, "on_request", None)
                if on_request is not None:
                    request = str(await _maybe_await(on_request(work, request)))

            answer = await lead_handle.run(request)
            answer = await self._answer_loop(work, lead_handle, answer)
            return TeamRun(answer=answer, transcript=work.transcript, hooks_data=work.data)
        finally:
            # Teardown hooks run even on a mid-run fault, and the retire runs even when a
            # teardown hook raises — the registry must be empty on every path. One hook's
            # raise must not silence another's teardown, so each is guarded and the first
            # error resurfaces only when nothing else is already propagating.
            errors: list[BaseException] = []
            for hook in self.hooks:
                on_teardown = getattr(hook, "on_teardown", None)
                if on_teardown is None:
                    continue
                try:
                    await _maybe_await(on_teardown(work))
                except BaseException as error:  # noqa: BLE001 — collected, re-raised below
                    errors.append(error)
            await asyncio.gather(
                *(member.retire() for member in self.members),
                *([lead_handle.terminate_now()] if lead_handle is not None else []),
                return_exceptions=True,
            )
            if errors and sys.exception() is None:
                raise errors[0]

    # ── The answer loop ──

    async def _answer_loop(self, work: Workspace, lead_handle: Any, answer: Any) -> Any:
        """Every hook with `on_answer` reviews in order; `Revise` re-runs the lead, bounded.

        The rounds counter is per hook, so two revising hooks each get their own budget, and
        the cap is read off the *latest* verdict — a hook may lower it mid-loop and the loop
        honours the new number. Cap exhaustion passes the last answer and records itself; a
        verdict that is neither `Accept` nor `Revise` is a wiring bug and raises naming the
        hook, because a `None` silently treated as accept would grade nothing while looking
        like a review happened.
        """
        for hook in self.hooks:
            on_answer = getattr(hook, "on_answer", None)
            if on_answer is None:
                continue
            label = type(hook).__name__
            rounds = 0
            while True:
                verdict = await _maybe_await(on_answer(work, answer))
                if isinstance(verdict, Accept):
                    break
                if not isinstance(verdict, Revise):
                    raise RuntimeError(
                        f"{label}.on_answer returned {verdict!r}; it must return Accept() or "
                        f"Revise(feedback) — anything else silently reviewed nothing."
                    )
                if rounds >= verdict.cap:
                    work.transcript.append({"kind": "revise_cap", "hook": label, "rounds": rounds})
                    break
                rounds += 1
                work.transcript.append(
                    {"kind": "revise", "hook": label, "round": rounds, "feedback": verdict.feedback}
                )
                answer = await lead_handle.run(
                    f"Your answer was reviewed and needs revision.\n\nFeedback: {verdict.feedback}"
                )
        return answer

    # ── The tool composer ──

    def _lead_hook(self, work: Workspace) -> Any:
        """The lead's one `config_hook`, folding every tool source into one patch.

        The runtime calls exactly one hook per cycle and its `tools` patch replaces the
        compiled tools (`ai_thread.py:548-553`; `config.py:166-185`, both re-verified against
        the installed package), so composition happens here or not at all. Order per cycle:
        the lead's own hook runs first and its full patch is honoured — its `tools`, when it
        sets them, stand in for the compiled `tools=` rather than stacking on top (the
        replace semantics the lead's author already wrote against); then the members-as-tools;
        then each hook's `tools_for_lead`, rebuilt against that cycle's `ctx`.
        """
        own_hook = self.lead.config.config_hook
        compiled_tools = list(self.lead.config.tools)

        def hook(ctx: ThreadContext) -> ThreadKwargs:
            patch: dict[str, Any] = {}
            tools = compiled_tools
            if own_hook is not None:
                patch = dict(own_hook(ctx))
                if "tools" in patch:
                    tools = list(patch["tools"])
            contributed: list[AgentTool] = []
            for h in self.hooks:
                tools_for_lead = getattr(h, "tools_for_lead", None)
                if tools_for_lead is not None:
                    contributed.extend(tools_for_lead(work, ctx))
            patch["tools"] = [*tools, *self._member_tools(work), *contributed]
            return patch  # type: ignore[return-value]

        return hook

    def _member_tools(self, work: Workspace) -> list[AgentTool]:
        """One tool per member, targeting the member's *live* thread.

        Named after the member, one `request: str` parameter — the whole of what `Recruit.ask`
        guarantees, so any member shape joins the lead's wire the same way. The call reaches
        the live thread, not a throwaway compile, so a member's history accrues across the
        lead's calls to it. Each call is recorded on the transcript by the wire that carried
        it, not reported by the model — and always under the member's *real* name, whatever
        the tool had to be called.
        """
        tools: list[AgentTool] = []
        for member in self.members:
            tools.append(self._member_tool(work, member))
        return tools

    def _member_tool(self, work: Workspace, member: Recruit) -> AgentTool:
        description = (
            f"Ask your team member {member.name} to do one piece of work and return its "
            f"answer. It sees only your request, so state everything it needs."
        )

        @strands_tool(name=_tool_name(member.name), description=description)
        async def call(request: str) -> str:
            answer = str(await member.ask(request))
            work.transcript.append(
                {"kind": "member", "member": member.name, "request": request, "answer": answer}
            )
            return answer

        return call

    def _equip_members(self, work: Workspace) -> None:
        """Fold every `tools_for_member` into one equipped hook per member — before spawn.

        Skipped entirely when no hook contributes member tools, so the bare path spawns
        members exactly as their adapters would alone (and a member carrying its own
        `config_hook=` override is only refused when something actually needs the slot).
        Recruits without an `equip` seam are skipped, not refused: a mixed cast is
        legitimate, and a member that cannot take tools is a fact rather than a fault.
        """
        contributors = [h for h in self.hooks if getattr(h, "tools_for_member", None) is not None]
        if not contributors:
            return
        for member in self.members:
            equip = getattr(member, "equip", None)
            if not callable(equip):
                continue

            def hook(ctx: ThreadContext, member: Recruit = member) -> ThreadKwargs:
                tools: list[AgentTool] = []
                for h in contributors:
                    tools.extend(h.tools_for_member(work, member, ctx))
                return {"tools": tools}

            equip(hook)

    # ── Guards ──

    def _check_no_duplicate_member_names(self, members: Sequence[Recruit]) -> None:
        """Refuse two members under one name, at construction.

        Each member becomes a lead tool named `member.name`, and two tools sharing a name
        shadow silently — the lead would reach one member believing it reached either. The
        legacy module measured the dict-keyed variant of this loss
        (the old flat `team.py`'s `_check_no_duplicate_members`); the tool-named variant is the
        same defect one layer down, refused at the same place: before anything spawns.
        """
        seen: dict[str, str] = {}
        duplicates: set[str] = set()
        for member in members:
            wire = _tool_name(member.name)
            if wire in seen:
                duplicates.add(f"{seen[wire]!r}/{member.name!r}")
            seen[wire] = member.name
        if duplicates:
            raise RuntimeError(
                f"team: members {sorted(duplicates)} collide on the lead's wire; members join "
                f"the lead as tools named after them (dots mapped to underscores for the tool "
                f"registry), so the later would shadow the earlier silently. Give them "
                f"distinct names."
            )

    def __repr__(self) -> str:
        return (
            f"<Team lead={self.lead.name!r} members={[m.name for m in self.members]!r} "
            f"hooks={[type(h).__name__ for h in self.hooks]!r}>"
        )


def _tool_name(name: str) -> str:
    """A member's name as a wire-legal tool name.

    Strands validates tool names against `^[a-zA-Z0-9_\\-]{1,}$` and 64 chars
    (`strands/tools/tools.py:66-78`, measured: a dotted name is *dropped from the registry
    with only a warning logged*, so the lead silently loses the member). `Member` names are
    `{owner}.{method}` by construction, so the dot must be mapped, not refused. The transcript
    keeps the real name; only the wire sees this one.
    """
    mapped = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    return mapped[:64] or "member"


async def _maybe_await(value: Any) -> Any:
    """Await `value` when a hook method chose to be async; pass it through otherwise."""
    if inspect.isawaitable(value):
        return await value
    return value
