"""Counterfactual suffix replay for rehearsals: the decision, its fallbacks, and the record.

Shepherd's counterfactual replay forks a recorded thread at the first decision an edit
alters and replays only the suffix, instead of re-running the whole rehearsal. The
mechanism is pure composition of what already exists — `MethodThread.fork` /
`spawn(seed_from=)` — and the first two tests prove it works and is cheaper, measured
on scripted-model call counts, when an edit genuinely first bites mid-trajectory.

The finding for *this* workload is the honest negative, and the rest of the module
pins it. Both of minelearn's learnable-rehearsal fallback conditions always fire:

- No recorded thread survives a training round: rounds run through `AIFunction.trace`,
  which tears its thread down before returning, and a dead id cannot seed a fork.
- Every real toolkit edit is globally coupled: the toolkit is a `Procedural` executed
  at sandbox setup and advertised in the first prompt's preamble, so the first altered
  decision is decision 0 and the suffix is the whole trajectory. Worse than break-even:
  minelearn's scratch rehearsal is model-free, so a replay from decision 0 would spend
  model cycles that scratch does not.

harnesslearn is the same shape sharpened: `coverage_weight` parameterises the gate's
objective at every swept threshold and opens the propose prompt, and the gate is
deterministic. So both loops record `SCRATCH` with the losing condition named, every
round, on the `Attempt` / `Round` — a declined replay and a missing replay path must
not read the same.

One compounding note the replay tests exercise implicitly: `opus5` enables prompt
caching, so where a replay *does* run, the forked branch's shared prefix bills at
cache-read rates — the fork-beam saving carrying over to counterfactual replay free.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.casestudy.harnesslearn import HarnessKnobs, replay_path
from pneuma.casestudy.minelearn import (
    REPLAY,
    SCRATCH,
    Guidance,
    LearningMiner,
    check_toolkit,
    replay_decision,
    toolkit_edit_reach,
)
from pneuma.casestudy.toolkit import REHEARSAL_LOG, SEED_TOOLKIT
from pneuma.detect.discrimination import Discrimination
from pneuma.memory import TursoMemoryBackend
from pneuma.method import MethodAgent, ai_method

_ANSWER = (
    'final_answer(start_activity="Alpha", terminal_activities=["Gamma"], '
    'edges=[{"source": "Alpha", "target": "Gamma", "cases": 3}], '
    'threshold_used=1, method="replay probe")\n'
)

_CYCLE = [Turn(tool_calls=(("python_executor", {"code": _ANSWER}),)), Turn(text="ok")]
"""One scripted `discover` cycle: a code turn calling `final_answer`, then a close."""

GROWN = SEED_TOOLKIT + (
    '\n\ndef rare_handoffs(counted):\n    """Handoffs one case walked."""\n'
    "    return [(s, t) for s, t, _, c in counted if c == 1]\n"
)
"""A healthy local-looking edit: one helper added, everything else untouched."""


def _events() -> pl.DataFrame:
    """The rehearsal log as an event frame, so a whole loop runs in milliseconds."""
    return pl.read_csv(REHEARSAL_LOG.encode()).sort(["case_id", "position"])


# ── The replay mechanism exists and is cheaper — where a mid-trajectory edit exists ──


async def test_a_live_recorded_thread_with_a_mid_trajectory_edit_chooses_replay() -> None:
    """The positive case the fallback conditions are measured against.

    `replay_decision` is only honest about always declining if it *would* choose
    replay when both conditions clear: a live recorded thread, and an edit whose first
    altered decision is past 0. The decision names the thread to fork, and that id is
    the one `fork` / `spawn(seed_from=)` accept.
    """
    async with RuntimeHarness() as h:
        recorded = await LearningMiner().spawn(
            "discover", h.coordinator, model=ScriptedModel(list(_CYCLE))
        )
        decision = replay_decision(recorded, first_altered_decision=1)

        assert decision.path == REPLAY
        assert decision.fork_source == str(recorded.id)
        assert "decision 1" in decision.reason
        await recorded.retire()


async def test_replaying_a_suffix_costs_fewer_model_calls_than_scratch() -> None:
    """The economics, on scripted-model call counts rather than on an argument.

    A recorded two-decision trajectory, an edit that first alters decision 1: replay
    forks the recorded thread (pure `MethodThread.fork` — no new kernel semantics) and
    re-takes only decision 1, while a from-scratch re-run re-takes both. Costs are
    *measured* off each `ScriptedModel`'s consumed-turn counter rather than assumed,
    because turns-per-cycle is a runtime detail this test must not encode. The fork
    also proves the seeded-history caveat is moot on this path: the forked thread
    hosts the *same* method with the same tool schema, so the copied `toolUse` blocks
    are for tools the new thread does offer.
    """
    capacity = len(_CYCLE) * 4
    recorded_model = ScriptedModel(_CYCLE * 4)
    scratch_model = ScriptedModel(_CYCLE * 4)
    async with RuntimeHarness() as h:
        recorded = await LearningMiner().spawn("discover", h.coordinator, model=recorded_model)
        await recorded.run(SEED_TOOLKIT, "advice", REHEARSAL_LOG, 3, 3)
        await recorded.run(SEED_TOOLKIT, "tighter", REHEARSAL_LOG, 3, 3)
        recorded_cost = capacity - recorded_model.remaining_turns

        # Counterfactual suffix replay: fork at decision 1, re-take only the suffix
        # with the edited toolkit. The fork copies the log, so the prefix is context
        # the replayed decision sees without a single model call being spent on it —
        # and with `opus5`'s cache point, a live provider would bill that shared
        # prefix at cache-read rates on top.
        fork = await recorded.fork()
        await fork.run(GROWN, "tighter", REHEARSAL_LOG, 3, 3)
        replay_cost = capacity - recorded_model.remaining_turns - recorded_cost

        # The alternative the replay is priced against: re-run the whole two-decision
        # trajectory from scratch with the edited toolkit.
        scratch = await LearningMiner().spawn("discover", h.coordinator, model=scratch_model)
        await scratch.run(GROWN, "advice", REHEARSAL_LOG, 3, 3)
        await scratch.run(GROWN, "tighter", REHEARSAL_LOG, 3, 3)
        scratch_cost = capacity - scratch_model.remaining_turns

        assert replay_cost >= 1, "the replayed suffix still takes real model calls"
        assert replay_cost < scratch_cost, (
            f"replay re-took {replay_cost} model turn(s) against the from-scratch "
            f"re-run's {scratch_cost}: the shared prefix is exactly the saving"
        )
        await fork.retire()
        await scratch.retire()
        await recorded.retire()


# ── Fallback one: no recorded thread ──


async def test_no_recorded_thread_falls_back_to_scratch_and_says_so() -> None:
    """Both shapes of absence: never recorded, and recorded but torn down.

    The second is the one `trace` produces on every training round — the thread
    existed, and its id is now unforkable — and `retire()` makes a `MethodThread`
    report it honestly through `live`.
    """
    absent = replay_decision(None, first_altered_decision=1)
    assert absent.path == SCRATCH
    assert "no recorded thread" in absent.reason
    assert absent.fork_source == ""

    async with RuntimeHarness() as h:
        recorded = await LearningMiner().spawn(
            "discover", h.coordinator, model=ScriptedModel(list(_CYCLE))
        )
        await recorded.retire()
        dead = replay_decision(recorded, first_altered_decision=1)

    assert dead.path == SCRATCH
    assert "no recorded thread" in dead.reason


# ── Fallback two: the edit is globally coupled ──


async def test_a_globally_coupled_edit_falls_back_to_scratch_even_with_a_live_thread() -> None:
    """A live thread is not enough: an edit reaching decision 0 has no suffix to save.

    This is Shepherd's admitted limit measured on this parameter — the toolkit is
    consumed at sandbox setup, so `toolkit_edit_reach` reports 0 and the fallback
    names the coupling rather than a bookkeeping failure.
    """
    async with RuntimeHarness() as h:
        recorded = await LearningMiner().spawn(
            "discover", h.coordinator, model=ScriptedModel(list(_CYCLE))
        )
        decision = replay_decision(
            recorded, first_altered_decision=toolkit_edit_reach(SEED_TOOLKIT, GROWN)
        )
        await recorded.retire()

    assert decision.path == SCRATCH
    assert "globally coupled" in decision.reason
    assert "model-free" in decision.reason, "the cost half of the argument must be stated"


def test_every_real_toolkit_edit_reaches_decision_zero() -> None:
    """The coupling measurement itself: None for no edit, 0 for any edit.

    0 even for an added helper nothing calls, because the addition still changes the
    source executed at sandbox setup and the signatures advertised in the first
    prompt — the two channels that make the coupling structural rather than a
    property of which helper changed.
    """
    assert toolkit_edit_reach(SEED_TOOLKIT, SEED_TOOLKIT) is None
    assert toolkit_edit_reach(SEED_TOOLKIT, GROWN) == 0
    assert toolkit_edit_reach(SEED_TOOLKIT, SEED_TOOLKIT + "\n# comment\n") == 0


# ── The rollback contract is unchanged, and the check records which path ran ──


def test_check_toolkit_records_scratch_with_the_reason_on_a_healthy_edit(
    tmp_path: Path,
) -> None:
    """The record, on the path a healthy round takes.

    An edited toolkit: the check rehearses from scratch, passes, and the
    `ToolkitCheck` says scratch ran and why — because a loop that silently declined
    replay every round is indistinguishable from one with no replay path unless the
    decision is on the record. Both fallback conditions hold here (no recorded
    thread, coupled edit), and the reason named is the coupling, deliberately: it is
    the durable cause, true of every round of this workload, where the missing
    thread is the incidental one.
    """
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        memory.save("toolkit", GROWN)
        outcome = check_toolkit(memory)

        assert outcome.report.ok
        assert outcome.replay.path == SCRATCH
        assert "globally coupled" in outcome.replay.reason
    finally:
        memory.close()


async def test_a_failed_rehearsal_still_rolls_back_when_a_recorded_thread_exists(
    tmp_path: Path,
) -> None:
    """The rollback contract, unchanged by the replay machinery.

    The strongest configuration for the replay path — a live recorded thread handed
    straight to `check_toolkit` — must change nothing about what a failing candidate
    does: rolled back to the Frozen last good, reason kept, and the decision records
    scratch with the *coupling* as the cause, since with a live thread the first
    fallback condition no longer applies.
    """
    broken = "import os\n\n" + SEED_TOOLKIT
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        async with RuntimeHarness() as h:
            recorded = await LearningMiner().spawn(
                "discover", h.coordinator, model=ScriptedModel(list(_CYCLE))
            )
            memory.save("toolkit", broken)
            outcome = check_toolkit(memory, recorded_thread=recorded)
            await recorded.retire()

        assert outcome.rolled_back is True
        assert outcome.reason, "a rollback with no stated reason is a silent rollback"
        assert str(memory.fetch("toolkit")) == SEED_TOOLKIT
        assert outcome.replay.path == SCRATCH
        assert "globally coupled" in outcome.replay.reason
    finally:
        memory.close()


async def test_train_records_the_rehearsal_path_on_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole loop, offline: every `Attempt` carries path and fallback reason.

    Round 0 rehearses an unchanged seed ("no edit to replay"); to see the coupling
    reason a rewritten toolkit is planted mid-loop, which round 1's check meets as a
    candidate differing from last_good. Both rounds must say scratch — the loop never
    holds a recorded thread, and the edit reaches decision 0 anyway.
    """
    from pneuma.casestudy import minelearn

    class _NoOp:
        async def step(self, traced: Any, feedback: str, backends: list[Any]) -> None:
            del feedback, backends
            # The planted rewrite: what a consolidation would have written.
            memory.save("toolkit", GROWN)

    monkeypatch.setattr(minelearn, "TextGradOptimizer", _NoOp)
    # The pre-flight objective probe refuses the three-case rehearsal log (its
    # coverage term cannot discriminate there), and the probe's own verdict is
    # T5's concern, not this test's: stub it to a no-op report.
    monkeypatch.setattr(
        minelearn,
        "probe_objective",
        lambda *a, **k: SimpleNamespace(raise_if_pathological=lambda subject: None),
    )
    original = minelearn.LearningMiner.compiled

    def _scripted(self: Any, name: str, **overrides: Any) -> Any:
        return original(self, name, **overrides).replace(model=ScriptedModel(_CYCLE * 2))

    monkeypatch.setattr(minelearn.LearningMiner, "compiled", _scripted)

    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        training = await minelearn.train(
            _events(), tmp_path / "unused.db", rounds=2, sample_cases=None, memory=memory
        )
    finally:
        memory.close()

    assert len(training.attempts) == 2
    assert [a.rehearsal_path for a in training.attempts] == [SCRATCH, SCRATCH]
    assert training.attempts[0].rehearsal_fallback_reason == "no edit to replay"
    assert "globally coupled" in training.attempts[1].rehearsal_fallback_reason
    assert all(not a.rolled_back for a in training.attempts)


# ── harnesslearn: the same decision, recorded per round ──


def test_the_harness_weight_edit_is_always_globally_coupled() -> None:
    """`replay_path` names the sharper coupling: the weight is the objective.

    Every swept threshold evaluates the objective closed over the candidate weight,
    and the propose prompt opens with it — there is no decision the edit does not
    alter. And the no-edit case must not claim coupling it never measured.
    """
    unchanged = replay_path(0.5, 0.5)
    assert unchanged.path == SCRATCH
    assert unchanged.reason == "no edit to replay"

    edited = replay_path(0.5, 0.62)
    assert edited.path == SCRATCH
    assert "globally coupled" in edited.reason
    assert "every swept threshold" in edited.reason


async def test_harness_train_records_the_path_on_every_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness loop, offline: each `Round` carries path and reason.

    The gate is stubbed to a fixed admission — which detector admits what is
    T2/T5 territory; what this pins is that `train` computes and records the
    replay decision for the weight each round actually proposed against the one
    it recalled. Round 0 proposes 0.62 against a recalled 0.5, an edit; the
    consolidation is stubbed to a no-op, so round 1 proposes 0.62 against 0.5
    again — both rounds are edits and both must say scratch with the coupling
    named.
    """
    from pneuma.casestudy import harnesslearn

    admission = SimpleNamespace(
        ok=True,
        weight=0.62,
        quality=0.5,
        threshold=19,
        emptying=Discrimination(
            subject="emptying costs score",
            observations=4,
            separating=4,
            unit="shrinking pair",
            kind="harness parameter",
        ),
        rules=Discrimination(
            subject="derived compliance rules can fire",
            observations=3,
            separating=3,
            unit="attached rule",
            kind="harness parameter",
        ),
        refusals=(),
        baseline_rules=3,
        report_text=lambda: "stub",
    )

    async def _no_learn(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        return 1

    script = [
        Turn(
            tool_calls=(
                (
                    "HarnessProposal",
                    {"coverage_weight": 0.62, "evidence": "leaning toward coverage"},
                ),
            )
        ),
    ] * 4
    original = harnesslearn.HarnessProposer.compiled

    def _scripted(self: Any, name: str, **overrides: Any) -> Any:
        return original(self, name, **overrides).replace(model=ScriptedModel(script))

    monkeypatch.setattr(harnesslearn, "learn", _no_learn)
    monkeypatch.setattr(harnesslearn.HarnessProposer, "compiled", _scripted)
    monkeypatch.setattr(
        harnesslearn.HarnessProposer,
        "__init__",
        lambda self, *a, **k: harnesslearn.GatedProposer.__init__(
            self, gate=lambda candidate: admission
        ),
    )

    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        training = await harnesslearn.train(
            _events(), tmp_path / "unused.db", rounds=2, memory=memory
        )
    finally:
        memory.close()

    assert len(training.rounds) == 2
    assert [r.path for r in training.rounds] == [SCRATCH, SCRATCH]
    for entry in training.rounds:
        assert entry.weight == 0.62
        assert "globally coupled" in entry.path_reason


# ── Live: a replayed suffix reads the cache the recorded run wrote ──
#
# Module level, as everywhere else: `compile_ai_method` resolves annotations against
# module globals, so the output type cannot be function-local.


class Reading(BaseModel):
    """A minimal answer shape for the live replay."""

    value: int = Field(description="Any integer between 1 and 100.")
    why: str = Field(description="One sentence of reasoning.")


class Interpreter(MethodAgent):
    """The smallest replayable capability: read a briefing, answer a question."""

    name = "replay-interpreter"

    @ai_method(Reading, description="Answer one question about the briefing", max_attempts=2)
    def read(self, briefing: str, question: str) -> Reading:
        """Briefing:
        {briefing}

        Question: {question}
        """


class UsageRecording(Model):
    """Delegate to a real model, keeping each call's usage from the metadata event.

    The same shape `test_model_cache.py` uses, duplicated rather than imported because
    tests/library must not become an import surface for tests/app.
    """

    def __init__(self, inner: Model) -> None:
        super().__init__()
        self._inner = inner
        self.usages: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self._inner.get_config()

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.structured_output(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        inner = self._inner.stream(*args, **kwargs)

        async def _tap() -> Any:
            async for event in inner:
                if isinstance(event, dict) and "usage" in event.get("metadata", {}):
                    self.usages.append(dict(event["metadata"]["usage"]))
                yield event

        return _tap()


# Long enough that the recorded prefix clears the provider's minimum cacheable length
# (~4k tokens for Opus-class); content is irrelevant, only its stability across the
# recorded thread and its fork matters.
_BRIEFING = "\n".join(
    f"Rule {i}: the reading must not equal {i} plus any previously rejected value."
    for i in range(700)
)

_live_replay = pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE_REPLAY") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE_REPLAY=1 to measure cache reads on a replayed suffix",
)


@_live_replay
async def test_live_a_replayed_suffix_reads_the_prefix_from_the_provider_cache() -> None:
    """The prompt-cache benefit compounding into counterfactual replay, measured.

    A recorded thread takes one decision over a long briefing; the counterfactual
    fork replays a suffix — the same capability, an edited question — on a copy of
    that history. `opus5` puts a cachePoint at the end of every request's last user
    message, so the recorded call wrote its prefix to the provider cache and the
    replayed suffix's request, byte-identical up to the fork point, must report
    `cacheReadInputTokens > 0`: the prefix a replay does not re-take is also a prefix
    it does not pay full ingestion for.
    """
    from pneuma.model import opus5

    model = UsageRecording(opus5("low", max_tokens=4_096, show_thinking=False))
    async with RuntimeHarness() as h:
        recorded = await Interpreter().spawn("read", h.coordinator, model=model)
        await recorded.run(briefing=_BRIEFING, question="What is rule 3 about?")

        fork = await recorded.fork()
        await fork.run(briefing=_BRIEFING, question="And rule 5 — one sentence.")

        assert len(model.usages) >= 2
        read = model.usages[-1].get("cacheReadInputTokens", 0)
        assert read > 0, f"the replayed suffix read nothing from the cache: {model.usages}"
        await fork.retire()
        await recorded.retire()
