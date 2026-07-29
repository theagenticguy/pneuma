"""Offline tests for `@ai_method`: typed contracts, templating, composition.

Every claim the decorator paradigm makes is checked against the schema the model
would actually see, not against our own bookkeeping.
"""

from __future__ import annotations

import os
import tempfile
from typing import Literal

from ai_functions import JSONMemoryBackend
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import InputShape
from ai_functions.types.graph import ParameterView, collect_nodes
from pydantic import BaseModel, Field

from pneuma.method import MethodAgent, ai_method
from pneuma.typed_cast import Analyst

Mode = Literal["fast", "thorough"]


class Answer(BaseModel):
    text: str


class Tips(BaseModel):
    guidance: str = Field(default="", description="What has been learned so far")


class Base(MethodAgent):
    name = "base"

    @ai_method(Answer, description="Inherited capability")
    def inherited(self, q: str) -> Answer:
        """Answer {q}."""


class Solver(Base):
    def __init__(self, style: str) -> None:
        self.style = style
        self.name = f"solver-{style}"

    @ai_method(Answer, description="Solve a problem in this solver's style")
    def solve(self, problem: str, mode: Mode = "fast", depth: int = 3) -> Answer:
        """Solve {problem} in the {self.style} style, {mode}, to depth {depth}."""

    @ai_method(Answer, description="Learn from accumulated guidance")
    def learn(self, guidance: str, problem: str) -> Answer:
        """Guidance so far: {guidance}

        Now solve: {problem}
        """


def scripted() -> ScriptedModel:
    return ScriptedModel([Turn(tool_calls=(("Answer", {"text": "ok"}),))])


# ── The typed contract ──


def test_typed_parameters_survive_compilation() -> None:
    """The whole point: the model sees the real signature, not `request: str`."""
    fn = Solver("terse").compiled("solve")
    assert fn.input_shape is InputShape.STRUCTURED


async def test_tool_schema_carries_types_defaults_and_enums() -> None:
    """`AIFunction` is a ToolProvider; this is what a calling agent chooses from."""
    fn = Solver("terse").compiled("solve")
    schema = (await fn.load_tools())[0].tool_spec["inputSchema"]["json"]
    assert schema["properties"]["mode"]["enum"] == ["fast", "thorough"]
    assert schema["properties"]["depth"]["default"] == 3
    assert schema["required"] == ["problem"]


async def test_self_never_reaches_the_schema() -> None:
    """A bound method drops `self`, so the model is never asked to supply it."""
    fn = Solver("terse").compiled("solve")
    schema = (await fn.load_tools())[0].tool_spec["inputSchema"]["json"]
    assert set(schema["properties"]) == {"problem", "mode", "depth"}


async def test_each_capability_gets_its_own_tool_name() -> None:
    """Two `@ai_method`s on one agent must not collide when handed to a caller."""
    names = {(await fn.load_tools())[0].tool_name for fn in Solver("terse").agents()}
    assert names == {"solver-terse.solve", "solver-terse.learn", "solver-terse.inherited"}


# ── The docstring as prompt template ──


async def test_docstring_interpolates_arguments_and_instance_state() -> None:
    """Prompt stays declarative text: `{self.style}` and `{mode}` in one template."""
    prompt = await Solver("terse").compiled("solve").render_prompt("halting", mode="thorough")
    assert prompt == "Solve halting in the terse style, thorough, to depth 3."


async def test_two_instances_render_two_prompts_from_one_template() -> None:
    a = await Solver("terse").compiled("solve").render_prompt("x")
    b = await Solver("verbose").compiled("solve").render_prompt("x")
    assert "terse" in a and "verbose" in b and a != b


def test_ai_methods_are_found_across_the_mro() -> None:
    assert set(Solver("terse").ai_methods()) == {"solve", "learn", "inherited"}


# ── Composition: an agent as a typed tool, invoked as a function ──


async def test_agent_is_callable_with_typed_arguments() -> None:
    async with RuntimeHarness():
        fn = Solver("terse").compiled("solve", model=scripted())
        result = await fn("halting problem", mode="thorough", depth=1)
        assert isinstance(result, Answer)


async def test_one_agent_holds_another_as_a_tool() -> None:
    """Composition is Python typing, not a message bus."""
    async with RuntimeHarness():
        helper = Solver("terse").compiled("solve", model=scripted())
        lead = Solver("lead").compiled("learn", model=scripted(), tools=[helper])
        assert helper in lead.config.tools


# ── Learnable parameters: the gap the `str`-only shape could not close ──


async def test_recalled_memory_is_a_gradient_target() -> None:
    """`collect_nodes` reads call arguments, so guidance must arrive as one."""
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    async with RuntimeHarness():
        memory = JSONMemoryBackend(Tips, "t", path=path)
        try:
            learned = await memory.recall("guidance")
            assert isinstance(learned, ParameterView)
            assert len(collect_nodes(((learned, "problem"), {}))) == 1
        finally:
            memory.close()


async def test_traced_call_yields_an_optimizable_parameter_node() -> None:
    """The end of the chain: a real `ParameterNode` a TextGrad step can update."""
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    async with RuntimeHarness():
        memory = JSONMemoryBackend(Tips, "t", path=path)
        try:
            learned = await memory.recall("guidance")
            fn = Solver("terse").compiled("learn", model=scripted())
            result = await fn.trace(learned, "solve x")
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
            assert graph.parameters[0].requires_grad
        finally:
            memory.close()


# ── The rewritten cast keeps the demo's information asymmetry ──


async def test_analysts_keep_their_planes_private() -> None:
    metrics = await Analyst("metrics").compiled("analyze").render_prompt("13:50-14:10")
    logs = await Analyst("logs").compiled("analyze").render_prompt("13:50-14:10")
    assert "m-checkout-api" in metrics
    assert "m-checkout-api" not in logs


async def test_analyst_focus_enum_reaches_the_caller() -> None:
    fn = Analyst("traces").compiled("analyze")
    schema = (await fn.load_tools())[0].tool_spec["inputSchema"]["json"]
    assert schema["properties"]["focus"]["enum"] == [
        "latency",
        "errors",
        "saturation",
        "ordering",
    ]
