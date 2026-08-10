"""Offline tests for `team/hooks/learning.py`: the Learning hook, the trace seam, `train`.

Delivery claims are asserted on the wire (the lead model's own contexts), graph claims from
the reconstructed graph's `parameters` — never from what the hook says it staged. Models are
scripted throughout; the optimizer in `train` tests is a scripted stand-in because the real
backward model's routable target ids carry random `unique_name` suffixes
(`optimizer/_formatting.py:14-16`) that no fixed script can name — the stand-in still builds
the REAL graph from the kept trace, so the seam under test is the library's, not a fake.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import pytest
from ai_functions import Frozen, JSONMemoryBackend, Procedural, TextGradOptimizer
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types import Result
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Accept, Revise, Team, TeamRun, Workspace
from pneuma.team.hooks import Learning, TrainingRound, compose_feedback, traced_result, train

# ── Fixtures (module level: compile resolves annotations against module globals) ──

SEED = "Prefer citing a member's evidence over guessing."


class Guidance(BaseModel):
    guidance: str = Field(default=SEED, description="Advice for the team lead.")


class CodeAndFrozen(BaseModel):
    helper: Procedural
    pinned: Frozen[str] = "never learns"
    guidance: str = Field(default=SEED, description="Advice.")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="What was relied on")


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on the request")
    def decide(self, question: str) -> Ruling:
        """Rule on {question}."""


class Counting(Model):
    """Composes a `ScriptedModel` and records each call's context — the wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
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
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


def ruling(*, admitted: bool = True, cites: list[str] | None = None) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": cites or []}),))


def scripted_lead(turns: list[Turn]) -> tuple[Any, Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


def memory_at(tmp_path: Path) -> JSONMemoryBackend:
    return JSONMemoryBackend(Guidance, actor_id="team", path=tmp_path / "mem.json")


class ScriptedOptimizer:
    """A stand-in stepper that builds the REAL graph, then rewrites the guidance.

    The assert inside `step` is load-bearing: `train` must hand it a trace whose graph
    carries a grad-enabled parameter node, or the real optimizer would walk an empty
    graph and consolidate nothing — silently. Rewriting via `backend.save` keeps the
    step model-free while exercising the same backend the real consolidation targets.
    """

    def __init__(self, rewrite: str) -> None:
        self.rewrite = rewrite
        self.stepped: list[tuple[Result[Any], str]] = []

    async def step(self, result: Result[Any], feedback: str, backends: list[Any]) -> Any:
        graph = await build_graph_from_result(result, backends)
        live = [p for p in graph.parameters if p.requires_grad]
        assert live, "the kept trace reconstructs to a graph with no parameter node"
        for parameter in live:
            assert parameter.backend is not None
            parameter.backend.save(parameter.name, self.rewrite)
        self.stepped.append((result, feedback))
        return graph


# ── 1. The hook: delivery on the wire, zero overhead, no premature emission ──


async def test_guidance_reaches_the_leads_wire_and_the_view_stays_unemitted(
    tmp_path: Path,
) -> None:
    """The seed guidance text is IN the lead model's context (the request fold delivered),
    and the staged view is still `emitted=False` after the run — the deferred-emission
    contract that keeps a bare, untraced run out of every event log."""
    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = Team(lead, [], hooks=[Learning(memory)])
        run = await team.run("who is right", h.worker.coordinator)

    assert any(SEED in p for p in lead_model.prompts(0)), "guidance never reached the wire"
    staged = run.hooks_data["learning"]
    assert staged["view"].value == SEED
    assert staged["view"].emitted is False, "the recall leaked into a log before tracing"
    memory.close()


async def test_a_hooked_but_untraced_run_costs_exactly_the_bare_teams_model_calls(
    tmp_path: Path,
) -> None:
    """Zero overhead, counted: the hooked team spends the same one lead cycle the bare team
    does — the JSON recall is storage-only and emission is deferred, so learning costs
    nothing until `traced_result` is asked for."""
    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        bare_lead, bare_model = scripted_lead([ruling()])
        await Team(bare_lead, []).run("go", h.worker.coordinator)

        hooked_lead, hooked_model = scripted_lead([ruling()])
        await Team(hooked_lead, [], hooks=[Learning(memory)]).run("go", h.worker.coordinator)

    assert len(hooked_model.contexts) == len(bare_model.contexts) == 1
    memory.close()


async def test_empty_guidance_is_rendered_as_an_explicit_absence(tmp_path: Path) -> None:
    """An empty parameter must not leave a heading over nothing — the model would fill the
    section in, and a transcript reader could not tell empty from dropped."""
    memory = memory_at(tmp_path)
    memory.save("guidance", "")
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        await Team(lead, [], hooks=[Learning(memory)]).run("go", h.worker.coordinator)

    assert any("(no guidance learned yet)" in p for p in lead_model.prompts(0))
    memory.close()


# ── 2. The trace seam ──


async def test_traced_result_yields_a_graph_carrying_the_guidance_parameter_node(
    tmp_path: Path,
) -> None:
    """The whole seam, closed: run the team, finish the trace, reconstruct the graph, and
    the guidance is a grad-enabled `ParameterNode` wired to the live backend — asserted by
    walking the graph's parameters (the learning.py:319 pattern), not the hook's claims."""
    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling(cites=["ledger"])])
        team = Team(lead, [], hooks=[Learning(memory)])
        run = await team.run("who is right", h.worker.coordinator)
        result = await traced_result(run)
        graph = await build_graph_from_result(result, [memory])

    assert result.value is run.answer
    assert [v.value for v in result.inputs] == [SEED], "the view rides inputs by identity"
    nodes = [(p.name, p.requires_grad, p.backend) for p in graph.parameters]
    assert nodes == [("guidance", True, memory)], f"graph carries {nodes}"
    memory.close()


async def test_every_run_recalls_fresh_so_each_trace_carries_its_own_node(
    tmp_path: Path,
) -> None:
    """The first-trace trap, structurally avoided: two runs on one team both reconstruct to
    graphs with a live guidance node, because each `on_assemble` recalls its own view. A
    hook that cached the view on `self` would pass run one and hand run two an empty graph."""
    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling(), ruling()])
        team = Team(lead, [], hooks=[Learning(memory)])
        names = []
        for request in ("first", "second"):
            run = await team.run(request, h.worker.coordinator)
            graph = await build_graph_from_result(await traced_result(run), [memory])
            names.append([p.name for p in graph.parameters])

    assert names == [["guidance"], ["guidance"]], "a later run lost its gradient edge"
    memory.close()


async def test_an_interpolated_view_kills_the_edge_and_traced_result_refuses(
    tmp_path: Path,
) -> None:
    """The planted identity break: replace the staged view with an f-string of it — same
    text, dead edge — and `traced_result` must refuse rather than return a Result whose
    graph would silently carry no parameter node."""
    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, [], hooks=[Learning(memory)]).run("go", h.worker.coordinator)
        run.hooks_data["learning"]["view"] = f"{run.hooks_data['learning']['view']}"
        with pytest.raises(RuntimeError, match="identity was lost"):
            await traced_result(run)
    memory.close()


async def test_the_edge_survives_a_team_run_started_inside_a_live_thread_scope(
    tmp_path: Path,
) -> None:
    """The ambient-scope trap, pinned: `team.run` called from inside a running cycle (an
    explicit `thread_scope` stands in for the one the runtime opens) must still produce a
    trace whose graph carries the guidance node. Without `no_thread_scope` around the
    recall, the event would emit against the CALLER's thread, the view would arrive
    `emitted=True`, the deferred emission would no-op, and `graph.parameters` would be
    empty with every other test still green (recall-injection-and-marker-traps.md #1)."""
    from ai_functions.types import thread_scope

    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        coord = h.worker.coordinator
        lead, _ = scripted_lead([ruling()])
        team = Team(lead, [], hooks=[Learning(memory)])
        bystander = await coord.spawn(lead, thread_name="bystander")
        try:
            with thread_scope(coord, bystander.id):
                run = await team.run("go", coord)
        finally:
            await bystander.terminate_now()
        graph = await build_graph_from_result(await traced_result(run), [memory])
        bystander_events = await coord.get_events(bystander.id)

    assert [p.name for p in graph.parameters] == ["guidance"], "the ambient scope ate the edge"
    assert not any(type(e).__name__ == "ParameterRecalledEvent" for e in bystander_events), (
        "the recall leaked into the caller's log"
    )
    memory.close()


async def test_traced_result_refuses_a_run_with_no_learning_hook(tmp_path: Path) -> None:
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling()])
        run = await Team(lead, []).run("go", h.worker.coordinator)
    with pytest.raises(RuntimeError, match="no hooks_data\\['learning'\\]"):
        await traced_result(run)


# ── 3. train ──


async def test_train_steps_the_optimizer_over_the_kept_trace_and_the_guidance_changes(
    tmp_path: Path,
) -> None:
    """The loop's whole point, measured: after `train`, the stored guidance is the new text,
    the round records before/after, and the optimizer stepped exactly once with the composed
    feedback — over a trace whose graph the scripted stepper verified was live."""
    memory = memory_at(tmp_path)
    stepper = ScriptedOptimizer("Always cite the ledger entry by id.")
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling(), ruling()])
        team = Team(lead, [], hooks=[Learning(memory)])
        round_result = await train(
            team, ["case one", "case two"], coordinator=h.worker.coordinator, optimizer=stepper
        )

    assert isinstance(round_result, TrainingRound)
    assert round_result.guidance_before == SEED
    assert round_result.guidance_after == "Always cite the ledger entry by id."
    assert round_result.changed is True
    assert memory.fetch("guidance") == "Always cite the ledger entry by id."
    assert len(round_result.runs) == 2
    assert len(stepper.stepped) == 1, "one step per batch, over the kept trace"
    assert stepper.stepped[0][1] == round_result.feedback
    memory.close()


async def test_feedback_defaults_to_composing_the_batchs_revise_findings(
    tmp_path: Path,
) -> None:
    """`feedback_fn=None` reads what the runs recorded: a revising hook's feedback and its
    cap exhaustion both appear in the composed text the optimizer receives."""

    class Demanding:
        def on_answer(self, work: Workspace, answer: Any) -> Revise:
            return Revise("cite the ledger, not your gut", cap=1)

    memory = memory_at(tmp_path)
    stepper = ScriptedOptimizer("sharper")
    async with RuntimeHarness() as h:
        lead, _ = scripted_lead([ruling(), ruling()])
        team = Team(lead, [], hooks=[Learning(memory), Demanding()])
        round_result = await train(
            team, ["case"], coordinator=h.worker.coordinator, optimizer=stepper
        )

    assert "cite the ledger, not your gut" in round_result.feedback
    assert "cap" in round_result.feedback, "cap exhaustion is a finding, not a footnote"
    memory.close()


def test_compose_feedback_reads_review_findings_and_says_so_when_clean() -> None:
    """Unit-level: a T8c-shaped review entry lands in the prose; an all-clean batch composes
    the keep-message rather than inventing an improvement request."""
    with_findings = TeamRun(
        answer="a",
        hooks_data={
            "review": [
                {"hook": "Critic", "outcome": "findings", "review": "the cite is wrong"},
                {"hook": "Critic", "outcome": "clean", "review": "NO-FINDINGS"},
            ]
        },
    )
    text = compose_feedback([with_findings])
    assert "the cite is wrong" in text
    assert "NO-FINDINGS" not in text, "a clean review is not a finding"

    clean = compose_feedback([TeamRun(answer="a")])
    assert "do not add new advice" in clean


# ── 4. Guards at construction and at train ──


def test_learning_refuses_an_unknown_parameter(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="'nope' is not a parameter of Guidance"):
        Learning(memory_at(tmp_path), "nope")


def test_learning_refuses_a_procedural_parameter(tmp_path: Path) -> None:
    """Code is not advice: a Procedural target would have the optimizer rewriting executable
    behaviour under a hook that promises prose."""
    memory = JSONMemoryBackend(CodeAndFrozen, actor_id="team", path=tmp_path / "m.json")
    with pytest.raises(RuntimeError, match="'helper' is Procedural"):
        Learning(memory, "helper")
    memory.close()


def test_learning_refuses_a_frozen_parameter(tmp_path: Path) -> None:
    """A Frozen target can never receive a gradient — training against it reports rounds and
    learns nothing, the exact silent failure this loop exists to avoid."""
    memory = JSONMemoryBackend(CodeAndFrozen, actor_id="team", path=tmp_path / "m.json")
    with pytest.raises(RuntimeError, match="'pinned' is Frozen"):
        Learning(memory, "pinned")
    memory.close()


async def test_train_refuses_zero_and_two_learning_hooks(tmp_path: Path) -> None:
    memory = memory_at(tmp_path)
    lead, _ = scripted_lead([])
    with pytest.raises(RuntimeError, match="0 Learning hooks"):
        await train(Team(lead, []), ["case"])
    with pytest.raises(RuntimeError, match="2 Learning hooks"):
        await train(Team(lead, [], hooks=[Learning(memory), Learning(memory)]), ["case"])
    memory.close()


async def test_train_refuses_an_empty_batch(tmp_path: Path) -> None:
    memory = memory_at(tmp_path)
    lead, lead_model = scripted_lead([])
    with pytest.raises(RuntimeError, match="no cases"):
        await train(Team(lead, [], hooks=[Learning(memory)]), [])
    assert lead_model.contexts == [], "refused before anything ran"
    memory.close()


# ── 5. The live gate ──


@pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE_TEAM_LEARNING") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE_TEAM_LEARNING=1 to run the loop against the real model",
)
async def test_live_a_real_traced_run_and_one_real_textgrad_step_change_the_guidance(
    tmp_path: Path,
) -> None:
    """The whole loop against Bedrock, once: a real traced team run, one real
    `TextGradOptimizer.step`, and the measured question — did the stored guidance text
    actually change? Feedback names a concrete, checkable edit (mention the word
    "ledger") so the assertion is about the mechanism landing, not about model taste.
    Roughly four model calls (forward, backward, consolidate) at low effort.
    """
    from pneuma.model import opus5

    model = opus5("low", max_tokens=4_000, show_thinking=False)
    memory = JSONMemoryBackend(Guidance, actor_id="team", path=tmp_path / "live.json", model=model)
    memory.save("guidance", "Answer briefly.")

    async with RuntimeHarness() as h:
        lead = Chair().compiled("decide", model=model)
        team = Team(lead, [], hooks=[Learning(memory)])
        round_result = await train(
            team,
            ["Should the team admit the quarterly numbers as evidence?"],
            feedback_fn=lambda runs: (
                "The ruling cited nothing. Rewrite the guidance so it explicitly tells the "
                "lead to cite the ledger by name in every ruling — the word 'ledger' must "
                "appear in the guidance."
            ),
            coordinator=h.worker.coordinator,
            optimizer=TextGradOptimizer(model=model),
        )

    assert isinstance(round_result.runs[0].answer, Ruling), "the forward run returned typed"
    assert round_result.changed, (
        f"one real step left the guidance untouched: {round_result.guidance_after!r}"
    )
    assert round_result.guidance_after != "Answer briefly."
    memory.close()


async def test_hooks_compose_review_accepts_pass_through_unchanged(tmp_path: Path) -> None:
    """Learning beside an accepting reviewer: the answer loop stays the core's, the guidance
    stays on the wire, and the run still traces — the hook is a citizen, not a mode."""

    class Waves:
        def on_answer(self, work: Workspace, answer: Any) -> Accept:
            return Accept()

    memory = memory_at(tmp_path)
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling()])
        team = Team(lead, [], hooks=[Learning(memory), Waves()])
        run = await team.run("go", h.worker.coordinator)
        graph = await build_graph_from_result(await traced_result(run), [memory])

    assert len(lead_model.contexts) == 1
    assert [p.name for p in graph.parameters] == ["guidance"]
    memory.close()
