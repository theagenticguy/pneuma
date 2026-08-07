"""`@ai_method` — the library's decorator paradigm, applied to methods.

`agent.py` takes the object-oriented route to its logical end and pays for it: to stay
addressable by peers it compiles every agent down to one `str` parameter, which costs three
things the decorator gives away free. Rationale: `docs/design/method.md`.

1. **The typed contract.** `load_tools` builds an `AIFunction`'s schema from
   `inspect.signature(prompt_fn)`. A decorated function exposes `plane: Literal[...]`,
   `window: str`, `max_records: int = 20`; an `Agent` subclass exposes `request: string`.
   Composition is the whole point — an agent *is* a typed tool another agent calls — and a
   single `str` erases what makes the call checkable.

2. **The docstring as prompt template.** The decorator interpolates the docstring with the
   call's bound arguments. `Agent.brief()` concatenates strings in Python instead, so the
   prompt stops being declarative text and becomes control flow.

3. **Learnable parameters.** `TextGradOptimizer` routes gradients into `ParameterNode`s
   discovered by `collect_nodes((args, kwargs))` over the *call arguments*. State hidden on
   `self` is invisible, so an `Agent` subclass cannot be optimized at all.

A bound method recovers all three at once, because Python removes `self` from a bound
method's signature: `inspect.signature(instance.method)` is already the typed contract, while
`self` stays reachable for what varies per instance. The decorator keeps operating on a
function; the instance supplies the closure. That split is load-bearing, not stylistic — a
gradient target must be a call argument to be discoverable, and a fixed input a validator
needs belongs on `self`, where the optimizer cannot reach it.

    class Analyst(MethodAgent):
        def __init__(self, plane):
            self.evidence = load(plane)

        @ai_method(Finding, description="Analyze one plane over a window")
        def analyze(self, window: str, max_records: int = 20) -> Finding:
            '''Analyze the {self.plane} plane over {window}.
            Your private evidence: {self.evidence}
            '''

`Analyst("metrics").compiled("analyze")` is an `AIFunction` with `input_shape ==
STRUCTURED`, a tool schema carrying `window` and `max_records`, and a prompt rendered from
the docstring with this instance's evidence in it.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import typing
from collections.abc import Callable
from typing import Any

import tstr
from ai_functions import AIFunction, Coordinator, ThreadHandle
from ai_functions.ai_thread.config import ThreadConfig
from ai_functions.runtime.errors import ThreadNotFoundError
from ai_functions.types import InputShape, ThreadId

_MARKER = "__ai_method__"


def _owner_name(instance: object) -> str:
    """The name an instance's capabilities are published under.

    One definition, two consumers: the compiled tool name (`{owner}.{method}`)
    and the lifecycle's error messages. They have to agree, or an error names a
    thread the caller cannot find in the tool schema it was reading.
    """
    return getattr(instance, "name", None) or type(instance).__name__.lower()


class AIMethodSpec:
    """What `@ai_method` records on a method until an instance compiles it."""

    def __init__(self, output_type: type, config: dict[str, Any]) -> None:
        self.output_type = output_type
        self.config = config


def ai_method(output_type: type, /, **config: Any) -> Callable[[Callable[..., Any]], Any]:
    """Mark a method as an AI function whose prompt is its docstring.

    The method body is normally empty: returning `None` hands the docstring to
    the template renderer, exactly as the library's own decorator does. Return a
    string instead to compute the prompt directly and skip templating.

    Args:
        output_type: The structured type the agent must produce.
        config: `ThreadConfig` fields — `description`, `effort`, `post_conditions`,
            `max_attempts`, and so on.
    """

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        setattr(method, _MARKER, AIMethodSpec(output_type, config))
        return method

    return decorate


def is_ai_method(value: object) -> bool:
    return hasattr(value, _MARKER)


def compile_ai_method(instance: object, name: str, **overrides: Any) -> AIFunction[..., Any]:
    """Compile one `@ai_method` on `instance` into an `AIFunction`.

    The generated `prompt_fn` carries the *bound* method's signature, so the
    typed parameters reach the model and `self` does not. Rendering happens here
    rather than through the library's docstring path because that path builds its
    context from bound arguments alone, which would leave `{self.plane}` undefined.
    """
    method = getattr(instance, name)
    spec: AIMethodSpec = getattr(getattr(type(instance), name), _MARKER)
    signature = inspect.signature(method)
    doc = inspect.getdoc(method) or ""
    globalns = getattr(method.__func__, "__globals__", {})

    @functools.wraps(method)
    async def prompt_fn(*args: Any, **kwargs: Any) -> str:
        computed = method(*args, **kwargs)
        if inspect.iscoroutine(computed):
            computed = await computed
        if computed is not None:
            return str(computed)
        if not doc:
            raise ValueError(f"{type(instance).__name__}.{name} has no docstring and returned None")
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        context: dict[str, object] = {"self": instance, **bound.arguments}
        template = tstr.generate_template(doc, context, globals=globalns, use_eval=True)
        return tstr.render(template)

    prompt_fn.__signature__ = signature  # type: ignore[attr-defined]
    prompt_fn.__doc__ = None  # rendering is ours; keep the library off the docstring path

    # `load_tools` runs `get_type_hints` on this function to build the tool schema.
    # Under `from __future__ import annotations` the method's hints are strings that
    # only resolve against *its* module's globals, not this module's, so leaving them
    # as strings raises `NameError` on any type declared beside the agent. Resolving
    # them here stores real objects, which `get_type_hints` then returns untouched.
    #
    # `include_extras=True` is load-bearing: markers like `Procedural` are
    # `Annotated[str, ProceduralMarker(), ...]`, and the runtime reads that metadata
    # to decide whether a parameter is reusable code it should define in the
    # execution environment. Resolving without extras flattens it to plain `str`
    # and the code silently becomes an ordinary prompt argument.
    resolved = typing.get_type_hints(method.__func__, include_extras=True)
    resolved.pop("self", None)
    prompt_fn.__annotations__ = resolved

    # The name doubles as the tool name, so it has to identify the capability and
    # not just the agent: an agent with two `@ai_method`s would otherwise hand a
    # caller two tools with the same name, and the second would shadow the first.
    settings = {**spec.config, **overrides}
    owner = _owner_name(instance)
    config = ThreadConfig(
        name=settings.pop("name", None) or f"{owner}.{name}",
        description=settings.pop("description", None) or doc.split("\n", 1)[0],
        **settings,
    )
    return AIFunction(prompt_fn, spec.output_type, config)


class MethodThread:
    """One `@ai_method` running as a live thread: typed calls, shared history.

    `compiled(name)` is stateless — awaiting the `AIFunction` spawns a thread, runs
    one cycle, and tears it down, so two calls share nothing. A `MethodThread` keeps
    the thread alive between calls, which is what turns a capability into a
    *conversation*: `run(problem="halting")` then `run(problem="collatz")` and the
    second cycle's model context contains the first cycle's turns.

    **Why the unit is a method-thread, not an agent-thread.** A runtime thread wraps
    exactly one `Spawnable` — one `AIFunction`, one `prompt_fn`, one typed signature.
    An agent with three `@ai_method`s therefore has three threads, not one thread it
    multiplexes: there is no signature that is simultaneously `verify(claim: str)`
    and `determine(facts: list[str])`. Threads are cheap, and one Caseworker holding
    `verify` + `determine` + `advise` live at once is the normal shape. When a
    capability genuinely needs its sibling's context, `spawn(..., seed_from=other.id)`
    copies the sibling's log into the new thread — a deliberate handoff at a named
    point rather than ambient shared mutable state.

    **Why history is not kept on `self`.** It is tempting to accumulate turns on the
    agent instance and re-render them into each prompt. That would be invisible to the
    optimizer: `TextGradOptimizer` routes gradients into `ParameterNode`s that
    `collect_nodes` discovers by walking the *call arguments*, so anything hidden on
    `self` cannot be a gradient target (see this module's header). It would also
    duplicate machinery the runtime owns — history is the coordinator's event log,
    reconstructed fresh per cycle — and the two copies would drift the moment a cycle
    was summarized, forked, or replayed. So this class holds a `ThreadHandle` and
    nothing else that resembles state; the log is the single source of truth.

    **The `send_message` boundary.** A `MethodThread` is deliberately not addressable
    by `send_message`: that tool only targets threads whose input shape is
    `STR_PROMPT`, and a `MethodAgent` compiles to `STRUCTURED` precisely so its
    parameters stay typed. That is the tradeoff this module's header argues for, and
    it costs nothing here — peers reach a capability as a *typed tool* (`agents()`),
    which is checkable, where a chat box is not. `notify()` is the inbound side
    channel for the cases that still want one: it appends to the thread's log without
    starting a cycle, so the next `run` sees it as context.
    """

    __slots__ = ("_agent", "_method", "_handle", "_coordinator", "_live", "_qualified")

    def __init__(
        self,
        agent: MethodAgent,
        method: str,
        handle: ThreadHandle[..., Any],
        coordinator: Coordinator,
        *,
        qualified: str | None = None,
    ) -> None:
        self._agent = agent
        self._method = method
        self._handle = handle
        self._coordinator = coordinator
        self._live = True
        self._qualified = qualified or f"{_owner_name(agent)}.{method}"

    # ── Identity ──

    @property
    def id(self) -> ThreadId:
        """The underlying thread id — what `seed_from` and `parent_id` take."""
        return self._handle.id

    @property
    def live(self) -> bool:
        """False once `retire()` has run. No lifecycle op respawns a dead thread."""
        return self._live

    @property
    def handle(self) -> ThreadHandle[..., Any]:
        """The raw handle, for the runtime operations this class does not wrap."""
        self._require_live("handle")
        return self._handle

    @property
    def qualified_name(self) -> str:
        """The name the compiled tool is published under.

        Usually `{owner}.{method}`, but an `@ai_method(..., name=...)` rename wins,
        because an error message must name a thread the caller can actually find in
        the tool schema it was reading. `spawn` passes the compiled name in.
        """
        return self._qualified

    # ── Cycles ──

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """Run one cycle, with this method's real typed signature.

        Arguments are forwarded verbatim to the compiled `prompt_fn`, so this is
        `run(problem="halting", mode="thorough")` and never `run(some_string)` —
        the typed contract survives into the live thread. A binding mistake raises
        `TypeError` from the signature bind, which is the intended fail-loud path.
        """
        self._require_live("run")
        return await self._handle.run(*args, **kwargs)

    async def notify(self, text: str) -> None:
        """Push a side-channel message into the thread's history.

        No cycle starts; the text becomes context the next `run` sees. This is the
        inbound half of the `STRUCTURED` tradeoff described in the class docstring.
        """
        self._require_live("notify")
        await self._handle.notify(text)

    # ── Topology ──

    async def fork(self, *, parent_id: ThreadId | None = None) -> MethodThread:
        """Branch into a second thread over a copy of this history.

        The fork starts byte-identical and diverges from the fork point on — one
        capability exploring two continuations of the same context, which is how a
        proposer tries variants without contaminating the line it came from.

        `parent_id` passes through untouched, so the library's default holds: the
        fork is attributed to *this thread's* parent, making it a sibling rather than
        a child. Pass `self.id` to adopt it instead.
        """
        self._require_live("fork")
        forked = await self._handle.fork(parent_id=parent_id)
        # The library's fork() takes no thread_name, so the coordinator's log names
        # the fork after the bare prompt_fn. This wrapper keeps the qualified name
        # for errors and repr; the log-side rename needs upstream support.
        return MethodThread(
            self._agent, self._method, forked, self._coordinator, qualified=self._qualified
        )

    async def retire(self) -> None:
        """Tear the thread down now, without draining queued cycles.

        Idempotent against this object *and* against the runtime: retiring twice is
        not an error, and neither is retiring a thread something else already tore
        down (a supervisor via the raw handle, a coordinator-side terminate). A
        caller unwinding several threads should not crash mid-unwind because one was
        already gone — that would leave the rest alive, which is the exact failure
        an unwind loop exists to prevent. `ThreadNotFoundError` is a `KeyError`, so
        without this catch it would sail past callers expecting `RuntimeError`.
        """
        if not self._live:
            return
        self._live = False
        with contextlib.suppress(ThreadNotFoundError):
            await self._handle.terminate_now()

    # ── Guards ──

    def _require_live(self, op: str) -> None:
        """Refuse every operation on a retired thread, naming agent and method.

        Silently respawning would be worse than raising: the caller believes it is
        continuing a conversation, and it would get a blank one with the same typed
        signature — a wrong answer that looks like a right one.
        """
        if not self._live:
            raise RuntimeError(f"{self.qualified_name}: {op} after retire (thread {self.id})")

    def __repr__(self) -> str:
        state = "live" if self._live else "retired"
        return f"<MethodThread {self.qualified_name} id={self.id!r} {state}>"


class MethodAgent:
    """An object whose `@ai_method`s are typed AI functions over its own state.

    Every compiled method is an `AIFunction`, which is a `ToolProvider` — so one
    agent hands another a *typed* tool per capability rather than a chat box, and
    `agents(...)` collects them for exactly that purpose.

    Two ways to invoke a capability. `compiled(name)` is the stateless one: await it
    and one throwaway thread runs one cycle. `spawn(name, coordinator)` is the live
    one: it returns a `MethodThread` whose successive `run` calls share history.
    """

    name: str = "agent"

    @classmethod
    def ai_methods(cls) -> list[str]:
        """Names of every `@ai_method` on this class, base classes included."""
        found: list[str] = []
        seen: set[str] = set()
        for klass in cls.__mro__:
            for attr, value in vars(klass).items():
                if attr in seen:
                    continue
                seen.add(attr)
                if is_ai_method(value):
                    found.append(attr)
        return found

    def compiled(self, name: str, **overrides: Any) -> AIFunction[..., Any]:
        fn = compile_ai_method(self, name, **overrides)
        if (
            fn.input_shape is InputShape.NO_ARGS
            and inspect.signature(getattr(self, name)).parameters
        ):
            raise RuntimeError(f"{name} lost its parameters during compilation")
        return fn

    def agents(self, **overrides: Any) -> list[AIFunction[..., Any]]:
        """This object's capabilities as typed tools another agent can call."""
        return [self.compiled(name, **overrides) for name in self.ai_methods()]

    async def spawn(
        self,
        name: str,
        coordinator: Coordinator,
        *,
        parent_id: ThreadId | None = None,
        seed_from: ThreadId | None = None,
        thread_name: str | None = None,
        **overrides: Any,
    ) -> MethodThread:
        """Put one `@ai_method` on a live thread and return its lifecycle handle.

        Args:
            name: Which `@ai_method` to spawn. One agent may hold several live at
                once; each gets its own thread, because a thread wraps one
                `Spawnable` (see `MethodThread`).
            coordinator: The coordinator to spawn onto — always the caller's, never
                an implicit one. `AIFunction.spawn()` taking no coordinator builds a
                *private* in-memory coordinator plus worker per call, which would put
                this thread outside the caller's registry: invisible to peers, no
                parent edge, its own event log. Requiring the coordinator here makes
                that mistake unrepresentable.
            parent_id: Parent thread to attribute this one to, for the supervision
                tree the runtime maintains.
            seed_from: Another thread's id whose history this thread starts from.
                This is the sanctioned cross-method handoff: a thread cannot host two
                signatures, so `determine` inherits `verify`'s context by copying its
                log at spawn time rather than by sharing a thread with it. When the
                two methods have different output types, the inherited history
                carries `toolUse` blocks for a tool absent from this thread's
                schema; reconstruction handles that offline, but whether a live
                provider accepts historical tool calls it was not offered is the
                provider's decision, not this library's.
            thread_name: Display name in the event log. Defaults to the compiled tool
                name, so logs and tool schemas use one vocabulary.
            overrides: `ThreadConfig` overrides, exactly as `compiled` takes them.
                One key is unreachable from here: `name` collides with the first
                positional parameter, so renaming the published tool is
                decorator-config only (`@ai_method(..., name=...)`).

        Compiles through `self.compiled(name, **overrides)` rather than
        `compile_ai_method` directly, and that indirection is deliberate: tests bind
        a scripted model by replacing `compiled` on the *instance*, so routing spawn
        through it makes every live thread scriptable offline for free. Going to the
        module function would silently bypass those bindings and reach a real model.

        Returns:
            A `MethodThread`. Not cached on `self`: several may be live at once, and
            an agent that stashed handles would stop being safely reusable across
            coordinators.
        """
        fn = self.compiled(name, **overrides)
        # The published tool name, which an @ai_method(name=...) rename controls —
        # errors and logs must name a thread the caller can find in the tool schema.
        published = fn.config.name or f"{_owner_name(self)}.{name}"
        handle = await coordinator.spawn(
            fn,
            thread_name=thread_name or published,
            parent_id=parent_id,
            seed_from=seed_from,
        )
        return MethodThread(self, name, handle, coordinator, qualified=published)
