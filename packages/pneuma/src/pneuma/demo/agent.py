"""An object-oriented facade over `ai_functions.AIFunction`.

The library's own surface is decorator-first: `@ai_function` wraps a module-level
function whose docstring is the prompt template and whose signature fixes the
input shape. That makes an agent a *function*, so two agents that differ only in
private data have to be two functions.

`Agent` inverts it. A subclass declares the static parts of an agent in its class
body (result type, reasoning effort, tools, briefing text) and carries the varying
parts as instance attributes. Each instance compiles itself into one `AIFunction`
whose `prompt_fn` closes over `self`, so `Analyst(plane=metrics)` and
`Analyst(plane=logs)` are two live threads built from one class.

The compile step is where the two models meet. `AIFunction` infers its input
shape from the prompt function's signature — exactly one positional parameter
annotated `str` yields `InputShape.STR_PROMPT`, which is the only shape a peer
can reach through the runtime's `send_message` tool. So the generated prompt
function always has that exact signature, whatever the subclass does.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar, Self

from ai_functions import AIFunction, Coordinator, ThreadHandle
from ai_functions.ai_thread import PostCondition
from ai_functions.ai_thread.config import ThreadConfig
from ai_functions.types import InputShape, ThreadId
from strands.tools.decorator import DecoratedFunctionTool
from strands.types.tools import AgentTool

from ..model import Effort, opus5

ROSTER: dict[str, type[Agent]] = {}


class Agent:
    """Base class for an agent that compiles to one AI Function.

    Subclasses override `role`, `result_type`, and `brief()`. Everything else has
    a working default.
    """

    role: ClassVar[str] = "agent"
    purpose: ClassVar[str] = ""
    result_type: ClassVar[type] = str
    effort: ClassVar[Effort] = "xhigh"
    max_attempts: ClassVar[int] = 3
    hireable: ClassVar[bool] = True
    peer_tools: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.hireable:
            ROSTER[cls.role] = cls

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name or self.role
        self._handle: ThreadHandle[Any, Any] | None = None

    # ── Subclass extension points ──

    def brief(self, request: str) -> str:
        """Return the full prompt for one cycle. Override this."""
        raise NotImplementedError

    def system_prompt(self) -> str | None:
        return None

    def model(self) -> Any:
        """The model this agent runs on. Overridden in tests to script it."""
        return opus5(self.effort)

    def tools(self) -> list[AgentTool | Any]:
        """Tools this agent exposes. Defaults to its own `@tool` methods.

        Walks the MRO so an inherited tool is not dropped, and reads each name
        off the instance so `DecoratedFunctionTool.__get__` binds `self`. Two
        instances therefore get two distinct tool objects closing over their own
        state, and `self` never appears in the schema the model sees.
        """
        found: list[AgentTool | Any] = []
        seen: set[str] = set()
        for klass in type(self).__mro__:
            for attr, value in vars(klass).items():
                if attr in seen:
                    continue
                seen.add(attr)
                if isinstance(value, DecoratedFunctionTool):
                    found.append(getattr(self, attr))
        return found

    def post_conditions(self) -> list[PostCondition]:
        return []

    # ── Compilation to an AIFunction ──

    def build(self) -> AIFunction[[str], Any]:
        """Compile this instance into an `AIFunction` addressable by peers.

        The generated `prompt_fn` takes one `str` positional parameter so the
        library infers `InputShape.STR_PROMPT`. Its `__doc__` is left unset:
        returning a string from `prompt_fn` bypasses the docstring-as-template
        path entirely, which is what lets the prompt be computed from instance
        state rather than interpolated from a literal.
        """
        agent = self

        def prompt_fn(request: str) -> str:
            return agent.brief(request)

        prompt_fn.__name__ = self.name
        prompt_fn.__annotations__ = {"request": str, "return": str}
        prompt_fn.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)],
            return_annotation=str,
        )

        config = ThreadConfig(
            model=self.model(),
            system_prompt=self.system_prompt(),
            tools=tuple(self.tools()),
            post_conditions=tuple(self.post_conditions()),
            max_attempts=self.max_attempts,
            name=self.name,
            description=self.purpose or self.__class__.__doc__ or self.role,
            thread_name=self.name,
            coordinator_tools_enabled=self.peer_tools,
        )
        fn = AIFunction(prompt_fn, self.result_type, config)
        if fn.input_shape is not InputShape.STR_PROMPT:
            raise RuntimeError(f"{self.name} compiled to {fn.input_shape}, not str_prompt")
        return fn

    # ── Live thread management ──

    async def spawn(
        self,
        coordinator: Coordinator,
        *,
        parent_id: ThreadId | None = None,
    ) -> ThreadHandle[Any, Any]:
        self._handle = await coordinator.spawn(
            self.build(), thread_name=self.name, parent_id=parent_id
        )
        return self._handle

    @property
    def handle(self) -> ThreadHandle[Any, Any]:
        if self._handle is None:
            raise RuntimeError(f"{self.name} has not been spawned")
        return self._handle

    async def ask(self, request: str) -> Any:
        return await self.handle.run(request)

    async def retire(self) -> None:
        if self._handle is not None:
            await self._handle.terminate_now()
            self._handle = None

    def describe(self) -> str:
        return f"{self.role} ({self.name}): {self.purpose or 'no stated purpose'}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} effort={self.effort!r}>"

    @classmethod
    def roster(cls) -> dict[str, type[Self]]:
        return dict(ROSTER)  # type: ignore[return-value]
