"""Offline tests for `@ai_method`: typed contracts, templating, composition, threads.

Every claim the decorator paradigm makes is checked against the schema the model
would actually see, not against our own bookkeeping. The thread-lifecycle tests hold
to the same rule: history is asserted from what the *model* received, or from the
coordinator's own event log, never from state this code keeps for itself.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterable
from typing import Any, Literal

import pytest
from ai_functions import JSONMemoryBackend
from ai_functions.ai_thread.reconstruction import reconstruct_messages
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import Event, InputShape
from ai_functions.types.graph import ParameterView, collect_nodes
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, MethodThread, ai_method

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


class Caseworker(MethodAgent):
    """Two capabilities that hand work to each other — the `seed_from` fixture.

    Separate from `Solver` on purpose: `Solver`'s tool-name and MRO assertions pin an
    exact set of `@ai_method`s, and a third one there would break tests that are the
    frozen-surface regression contract.
    """

    name = "caseworker"

    @ai_method(Answer, description="Check whether a claim holds")
    def verify(self, claim: str) -> Answer:
        """Verify {claim}."""

    @ai_method(Answer, description="Decide, given what verification found")
    def determine(self, question: str) -> Answer:
        """Determine {question}."""


class Judged(BaseModel):
    ok: bool


class MixedWorker(MethodAgent):
    """Two capabilities with *different* output types — the cross-type handoff fixture.

    Module-level because `compile_ai_method` resolves annotations with
    `typing.get_type_hints` against module globals; a function-local output type
    cannot be resolved at compile time.
    """

    name = "mixedworker"

    @ai_method(Answer, description="Check whether a claim holds")
    def verify(self, claim: str) -> Answer:
        """Verify {claim}."""

    @ai_method(Judged, description="Judge, given what verification found")
    def judge(self, question: str) -> Judged:
        """Judge {question}."""


def scripted() -> ScriptedModel:
    return ScriptedModel([Turn(tool_calls=(("Answer", {"text": "ok"}),))])


class Capturing(Model):
    """A `ScriptedModel` that also records the messages each call received.

    `ScriptedModel` is `@final` and its `stream` ignores `messages` entirely, so
    there is no way to subclass it or read history back off it. Composition instead:
    this records what the model was handed, then delegates the response. That makes
    the history assertions below check the thing that actually matters — what reached
    the model — rather than any bookkeeping this repo keeps.
    """

    def __init__(
        self, turns: int, *, tool: str = "Answer", payload: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self._inner = ScriptedModel(
            [
                Turn(tool_calls=((tool, payload if payload is not None else {"text": f"a{i}"}),))
                for i in range(turns)
            ]
        )
        self.contexts: list[list[Any]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        return self._inner.stream(messages, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        """Every text block the model saw on its `call`-th invocation."""
        return _texts(self.contexts[call])


def _texts(messages: list[Any]) -> list[str]:
    return [b["text"] for m in messages for b in m.get("content", []) if "text" in b]


def _logged(events: list[Event]) -> list[str]:
    """The thread's history as the runtime itself reconstructs it per cycle."""
    return _texts(reconstruct_messages(events))


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


# ── The thread lifecycle: a capability that keeps its context between calls ──


async def test_spawned_method_runs_twice_and_the_second_call_sees_the_first() -> None:
    """The difference from `compiled()`: two calls, one conversation.

    Both calls carry the method's real typed keywords, and the second call's model
    context contains the first call's prompt — which is the whole claim.
    """
    async with RuntimeHarness() as h:
        model = Capturing(2)
        thread = await Solver("terse").spawn("solve", h.coordinator, model=model)
        assert isinstance(thread, MethodThread)

        first = await thread.run(problem="halting", mode="thorough", depth=1)
        second = await thread.run(problem="collatz")

        assert isinstance(first, Answer) and isinstance(second, Answer)
        assert (first.text, second.text) == ("a0", "a1")

        assert len(model.contexts) == 2
        assert not any("collatz" in p for p in model.prompts(0))
        assert any("halting" in p for p in model.prompts(1))
        assert any("collatz" in p for p in model.prompts(1))
        assert len(model.contexts[1]) > len(model.contexts[0])


async def test_the_live_thread_keeps_the_typed_signature() -> None:
    """A live thread is not a chat box: `run` binds the method's own parameters."""
    async with RuntimeHarness() as h:
        thread = await Solver("terse").spawn("solve", h.coordinator, model=Capturing(1))
        assert thread.qualified_name == "solver-terse.solve"
        with pytest.raises(TypeError):
            await thread.run(request="one string, the shape we gave up")


async def test_fork_shares_history_to_the_fork_point_then_diverges() -> None:
    """One context, two continuations — neither contaminating the other."""
    async with RuntimeHarness() as h:
        thread = await Solver("terse").spawn("solve", h.coordinator, model=Capturing(3))
        await thread.run(problem="shared")

        forked = await thread.fork()
        assert forked.id != thread.id
        assert _logged(await h.events(forked.id)) == _logged(await h.events(thread.id))

        await forked.run(problem="fork-only")
        await thread.run(problem="parent-only")

        fork_log = " ".join(_logged(await h.events(forked.id)))
        parent_log = " ".join(_logged(await h.events(thread.id)))
        assert "shared" in fork_log and "shared" in parent_log
        assert "fork-only" in fork_log and "fork-only" not in parent_log
        assert "parent-only" in parent_log and "parent-only" not in fork_log


async def test_a_sibling_method_can_be_seeded_from_another_thread() -> None:
    """The cross-method handoff.

    A thread hosts one signature, so `determine` cannot share `verify`'s thread. It
    inherits the context by copying `verify`'s log at spawn — and the proof is that
    `determine`'s *first* model call already contains `verify`'s turn.
    """
    async with RuntimeHarness() as h:
        worker = Caseworker()
        verify = await worker.spawn("verify", h.coordinator, model=Capturing(1))
        await verify.run(claim="the permit expired")

        determined = Capturing(1)
        determine = await worker.spawn(
            "determine", h.coordinator, seed_from=verify.id, model=determined
        )
        assert determine.id != verify.id
        assert determine.qualified_name == "caseworker.determine"

        await determine.run(question="is the applicant eligible")

        first_context = determined.prompts(0)
        assert any("the permit expired" in p for p in first_context)
        assert any("is the applicant eligible" in p for p in first_context)


async def test_notify_reaches_the_next_cycle_without_starting_one() -> None:
    """The inbound side channel a STRUCTURED thread cannot get from `send_message`."""
    async with RuntimeHarness() as h:
        model = Capturing(1)
        thread = await Solver("terse").spawn("solve", h.coordinator, model=model)

        await thread.notify("the deadline moved to Friday")
        assert model.contexts == []  # no cycle started

        await thread.run(problem="reschedule")
        assert any("the deadline moved to Friday" in p for p in model.prompts(0))


async def test_a_retired_thread_refuses_every_operation_by_name() -> None:
    """No silent respawn: a blank conversation with the right signature is a wrong
    answer that looks like a right one, so each op raises instead."""
    async with RuntimeHarness() as h:
        thread = await Solver("terse").spawn("solve", h.coordinator, model=Capturing(2))
        await thread.run(problem="first")

        await thread.retire()
        assert thread.live is False

        for op in (
            lambda: thread.run(problem="second"),
            lambda: thread.fork(),
            lambda: thread.notify("hello"),
        ):
            with pytest.raises(RuntimeError, match=r"solver-terse\.solve"):
                await op()
        with pytest.raises(RuntimeError, match=r"solver-terse\.solve"):
            _ = thread.handle


async def test_retire_is_idempotent() -> None:
    """A caller unwinding several threads should not have to track which it released."""
    async with RuntimeHarness() as h:
        thread = await Solver("terse").spawn("solve", h.coordinator, model=Capturing(1))
        await thread.retire()
        await thread.retire()
        assert thread.live is False


async def test_retire_survives_the_runtime_tearing_the_thread_down_first() -> None:
    """Idempotence must hold against the runtime, not just this object.

    A supervisor holding the raw handle can terminate the thread behind our back;
    `ThreadNotFoundError` is a `KeyError`, so without the catch in `retire` an
    unwind loop expecting `RuntimeError` would crash mid-unwind and leave the rest
    of its threads alive.
    """
    async with RuntimeHarness() as h:
        thread = await Solver("terse").spawn("solve", h.coordinator, model=Capturing(1))
        await thread.handle.terminate_now()

        await thread.retire()  # must not raise
        assert thread.live is False


async def test_seed_from_bridges_methods_with_different_output_types() -> None:
    """The handoff history carries `toolUse` blocks for a tool the second method
    never offers; reconstruction must still deliver the first method's turns."""
    async with RuntimeHarness() as h:
        worker = MixedWorker()
        verify_model = Capturing(1)
        verify = await worker.spawn("verify", h.coordinator, model=verify_model)
        await verify.run(claim="the invoice was paid")

        judge_model = Capturing(1, tool="Judged", payload={"ok": True})
        judge = await worker.spawn("judge", h.coordinator, seed_from=verify.id, model=judge_model)
        verdict = await judge.run(question="approve the refund?")

        assert verdict.ok is True
        assert any("the invoice was paid" in p for p in judge_model.prompts(0))


async def test_one_agent_holds_several_live_method_threads_at_once() -> None:
    """Why the lifecycle is a separate object and nothing caches on the agent."""
    async with RuntimeHarness() as h:
        worker = Caseworker()
        verify = await worker.spawn("verify", h.coordinator, model=Capturing(1))
        determine = await worker.spawn("determine", h.coordinator, model=Capturing(1))

        assert verify.id != determine.id
        assert verify.live and determine.live

        await verify.retire()
        assert determine.live is True
        assert await determine.run(question="still answering") is not None


async def test_spawn_compiles_through_the_instances_own_compiled() -> None:
    """The tests/app monkeypatch pattern must reach spawned threads too.

    `tests/app` scripts models by rebinding `compiled` on the *instance*; routing
    `spawn` through `self.compiled` is what makes a live thread inherit that for free
    instead of reaching a real model.
    """
    async with RuntimeHarness() as h:
        agent = Solver("terse")
        model = Capturing(1)
        original = type(agent).compiled
        seen: list[str] = []

        def compiled(name: str, **overrides: Any) -> Any:
            seen.append(name)
            overrides.setdefault("model", model)
            return original(agent, name, **overrides)

        agent.compiled = compiled  # type: ignore[method-assign]

        thread = await agent.spawn("solve", h.coordinator)
        result = await thread.run(problem="scripted offline")

        assert seen == ["solve"]
        assert isinstance(result, Answer)
        assert any("scripted offline" in p for p in model.prompts(0))


async def test_the_thread_is_named_for_the_capability_not_the_agent() -> None:
    """Event logs and tool schemas use one vocabulary: `{owner}.{method}`."""
    async with RuntimeHarness() as h:
        worker = Caseworker()
        thread = await worker.spawn("verify", h.coordinator, model=Capturing(1))
        info = await h.coordinator.get_thread_info(thread.id)
        assert info.thread_name == "caseworker.verify"

        named = await worker.spawn(
            "verify", h.coordinator, thread_name="second-opinion", model=Capturing(1)
        )
        assert (await h.coordinator.get_thread_info(named.id)).thread_name == "second-opinion"
