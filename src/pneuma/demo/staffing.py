"""Tools that let a running agent staff its own subagents.

The library injects two peer tools into every thread — `list_threads` to discover
peers and `send_message` to delegate to them — so an agent can talk to threads
that already exist. It cannot create one. The hooks are there: `ThreadConfig`
documents `config_hook` as the place to inject "`spawn_thread` closed over the
current runtime and `thread_id`", but nothing in the library ships that tool.

This module ships it. `staffing_tools(agent)` returns a `config_hook` that binds
three tools to the live cycle context, so the agent hiring the subagent is the
one the runtime records as its parent:

    hire(role, name, mandate)  -> spawn a roster class as a child thread
    delegate(name, request)    -> run a hired subagent and read its answer back
    dismiss(name)              -> terminate a subagent

Each `hire` passes `parent_id=ctx.thread_id`, which is what writes a
`ThreadSpawnedEvent` into the *hiring* agent's event log. That edge is the only
thing the library's `subtree_token_usage` walks, so cost attribution up the tree
is a free consequence of hiring correctly rather than something this module has
to account for itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.types import CustomEvent, ThreadContext, ThreadId
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from .agent import ROSTER, Agent


@dataclass
class Staff:
    """Registry of subagents one agent has hired, keyed by the name it chose."""

    hires: dict[str, Agent] = field(default_factory=dict)
    thread_ids: dict[str, ThreadId] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def record(self, action: str, **fields: Any) -> None:
        self.log.append({"action": action, **fields})

    @property
    def headcount(self) -> int:
        return len(self.hires)


def staffing_tools(
    staff: Staff,
    *,
    allow: Sequence[str] | None = None,
    max_hires: int = 4,
) -> Callable[[ThreadContext], ThreadKwargs]:
    """Build a `config_hook` that grants hire/delegate/dismiss for one cycle.

    `allow` restricts which roster roles are hireable; `None` means every
    registered role. `max_hires` caps the headcount so a confused agent cannot
    spawn without bound — the library enforces no depth or breadth limit of its
    own.
    """

    def hook(ctx: ThreadContext) -> ThreadKwargs:
        return {"tools": list(_build(ctx, staff, allow, max_hires))}

    return hook


def _roles(allow: Sequence[str] | None) -> dict[str, type[Agent]]:
    if allow is None:
        return dict(ROSTER)
    return {role: ROSTER[role] for role in allow if role in ROSTER}


def _build(
    ctx: ThreadContext,
    staff: Staff,
    allow: Sequence[str] | None,
    max_hires: int,
) -> list[AgentTool]:
    available = _roles(allow)
    catalog = "; ".join(f"{role}: {cls.purpose}" for role, cls in available.items())

    @strands_tool(
        name="hire",
        description=(
            "Create a new subagent that reports to you and works only on what you "
            "give it. Choose a role from the catalog, give it a short unique name, "
            f"and state its mandate in one or two sentences. Available roles -- {catalog}. "
            "Hiring only creates the subagent; call delegate to give it work."
        ),
    )
    async def hire(role: str, name: str, mandate: str) -> str:
        if role not in available:
            return f"error: no such role {role!r}; available: {sorted(available)}"
        if name in staff.hires:
            return f"error: you already have a subagent named {name!r}"
        if staff.headcount >= max_hires:
            return f"error: hiring cap reached ({max_hires}); dismiss someone first"

        cls = available[role]
        sub = cls(name=name)
        sub.mandate = mandate  # type: ignore[attr-defined]
        handle = await sub.spawn(ctx.coordinator, parent_id=ctx.thread_id)

        staff.hires[name] = sub
        staff.thread_ids[name] = handle.id
        staff.record("hire", role=role, name=name, mandate=mandate, thread_id=str(handle.id))
        ctx.on_event(
            CustomEvent(
                kind="staffing.hire",
                payload={"role": role, "name": name, "child_thread_id": str(handle.id)},
            )
        )
        return json.dumps({"hired": name, "role": role, "thread_id": str(handle.id)})

    @strands_tool(
        name="delegate",
        description=(
            "Give a request to a subagent you hired and wait for its answer. "
            "Pass the exact name you used when hiring. The subagent sees only your "
            "request and its own mandate, so state everything it needs."
        ),
    )
    async def delegate(name: str, request: str) -> str:
        sub = staff.hires.get(name)
        if sub is None:
            return f"error: you have not hired anyone named {name!r} (hired: {sorted(staff.hires)})"
        try:
            answer = await sub.ask(request)
        except Exception as exc:  # surfaced to the model, which can retry or re-scope
            staff.record("delegate_failed", name=name, error=repr(exc))
            return f"error: {name} failed: {exc}"
        staff.record("delegate", name=name, request=request, answer=str(answer))
        return str(answer)

    @strands_tool(
        name="dismiss",
        description=(
            "Terminate a subagent you hired when its work is finished. "
            "Its findings stay in your conversation; only the live thread ends."
        ),
    )
    async def dismiss(name: str) -> str:
        sub = staff.hires.pop(name, None)
        if sub is None:
            return f"error: no subagent named {name!r}"
        await sub.retire()
        staff.thread_ids.pop(name, None)
        staff.record("dismiss", name=name)
        return f"dismissed {name}"

    return [hire, delegate, dismiss]
