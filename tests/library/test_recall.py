"""Offline tests for `recall.py`: the marker, the injection, and the guards.

Three kinds of claim, checked three ways on purpose.

The *marker* is introspection, so `recalled_params` is called directly and the compiled
`prompt_fn.__annotations__` is read — the second is not redundant, because `include_extras=True`
in `compile_ai_method` is exactly the load-bearing line that a refactor drops silently
(`test_typed_cast_method.py` pins `Procedural` the same way).

The *injection* is a claim about the optimizer's graph, not about this code's bookkeeping, so it
is asserted from `build_graph_from_result` — the derivation, the retrieved ids, and the fact that
a second trace yields a live node *again*. Asserting that `trace` was handed a `ParameterView`
would pass with the view interpolated one layer down and the gradient edge gone.

The *guards* are checked by counting model calls and backend calls, never by trusting a message.
A guard that raises after retrieving, or after calling the model, has already spent what it exists
to protect: `SpyBackend` counts retrievals and every guard test scripts a model with **zero**
turns, so a call that reached the runtime raises `ScriptExhausted` instead of the expected error
and the test fails.

`JSONMemoryBackend` throughout. Its `_search` ranks with BM25 and carries
`{"results": {entry_id: value}}` (`json_backend.py:381-403`) — the mapping the whole narrow-
gradient chain depends on — with no embedder and no model call, so search mode is exercisable
offline. `TursoMemoryBackend` would need an embedder, and the deterministic one lives in
`tests/app`, which a library test file cannot import (`test_boundary.py:217-256`).
"""

from __future__ import annotations

import os
import tempfile
import typing
from collections.abc import AsyncIterable
from typing import Annotated, Any

import pytest
from ai_functions import JSONMemoryBackend, MemoryBackend
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types.graph import ParameterView, Traceable
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.recall import Recall, Recalled, recalled_params

# ── Fixtures ──
#
# Module level, all of them: `compile_ai_method` resolves annotations with
# `typing.get_type_hints` against module globals, so a function-local output type — or a
# function-local `Recalled` annotation — cannot be resolved at compile time.


class Choice(BaseModel):
    """What the navigator returns. Named for the tool call the scripted turns make."""

    transition: str = Field(description="The transition to take next.")
    reason: str = Field(description="Why.")


class Playbook(BaseModel):
    """The memory schema: a searchable collection and a full-recall scalar.

    **Four seeded entries, and the count is measured rather than chosen.** The claim a
    search-mode test has to support is that the *query* selects, not that retrieval returned
    something — so the entries have disjoint vocabulary and the corpus has to be big enough for
    BM25 to rank at all. It is not, at two: `BM25Okapi`'s IDF is `log((N - df + 0.5) / (df + 0.5))`,
    which for a term in one of two documents is `log(1.0) == 0`, so *every* score is 0.0 and
    `sorted` falls back to insertion order. Measured — at N=2 both queries score `[0.0, 0.0]` and
    the first entry always wins; at N=4 `"invoice disputed escalate finance"` scores
    `[0.0, 2.461, 0.0, 0.0]` and selects the entry it names. A two-entry fixture would have made
    `test_the_query_selects_which_entries_reach_the_prompt` pass or fail on entry order and prove
    nothing about the query.
    """

    guidance: list[str] = Field(
        default_factory=lambda: [
            "when the permit is pending, request the missing document before deciding",
            "when the invoice is disputed, escalate to finance rather than approving",
            "when the applicant is unknown, verify identity documents first",
            "when a deadline has passed, record the delay and continue",
        ],
        description="What has been learned about deciding, one entry per lesson.",
    )
    summary: str = Field(
        default="this actor tends to loop on states it has already visited",
        description="One accumulated description of the actor, read whole every time.",
    )


class Navigator(MethodAgent):
    """One search-mode capability and one full-mode capability on the same agent.

    `choose`'s marked parameter comes *first*, which is `casestudy/learning.py`'s real shape
    (`choose(self, playbook, state, options, facts)`) and the one the positional-shadow guard
    exists for. `advice` is deliberately not named `guidance`: the signature names the argument
    and the marker names the memory field, and nothing requires them to agree.
    """

    name = "navigator"

    @ai_method(Choice, description="Choose the next transition")
    def choose(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=2)],
        state: str,
        options: str,
    ) -> Choice:
        """Guidance retrieved for this decision: {advice}

        You are in {state}. The options are {options}. Choose one.
        """

    @ai_method(Choice, description="Choose using everything known about the actor")
    def reflect(
        self,
        profile: Annotated[str, Recalled("summary")],
        state: str,
    ) -> Choice:
        """What is known about this actor: {profile}

        You are in {state}. Choose.
        """

    @ai_method(Choice, description="Choose using both a search and a full recall")
    def deliberate(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=1)],
        profile: Annotated[str, Recalled("summary")],
        state: str,
    ) -> Choice:
        """Guidance: {advice}
        Profile: {profile}
        You are in {state}. Choose.
        """

    @ai_method(Choice, description="Choose, with the recalled parameter last")
    def weigh(
        self,
        state: str,
        advice: Annotated[list[str], Recalled("guidance", k=1)],
    ) -> Choice:
        """You are in {state}. Guidance: {advice}. Choose."""

    @ai_method(Choice, description="Choose with nothing recalled")
    def guess(self, state: str) -> Choice:
        """You are in {state}. Guess."""


class UnionNavigator(MethodAgent):
    """The marker on a `Traceable[...]` union — the shape `detect_procedural_params` handles.

    Worth a fixture rather than a local annotation because the union is how the runtime's own
    docstrings write a parameter that may arrive as a handle, and a marker reader that only
    looked at `__metadata__` would miss it entirely.
    """

    name = "union-navigator"

    @ai_method(Choice, description="Choose, annotated through the handle union")
    def choose(
        self,
        advice: Traceable[Annotated[list[str], Recalled("guidance", k=2)]],
        state: str,
    ) -> Choice:
        """Guidance: {advice}. You are in {state}. Choose."""


class QueryNavigator(MethodAgent):
    """A capability whose own parameter is named `queries` — the wiring collision."""

    name = "query-navigator"

    @ai_method(Choice, description="Choose, with a colliding parameter name")
    def choose(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=2)],
        queries: str = "",
    ) -> Choice:
        """Guidance: {advice}. Queries: {queries}. Choose."""


class OverrideNavigator(MethodAgent):
    """The same collision through `overrides`, which fails exactly as silently."""

    name = "override-navigator"

    @ai_method(Choice, description="Choose, with a colliding parameter name")
    def choose(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=2)],
        overrides: str = "",
    ) -> Choice:
        """Guidance: {advice}. Overrides: {overrides}. Choose."""


class MethodNavigator(MethodAgent):
    """A capability whose parameter is named `method` — allowed, because `trace` is
    positional-only in that slot. The contrast that keeps `RESERVED` honest at two entries."""

    name = "method-navigator"

    @ai_method(Choice, description="Choose, naming a parameter after trace's own first")
    def choose(
        self,
        advice: Annotated[list[str], Recalled("guidance", k=2)],
        method: str = "default",
    ) -> Choice:
        """Guidance: {advice}. Method: {method}. Choose."""


def store(schema: type[BaseModel] = Playbook) -> JSONMemoryBackend:
    """A fresh JSON-backed store in its own temp directory, so no two tests share entry ids."""
    return JSONMemoryBackend(schema, "navigator", path=os.path.join(tempfile.mkdtemp(), "pb.json"))


class SpyBackend(JSONMemoryBackend):
    """A real backend that also counts what was retrieved from it, and how.

    Composition is not available here: `Recall` takes a `MemoryBackend` and the counting has to
    happen on the *public* `recall`/`search`, which is where the base class emits the
    `ParameterRecalledEvent` (`base.py:348-407`). Overriding the `_`-hooks instead would count
    correctly and skip emission, which is the trap `turso_backend.py:384-388` documents — the
    views would carry no graph edge and every optimizer assertion below would go quiet. So this
    subclasses, delegates via `super()`, and stays honest.
    """

    def __init__(self, schema: type[BaseModel] = Playbook) -> None:
        super().__init__(schema, "navigator", path=os.path.join(tempfile.mkdtemp(), "pb.json"))
        self.calls: list[tuple[str, str, str | None]] = []
        """One `(kind, source, query)` per retrieval, in order."""

    async def recall(self, name: str, *args: Any, **kwargs: Any) -> ParameterView[Any]:
        self.calls.append(("recall", name, None))
        return await super().recall(name, *args, **kwargs)

    async def search(
        self, name: str, query: str, k: int = 5, *args: Any, **kwargs: Any
    ) -> ParameterView[Any]:
        self.calls.append(("search", name, query))
        return await super().search(name, query, k, *args, **kwargs)


class Counting(Model):
    """A `ScriptedModel` that reports how many times it was called and what it saw.

    `ScriptedModel` is `@final` and its `stream` ignores `messages`, so there is nothing to
    subclass and no history to read back off it — composition instead, exactly as `test_method.py`
    and `test_gated.py` do it. `Counting(0)` is the load-bearing case: a guard that raises before
    the model call leaves `contexts == []`, and a guard that raises *after* it raises
    `ScriptExhausted` instead of the error under test.
    """

    def __init__(self, turns: int) -> None:
        super().__init__()
        self._inner = ScriptedModel(
            [
                Turn(tool_calls=(("Choice", {"transition": f"T{i}", "reason": "r"}),))
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
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


def _scripting(agent: MethodAgent, model: Model) -> Any:
    """Bind a scripted model by replacing `compiled` on the *instance*.

    The `tests/app` pattern, and the reason `Recall.trace` compiles through `self._agent.compiled`
    at trace time rather than caching an `AIFunction` at wiring time: a cached one would silently
    bypass this binding and reach a real model.
    """
    original = type(agent).compiled

    def compiled(name: str, **overrides: Any) -> Any:
        overrides.setdefault("model", model)
        return original(agent, name, **overrides)

    return compiled


# ── The marker: what is declared, and what survives compilation ──


def test_the_marker_is_found_in_both_modes_and_unmarked_params_are_ignored() -> None:
    """Introspection is the binder's whole input, so it has to see exactly the marked ones."""
    navigator = Navigator()

    assert recalled_params(navigator.choose) == {"advice": Recalled("guidance", k=2)}
    assert recalled_params(navigator.reflect) == {"profile": Recalled("summary")}
    assert recalled_params(navigator.deliberate) == {
        "advice": Recalled("guidance", k=1),
        "profile": Recalled("summary"),
    }
    assert recalled_params(navigator.guess) == {}


def test_the_marker_is_found_through_the_handle_union() -> None:
    """`Traceable[Annotated[T, Recalled(...)]]` is how the runtime's own docs write a parameter
    that may arrive as a handle. A reader that only checked `__metadata__` would see nothing and
    silently perform no retrieval — the exact fail-soft this module exists to prevent."""
    assert recalled_params(UnionNavigator().choose) == {"advice": Recalled("guidance", k=2)}


def test_self_is_not_a_recalled_parameter() -> None:
    """A bound method's hints exclude `self`, which is why this needs no filtering step —
    asserted rather than assumed, because reading `__func__` instead would include it."""
    assert "self" not in recalled_params(Navigator().choose)


def test_an_unresolvable_annotation_raises_rather_than_reading_as_unmarked() -> None:
    """The runtime's `detect_procedural_params` swallows this and returns an empty set, which is
    right there and wrong here: the cost there is a lost sandbox definition, and the cost here is
    *no retrieval*, presenting as an agent that reads an empty playbook forever."""

    class Broken(MethodAgent):
        name = "broken"

        @ai_method(Choice, description="Choose")
        def choose(self, advice: NoSuchType, state: str) -> Choice:  # noqa: F821 — the typo under test
            """Guidance: {advice}. State: {state}."""

    with pytest.raises(NameError):
        recalled_params(Broken().choose)


def test_the_marker_survives_compilation_into_the_prompt_functions_annotations() -> None:
    """`compile_ai_method` resolves hints with `include_extras=True` (`method.py:146`). Without
    it the annotation flattens to `list[str]`, `recalled_params` on the compiled function sees
    nothing, and the parameter becomes an ordinary prompt argument — silently."""
    annotation = Navigator().compiled("choose").prompt_fn.__annotations__["advice"]
    assert any(isinstance(m, Recalled) for m in typing.get_args(annotation))


async def test_the_marked_parameter_stays_in_the_tool_schema() -> None:
    """The grounded decision, asserted. No upstream mechanism drops a parameter or auto-fills one
    (`load_tools`, `ai_function.py:412-442`), so hiding it would be an upstream change. Keeping it
    is also the better shape: a peer agent calling this capability supplies the content itself,
    and the binder is how a *training loop* supplies it from memory instead."""
    fn = Navigator().compiled("choose")
    schema = (await fn.load_tools())[0].tool_spec["inputSchema"]["json"]
    assert set(schema["properties"]) == {"advice", "state", "options"}
    assert "advice" in schema["required"]


def test_a_marker_that_could_retrieve_nothing_is_refused_at_construction() -> None:
    """`search(..., k=0)` returns an empty list and empty `results` meta without complaint: the
    prompt loses its guidance, the graph loses its retrieved entries, and nothing says so."""
    with pytest.raises(ValueError, match=r"at least 1"):
        Recalled("guidance", k=0)
    with pytest.raises(ValueError, match=r"at least 1"):
        Recalled("guidance", k=-1)


# ── The injection: a recalled value that is actually a gradient target ──


async def test_search_mode_injects_a_view_the_optimizer_can_find() -> None:
    """The end of the chain, asserted from the graph rather than from what `trace` was handed.

    A view interpolated one layer down would compute the identical prompt and produce no node, so
    the claim is only settled by `build_graph_from_result`: one parameter named for the *memory*
    field, derivation `search`, and non-empty `meta["results"]` — the `{entry_id: value}` mapping
    consolidation targets.
    """
    async with RuntimeHarness():
        memory = store()
        try:
            navigator = Navigator()
            model = Counting(1)
            binder = Recall(navigator, memory)

            result = await binder.trace(
                "choose",
                state="PENDING",
                options="request|decide",
                queries={"advice": "the permit is pending and a document is missing"},
                overrides={"model": model},
            )

            assert result.value.transition == "T0"
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
            parameter = graph.parameters[0]
            assert parameter.derivation == "search"
            assert parameter.requires_grad is True
            assert parameter.meta["results"], "no retrieved ids, so consolidation has no target"

            assert len(model.contexts) == 1
            assert any("permit is pending" in prompt for prompt in model.prompts(0))
        finally:
            memory.close()


async def test_the_query_selects_which_entries_reach_the_prompt() -> None:
    """Retrieval that ranked nothing would still fill the parameter and still build a graph, so
    `k=1` over two disjoint entries is the cheapest honest check that the *query* is used."""
    async with RuntimeHarness():
        memory = store()
        try:
            navigator = Navigator()
            model = Counting(1)

            result = await Recall(navigator, memory).trace(
                "deliberate",
                state="DISPUTED",
                queries={"advice": "invoice disputed escalate finance"},
                overrides={"model": model},
            )

            advice = next(
                view.value for view in result.inputs if getattr(view, "name", "") == "guidance"
            )
            assert advice == [
                "when the invoice is disputed, escalate to finance rather than approving"
            ]
        finally:
            memory.close()


async def test_a_second_trace_gets_a_fresh_node_because_nothing_is_cached() -> None:
    """The per-round rule, library edition.

    A `ParameterView` is emitted once, so a binder that stored one would produce a node on the
    first traced call and none on any later one — and the failure is silent: rounds are reported,
    the playbook never changes. Two traces, two live nodes, or the binder is caching.
    """
    async with RuntimeHarness():
        memory = store()
        try:
            binder = Recall(Navigator(), memory)
            model = Counting(2)
            names: list[list[str]] = []

            for _ in range(2):
                result = await binder.trace(
                    "choose",
                    state="PENDING",
                    options="a|b",
                    queries={"advice": "permit pending"},
                    overrides={"model": model},
                )
                graph = await build_graph_from_result(result, [memory])
                names.append([p.name for p in graph.parameters])

            assert names == [["guidance"], ["guidance"]], "a reused view yields no second node"
            assert len(model.contexts) == 2
        finally:
            memory.close()


async def test_full_mode_injects_a_full_recall() -> None:
    """`k=None` means the memory *is* the answer: derivation `full`, no query, whole value."""
    async with RuntimeHarness():
        memory = store()
        try:
            model = Counting(1)
            result = await Recall(Navigator(), memory).trace(
                "reflect", state="LOOPING", overrides={"model": model}
            )

            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["summary"]
            assert graph.parameters[0].derivation == "full"
            assert graph.parameters[0].requires_grad is True
            assert any("loop on states" in prompt for prompt in model.prompts(0))
        finally:
            memory.close()


async def test_two_marked_parameters_on_one_method_each_get_their_own_retrieval() -> None:
    """Why `queries` is a mapping rather than one string: a loop that recalls twice per round is
    the shape `casestudy/minelearn.py` already has, and both modes must coexist on one call."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            result = await Recall(Navigator(), memory).trace(
                "deliberate",
                state="PENDING",
                queries={"advice": "permit pending document"},
                overrides={"model": Counting(1)},
            )

            assert memory.calls == [
                ("search", "guidance", "permit pending document"),
                ("recall", "summary", None),
            ]
            graph = await build_graph_from_result(result, [memory])
            assert sorted(p.name for p in graph.parameters) == ["guidance", "summary"]
            assert {p.derivation for p in graph.parameters} == {"search", "full"}
        finally:
            memory.close()


async def test_an_unmarked_method_traces_with_no_retrieval_at_all() -> None:
    """The binder is not a gate: a capability with nothing marked still traces through it."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            result = await Recall(Navigator(), memory).trace(
                "guess", state="START", overrides={"model": Counting(1)}
            )
            assert result.value.transition == "T0"
            assert memory.calls == []
            assert result.inputs == []
        finally:
            memory.close()


async def test_a_marker_on_a_union_annotated_parameter_is_filled_end_to_end() -> None:
    """The union is found by `recalled_params`; this is the other half — it is also *filled*,
    and the view still becomes a node, because `trace` unwraps handles at the run boundary."""
    async with RuntimeHarness():
        memory = store()
        try:
            result = await Recall(UnionNavigator(), memory).trace(
                "choose",
                state="PENDING",
                queries={"advice": "permit pending"},
                overrides={"model": Counting(1)},
            )
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
        finally:
            memory.close()


# ── Explicit beats implicit ──


async def test_a_caller_supplied_value_passes_through_and_no_retrieval_happens() -> None:
    """What keeps a marked capability callable by a peer that has the content in hand.

    The backend spy is the assertion that matters: a binder that retrieved and then discarded
    would produce the identical prompt, an extra event in the log, and a wasted round trip.
    """
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(1)
            result = await Recall(Navigator(), memory).trace(
                "choose",
                advice=["the caller already knows what to do"],
                state="PENDING",
                options="a|b",
                overrides={"model": model},
            )

            assert memory.calls == [], "a supplied value must not trigger a retrieval"
            assert any("caller already knows" in prompt for prompt in model.prompts(0))

            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == [], "a plain value is not a parameter node"
            assert result.value.transition == "T0"
        finally:
            memory.close()


async def test_one_parameter_can_be_supplied_while_the_other_is_recalled() -> None:
    """Per parameter, not per call — otherwise a method with two markers is all or nothing."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            result = await Recall(Navigator(), memory).trace(
                "deliberate",
                profile="the caller's own read of the actor",
                state="PENDING",
                queries={"advice": "permit pending"},
                overrides={"model": Counting(1)},
            )

            assert memory.calls == [("search", "guidance", "permit pending")]
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
        finally:
            memory.close()


# ── The guards: every one of them fires before the model call ──


async def test_a_search_mode_parameter_with_no_query_raises_before_any_model_call() -> None:
    """The sharpest guard in the module, and `Counting(0)` is why the test is worth writing.

    There is no defensible default for a query: a derived or empty one retrieves entries ranked
    against the wrong question and the call *succeeds* with irrelevant guidance in the prompt —
    the fail-soft `memory/turso_backend.py:20-28` warns about from the other end. A model with
    zero turns proves the refusal came first: had the guard run late, the runtime would raise
    `ScriptExhausted` instead of this.
    """
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(0)
            with pytest.raises(RuntimeError) as raised:
                await Recall(Navigator(), memory).trace(
                    "choose", state="PENDING", options="a|b", overrides={"model": model}
                )

            message = str(raised.value)
            assert "'advice'" in message
            assert "'guidance'" in message, "the message must name the memory field too"
            assert "navigator.choose" in message
            assert memory.calls == [], "the guard must fire before any retrieval"
            assert model.contexts == [], "the guard must fire before any model call"
        finally:
            memory.close()


async def test_a_query_for_a_full_mode_parameter_is_refused() -> None:
    """Misuse fails loud. Silently ignoring the query would leave the caller believing retrieval
    was steered by it, which is worse than either honest behaviour."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(0)
            with pytest.raises(RuntimeError) as raised:
                await Recall(Navigator(), memory).trace(
                    "reflect",
                    state="LOOPING",
                    queries={"profile": "what is known"},
                    overrides={"model": model},
                )

            message = str(raised.value)
            assert "'profile'" in message
            assert "k=None" in message
            assert memory.calls == []
            assert model.contexts == []
        finally:
            memory.close()


async def test_a_query_for_an_unmarked_parameter_is_refused() -> None:
    """A query nobody reads is misuse the caller cannot see, so the message names what *is*
    marked rather than only what is not."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            with pytest.raises(RuntimeError, match=r"no parameter named 'state'"):
                await Recall(Navigator(), memory).trace(
                    "choose",
                    state="PENDING",
                    options="a|b",
                    queries={"advice": "permit", "state": "not a marked parameter"},
                    overrides={"model": Counting(0)},
                )
            assert memory.calls == []
        finally:
            memory.close()


async def test_a_query_for_an_explicitly_supplied_parameter_is_refused() -> None:
    """The two ways of filling a parameter are alternatives, not layers: accepting both would let
    a caller pass a carefully built query and never learn it was dead."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            with pytest.raises(RuntimeError, match=r"supplied explicitly"):
                await Recall(Navigator(), memory).trace(
                    "choose",
                    advice=["already known"],
                    state="PENDING",
                    options="a|b",
                    queries={"advice": "permit pending"},
                    overrides={"model": Counting(0)},
                )
            assert memory.calls == []
        finally:
            memory.close()


def test_a_parameter_named_queries_is_refused_at_wiring_time() -> None:
    """Measured, and the reason this is a guard rather than a convention: a call passing
    `queries=` fills *trace's* parameter and leaves the method's at its default, with no error,
    no retrieval for it, and a prompt missing whatever it carried. `bound()` refuses so the
    introspection path and the call path cannot disagree."""
    binder = Recall(QueryNavigator(), store())
    try:
        with pytest.raises(RuntimeError) as raised:
            binder.bound("choose")
        message = str(raised.value)
        assert "'queries'" in message
        assert "query-navigator.choose" in message
    finally:
        binder.backend.close()


async def test_the_wiring_guard_also_fires_on_the_traced_path() -> None:
    """Both entry points, or the guarantee is one call away from not applying — `gated.py`'s
    lesson, restated here because `bound()` is the only place the check lives."""
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(0)
            with pytest.raises(RuntimeError, match=r"'queries'"):
                await Recall(QueryNavigator(), memory).trace(
                    "choose", queries={"advice": "permit"}, overrides={"model": model}
                )
            assert memory.calls == []
            assert model.contexts == []
        finally:
            memory.close()


def test_a_parameter_named_overrides_is_refused_too() -> None:
    """`overrides` fails exactly as silently as `queries`, so guarding one and not the other
    would leave half the keyword surface unprotected for no reason."""
    binder = Recall(OverrideNavigator(), store())
    try:
        with pytest.raises(RuntimeError, match=r"'overrides'"):
            binder.bound("choose")
    finally:
        binder.backend.close()


async def test_a_parameter_named_method_is_allowed_because_trace_takes_it_positionally() -> None:
    """The contrast that keeps `RESERVED` at two entries rather than three.

    `method` is positional-only on `trace`, so a bound parameter of that name forwards through
    `**kwargs` and reaches the prompt. Asserted end to end rather than by reading the signature:
    the claim is that the value *arrives*, not merely that nothing raised.
    """
    async with RuntimeHarness():
        memory = store()
        try:
            model = Counting(1)
            assert Recall(MethodNavigator(), memory).bound("choose") == {
                "advice": Recalled("guidance", k=2)
            }
            await Recall(MethodNavigator(), memory).trace(
                "choose",
                method="THE-METHOD-ARGUMENT",
                queries={"advice": "permit pending"},
                overrides={"model": model},
            )
            assert any("THE-METHOD-ARGUMENT" in prompt for prompt in model.prompts(0))
        finally:
            memory.close()


async def test_positional_arguments_that_would_shift_onto_a_marked_parameter_are_refused() -> None:
    """The failure the settled design did not cover, and the worse of its two outcomes.

    Positionals bind left to right and cannot skip a slot, so on `choose(advice, state, options)`
    a call written `trace("choose", "PENDING", "a|b")` puts `"PENDING"` in the *advice* slot.
    Measured: `signature.bind_partial("S", "O")` reports `{'advice': 'S', 'state': 'O'}`. Inject
    anyway and the runtime raises `TypeError: got multiple values for argument 'advice'`; infer
    "the caller supplied it" and there is no retrieval at all and a state string standing in for
    the playbook — a wrong prompt that raises nothing. Refused up front instead, naming the fix.
    """
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(0)
            with pytest.raises(RuntimeError) as raised:
                await Recall(Navigator(), memory).trace(
                    "choose",
                    "PENDING",
                    "a|b",
                    queries={"advice": "permit pending"},
                    overrides={"model": model},
                )

            message = str(raised.value)
            assert "'advice'" in message
            assert "left to right" in message
            assert memory.calls == []
            assert model.contexts == []
        finally:
            memory.close()


async def test_positional_arguments_that_stop_short_of_a_marked_parameter_are_allowed() -> None:
    """The other side of the line, and why the guard counts slots instead of banning `*args`.

    `weigh(state, advice)` puts its marker *after* a plain parameter, so one positional reaches
    only `state` and shadows nothing. Refusing this would forbid the shape `AIFunction.trace`
    itself documents (`trace(topic="cats", joke_guidelines=await memory.recall(...))` mixes both)
    for no benefit. The retrieval still has to happen, so the spy is the assertion that matters —
    a guard that quietly skipped injection when any positional was present would also pass a test
    that only checked nothing raised.
    """
    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            model = Counting(1)
            result = await Recall(Navigator(), memory).trace(
                "weigh",
                "PENDING",
                queries={"advice": "permit pending missing document"},
                overrides={"model": model},
            )

            assert memory.calls == [("search", "guidance", "permit pending missing document")]
            assert any("PENDING" in prompt for prompt in model.prompts(0))
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
        finally:
            memory.close()


# ── Scriptability: compiled at trace time, through the instance ──


async def test_trace_compiles_through_the_instances_own_compiled() -> None:
    """The `tests/app` monkeypatch pattern must reach the binder, which is why nothing is
    compiled at wiring time.

    A binder that cached an `AIFunction` in `__init__` would have captured the real model before
    the rebinding happened, and the test would reach the network instead of failing — the least
    debuggable outcome available in an offline suite.
    """
    async with RuntimeHarness():
        memory = store()
        try:
            navigator = Navigator()
            model = Counting(1)
            navigator.compiled = _scripting(navigator, model)  # type: ignore[method-assign]

            result = await Recall(navigator, memory).trace(
                "choose",
                state="PENDING",
                options="a|b",
                queries={"advice": "permit pending"},
            )

            assert result.value.transition == "T0"
            assert len(model.contexts) == 1, "the instance binding did not reach the traced call"
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"]
        finally:
            memory.close()


async def test_retrieval_errors_propagate_unwrapped() -> None:
    """The contrast with `gated.py`, asserted rather than documented.

    Every hook `GatedProposer` calls runs inside a validator, where the runtime turns any
    exception into `[VALIDATION ERROR]` feedback — so a bug there has to be re-raised as
    something that says it is a bug. Retrieval runs before the model call and outside the
    validation path: there is no model to report to and no attempt to burn, so the backend's own
    exception must reach the caller with its type and traceback intact.
    """

    class Broken(SpyBackend):
        async def search(self, name: str, query: str, k: int = 5, *a: Any, **kw: Any) -> Any:
            raise KeyError("the store is unreachable")

    async with RuntimeHarness():
        memory = Broken()
        try:
            model = Counting(0)
            with pytest.raises(KeyError, match=r"unreachable"):
                await Recall(Navigator(), memory).trace(
                    "choose",
                    state="PENDING",
                    options="a|b",
                    queries={"advice": "permit pending"},
                    overrides={"model": model},
                )
            assert model.contexts == [], "retrieval failed, so no cycle should have started"
        finally:
            memory.close()


def test_the_binder_exposes_what_it_bound_without_letting_it_be_repointed() -> None:
    """`backend` is what a caller hands `build_graph_from_result`, so it has to be reachable; a
    binder that could be re-pointed would let a caller believe the markers it introspected still
    describe the agent being called."""
    navigator = Navigator()
    memory = store()
    try:
        binder = Recall(navigator, memory)
        assert binder.agent is navigator
        assert binder.backend is memory
        assert isinstance(binder.backend, MemoryBackend)
        assert "navigator" in repr(binder)
        with pytest.raises(AttributeError):
            binder.backend = store()  # type: ignore[misc]
    finally:
        memory.close()


# ── The critic's findings: ambient scope, duplicate markers, positional-only ──


async def test_recall_under_an_ambient_thread_scope_still_lands_the_node_on_the_traced_thread() -> (
    None
):
    """The binder must work from *inside* a running cycle, which is where it will actually live.

    The runtime opens a `thread_scope` for every executing cycle, and `recall`/`search` emit
    against the ambient scope when one is active (`base.py:275-291`) — marking the view emitted,
    so `AIFunction.trace`'s own flush no-ops ("one logical recall, one event") and the *traced*
    thread's log carries no `ParameterRecalledEvent`. Without the `no_thread_scope` wrap in
    `Recall.trace`, this test's graph is empty: rounds reported, nothing learned, no error
    anywhere — the exact failure the module exists to prevent, reachable the moment a capability
    body or a gate uses the binder. Measured both ways: with the wrap removed, `graph.parameters
    == []` and the recall event sits on the ambient thread's log instead.
    """
    from ai_functions.types import thread_scope

    async with RuntimeHarness() as h:
        memory = store()
        try:
            navigator = Navigator()
            host = await navigator.spawn("reflect", h.coordinator, model=Counting(1))
            with thread_scope(h.coordinator, host.id):
                result = await Recall(navigator, memory).trace(
                    "choose",
                    state="PENDING",
                    options="a|b",
                    queries={"advice": "permit pending"},
                    overrides={"model": Counting(1)},
                )
            graph = await build_graph_from_result(result, [memory])
            assert [p.name for p in graph.parameters] == ["guidance"], (
                "the recall event leaked onto the ambient thread instead of the traced one"
            )
            assert graph.parameters[0].meta["results"]
            await host.retire()
        finally:
            memory.close()


def test_two_distinct_markers_on_one_annotation_are_refused() -> None:
    """Which store a parameter reads from must not depend on annotation order.

    A merge that keeps both sides' metadata, or a union whose members were marked separately,
    produces exactly this shape — and first-wins resolution would run whichever retrieval the
    file happened to order first, silently. The identical marker twice is fine: there is nothing
    to disambiguate.
    """

    def conflicted(
        advice: Annotated[list[str], Recalled("guidance", k=2), Recalled("summary")],
    ) -> None: ...

    with pytest.raises(TypeError, match=r"2 distinct `Recalled` markers"):
        recalled_params(conflicted)

    def repeated(
        advice: Annotated[list[str], Recalled("guidance", k=2), Recalled("guidance", k=2)],
    ) -> None: ...

    assert recalled_params(repeated) == {"advice": Recalled("guidance", k=2)}


async def test_a_positional_only_marked_parameter_is_refused_before_any_retrieval() -> None:
    """The binder injects by keyword, so a positional-only marked slot is unfillable.

    Without the wiring refusal the failure arrives *after* the retrieval was spent, as
    `TypeError: positional-only arguments passed as keyword arguments` — Python calling
    mechanics, three frames from the wiring mistake. The spy proves the guard fires before the
    backend is touched.
    """

    class Cornered(MethodAgent):
        name = "cornered"

        @ai_method(Choice, description="A marked parameter locked behind the slash")
        def choose(
            self,
            advice: Annotated[list[str], Recalled("guidance", k=2)],
            /,
            state: str = "S",
        ) -> Choice:
            """Guidance: {advice}. State: {state}. Choose."""

    async with RuntimeHarness():
        memory = SpyBackend()
        try:
            with pytest.raises(RuntimeError, match=r"positional-only"):
                await Recall(Cornered(), memory).trace(
                    "choose",
                    queries={"advice": "anything"},
                    overrides={"model": Counting(0)},
                )
            assert memory.calls == [], "the refusal must precede the retrieval it protects"
        finally:
            memory.close()
