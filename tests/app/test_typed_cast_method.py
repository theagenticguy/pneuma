"""`@ai_method` against the demo's typed cast: private planes, enums, executed bodies.

These exercise the decorator through `pneuma.demo.typed_cast`, so they belong with the
application rather than the library. The offline contract tests that need no cast live in
`tests/library/test_method.py`.
"""

from __future__ import annotations

import os
import tempfile
import typing

from ai_functions import JSONMemoryBackend
from ai_functions.ai_thread.config import CodeExecutionMode
from ai_functions.memory.procedural import ProceduralMarker
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn

from pneuma.demo.typed_cast import Analyst, Burst, Quant, Toolbox


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


# ── The third case: an AI function whose body is executed code ──


async def test_procedural_annotation_survives_compilation() -> None:
    """`Procedural` is `Annotated[str, ProceduralMarker(), ...]`. Resolving hints
    without `include_extras` flattens it to `str`, and the runtime then treats
    reusable code as an ordinary prompt argument — silently, with no error."""
    fn = Quant().compiled("quantify")
    annotation = fn.prompt_fn.__annotations__["toolbox"]
    assert any(isinstance(m, ProceduralMarker) for m in typing.get_args(annotation))


def test_code_execution_config_reaches_the_thread() -> None:
    fn = Quant().compiled("quantify")
    assert fn.config.code_execution_mode is CodeExecutionMode.LOCAL


async def test_agent_executes_a_recalled_helper_in_the_sandbox() -> None:
    """Reusable *code*, end to end: `parse_rows` comes out of procedural memory, the
    runtime defines it in the sandbox, and the numbers are computed there.

    The sentinel matters. `final_answer` takes the output model's fields as keyword
    arguments, and getting that wrong fails *silently* — the executor records an
    error, the cycle continues, and a later structured turn supplies the answer
    instead. Tagging `peak_at` proves which path produced the value.
    """
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    async with RuntimeHarness():
        memory = JSONMemoryBackend(Toolbox, "quant", path=path)
        try:
            quant = Quant()
            fallback = {
                "peak_value": -99.0,
                "peak_at": "FROM_FALLBACK",
                "baseline": 1.0,
                "multiple": -99.0,
            }
            model = ScriptedModel(
                [
                    Turn(
                        tool_calls=(
                            (
                                "python_executor",
                                {
                                    "code": "parsed = parse_rows(rows)\n"
                                    "final_answer(peak_value=float(len(parsed)), "
                                    "peak_at='FROM_SANDBOX', baseline=1.0, "
                                    "multiple=float(len(parsed)))"
                                },
                            ),
                        )
                    ),
                    Turn(tool_calls=(("Burst", fallback),)),
                    Turn(tool_calls=(("Burst", fallback),)),
                ]
            )
            fn = Quant().compiled("quantify", model=model)
            result = await fn.trace(
                "p99_ms", quant.plane_text("metrics"), await memory.recall("helpers")
            )
            assert isinstance(result.value, Burst)
            assert result.value.peak_at == "FROM_SANDBOX"
            assert result.value.peak_value > 1  # rows counted by the recalled helper
        finally:
            memory.close()


async def test_reusable_code_is_itself_a_learnable_parameter() -> None:
    """The toolbox is procedural memory, so a TextGrad step can rewrite the code."""
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    async with RuntimeHarness():
        memory = JSONMemoryBackend(Toolbox, "quant", path=path)
        try:
            model = ScriptedModel(
                [
                    Turn(
                        tool_calls=(
                            (
                                "Burst",
                                {
                                    "peak_value": 2.0,
                                    "peak_at": "x",
                                    "baseline": 1.0,
                                    "multiple": 2.0,
                                },
                            ),
                        )
                    )
                ]
                * 3
            )
            fn = Quant().compiled("quantify", model=model)
            result = await fn.trace(
                "errors", Quant().plane_text("logs"), await memory.recall("helpers")
            )
            graph = await build_graph_from_result(result, [memory])
            assert [(p.name, p.procedural) for p in graph.parameters] == [("helpers", True)]
        finally:
            memory.close()
