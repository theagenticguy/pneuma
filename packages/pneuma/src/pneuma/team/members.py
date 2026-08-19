"""The member shapes a team accepts: the `Recruit` protocol and its two library adapters.

Moved verbatim from the old flat `team.py` — the protocol, the `Member`
adapter and `DynamicAgent` are the parts of the old module that were never about phases or
oracles, so they survive the hooks rebuild unchanged. They live beside `core.py` rather than
inside it because they answer a different question: `core.py` says what a team *does* with a
member, this module says what shapes may *be* one. Wave 2's hiring hook builds on the same
three verbs.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.types import ThreadContext

from ..method import MethodAgent, ai_method

__all__ = ["DynamicAgent", "Member", "Recruit"]


@runtime_checkable
class Recruit(Protocol):
    """A member or a hire: something with a name that spawns, answers once, and retires.

    Three verbs and nothing else, because that is the whole of what the skeleton does to a
    member — it stands one up, asks it one question, and takes it down in the `finally`.
    Anything richer would be a contract the library cannot honour for every member shape it
    wants to accept.

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
        assembled — a wiring mistake reported from the middle of a run.
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

        The seam hook-contributed member tools ride in on: the core equips each member with one
        composed hook between construction and `assemble`, without the caller writing any
        wiring. Refused when the member was constructed with its own `config_hook=` override,
        because the runtime calls exactly one hook per cycle (`ai_thread.py:548-554`,
        executable: tests/library/test_ai_functions_contract.py), so
        silently dropping either side costs tools invisibly. A *previously equipped* hook is
        replaced rather than refused: the equipped slot is team-owned, and a second run on the
        same handle re-equips the same cast.

        The hook's `tools` patch replaces the compiled tools for the cycle (the merge
        semantics `config.py:166-185` documents; executable:
        tests/library/test_ai_functions_contract.py), so `spawn` composes the member's own
        `tools=` override back in ahead of whatever the hook adds — a member that carried
        tools must not lose them to a hook it never asked about.
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
    one. A hiring tool refuses the same case as text *before* construction, so the model reads
    the problem; this guard is for every other caller.
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
