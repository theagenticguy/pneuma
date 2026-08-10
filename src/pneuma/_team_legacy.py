"""`Team` — a deterministic orchestrator that runs as a thread itself.

`demo/warroom.py` is one incident with four telemetry planes, and underneath it is a skeleton
worth having on its own: fan out to members concurrently behind a barrier, run a lead against a
post-condition oracle with a budgeted hiring seam, roll the whole subtree's tokens up, and retire
everybody whatever happened. None of that is about incidents. What *is* about incidents — the
planted root cause, the four planes, the `ROSTER` of hireable roles — is the part a second team
would have to delete before it could start. This module is the skeleton with the incident taken
out. Rationale: `docs/design/team.md`.

**The library satisfies `Spawnable` + `Thread` in plain Python, and that is the whole trick.**
`Spawnable` asks for two members (`to_thread`, `input_shape`) and `Thread` for seven more, none
of which require a model anywhere in the control flow (`protocols.py:129-308`). A workflow that
implements them gets a handle, a lifecycle, its own event log, and token rollup from every child
it spawns, and `coordinator.spawn` special-cases nothing — the worker just calls `to_thread()`
(`runtime/worker.py:488`). So the phase order here is ordinary `asyncio` and reproducible in a
way a prompt-driven orchestrator is not.

**Members join the lead as typed tools, not as chat peers.** The runtime's `send_message` only
targets threads whose `input_shape` is `STR_PROMPT` (`ai_thread/tools.py:172-176`), so an agent
addressable by a message bus is an agent that compiled its parameters down to one `str` — the
cost `method.py`'s header itemises. A `MethodAgent` compiles to `STRUCTURED` and joins a lead
through `agents()`, which is checkable where a chat box is not; `notify()` is the inbound side
channel for the cases that still want one. This module therefore mentions `send_message`
nowhere. The demo's `STR_PROMPT` cast keeps working because it *subclasses* this skeleton and
supplies its own members, not because the skeleton knows what a plane is.

    class Bench(Team):
        def members(self): return [self.left, self.right]
        def briefing(self, member): return "Read your evidence and report it."
        def lead_function(self): return self.chair.compiled("decide", tools=[...])
        def oracle(self, response): assert response.confident, "say why"

`await coordinator.spawn(Bench())` then `handle.run("go")` returns a `TeamRun` carrying the
verdict, each member's briefing, the hiring log, and the subtree's tokens and turns.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, Self, runtime_checkable

from ai_functions import AIFunction
from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.runtime.usage import last_event_id, subtree_usage
from ai_functions.types import CustomEvent, InputShape, ThreadContext, ThreadId
from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer
from strands.tools.decorator import tool as strands_tool
from strands.types.tools import AgentTool

from .method import MethodAgent, ai_method

__all__ = [
    "DISCOVERY_KINDS",
    "DynamicAgent",
    "Member",
    "Recruit",
    "Roster",
    "Team",
    "TeamRun",
    "Worklog",
    "discovery_tools",
    "hiring_tools",
]


@runtime_checkable
class Recruit(Protocol):
    """A member or a hire: something with a name that spawns, answers once, and retires.

    Three verbs and nothing else, because that is the whole of what the skeleton does to a
    member — it stands one up, asks it one question behind the barrier, and takes it down in the
    `finally`. Anything richer would be a contract the library cannot honour for every member
    shape it wants to accept.

    Deliberately a protocol rather than a base class, for `gated.Gate`'s reason: the members
    worth having already exist and already differ. `demo.agent.Agent` satisfies this as written
    (`spawn(coordinator, parent_id=...)`, `ask(request)`, `retire()`, `name` —
    `agent.py:138-161`), so the demo's `STR_PROMPT` cast needs no adapter at all. A `MethodAgent`
    adapts by holding the `MethodThread` its own `spawn` returns and routing `ask` to one typed
    method whose single parameter is a `str`; `Member` below is that adapter, kept in the library
    because the typed path is the library's default and no team should rewrite it.

    `spawn` returns whatever the implementation's handle is (`Any`): the skeleton reads only
    `.id` off it, and demanding a `ThreadHandle` would exclude `MethodThread`, which is the
    library's own first-class member type.
    """

    name: str

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        """Put this recruit on a live thread as a child of `parent_id`."""
        ...

    async def ask(self, request: str) -> Any:
        """Run one cycle with `request` and return its result."""
        ...

    async def retire(self) -> None:
        """Tear the thread down. Idempotent — an unwind loop must not crash mid-unwind."""
        ...


class Member:
    """A `MethodAgent` capability as a `Recruit`: the typed member, adapted once.

    The library's first-class member is a `MethodAgent` whose capability is a typed
    `@ai_method`, and everything about it already works — `spawn` puts it on a live thread,
    `MethodThread.run` forwards typed keywords, `retire` is idempotent against the runtime. The
    only thing missing is `Recruit`'s `ask(request: str)`, because a typed signature is not one
    string. So this adapter names *which* keyword the briefing arrives as, and that is the whole
    of it.

    Args:
        agent: The agent whose capability joins the team.
        method: Which `@ai_method` to put on a thread. One agent may contribute several members,
            because a thread hosts exactly one signature (`MethodThread`'s docstring).
        parameter: The keyword `ask` passes the briefing as. Defaults to the method's first
            parameter, which is right for the ordinary shape and explicit for the rest.
        overrides: `ThreadConfig` overrides for the compilation — `model=` is how a test scripts
            a member offline.

    The member stays `STRUCTURED`, which is the point: it is unreachable by `send_message`
    (`ai_thread/tools.py:172-176`) and reachable by the lead as a typed tool through
    `agent.agents()`. `notify()` is its inbound side channel, and this adapter deliberately does
    not wrap it — a caller wanting it holds the `MethodThread` this exposes.
    """

    __slots__ = ("agent", "method", "name", "_parameter", "_overrides", "_equipped", "_thread")

    def __init__(
        self,
        agent: Any,
        method: str,
        *,
        parameter: str | None = None,
        **overrides: Any,
    ) -> None:
        self.agent = agent
        self.method = method
        self.name = f"{getattr(agent, 'name', type(agent).__name__.lower())}.{method}"
        self._parameter = parameter or self._first_parameter(agent, method)
        self._overrides = overrides
        self._equipped: Any = None
        self._thread: Any = None

    @staticmethod
    def _first_parameter(agent: Any, method: str) -> str:
        """The keyword a briefing lands in, or a refusal naming the fix.

        A capability with no parameters cannot be briefed at all, and the failure without this
        check arrives as a `TypeError` from the signature bind *after* the team has been
        assembled and the barrier entered — a wiring mistake reported from the middle of a run.
        """
        parameters = [
            name
            for name, parameter in inspect.signature(getattr(agent, method)).parameters.items()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if not parameters:
            raise RuntimeError(
                f"{method!r} takes no positional parameter, so a briefing has nowhere to go; "
                f"give the capability a parameter or pass parameter= naming a keyword one"
            )
        return parameters[0]

    @property
    def thread(self) -> Any:
        """The live `MethodThread`, for the runtime operations this adapter does not wrap."""
        if self._thread is None:
            raise RuntimeError(f"{self.name}: not spawned yet")
        return self._thread

    def equip(self, config_hook: Callable[[ThreadContext], ThreadKwargs]) -> None:
        """Attach a per-cycle `config_hook`, applied at the next `spawn`.

        The seam the worklog rides in on: `Team.execute` equips each member with its
        discovery tool between `members()` and `assemble`, without the subclass writing any
        wiring. Refused when the member was constructed with its own `config_hook=` override,
        for `_gated_lead`'s reason — the runtime calls exactly one hook per cycle
        (`ai_thread.py:548-553`), so silently dropping either side costs tools invisibly.
        A *previously equipped* hook is replaced rather than refused: the equipped slot is
        team-owned, and a second run on the same handle re-equips the same cast.

        The hook's `tools` patch replaces the compiled tools for the cycle (the merge
        semantics `config.py:166-185` documents), so `spawn` composes the member's own
        `tools=` override back in ahead of whatever the hook adds — a member that carried
        tools must not lose them to a worklog it never asked about.
        """
        if self._overrides.get("config_hook") is not None:
            raise RuntimeError(
                f"{self.name}: this member already carries a config_hook, and the runtime "
                f"calls exactly one hook per cycle — compose them into one hook instead"
            )
        self._equipped = config_hook

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        overrides = dict(self._overrides)
        if self._equipped is not None:
            own_tools = list(overrides.get("tools") or [])
            equipped = self._equipped

            def hook(ctx: ThreadContext) -> ThreadKwargs:
                patch = dict(equipped(ctx))
                patch["tools"] = [*own_tools, *patch.get("tools", [])]
                return patch  # type: ignore[return-value]

            overrides["config_hook"] = hook
        self._thread = await self.agent.spawn(
            self.method, coordinator, parent_id=parent_id, **overrides
        )
        return self._thread

    async def ask(self, request: str) -> Any:
        return await self.thread.run(**{self._parameter: request})

    async def retire(self) -> None:
        if self._thread is not None:
            await self._thread.retire()

    def __repr__(self) -> str:
        return f"<Member {self.name!r}>"


class DynamicAgent(MethodAgent):
    """A `MethodAgent` whose prompt is written by the lead at runtime. The contract is not.

    Shepherd (arXiv 2605.10913) measured runtime agent synthesis — a lead writing a new
    subagent's instructions mid-run — as a layer worth having, and this class is that idea
    admitted through the library's typed front door: the *instructions* are dynamic, the
    signature, the output type, the tool set and the lifecycle are all fixed here, at review
    time. What varies per hire lives on `self`, where `compile_ai_method` renders it into the
    prompt (`method.py:103-124`) and the optimizer cannot reach it — the same split every
    static `MethodAgent` already makes, so a synthesized agent is not a new kind of thing.

    **One published ability, and provably one.** `ai_methods()` walks the MRO
    (`method.py:341-352`), so a base-class `@ai_method` would leak into this class's published
    tool set; `MethodAgent` declares none and this class declares exactly `answer`, and a test
    pins that the set is `["answer"]` rather than trusting the docstring. `answer` carries a
    second typed parameter deliberately: a single positional `str` compiles to `STR_PROMPT`
    (`ai_function.py:51-84`, measured), which would make the synthesized thread the one member
    shape addressable by every peer's free-text `send_message` (`ai_thread/tools.py:172-176`).
    The `context` parameter keeps the compiled shape `STRUCTURED`, so a dynamic hire sits
    behind exactly the boundary a catalog hire does.

    Instructions are refused when empty at construction — a wiring-time guard, not a model
    refusal, because by the time an instance exists the caller has already decided to build
    one. The `hire_dynamic` tool refuses the same case as text *before* construction, so the
    model reads the problem; this guard is for every other caller.
    """

    def __init__(self, name: str, instructions: str) -> None:
        if not str(instructions).strip():
            raise ValueError(
                f"DynamicAgent {name!r}: instructions are empty; a dynamic agent's whole "
                f"identity is its instructions, so there is nothing to synthesize from"
            )
        self.name = name
        self.instructions = instructions

    @ai_method(str, description="Carry out one request under this agent's own instructions")
    def answer(self, request: str, context: str = "") -> str:
        """{self.instructions}

        Additional context, if any: {context}

        Request: {request}"""


@dataclass
class Roster:
    """Who a team hired, keyed by the name the model chose, plus the evidence it was budgeted.

    Generalised from `demo/staffing.Staff` by one change: `hires` holds `Recruit`s rather than
    the demo's `Agent`s, so the registry no longer names the application's base class. The
    `log` is what the team reports and what a reader audits — every hire, delegation, dismissal
    and delegation *failure*, in order.
    """

    hires: dict[str, Recruit] = field(default_factory=dict)
    thread_ids: dict[str, ThreadId] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def record(self, action: str, **fields: Any) -> None:
        self.log.append({"action": action, **fields})

    @property
    def headcount(self) -> int:
        return len(self.hires)


class TeamRun(BaseModel):
    """Everything one team run produced, generic over what the lead returns.

    `verdict` is `Any` because the lead's output type is the subclass's choice, and that has one
    consequence worth stating rather than discovering: a pydantic field typed `Any` serialises a
    `BaseModel` fine and validates it back as a plain `dict`, so
    `deserialize_result(serialize_result(run))` does not equal `run` — which is precisely the
    round-trip `protocols.py` states as an `Ensures` for the pair. Measured. A subclass narrows
    the field (`verdict: Verdict`) and names the subclass from `Team.run_type()`; then the
    guarantee holds. `demo.warroom.Investigation` is already exactly that shape.
    """

    verdict: Any
    correct: bool
    oracle_failures: list[str]
    briefings: dict[str, str]
    hiring_log: list[dict[str, Any]]
    negotiation: list[dict[str, Any]] = Field(default_factory=list)
    """The negotiation transcript: one entry per round, empty when the phase did not run.

    Each entry carries `round`, `plan` (what was fanned out), `objections` (member name → its
    answer, rendered exactly as `brief` renders — errors included), `approved` (who approved),
    `outcome` (`unanimous`, `revised`, or `cap_reached`), and `revision` (the revised plan, absent
    on a unanimous round). Plain dicts rather than a model, for `hiring_log`'s reason: the
    transcript is an audit surface a reader walks, not a contract a caller binds to.
    """

    worklog: list[dict[str, Any]] = Field(default_factory=list)
    """The team worklog: every posted discovery, in posting order, empty when disabled.

    Each entry carries `kind` (one of `DISCOVERY_KINDS`), `body`, `source` (the posting member's
    name, bound by the tool rather than reported by the model), `delivered` (who the fan-out
    reached, `"lead"` included), and `failed` (name → the error that notify raised). Plain dicts
    for `hiring_log`'s reason: an audit surface a reader walks, not a contract a caller binds to.
    The durable record — a fork drops pending notifies (`gated.py`, measured), so this list is
    what survives when the in-flight deliveries do not.
    """

    input_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float

    @model_serializer(mode="wrap")
    def _without_empty_negotiation(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialise without the `negotiation` and `worklog` keys when those phases did not run.

        Backward compatibility as a property of the artifact rather than a hope: the demo's
        `investigation.json` is published with nine keys (`demo/warroom.py` pins "same nine keys,
        same order"), and a team that never negotiated — and never enabled a worklog — must keep
        producing exactly that shape. Dropped only when empty, so a run that *did* use either
        phase reports it; validation fills the default back in, so
        `deserialize_result(serialize_result(run)) == run` still holds for a narrowed run type.
        """
        data = handler(self)
        if not self.negotiation:
            data.pop("negotiation", None)
        if not self.worklog:
            data.pop("worklog", None)
        return data


# ── The hiring seam ──


def hiring_tools(
    roster: Roster,
    catalog: Mapping[str, Callable[[str], Recruit]],
    *,
    max_hires: int = 4,
    worklog: Worklog | None = None,
    dynamic: Callable[[str, str], Recruit] | None = None,
) -> Callable[[ThreadContext], ThreadKwargs]:
    """Build a `config_hook` granting hire/delegate/dismiss for one cycle.

    The library injects two peer tools into every thread — `list_threads` to discover peers and
    `send_message` to talk to them — so an agent can reach a thread that already exists. It
    cannot create one. `ThreadConfig` documents `config_hook` as the place to inject
    "`spawn_thread` closed over the current runtime and `thread_id`" (`config.py:166-185`) and
    nothing upstream ships that tool. This is it, roster-agnostically.

    **A hire reserves its name and its headcount before it awaits anything.** The three refusals
    and the registration into `roster.hires` run in one synchronous stretch, and only then is the
    spawn awaited — a rollback undoing the reservation if it raises. The reason is measured: the
    runtime's default tool executor is `ConcurrentToolExecutor` (`strands/agent/agent.py:462`), so
    two `hire` calls in *one* assistant turn run as two interleaved tasks. With the registration on
    the far side of the `await`, both read the same pre-hire roster, both pass, and the cap is a
    number nobody enforced; two hires sharing one name are worse — the second overwrites the first
    in `roster.hires`, and the first's live thread is then unreachable by `dismiss`, by `execute`'s
    `finally` and by `teardown`. A reservation is preferred to a `Lock` because it needs no
    contention surface: the checks and the write already happen inside one event-loop step.

    Args:
        roster: Where hires and the action log are recorded. The caller keeps the reference, so
            a `Team` can report the log after the lead's thread is gone. A `Team` replaces its
            roster at the top of every `execute`, so a hook built here belongs to one run.
        catalog: Role name → factory, called as `factory(name)`. A plain mapping the caller
            supplies rather than a registry of the library's own: what a team may hire is a
            property of that team, and a module-level `ROSTER` would make every team share one.
            Whatever a factory returns must satisfy `Recruit`.
        max_hires: Headcount cap. The runtime enforces no depth or breadth limit of its own, so
            a confused lead can spawn without bound unless something here says no.
        dynamic: Name + instructions → recruit, or `None` — and `None` is the default and the
            recommendation. When supplied, a fourth tool `hire_dynamic` joins the three: the
            lead writes a new subagent's instructions itself, at runtime, instead of choosing
            from the reviewed catalog (`DynamicAgent` carries the argument). A separate tool
            rather than a sentinel role in `catalog`, deliberately: the catalog `hire`'s
            contract stays byte-identical whether or not synthesis is enabled, and the roster's
            log distinguishes a reviewed-catalog hire (`"hire"`) from a synthesized one
            (`"hire_dynamic"`, instructions recorded verbatim) — the audit trail is the safety
            story for prompts nobody reviewed. Every hire discipline is shared: the same
            `max_hires` budget, the same name reservation, the same worklog equip, and the
            same `delegate`/`dismiss` reach both kinds, because both live in one roster.

    Returns:
        A `config_hook`. Every cycle it rebuilds the three tools against *that* cycle's
        `ThreadContext`, which is what makes `parent_id=ctx.thread_id` correct: the agent doing
        the hiring is the one the runtime records as the parent, and that `THREAD_SPAWNED` edge
        is the only thing `subtree_usage` walks (`runtime/usage.py:65-72`). Cost attribution up
        the tree is then a free consequence of hiring correctly.

    **Mandate goes through the factory, not onto the instance.** `demo/staffing.py:109` sets
    `sub.mandate = mandate` behind a `type: ignore`, which works because every roster class
    happens to declare the attribute. A library cannot assume that: the `Recruit` protocol says
    nothing about a mandate, and injecting one would either fail on a `__slots__` recruit or
    silently create a field nothing reads. So the mandate reaches the tool as an argument and
    the *factory* decides what to do with it — a closure or a `partial` over whatever the
    recruit's constructor actually takes. Wave 2's binding keeps the demo's behaviour inside its
    own factories, where the attribute is real.
    """

    def hook(ctx: ThreadContext) -> ThreadKwargs:
        return {"tools": list(_hiring(ctx, roster, catalog, max_hires, worklog, dynamic))}

    return hook


def _hiring(
    ctx: ThreadContext,
    roster: Roster,
    catalog: Mapping[str, Callable[[str], Recruit]],
    max_hires: int,
    worklog: Worklog | None = None,
    dynamic: Callable[[str, str], Recruit] | None = None,
) -> list[AgentTool]:
    """The three tools, bound to one cycle's context.

    Every failure here returns text rather than raising, and the reason is measured: a tool that
    returns `"error: ..."` reaches the model as a *successful* tool result whose content is that
    string, and the cycle continues — so the model reads the problem and fixes it. All three
    hire-side failures are ones a model can fix (pick a known role, pick an unused name, dismiss
    someone first), so all three are text. An exception would instead surface as a tool fault
    the model cannot act on, in the middle of a cycle it was going to complete.
    """
    roles = "; ".join(sorted(catalog)) or "(none)"

    async def commission(name: str, recruit: Recruit) -> Any:
        """Reserve, spawn, roll back on failure, and open the worklog channel — once, for both
        kinds of hire.

        Shared so the race discipline cannot drift between them: the reservation into
        `roster.hires` runs *before* the spawn await, and awaiting this coroutine executes its
        body synchronously up to that first genuine suspension (`await recruit.spawn`) — so the
        reservation sits in the same event-loop step as the caller's refusals, exactly as if it
        were inlined. A second tool call in the same assistant turn resumes only at an await,
        and by then the name is taken and the headcount spent. Rolled back if the spawn raises,
        so a failed hire holds neither the name nor a slot.
        """
        if worklog is not None:
            equip = getattr(recruit, "equip", None)
            if callable(equip):
                equip(discovery_tools(worklog, name))
        roster.hires[name] = recruit
        try:
            handle = await recruit.spawn(ctx.coordinator, parent_id=ctx.thread_id)
        except BaseException:
            del roster.hires[name]
            raise
        roster.thread_ids[name] = handle.id
        if worklog is not None:
            notify = getattr(handle, "notify", None)
            if callable(notify):
                # Replay first (`register` delivers every prior entry), so a hire joins knowing
                # what the team already flagged — a helper hired *because* of an obstacle
                # should not be the one teammate who never heard of it.
                await worklog.register(name, notify)
        return handle

    @strands_tool(
        name="hire",
        description=(
            "Create a new subagent that reports to you and works only on what you give it. "
            "Choose a role from the catalog, give it a short unique name, and state its mandate "
            f"in one or two sentences. Available roles -- {roles}. Hiring only creates the "
            "subagent; call delegate to give it work."
        ),
    )
    async def hire(role: str, name: str, mandate: str) -> str:
        if role not in catalog:
            return f"error: no such role {role!r}; available: {sorted(catalog)}"
        if name in roster.hires:
            return f"error: you already have a subagent named {name!r}"
        if roster.headcount >= max_hires:
            return f"error: hiring cap reached ({max_hires}); dismiss someone first"

        # Built only after all three refusals, so a rejected hire spawns nothing. A cap checked
        # after the spawn would be a cap that still spent the thread it was refusing.
        recruit = catalog[role](name)

        # `commission` registers the name *before* its spawn await, in the same synchronous
        # stretch as the three checks above (awaiting a coroutine runs its body up to the first
        # real suspension). The tool executor is concurrent, so a second `hire` in this turn
        # resumes on that first `await` and must find this name taken and this headcount spent
        # — see the reservation paragraph in `hiring_tools`.
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
            "Create a new subagent by writing its instructions yourself, when NO catalog role "
            "fits the work. Prefer hire with a catalog role whenever one fits — catalog roles "
            "were reviewed and carry their own knowledge; an agent you synthesize knows only "
            "what your instructions say. Give it a short unique name, instructions that state "
            "who it is and how it should work, and its mandate in one or two sentences. "
            "Hiring only creates the subagent; call delegate to give it work."
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
        # The same reservation discipline as `hire`, through the same `commission`: the name
        # is registered before the spawn await, so a concurrent second call in this assistant
        # turn finds it taken. Instructions are recorded VERBATIM — nobody reviewed this
        # prompt, so the audit trail is the safety story, and a truncated or paraphrased log
        # entry would be an audit of a different agent.
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
        except Exception as error:  # noqa: BLE001 — the model can retry or re-scope; see _hiring
            roster.record("delegate_failed", name=name, error=repr(error))
            return f"error: {name} failed: {error}"
        roster.record("delegate", name=name, request=request, answer=str(answer))
        return str(answer)

    @strands_tool(
        name="dismiss",
        description=(
            "Terminate a subagent you hired when its work is finished. Its answers stay in your "
            "conversation; only the live thread ends."
        ),
    )
    async def dismiss(name: str) -> str:
        recruit = roster.hires.get(name)
        if recruit is None:
            return f"error: no subagent named {name!r}"

        # Retired first, unregistered only on success, and the asymmetry is the point. A `pop`
        # before the await hands the recruit to a local variable and drops the roster's only
        # reference to it, so a `retire` that raises — a coordinator hiccup, a
        # `ThreadNotFoundError` from something else's teardown — leaves a live thread that
        # `execute`'s `finally`, `teardown` and a second `dismiss` all walk `roster.hires` to
        # find and none of them can reach. Left registered, the raise is retried by every one of
        # them, and `retire` is idempotent per `MethodThread`'s contract, so the retry is free.
        await recruit.retire()

        del roster.hires[name]
        roster.thread_ids.pop(name, None)
        if worklog is not None:
            # Closed with the thread, so later posts do not record a predictable failure
            # against a teammate the team already agreed is gone.
            worklog.channels.pop(name, None)
        roster.record("dismiss", name=name)
        return f"dismissed {name}"

    # `hire_dynamic` exists on the wire only when a synthesis factory was supplied: an absent
    # tool cannot be called wrongly, and a team that never opted in keeps the exact three-tool
    # surface it always had.
    if dynamic is not None:
        return [hire, hire_dynamic, delegate, dismiss]
    return [hire, delegate, dismiss]


# ── The worklog seam ──

DISCOVERY_KINDS = ("bears-on-teammate", "contradicts-plan", "obstacle", "dead-end")
"""The four things worth interrupting nobody about.

A closed vocabulary rather than free text, which is the worklog's whole difference from a chat
channel: AgentRadio (arXiv 2607.28430) ran its passive step over free-text broadcasts and still
measured +10.5 points, and typed payloads are this library's standing bet everywhere free text
was the alternative (`method.py`'s header). A kind the model invents is refused as text, so the
model picks a real one and posts again.
"""


@dataclass
class Worklog:
    """The team-owned discovery log for one run, plus the fan-out that makes it *passive*.

    Two halves, deliberately in one object. `entries` is the durable record `TeamRun.worklog`
    publishes — a fork drops pending notifies (`gated.py`, measured), so the list is what
    survives when in-flight deliveries do not. `channels` is the fan-out surface: member name →
    an async send that lands the text where that member reads it at its own next step. `notify`
    appends to a thread's log without starting a cycle (`method.py:261-268`), so a delivery here
    is *step-boundary* by construction — a teammate sees the discovery at its next model call
    and is never interrupted mid-thought, which is the interruption cost passive awareness
    exists to avoid.

    **`post` reserves before it awaits**, the hiring seam's lesson restated: the entry is
    appended to `entries` in the same synchronous stretch that builds it, and only then is any
    `send` awaited. The tool executor is concurrent (`strands/agent/agent.py:462`), so two posts
    in one assistant turn interleave — with the append on the far side of an await, both posts
    would build against the same list tail and a collision could drop one. A list rather than a
    dict-keyed aggregation for the same reason: appends cannot collide, keys can.

    **One channel failing never stops the rest.** Each delivery is awaited under its own
    handler; a retired thread raises out of `notify` and the failure is recorded on the entry —
    `failed[name] = repr(error)` — while the loop continues. A crashed fan-out would turn one
    dead teammate into a run-ending fault, which is `brief`'s `return_exceptions=True` argument
    at worklog scale.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    channels: dict[str, Callable[[str], Awaitable[None]]] = field(default_factory=dict)

    def render(self, entry: Mapping[str, Any]) -> str:
        """The text a teammate reads: attributed, kind first, one block."""
        return (
            f"[team worklog] {entry['source']} flagged {entry['kind']}: {entry['body']}\n"
            f"This is awareness, not an instruction — weigh it against what you alone know."
        )

    async def register(self, name: str, send: Callable[[str], Awaitable[None]]) -> None:
        """Open a channel, and replay every prior entry into it.

        The replay is what makes registration order not matter: the lead's channel opens only
        after its thread exists, which is *after* the briefing phase — exactly when members
        post their first discoveries. Without the replay those entries would reach every
        member and never the lead, invisibly. Deliveries here record on the entries exactly
        as `post`'s do, so the audit does not distinguish a replayed delivery from a live one
        — both answer "who saw this".
        """
        self.channels[name] = send
        for entry in list(self.entries):
            if entry["source"] == name or name in entry["delivered"] or name in entry["failed"]:
                continue
            await self._deliver(entry, name, send)

    async def post(self, kind: str, body: str, source: str) -> dict[str, Any]:
        """Append one discovery and fan it to every channel except the poster's own.

        The poster is excluded because it already knows: a discovery echoed back would spend a
        slot in its next context restating what it just said. Everyone else — the other
        members, the hires, the lead — gets the rendered text at their own next step.
        """
        entry: dict[str, Any] = {
            "kind": kind,
            "body": body,
            "source": source,
            "delivered": [],
            "failed": {},
        }
        self.entries.append(entry)  # reserved before any await — see the class docstring
        for name, send in list(self.channels.items()):
            if name == source:
                continue
            await self._deliver(entry, name, send)
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


def discovery_tools(worklog: Worklog, poster: str) -> Callable[[ThreadContext], ThreadKwargs]:
    """Build a `config_hook` granting `post_discovery` for one member's cycles.

    The `hiring_tools` precedent, member-side: `ThreadConfig` documents `config_hook` as the
    place to inject cycle-bound tools (`config.py:166-185`), and nothing upstream ships this
    one. `poster` is bound here rather than taken as a tool parameter, so the `source` on every
    entry is the name the team wired and not a name the model chose — attribution an audit can
    trust is attribution the model cannot spoof.
    """

    def hook(ctx: ThreadContext) -> ThreadKwargs:
        return {"tools": [_discovery(ctx, worklog, poster)]}

    return hook


def _discovery(ctx: ThreadContext, worklog: Worklog, poster: str) -> AgentTool:
    """The one tool, bound to one cycle's context. Failures are text, for `_hiring`'s reason:
    a wrong kind is a mistake the model can fix, so it reads the refusal and posts again."""
    kinds = ", ".join(DISCOVERY_KINDS)

    @strands_tool(
        name="post_discovery",
        description=(
            "Flag a discovery your teammates should see at their next step. Use it when you "
            "find something that bears on a teammate's work, contradicts the current plan, is "
            "an obstacle, or marks a dead end nobody should re-explore. Your teammates read it "
            f"as context, not as an interruption. kind must be one of: {kinds}."
        ),
    )
    async def post_discovery(kind: str, body: str) -> str:
        if kind not in DISCOVERY_KINDS:
            return f"error: no such kind {kind!r}; pick one of: {kinds}"
        entry = await worklog.post(kind, body, poster)
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


# ── The orchestrator ──


@dataclass
class Team:
    """A `Spawnable` that stands up a cast, runs a lead against an oracle, and grades itself.

    The skeleton is fixed and the cast is supplied. Phases, the barrier, the budget, the
    oracle attach, the usage rollup and the teardown belong to this class; who the members are,
    what the lead is, what it may hire and what *correct* means belong to the subclass. That is
    `gated.py`'s split restated at team scale — the base owns the mechanism, the subclass owns
    the judgment — and it is why this module imports nothing from `demo/`.

    **Four template methods are required and their absence is refused at wiring time.**
    `members`, `briefing`, `lead_function` and `oracle` have no defensible default. A base that
    supplied one would either run a team with nobody in it or grade every verdict correct, and
    the second is the fail-soft this kernel keeps refusing: the run reports a graded result and
    the grading never happened. `__post_init__` names every missing override at once, before any
    thread is spawned — the `recall.bound`, `gated._check_no_collision`,
    `ProcessAgent._check_no_decider_handler` precedent.

    **The roster lives for one run, and `execute` is where that is enforced.** A `Team` instance
    outlives a single `handle.run`, so the field's default is a construction-time object and
    nothing more; `execute` replaces it with a fresh instance of the same class before anything is
    spawned. Every promise attached to a roster — the headcount cap, the duplicate-name refusal,
    the hiring log a report publishes — is a promise about one run, and a roster that survived into
    the next one would make all three quietly false there.

    **What this class deliberately does not do.** No learning loop, no persistent roster, no
    cross-*team* messaging, no forkable run, no `send_message` anywhere. Cross-*member*
    awareness exists behind `worklog_enabled` — typed, step-boundary, off by default — and
    inline agent synthesis behind `dynamic_subagents` — fixed contract, verbatim-audited
    instructions, off by default. `docs/design/team.md` carries the argument for relaxing
    exactly that much and no more.
    """

    name: str = "team"
    max_hires: int = 3
    negotiation_rounds: int = 0
    """How many plan→objection→revision rounds may run between the briefing and the verdict.

    Zero — the default — is the pre-negotiation skeleton exactly: one gated lead cycle over
    `render_brief`, no fan-out, and a `TeamRun` whose `negotiation` list is empty and absent from
    the serialised artifact. A positive value bounds the phase; it never mandates it, because a
    round in which every member approves ends the negotiation early.

    A field rather than a run parameter for the reason every other knob here is one: `Team` is a
    dataclass whose configuration is its fields (`max_hires` above), a run is driven by one string
    the protocol fixes, and the round budget is a property of the *team* — how much deliberation
    this cast is worth — not of any single question it is asked.

    Evidence for the phase existing at all: AgentRadio (arXiv 2607.28430) measured negotiation as
    its single biggest layer (+67 net rubrics); one-shot plans shared a blind spot their members
    could each see. `docs/design/team.md` carries the argument and the caveats.
    """

    dynamic_subagents: bool = False
    """Whether the lead may synthesize a subagent inline, instructions and all, at runtime.

    Off — the default — is the pre-synthesis skeleton exactly: no `hire_dynamic` tool on the
    lead's wire, and the hiring surface is the catalog and nothing else. On, the lead gains
    `hire_dynamic(name, instructions, mandate)`: a `DynamicAgent` is built from the lead's own
    instructions, wrapped in the ordinary `Member` adapter (via `dynamic_recruit`, the
    overridable factory), and joins the roster under the same `max_hires` budget, the same
    name reservation, the same worklog equip, and the same `delegate`/`dismiss`/teardown reach
    as a catalog hire — no parallel path anywhere. The typed contract is fixed at review time;
    only the prompt is the model's (`DynamicAgent` carries the argument).

    Evidence for the feature existing at all: Shepherd (arXiv 2605.10913) measured runtime
    agent synthesis as a layer worth having. The cost is real — an agent whose instructions
    nobody reviewed — and the mitigation is the audit trail: the roster records the
    instructions verbatim under `kind="hire_dynamic"`, so `TeamRun.hiring_log` shows exactly
    what each synthesized agent was told to be. The tool's own description tells the lead to
    prefer catalog roles when one fits. `docs/design/team.md` carries the argument for the
    boundary and for the default being off.
    """

    worklog_enabled: bool = False
    """Whether members get a `post_discovery` tool whose posts fan back to their teammates.

    Off — the default — is the pre-worklog skeleton exactly: no tool injected, no channel
    opened, and a `TeamRun` whose `worklog` list is empty and absent from the serialised
    artifact. On, each `Member` in the cast is equipped with `discovery_tools` before it is
    spawned, every member's `MethodThread.notify` and the lead's handle become fan-out
    channels, and posted discoveries land in each teammate's *next* model context — passive
    awareness, never an interruption.

    Evidence for the feature existing at all: AgentRadio (arXiv 2607.28430) measured passive
    awareness at +10.5 points net, concentrated on cross-cutting tasks — and this skeleton's
    members hold disjoint evidence by design, which is exactly the shape where one member's
    dead end is another's answer. `docs/design/team.md` carries the argument and the
    relaxation of the no-cross-team-messaging non-goal it required.
    """

    input_shape: InputShape = InputShape.STR_PROMPT
    """The team itself is drivable by one string, so a CLI can `handle.run(question)`.

    Not a claim about the members. This shape makes the *team* addressable as a chat-style peer;
    each member's own shape is its own business, and the library's first-class member is
    `STRUCTURED`.
    """

    worklog: Worklog = field(default_factory=Worklog)
    """The discovery log for the run currently in flight, replaced at the top of `execute`.

    Per run for the roster's reason, one line down: every promise on it — the poster exclusion,
    the delivery record, the published `TeamRun.worklog` — is a promise about one run, and a
    log carried into run 2 would open run 2's report with run 1's discoveries and replay them
    into run 2's freshly spawned threads. Reset as `type(self.worklog)()` so a narrowed
    subclass keeps its class. Read it after a run for the log that run kept.
    """

    roster: Roster = field(default_factory=Roster)
    """The hiring registry for the run currently in flight, replaced at the top of `execute`.

    The class matters and the instance does not: `execute` stands up a `type(self.roster)()`, so a
    subclass narrowing this field keeps its own type on every run — `WarRoom`'s `Staff` is what
    lands a hire's mandate on the agent, and a reset that hard-coded `Roster()` would drop that on
    run 2 with nothing raised. Read it after a run for the roster that run used.
    """

    REQUIRED = ("members", "briefing", "lead_function", "oracle")
    """The overrides with no honest default, in one place.

    A class attribute rather than a module constant for the reason `GatedProposer.REASK` is one:
    a subclass that legitimately supplies one of these another way says so once.
    """

    BRIEFING_ERROR = "error: "
    """The prefix `brief` renders a failed member's briefing with, single-sourced.

    Two places read it — the rendering in `brief` and the all-failed refusal in `execute` — and
    they have to agree, because the refusal's whole job is to notice that every string in the
    mapping is one of these. A class attribute rather than two literals so a subclass that renders
    failures differently moves both at once, and so the coupling is visible rather than discovered
    when a run with a dead cast quietly reaches its lead.
    """

    APPROVAL = "APPROVED"
    """The token a member answers with to approve the lead's plan, single-sourced.

    Two places read it — the instruction `plan_request` renders for the member and the check
    `approves` runs over the answer — and they have to agree, for `BRIEFING_ERROR`'s reason: the
    check's whole job is to notice the token the instruction asked for. A class attribute so a
    subclass that wants another vocabulary moves both at once.
    """

    def __post_init__(self) -> None:
        self._check_required_overrides()
        self._check_negotiation_rounds()

    # ── What the subclass supplies ──

    def members(self) -> Sequence[Recruit]:
        """The cast, in the order it is assembled. Required.

        Called once per run, inside `execute`, so a subclass may build its members per run
        rather than at construction — and so a scripted model bound onto an agent after the
        `Team` was constructed still reaches them.
        """
        raise NotImplementedError(f"{self.name}: members() must return this team's cast")

    def briefing(self, member: Recruit) -> str:
        """The one request each member answers behind the barrier. Required.

        Per member rather than one shared string, because the interesting teams are the
        asymmetric ones: a member holding a private view needs to be told what to do with *that*
        view. A subclass wanting one opening line returns the same string for everybody.
        """
        raise NotImplementedError(
            f"{self.name}: briefing(member) must return what {member.name!r} is asked"
        )

    def lead_function(self) -> AIFunction[..., Any]:
        """The lead, compiled but NOT yet gated. Required.

        Return it *without* the oracle and *without* the hiring hook: `execute` attaches both
        through `.replace(...)`, and attaching them here too would mean the composition happened
        twice in two places with the second silently winning (`replace` overwrites — see
        `_gated_lead`). Post-conditions and a `config_hook` the lead legitimately carries for
        its own reasons are preserved; that is what `_gated_lead` is for.
        """
        raise NotImplementedError(
            f"{self.name}: lead_function() must return the lead's compiled AIFunction"
        )

    def oracle(self, response: Any) -> None:
        """Post-condition on the lead's verdict: raise `AssertionError` to refuse. Required.

        The runtime runs every validator against the result before the cycle returns and turns
        any exception into the text of a `[VALIDATION ERROR]` user turn the *next* attempt reads
        (`gated.py`'s header, `ai_thread.py:640-664`). So refusal is the default and the
        oracle's own words are the re-ask feedback — the model that has to fix the verdict is
        handed the reason without the caller writing any glue.

        Required rather than defaulted precisely because the default would have to be "raise
        nothing", i.e. grade every verdict correct while reporting that it was graded.
        """
        raise NotImplementedError(
            f"{self.name}: oracle(response) must accept or refuse the lead's verdict"
        )

    def catalog(self) -> Mapping[str, Callable[[str], Recruit]]:
        """Role name → factory the lead may hire from. Default: nothing is hireable.

        An empty catalog is an honest default and a meaningful configuration — a team whose cast
        is fixed grants no hiring tools at all, which is one fewer thing a lead can do wrong.
        """
        return {}

    def dynamic_recruit(self, name: str, instructions: str) -> Recruit:
        """Name + instructions → the recruit `hire_dynamic` spawns. Consulted only when
        `dynamic_subagents` is on.

        The default is the library's own shape: a `DynamicAgent` behind the ordinary `Member`
        adapter, so the synthesized agent satisfies `Recruit`, joins the roster, gets the
        worklog equip and is retired by every unwind path exactly as a catalog hire is — the
        skeleton learns nothing new about it. Overridable for the catalog-factory's reason:
        what a hire *runs on* is the team's business, and a test overrides this to bind a
        scripted model (`Member`'s `model=` override) so no synthesized agent can reach a real
        model from an offline suite.
        """
        return Member(DynamicAgent(name, instructions), "answer")

    def grade(self, verdict: Any) -> tuple[bool, list[str]]:
        """The post-run judgment: `(correct, failures)`. Default: `(True, [])`.

        Defaulted, unlike `oracle`, and the asymmetry is the point. By the time this runs the
        oracle has already gated: a verdict that reached here satisfied it, or the cycle
        exhausted its attempts and raised. So a team whose oracle *is* its whole standard leaves
        this alone and the report says correct, truthfully.

        It stays a separate hook because the two answer different questions. The oracle is
        checked per attempt and its text is written for a model that must revise; a grade is
        computed once, for a reader, and may apply a standard it would be wrong to re-ask
        against — a stricter check the model was not told about, or one too expensive to run on
        every attempt. `demo/warroom.py:116-118` calls `incident.verify` a second time for
        exactly that reason.
        """
        del verdict
        return True, []

    def render_brief(self, request: str, briefings: Mapping[str, str]) -> str:
        """What the lead is actually asked: the request, then what each member reported.

        The seam that makes the barrier worth holding. `brief` waits for every member and then —
        before this existed — put the answers only into the returned `TeamRun`, so the lead was
        spawned with the bare `request` and read the evidence nowhere. A team whose members all
        failed still ran its lead, cold, and `grade` could call the result correct: the paragraph
        in `brief` claiming the lead "can see in its own briefing text that one plane is missing"
        described a delivery that did not happen. It happens here.

        Rendered as one text block rather than as tools or turns, because that is the only channel
        every lead shape shares. A lead is an `AIFunction` over a typed `prompt_fn` and the
        skeleton drives it with `lead_handle.run(text)`, which binds to its first parameter for a
        `STRUCTURED` lead as much as for a `STR_PROMPT` one (measured — `docs/design/team.md`). A
        richer delivery would have to know the lead's signature, which is the subclass's business.

        Overridable because composition is a judgment: a lead that reaches its members another way
        wants them left out, and a lead with a strict prompt format wants its own headings. An
        override returning `request` unchanged restores the pre-delivery behaviour deliberately,
        which is the honest way to want it.
        """
        if not briefings:
            return request
        lines = "\n".join(f"{name}: {text}" for name, text in briefings.items())
        return f"{request}\n\nWhat your team reported:\n{lines}".strip()

    def render_plan(self, verdict: Any) -> str:
        """The lead's verdict as the text a member reviews. Default: `str(verdict)`.

        A seam for the same reason `render_brief` is one: the plan crosses to the members as text
        because text is the only channel every member shape shares — `Recruit` guarantees `ask`
        and nothing richer (`Member.ask` lands it in the typed keyword the adapter names). What
        *of* the verdict is worth a member's review is the subclass's judgment; a lead whose
        output carries private fields the members must not see overrides this and says so.
        """
        return str(verdict)

    def plan_request(self, plan: str) -> str:
        """What each member is asked when the plan fans out. Contains the plan verbatim.

        The instruction and the check have to agree on the token, which is why both read
        `APPROVAL` — an instruction asking for one word and a check looking for another would
        make unanimity unreachable and every negotiation run to its cap, silently. The plan is
        embedded rather than referenced because the member's model sees only what this string
        carries: a delivery claim needs a wire (the `render_brief` precedent), and the wire here
        is this text.
        """
        return (
            f"Your lead proposes the following plan. Review it against what you alone know. "
            f"If it is sound, answer with the single word {self.APPROVAL}. Otherwise state "
            f"your objections and what you would change.\n\nPlan:\n{plan}"
        )

    def approves(self, answer: str) -> bool:
        """Whether a member's negotiation answer counts as approval.

        Containment rather than equality, deliberately: a typed member answers with a pydantic
        model, and `str(model)` embeds the token inside `field='APPROVED'` rather than standing
        alone — an equality check would silently veto every typed member and every negotiation
        would run to its cap. The tradeoff mirrors `BRIEFING_ERROR`'s: an objection that *quotes*
        the token would be miscounted as approval, and a subclass with a stricter vocabulary
        overrides this and `plan_request` together. A rendered error can never approve — a member
        whose thread died did not review anything, so it blocks unanimity rather than faking it.
        """
        return not answer.startswith(self.BRIEFING_ERROR) and self.APPROVAL in answer

    def render_objections(self, objections: Mapping[str, str], approved: Sequence[str]) -> str:
        """What the lead is asked when revising: every non-approving answer, attributed.

        One text block, for `render_brief`'s reason — the lead's first parameter is the only
        channel every lead shape shares. The approving members are named rather than dropped
        because a lead revising against two objections should know the other two signed off:
        a revision that undoes what the approvers approved is a worse plan wearing a fix's
        clothes. Errors ride along under their `BRIEFING_ERROR` rendering, exactly as briefings
        do — a member that could not review is a fact about the plan's audit, not a secret.
        """
        lines = "\n".join(
            f"{name}: {text}" for name, text in objections.items() if name not in approved
        )
        approvals = f"\n\nAlready approved by: {', '.join(approved)}." if approved else ""
        return (
            f"Your team reviewed your plan and not everyone approved. Revise the plan to "
            f"answer the objections, or defend the parts they read wrongly."
            f"\n\nObjections:\n{lines}{approvals}"
        ).strip()

    def run_type(self) -> type[TeamRun]:
        """Which `TeamRun` class `execute` builds and `deserialize_result` validates against.

        The seam that makes the protocol's round-trip guarantee reachable. `TeamRun.verdict` is
        `Any`, and a pydantic `Any` field validates a serialised `BaseModel` back as a plain
        `dict` — measured — so `deserialize_result(serialize_result(run)) == run` is *false* for
        the base while `protocols.py` states it as an `Ensures`. A subclass declaring
        `verdict: Verdict` on a `TeamRun` subclass and naming it here round-trips equal.
        """
        return TeamRun

    # ── Spawnable ──

    def to_thread(self) -> Self:
        return self

    # ── The fixed skeleton ──

    async def execute(self, ctx: ThreadContext, request: str) -> TeamRun:
        """Assemble, brief behind a barrier, run the gated lead, grade, and retire everybody.

        Four phases in that order, in ordinary `asyncio`, with no model anywhere in the control
        flow — which is what makes a run reproducible in a way a prompt-driven orchestrator is
        not.

        **The roster is per run, replaced here rather than at construction.** One `Team` instance
        keeps one handle, and a handle runs as many times as it is called; a roster carried across
        those calls makes every guarantee attached to it false for the second run. Measured on the
        instance, running the same handle twice: run 2 opened its report with run 1's hiring log,
        found run 1's names already taken, started `max_hires` short by run 1's headcount, and
        `delegate` reached a hire run 1's `finally` had already retired. So the first thing this
        does is stand up a fresh roster of the *same class* — `WarRoom` narrows the field to
        `Staff` for its mandate hook, and a base that reset to `Roster()` would silently demote
        it. Within one run the roster persists exactly as before: a cycle-1 hire is still there
        for a cycle-2 `delegate`, which is what makes the hiring seam usable at all.

        Args:
            ctx: The per-cycle context the worker builds. Its `coordinator` is what every child
                is spawned onto and its `thread_id` is the parent of all of them, so the whole
                run is one subtree in one event log.
            request: The string the team was driven with, appended to each member's briefing and
                carried into the lead's through `render_brief`.

        Returns:
            A `run_type()` instance. Usage is measured from a baseline captured *before* the
            first spawn and rolled up with `subtree_usage(since_id=baseline)`, so a team spawned
            with `seed_from` does not bill itself for the log it inherited
            (`runtime/usage.py:9-16`).
        """
        started = time.monotonic()
        baseline = await last_event_id(ctx.coordinator, ctx.thread_id)
        self.roster = type(self.roster)()
        self.worklog = type(self.worklog)()

        # The cast is *listed* and the lead is *composed* before anything is spawned, and the
        # order between them matters twice. `members()` first, because a subclass may build its
        # members there and hand them to `lead_function()` as tools. `_gated_lead()` second and
        # still before `assemble`, because that is where the wiring guards live and a guard that
        # fires after the barrier is a guard that already spent what it protects — measured with
        # the composition left in its original place, a colliding oracle was refused only after
        # both members had been spawned, briefed with a real model call, and retired.
        cast = list(self.members())
        self._check_no_duplicate_members(cast)
        if self.worklog_enabled:
            self._equip_worklog(cast)
        lead_fn = self._gated_lead()

        lead_handle: Any = None
        try:
            await self.assemble(ctx, cast)
            briefings = await self.brief(ctx, cast, request)
            self._check_some_briefing_survived(briefings)

            lead_handle = await ctx.coordinator.spawn(
                lead_fn, thread_name=f"{self.name}-lead", parent_id=ctx.thread_id
            )
            if self.worklog_enabled:
                # Registered as late as a channel can be, and the replay inside `register` is
                # what makes that safe: a discovery posted during the briefing phase — before
                # this thread existed — is delivered here, and the pending notify drains into
                # the lead's *first* model context, ahead of the `run` below.
                await self.worklog.register("lead", lead_handle.notify)
            ctx.on_event(CustomEvent(kind="team.lead_running", payload={"request": request}))
            verdict = await lead_handle.run(self.render_brief(request, briefings))
            verdict, negotiation = await self.negotiate(ctx, cast, lead_handle, verdict)
        finally:
            # Unconditional, and covering the lead too: a mid-run fault must not leave a cast of
            # live threads behind on the coordinator. `return_exceptions=True` because one
            # already-terminated member must not abort the unwind of the rest, which is the
            # exact failure an unwind loop exists to prevent.
            await asyncio.gather(
                *(member.retire() for member in cast),
                *(hire.retire() for hire in list(self.roster.hires.values())),
                *([lead_handle.terminate_now()] if lead_handle is not None else []),
                return_exceptions=True,
            )

        correct, failures = self.grade(verdict)
        usage, turns = await subtree_usage(ctx.coordinator, ctx.thread_id, since_id=baseline)
        ctx.on_event(CustomEvent(kind="team.graded", payload={"correct": correct}))

        return self.run_type()(
            verdict=verdict,
            correct=correct,
            oracle_failures=failures,
            briefings=briefings,
            hiring_log=self.roster.log,
            negotiation=negotiation,
            worklog=self.worklog.entries,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            turns=turns,
            wall_seconds=round(time.monotonic() - started, 1),
        )

    async def assemble(self, ctx: ThreadContext, cast: Sequence[Recruit]) -> None:
        """Phase 1: put every member on a live thread as this team's child.

        Sequential rather than gathered, deliberately. Spawning is cheap and does not call a
        model, so concurrency here buys nothing measurable, while a serial loop makes the event
        log's order the cast's declared order — which is what a reader of a live tape is
        matching against `members()`. The concurrency that matters is in `brief`, where the model
        calls are.

        `parent_id=ctx.thread_id` on every member is what writes the `THREAD_SPAWNED` edge
        `subtree_usage` walks, so the rollup at the end of `execute` needs no bookkeeping of its
        own.
        """
        for member in cast:
            handle = await member.spawn(ctx.coordinator, parent_id=ctx.thread_id)
            if self.worklog_enabled:
                self._open_channel(member.name, handle)
        ctx.on_event(
            CustomEvent(kind="team.assembled", payload={"members": [m.name for m in cast]})
        )

    async def brief(
        self, ctx: ThreadContext, cast: Sequence[Recruit], request: str
    ) -> dict[str, str]:
        """Phase 2: every member answers its own briefing, concurrently, behind a barrier.

        **The barrier is the design, not an implementation detail.** A lead that started
        interrogating while half the team was still reading would produce a verdict whose
        evidence depended on scheduling — the same team, the same data, a different answer. So
        `asyncio.gather` waits for all of them, and a member that fails becomes a rendered error
        string in the returned mapping rather than an exception that takes the run down:
        `return_exceptions=True`. A four-member team in which one member's thread died is still a
        team worth asking, and `render_brief` puts these strings — the surviving answers and the
        `BRIEFING_ERROR` ones alike — into the text the lead is asked, which is what lets it see
        that one plane is missing. That delivery is a separate method and was once absent
        altogether; the claim is now `render_brief`'s to keep. Measured: `gather` starts every
        coroutine before any completes and returns exceptions positionally, so the pairing below
        is sound.

        Returns:
            Member name → its answer as a string, or `BRIEFING_ERROR + repr` for the ones that
            raised. Keyed by name, which `_check_no_duplicate_members` is what makes lossless —
            two members sharing a name would collapse to one entry and drop a briefing silently.
            A subclass wanting a different key overrides this whole phase, as `WarRoom` does to
            key by plane.
        """
        answers = await asyncio.gather(
            *(member.ask(f"{self.briefing(member)}\n\n{request}".strip()) for member in cast),
            return_exceptions=True,
        )
        briefings = {
            member.name: (
                f"{self.BRIEFING_ERROR}{answer!r}"
                if isinstance(answer, BaseException)
                else str(answer)
            )
            for member, answer in zip(cast, answers, strict=True)
        }
        ctx.on_event(CustomEvent(kind="team.briefings_in", payload={"count": len(briefings)}))
        return briefings

    async def negotiate(
        self, ctx: ThreadContext, cast: Sequence[Recruit], lead_handle: Any, verdict: Any
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Phase 2½, optional: fan the lead's plan to the members, gather objections, revise.

        Bounded by `negotiation_rounds` and off by default — with the budget at zero this
        returns `(verdict, [])` before touching anything, and `execute` is byte-for-byte the
        pre-negotiation skeleton. The evidence for wanting it at all is AgentRadio's
        (arXiv 2607.28430): the members hold disjoint evidence *by design*, so a plan drafted
        from their one-shot briefings can carry a flaw any one of them would catch on sight —
        and negotiation was their single biggest measured layer (+67 net rubrics).

        One round is: `render_plan(verdict)` fans out inside `plan_request` via each member's
        own `ask` — the same barrier, the same `return_exceptions=True`, the same error
        rendering as `brief`, because a member that dies mid-objection is a briefing failure's
        twin and must not take the run down. Then either every member `approves` and the
        negotiation ends early (`unanimous`), or the objections go back to the lead as one
        `run(render_objections(...))` — a full gated cycle, so the *revised* plan faces the
        oracle exactly as the draft did. A round whose revision lands on the cap is marked
        `cap_reached`: the run proceeds with that last gated revision, and the transcript says
        the team never reached unanimity rather than implying it did.

        The objections travel through `ask` rather than `notify`, deliberately. `Recruit`
        guarantees three verbs (`team.py`'s protocol) and `notify` is not one of them — the
        `Member` adapter deliberately does not wrap it — and an answer is *wanted* here, which
        notify by construction never produces. The lead's revision likewise rides `run` rather
        than a side channel, because the runtime turns a refused revision into re-ask feedback
        only on that path.

        Returns the final verdict and the transcript `execute` records on the `TeamRun` —
        delivery into the report is this method's contract, and delivery into the *prompts* is
        pinned by tests reading the scripted models' own contexts, for the `render_brief`
        precedent's reason: a transcript can be populated by a phase whose text never arrived.
        """
        if not cast:
            # A team with no members has nobody to negotiate with, and the alternative is worse
            # than a no-op: an empty round is vacuously unanimous (everyone of nobody approved),
            # so the transcript would record a consensus that no member ever gave. `Toy(cast=[])`
            # is the shape half the hiring tests use, and it must stay negotiation-silent.
            return verdict, []

        transcript: list[dict[str, Any]] = []
        for round_number in range(1, self.negotiation_rounds + 1):
            plan = self.render_plan(verdict)
            answers = await asyncio.gather(
                *(member.ask(self.plan_request(plan)) for member in cast),
                return_exceptions=True,
            )
            objections = {
                member.name: (
                    f"{self.BRIEFING_ERROR}{answer!r}"
                    if isinstance(answer, BaseException)
                    else str(answer)
                )
                for member, answer in zip(cast, answers, strict=True)
            }
            approved = [name for name, text in objections.items() if self.approves(text)]
            entry: dict[str, Any] = {
                "round": round_number,
                "plan": plan,
                "objections": objections,
                "approved": approved,
            }

            if len(approved) == len(objections):
                entry["outcome"] = "unanimous"
                transcript.append(entry)
                ctx.on_event(
                    CustomEvent(
                        kind="team.negotiated",
                        payload={"rounds": round_number, "outcome": "unanimous"},
                    )
                )
                return verdict, transcript

            verdict = await lead_handle.run(self.render_objections(objections, approved))
            entry["revision"] = self.render_plan(verdict)
            entry["outcome"] = (
                "cap_reached" if round_number == self.negotiation_rounds else "revised"
            )
            transcript.append(entry)

        if transcript:
            ctx.on_event(
                CustomEvent(
                    kind="team.negotiated",
                    payload={"rounds": len(transcript), "outcome": transcript[-1]["outcome"]},
                )
            )
        return verdict, transcript

    def _equip_worklog(self, cast: Sequence[Recruit]) -> None:
        """Give every equippable member a `post_discovery` tool bound to its own name.

        Only `Member`s (and anything else exposing `equip`) get the tool: `Recruit` guarantees
        three verbs and a hook seam is not one of them, so a recruit shape without the seam
        joins the team exactly as before — it can still *receive* discoveries if its handle
        exposes `notify` (see `_open_channel`), it just cannot post them. Skipped rather than
        refused because the mixed cast is legitimate: a scripted spy next to two typed members
        is half this suite's shape.
        """
        for member in cast:
            equip = getattr(member, "equip", None)
            if callable(equip):
                equip(discovery_tools(self.worklog, member.name))

    def _open_channel(self, name: str, handle: Any) -> None:
        """Register one spawned thread as a worklog channel, if it can receive at all.

        The channel is the handle's own `notify` (`method.py:261-268` for a `Member`'s
        `MethodThread`, `handle.py:120` for a raw `ThreadHandle`): append to the thread's log,
        no cycle started, read at the next model-call boundary — step-boundary delivery by
        construction. A handle without `notify` (a test spy's fake) simply gets no channel,
        for `_equip_worklog`'s reason: the mixed cast is legitimate and a member that cannot
        receive is a fact, not a fault. Synchronous registration, deliberately — at
        `assemble` time the worklog is empty, so `register`'s replay has nothing to do, and
        opening the channel inside the spawn loop keeps the assembled order the declared one.
        """
        notify = getattr(handle, "notify", None)
        if callable(notify):
            self.worklog.channels[name] = notify

    def _gated_lead(self) -> AIFunction[..., Any]:
        """The lead with the oracle prepended and the hiring hook attached, losing nothing.

        `AIFunction.replace` merges through `dataclasses.replace` (`ai_function.py:407`,
        `_merge_config`:32-49), so a field named in the call **overwrites** rather than appends —
        measured: a template carrying `post_conditions=(existing,)` replaced with `[added]` ends
        up with `['added']` alone. A naive `replace(post_conditions=[self.oracle])` would
        therefore silently delete every post-condition the subclass's lead already carried, and
        the failure mode is the worst available: the checks are gone, nothing raises, and the run
        reports a gated verdict. So the lead's own conditions are read off its config and kept,
        with the oracle *prepended* — `gated.gated()`'s composition, for the same reason.

        `config_hook` cannot be composed the same way, because a hook is one callable and the
        runtime calls exactly one (`ai_thread.py:548-553`). A lead that arrives with its own hook
        and a team that has a catalog is a genuine conflict, and it is refused loudly here rather
        than resolved by precedence: silently dropping either one costs the lead its tools or the
        team its hiring, invisibly. A subclass that needs both composes them itself and returns a
        lead whose single hook does everything, or supplies its own tools through the compiled
        `tools=` instead.
        """
        lead = self.lead_function()
        catalog = self.catalog()
        conditions = [self.oracle, *lead.config.post_conditions]
        self._check_no_oracle_collision(lead, conditions)
        if not catalog and not self.dynamic_subagents:
            return lead.replace(post_conditions=conditions)
        if lead.config.config_hook is not None:
            raise RuntimeError(
                f"{self.name}: lead_function() already carries a config_hook and this team has a "
                f"hiring surface (a catalog, or dynamic_subagents), but the runtime calls exactly "
                f"one hook per cycle — attaching the hiring tools would drop the lead's hook and "
                f"its tools with it. Compose them into one hook in lead_function(), or disable "
                f"hiring for this team."
            )
        return lead.replace(
            post_conditions=conditions,
            config_hook=hiring_tools(
                self.roster,
                catalog,
                max_hires=self.max_hires,
                worklog=self.worklog if self.worklog_enabled else None,
                dynamic=self.dynamic_recruit if self.dynamic_subagents else None,
            ),
        )

    # ── Remaining Thread protocol surface ──

    async def notify(self, text: str) -> None:
        """Accepted and ignored: the phases are fixed, so there is no boundary to observe.

        `Thread.notify` documents a no-op as the sanctioned implementation for threads that
        ignore injections, and a team is one — the interesting side channel is a *member's*
        `notify`, which reaches a live agent's next cycle.
        """
        del text

    async def fork(self) -> Self:
        """Refused. A team run is not forkable, and the protocol allows saying so.

        A fork copies the event log, which is the whole of an `AIThread`'s state and nowhere near
        the whole of a team's: the members are live threads owned by this instance and the roster
        is a mutable registry. A fork would share both, so two "independent" branches would
        retire each other's members and hire into one dict. `protocols.py:235` names
        `NotImplementedError` as the honest answer.
        """
        raise NotImplementedError(f"{self.name}: a team run is not forkable")

    async def teardown(self) -> None:
        """Release the hires when something outside the run terminates this thread.

        The `finally` in `execute` covers the normal and the faulting path; this covers the third
        one — `terminate_now` from a supervisor or a coordinator shutdown, where `execute` may
        never have been entered. Measured: the worker awaits this on termination and does *not*
        await it when `execute` raises, so both are needed and neither is redundant. `retire` is
        idempotent per `MethodThread`'s contract, so overlapping is harmless.
        """
        await asyncio.gather(
            *(hire.retire() for hire in list(self.roster.hires.values())), return_exceptions=True
        )

    def serialize_result(self, result: TeamRun) -> str:
        return result.model_dump_json(indent=2)

    def deserialize_result(self, payload: str) -> TeamRun:
        return self.run_type().model_validate_json(payload)

    # ── Guards ──

    def _check_no_duplicate_members(self, cast: Sequence[Recruit]) -> None:
        """Refuse a cast with two members under one name, before anything is spawned.

        `brief` keys its mapping by `member.name`, because a name is the only identity `Recruit`
        guarantees. So a cast holding two members called `plane` produces a mapping with *one*
        entry — the last one wins, the earlier briefing is gone from `TeamRun.briefings` and from
        whatever the lead is shown, and no exception marks the loss. Measured on a two-member cast
        answering `FIRST` and `SECOND`: the report carried `{'plane': 'SECOND'}` and the run was
        graded correct. The half that costs most is the one the report cannot show — a reader
        counting `len(briefings)` against `len(members())` is the only person who could notice.

        Here rather than in `brief` for the reason `_gated_lead`'s placement records: this is a
        wiring mistake, and a wiring mistake reported after the barrier has already spent a spawn
        and a real model call per member. Placed with the other pre-spawn guards, it costs
        nothing. And the fix is the caller's either way — name them apart, or override `brief` and
        key by something else, which is what a team with a genuine reason to repeat a name does.
        """
        seen: set[str] = set()
        duplicates: set[str] = set()
        for member in cast:
            if member.name in seen:
                duplicates.add(member.name)
            seen.add(member.name)
        if duplicates:
            raise RuntimeError(
                f"{self.name}: members() returned more than one member named "
                f"{sorted(duplicates)!r}, and "
                f"briefings are keyed by name — the later one would overwrite the earlier and its "
                f"briefing would vanish from the report with nothing raised. Give them distinct "
                f"names, or override brief() to key by something else."
            )

    def _check_some_briefing_survived(self, briefings: Mapping[str, str]) -> None:
        """Refuse to run the lead when every member failed. Raised, not rendered.

        The one member failure that is *not* recoverable, and the asymmetry with `brief`'s
        `return_exceptions=True` is the whole argument. A four-member team missing one plane is
        still a team worth asking, because three planes of evidence remain and the lead can be
        told which one is absent. A team with nothing is not: the lead holds no evidence of its own
        — that is why there is a team — so it would reason from the request alone, produce a
        verdict shaped like a real one, and `grade` would have no way to tell. Measured with both
        members raising: the lead ran, its context mentioned no error at all, and the run reported
        `correct=True`.

        Raised rather than returned as text because there is no model here to fix it. A dead cast
        is a coordinator, a network or a wiring failure at the level *above* the lead, and the only
        honest report is the one that names the members and their errors to the caller — who is
        the party that can do something. An empty cast is a different thing and is allowed: a team
        that declares no members has not lost any, and `Toy(cast=[])` is the shape half these tests
        use to exercise the hiring seam alone.

        Detected from the `BRIEFING_ERROR` prefix rather than from the exceptions themselves, and
        the tradeoff is worth naming: a member whose *successful* answer begins with that prefix
        would be miscounted, and every member would have to do it for this to fire. The alternative
        — pairing exceptions positionally inside `brief` — costs more than it buys, because `brief`
        is the overridable phase and a subclass re-keying its mapping (`WarRoom` keys by plane)
        would have to thread the exceptions through too. The prefix is the contract between the two
        halves, which is why it is one class attribute and not two literals.
        """
        if not briefings or not all(
            text.startswith(self.BRIEFING_ERROR) for text in briefings.values()
        ):
            return
        detail = "; ".join(f"{name}: {text}" for name, text in briefings.items())
        raise RuntimeError(
            f"{self.name}: every one of the {len(briefings)} member(s) failed its briefing, so "
            f"the lead would have no evidence at all and would rule from the request alone — a "
            f"verdict that looks graded and was not. Refused before the lead is spawned. The "
            f"failures were — {detail}"
        )

    def _check_no_oracle_collision(
        self, lead: AIFunction[..., Any], conditions: Sequence[Any]
    ) -> None:
        """Refuse at wiring time if a post-condition's result parameter shares a lead parameter.

        `ai_thread` passes the result positionally and then injects, by keyword, every bound
        argument whose name appears in the validator's signature (`ai_thread.py:1016-1018`). Those
        two rules are useful together — a validator that wants the call's `question` just names
        it — and fatal for the *first* parameter, which already holds the result: the same slot
        filled twice raises `TypeError: got multiple values for argument`, which the runtime then
        catches and reports to the model as a validation failure. The oracle appears to refuse
        every verdict, the message makes no sense, and the fix is a one-word rename nothing points
        at.

        This is `gated._check_no_collision` at team scale, and it is reachable here for a reason
        measured during this build: a lead is an `AIFunction` over a typed `prompt_fn`, so
        `decide(question, rigour)` is the ordinary shape and an oracle whose first parameter is
        named `question` is one careless rename away. Every attached condition is checked, not
        just the oracle — the trap is a property of the runtime's kwarg injection and an extra
        condition hits it the same way.

        Only the first parameter, deliberately: forbidding the rest would forbid the injection the
        runtime documents and this class has no reason to prevent.
        """
        parameters = set(inspect.signature(lead.prompt_fn).parameters)
        for condition in conditions:
            first = self._first_parameter(condition)
            if first is not None and first in parameters:
                label = getattr(condition, "__name__", repr(condition))
                raise RuntimeError(
                    f"{self.name}: the post-condition {label!r}'s result parameter is named "
                    f"{first!r}, which is also a parameter of the lead; the runtime would pass "
                    f"the verdict positionally and {first!r} by keyword, and `TypeError: got "
                    f"multiple values for argument {first!r}` is then reported to the model as a "
                    f"validation failure — so the oracle would appear to refuse every verdict for "
                    f"a reason no model can act on. Rename one of them."
                )

    @staticmethod
    def _first_parameter(validator: Any) -> str | None:
        """The name of the slot the verdict lands in, or None if the validator takes no args."""
        positional = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        names = [
            name
            for name, parameter in inspect.signature(validator).parameters.items()
            if parameter.kind in positional
        ]
        return names[0] if names else None

    def _check_negotiation_rounds(self) -> None:
        """Refuse a negative round budget at construction, where every wiring refusal lives.

        `range(1, 0)` is empty, so a negative value would *behave* exactly like zero — a
        configuration that reads as "negotiate backwards" silently meaning "never negotiate" is
        the fail-soft this kernel keeps refusing. Zero is the meaningful default and stays
        allowed; anything below it is a typo, and a typo is the caller's to see now rather than
        a run's to hide.
        """
        if self.negotiation_rounds < 0:
            raise RuntimeError(
                f"{self.name}: negotiation_rounds={self.negotiation_rounds} is negative, which "
                f"would silently behave as 0 — pass 0 to disable negotiation, or a positive cap."
            )

    def _check_required_overrides(self) -> None:
        """Refuse a team missing any required override, naming all of them at once.

        At construction, so nothing has been spawned and no model has been called. Every
        omission is reported together rather than one per run: a team with three missing
        overrides would otherwise cost three failed wirings to diagnose, and the reader fixing
        them has the same amount of work either way.

        Compared against the base's *function objects* rather than by `hasattr`, because every
        one of them exists — they are defined right here and raise. `getattr(type(self), name)`
        is the plain function for a normal method, so `is` against `Team`'s own is exactly the
        question "did anybody override this".
        """
        missing = [
            name for name in self.REQUIRED if getattr(type(self), name, None) is getattr(Team, name)
        ]
        if missing:
            raise RuntimeError(
                f"{self.name}: {missing!r} must be overridden before this team can run. Each has "
                f"no defensible default: a base members() would run a team with nobody in it, "
                f"and a base oracle() that raised nothing would grade every verdict correct "
                f"while reporting that it had been graded. Refused here, at construction, "
                f"before any thread is spawned."
            )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} max_hires={self.max_hires}>"
