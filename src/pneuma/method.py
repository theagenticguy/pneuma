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

import functools
import inspect
import typing
from collections.abc import Callable
from typing import Any

import tstr
from ai_functions import AIFunction
from ai_functions.ai_thread.config import ThreadConfig
from ai_functions.types import InputShape

_MARKER = "__ai_method__"


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
    owner = getattr(instance, "name", None) or type(instance).__name__.lower()
    config = ThreadConfig(
        name=settings.pop("name", None) or f"{owner}.{name}",
        description=settings.pop("description", None) or doc.split("\n", 1)[0],
        **settings,
    )
    return AIFunction(prompt_fn, spec.output_type, config)


class MethodAgent:
    """An object whose `@ai_method`s are typed AI functions over its own state.

    Every compiled method is an `AIFunction`, which is a `ToolProvider` — so one
    agent hands another a *typed* tool per capability rather than a chat box, and
    `agents(...)` collects them for exactly that purpose.
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
