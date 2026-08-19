"""Offline tests for `team/expedition.py`: the code-owned bounded outer loop.

Every delivery claim is checked ON THE WIRE — from the contexts a recording model actually
received — never from the returned `ExpeditionResult` alone (the render_brief lesson,
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`).
The digest claim in particular: round 2's request must carry round 1's answer text in the
lead model's own context, or `next_request` composed a digest nobody received.

`Counting` composes rather than subclasses — `ScriptedModel` is `@final` — and every fixture
output type is module level, because `compile_ai_method` resolves annotations with
`typing.get_type_hints` against module globals (`method.py:146`).
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Sequence
from typing import Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Team
from pneuma.team.expedition import Expedition, ExpeditionResult, Round

# ── Output types, module level for get_type_hints ──


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which findings were relied on")


# ── The cast: one lead, no members — the expedition drives the loop, not the team ──


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what is asked")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


# ── Recording model: contexts on the wire ──


class Counting(Model):
    """Composes a `ScriptedModel` and records what each call carried on the wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []
        self.tool_specs: list[list[str]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(
        self, messages: Any, tool_specs: Any = None, *args: Any, **kwargs: Any
    ) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        self.tool_specs.append([spec["name"] for spec in (tool_specs or [])])
        return self._inner.stream(messages, tool_specs, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


# ── 1. The budget stop ──


async def test_always_fresh_rounds_run_to_the_cap_and_stop_on_max_rounds() -> None:
    """Three distinct scripted answers, novelty pinned always-fresh: the loop spends exactly
    `max_rounds` rounds, stops with 'max_rounds', and the result's answer is round 3's."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["one"]), ruling(cites=["two"]), ruling(cites=["three"])]
        )
        expedition = Expedition(Team(lead, []), max_rounds=3, novelty=lambda answer, rounds: True)
        result = await expedition.run("survey the terrain", h.worker.coordinator)

    assert isinstance(result, ExpeditionResult)
    assert result.stopped == "max_rounds"
    assert len(result.rounds) == 3
    assert [r.index for r in result.rounds] == [1, 2, 3]
    assert all(r.fresh for r in result.rounds)
    assert len(lead_model.contexts) == 3, "exactly one lead cycle per round, no fourth"
    assert result.answer == result.rounds[-1].answer
    assert "three" in result.answer


# ── 2. The dry stop ──


async def test_a_repeated_answer_is_stale_under_default_novelty_and_dries_the_loop() -> None:
    """The team says the same thing twice; default novelty grades round 2 stale; with
    dry_after=1 the loop stops right there — two rounds, stopped='dry', no third cycle."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["SAME"]), ruling(cites=["SAME"]), ruling(cites=["never-reached"])]
        )
        expedition = Expedition(Team(lead, []), max_rounds=5, dry_after=1)
        result = await expedition.run("survey the terrain", h.worker.coordinator)

    assert result.stopped == "dry"
    assert len(result.rounds) == 2
    assert result.rounds[0].fresh is True and result.rounds[1].fresh is False
    assert len(lead_model.contexts) == 2, "the dry stop spent no third lead cycle"
    assert result.answer == result.rounds[1].answer


# ── 3. Guard-must-fire: the digest actually reaches the model ──


async def test_round_two_request_carries_round_one_answer_on_the_leads_wire() -> None:
    """The default `next_request` digest is asserted where delivery happens: round 2's lead
    context contains round 1's answer text (the 'ALPHA-FINDING' marker) AND the base request.
    Drop the digest from `next_request` and this fails — round 2's context would carry only
    the base request, never the marker. Also pinned: round 1's request is the base verbatim,
    with no digest scaffolding."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling(cites=["ALPHA-FINDING"]), ruling(cites=["beta"])])
        expedition = Expedition(Team(lead, []), max_rounds=2, novelty=lambda answer, rounds: True)
        result = await expedition.run("map the caves", h.worker.coordinator)

    assert len(lead_model.contexts) == 2
    # Round 1: the base request verbatim, no digest.
    assert any("map the caves" in p for p in lead_model.prompts(0))
    assert not any("Prior rounds found" in p for p in lead_model.prompts(0))
    # Round 2: the digest carrying round 1's exact answer string reached the model.
    assert any("ALPHA-FINDING" in p for p in lead_model.prompts(1)), (
        "round 1's answer never reached round 2's model — the digest was composed but not wired"
    )
    assert any("map the caves" in p for p in lead_model.prompts(1)), (
        "the base request must survive into later rounds"
    )
    assert any("Prior rounds found" in p for p in lead_model.prompts(1))
    assert result.rounds[0].answer in result.rounds[1].request


# ── 4. The halt seam ──


async def test_should_continue_returning_false_halts_after_one_round() -> None:
    """The external-budget hook: `should_continue` says False after round 1, the loop stops
    with 'halted' and spends no second lead cycle — even though both the cap and the dry
    threshold had budget left."""
    consulted: list[int] = []

    def broke(rounds: list[Round]) -> bool:
        consulted.append(len(rounds))
        return False

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead([ruling(cites=["one"]), ruling(cites=["two"])])
        expedition = Expedition(Team(lead, []), max_rounds=5, should_continue=broke)
        result = await expedition.run("go", h.worker.coordinator)

    assert result.stopped == "halted"
    assert len(result.rounds) == 1
    assert consulted == [1], "consulted once, after the round"
    assert len(lead_model.contexts) == 1, "the halt spent no second lead cycle"


# ── 5. Constructor refusals ──


def test_a_zero_round_cap_is_refused_at_construction() -> None:
    lead, _ = scripted_lead([ruling()])
    with pytest.raises(ValueError, match="max_rounds=0"):
        Expedition(Team(lead, []), max_rounds=0)


def test_a_zero_dry_threshold_is_refused_at_construction() -> None:
    lead, _ = scripted_lead([ruling()])
    with pytest.raises(ValueError, match="dry_after=0"):
        Expedition(Team(lead, []), max_rounds=3, dry_after=0)


# ── 6. Consecutive-dry semantics ──


async def test_a_single_stale_round_between_fresh_ones_does_not_dry_the_loop() -> None:
    """dry_after counts CONSECUTIVE stale rounds: fresh, fresh, stale is a streak of one
    under dry_after=2, so the loop runs to the cap and stops on 'max_rounds' — a single
    repetition amid progress is not a dry well."""
    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["A"]), ruling(cites=["B"]), ruling(cites=["A"])]
        )
        expedition = Expedition(Team(lead, []), max_rounds=3, dry_after=2)
        result = await expedition.run("go", h.worker.coordinator)

    assert result.stopped == "max_rounds"
    assert len(result.rounds) == 3
    assert [r.fresh for r in result.rounds] == [True, True, False], (
        "round 3 repeats round 1 verbatim, so default novelty grades it stale"
    )
    assert len(lead_model.contexts) == 3


# ── 7. Async novelty ──


async def test_an_async_novelty_callable_is_awaited_and_its_verdict_drives_the_dry_stop() -> None:
    """`novelty` as an async def is awaited, not truth-tested as a coroutine object (a
    coroutine is always truthy — un-awaited, every round would grade fresh and this loop
    would run to the cap instead of drying at round 2)."""
    graded: list[str] = []

    async def stale_after_one(answer: str, rounds: list[Round]) -> bool:
        graded.append(answer)
        return len(rounds) == 0

    async with RuntimeHarness() as h:
        lead, lead_model = scripted_lead(
            [ruling(cites=["one"]), ruling(cites=["two"]), ruling(cites=["three"])]
        )
        expedition = Expedition(Team(lead, []), max_rounds=5, dry_after=1, novelty=stale_after_one)
        result = await expedition.run("go", h.worker.coordinator)

    assert result.stopped == "dry"
    assert len(result.rounds) == 2
    assert [r.fresh for r in result.rounds] == [True, False]
    assert len(graded) == 2, "the async callable was awaited each round"
    assert len(lead_model.contexts) == 2, "an un-awaited coroutine would have run to the cap"
