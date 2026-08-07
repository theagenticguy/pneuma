"""The demo's binding of the library's hiring seam onto `ROSTER`.

`team.hiring_tools` ships the three tools — `hire`, `delegate`, `dismiss` — that let a running
agent create the subagents the runtime cannot create for it. What it deliberately does not ship is
*who* is hireable: it takes a catalog of `role -> factory(name)`, because what a team may hire is a
property of that team and `demo/agent.ROSTER` is a module-level registry every agent in the process
shares (`agent.py:53-56`). This module is the join, plus the two pieces of the demo's behaviour a
library has no way to assume.

    staffing_tools(staff, allow=["historian"], max_hires=2)

The two pieces. A hire's mandate lands on the hired agent's `mandate` attribute, which every
hireable class in `cast.py` declares and no `Recruit` promises. And the role catalog the lead reads
carries each role's *purpose*, not just its name, because the lead is choosing between roles and a
bare list of three words is not a choice it can make well.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.types import ThreadContext

from ..team import Recruit, Roster, hiring_tools
from .agent import ROSTER, Agent


@dataclass
class Staff(Roster):
    """The demo's roster: it also puts the model's mandate onto the agent it hired.

    `hiring_tools` hands the mandate to the *tool*, records it on the log, and stops there,
    because the `Recruit` protocol says nothing about a mandate and a library that injected one
    would either fail on a `__slots__` recruit or silently create a field nothing reads
    (`team.py:273-277`). The demo is where the attribute is real: `Historian`, `Skeptic` and
    `Correlator` each declare `mandate: str = ""` and interpolate it into their own briefing
    (`cast.py:133`, `:159`, `:188`), so a hire whose mandate never arrived would run on the words
    "Your mandate: " and nothing else.

    `record` is the seam because it is the only hook that sees the mandate and the instance at
    once — the catalog's factory is called as `factory(name)` and never learns why it was hired
    (`team.py:259`). It works because `hire` registers the recruit and *then* records
    (`team.py:332-340`), and because the attribute is read at cycle time by a `prompt_fn` closed
    over the instance (`agent.py:110-111`), so a mandate set here lands long before the first
    `delegate` runs a model. That ordering is checked rather than assumed: a `hiring_tools` that
    recorded before registering would drop every mandate, and a hire briefed on nothing is not a
    hire that visibly failed.
    """

    def record(self, action: str, **fields: Any) -> None:
        if action == "hire":
            hire = self.hires.get(fields["name"])
            if hire is None:
                raise RuntimeError(
                    f"a hire named {fields['name']!r} was logged before it was registered, so its "
                    f"mandate has nowhere to land; hiring_tools must add to `hires` before it "
                    f"calls `record`"
                )
            hire.mandate = fields["mandate"]  # type: ignore[attr-defined]
        super().record(action, **fields)


def staffing_tools(
    staff: Staff,
    *,
    allow: Sequence[str] | None = None,
    max_hires: int = 4,
) -> Callable[[ThreadContext], ThreadKwargs]:
    """Build a `config_hook` that grants hire/delegate/dismiss over `ROSTER` for one cycle.

    `allow` restricts which roster roles are hireable; `None` means every registered role. The
    narrowing is the demo's own, because the library has no `allow` parameter — there the catalog
    *is* the narrowing, so restricting what one lead may hire is a matter of handing it a smaller
    mapping. An `allow` entry naming no registered role is dropped rather than refused, which is
    what a caller wants of a filter over a registry it does not own.

    `max_hires` caps the headcount so a confused agent cannot spawn without bound — the library
    enforces no depth or breadth limit of its own.
    """
    roles = ROSTER if allow is None else {r: ROSTER[r] for r in allow if r in ROSTER}
    catalog: dict[str, Callable[[str], Recruit]] = {r: _factory(c) for r, c in roles.items()}
    hook = hiring_tools(staff, catalog, max_hires=max_hires)

    def demo_hook(ctx: ThreadContext) -> ThreadKwargs:
        kwargs = hook(ctx)
        _describe_roles(kwargs.get("tools") or (), roles)
        return kwargs

    return demo_hook


def _factory(cls: type[Agent]) -> Callable[[str], Agent]:
    """One role's factory, closed over its class in a scope a loop cannot rebind.

    A `lambda name: cls(name=name)` written inline in the comprehension above would close over the
    comprehension's `cls` *cell*, so every factory would build whichever class the loop saw last —
    every hire the same role, and nothing would say so.
    """
    return lambda name: cls(name=name)


def _describe_roles(tools: Sequence[Any], roles: dict[str, type[Agent]]) -> None:
    """Put each role's purpose back into the `hire` tool's description, in place.

    `_hiring` renders the catalog as `"; ".join(sorted(catalog))` — role names, because a name is
    all a `Mapping[str, Callable]` has. The demo has more: `Agent.purpose` says what a role is
    *for*, and the lead is choosing between three of them. `historian` alone leaves that choice to
    the word; `historian: Builds a cross-plane timeline and flags effects that precede their
    supposed cause` makes it. Nothing offline pins this text, which is why it is restored here
    rather than left to be noticed in a live run's verdict.

    The library rebuilds these tools every cycle and `tool_spec` is a plain mutable dict on each
    fresh object (measured), so editing one cannot leak into another cycle or another team. A
    missing anchor raises: if that wording changes upstream the substitution would otherwise no-op
    and the purposes would vanish from a prompt with nothing to report it.
    """
    names = "; ".join(sorted(roles)) or "(none)"
    purposes = "; ".join(f"{role}: {cls.purpose}" for role, cls in roles.items())
    anchor = f"Available roles -- {names}."
    for tool in tools:
        if tool.tool_name != "hire":
            continue
        described = tool.tool_spec["description"]
        if anchor not in described:
            raise RuntimeError(
                f"the hire tool's description no longer contains {anchor!r}, so each role's "
                f"purpose cannot be restored into it; team.hiring_tools has reworded the catalog "
                f"and this binding has to follow it"
            )
        tool.tool_spec["description"] = described.replace(anchor, f"Available roles -- {purposes}.")
        return
    raise RuntimeError("team.hiring_tools returned no tool named 'hire'")
