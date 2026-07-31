"""Tests for the loop that learns the harness, and for the gate that admits or rejects it.

The thing under test is authority: the agent proposes a harness parameter and the detectors
decide whether it may be used. So the tests are organised around what could go wrong with
*authority*, not around the functions.

## The three properties that matter, and why

**The gate must be able to reject.** A gate that cannot reject is the exact defect class
this whole project keeps finding: a check that passes without ever having been in a position
to fail. So the negative controls come first, they are deliberately pathological, and each
one is pathological in a *different* way, because the two halves of the gate are independent
detectors and a control that only exercises one of them would leave the other unmeasured.

**The safety property must be enforced by structure, not by argument.** `HarnessKnobs`
declares one field, and the parameters that could widen what the runtime permits are absent
rather than defended. That is asserted through the failure Pydantic actually produces —
`KeyError` from `_resolve_field` — because "we did not add that field" is a claim about the
present and `KeyError` is a claim about every future round.

**The measured baseline must be honest.** `test_the_honest_baseline` states the seed's
quality and the exhaustive ceiling as measurements, and the loop's result is compared
against the ceiling rather than against a flattering starting point. The finding it records
is negative and it is asserted, not merely written down: from the honest seed the numeric
search provably cannot reach the optimum, and the test computes the horizon that proves it.

## What is offline, what needs a live run

Offline covers everything whose failure would be *silent*, which is nearly all of it. The
gate is deterministic: `compose`, `probe`, `mine` and `apply_derived_rules` involve no model
call, so admission, rejection, quality and every discrimination measurement are exactly
reproducible. The score channel is deterministic too — `_numeric_update` is a stated search
with no randomness — so the loop's trajectory is asserted step by step rather than sampled.

`train`'s wiring is offline via a scripted forward model and a recorded optimizer, which is
the pattern `test_minelearn.py` established, and for the same reason: the wiring is where
the single-use-`ParameterView` and call-argument rules are easy to get wrong, and getting
them wrong produces a loop that reports rounds while the parameter stays at seed.

Live needs Bedrock and is marked. Two questions genuinely require it and neither can be
faked: whether a real proposer, shown a real rejection, proposes something admissible on
retry; and whether the backward pass routes a gradient to the numeric parameter at all when
the model is the one choosing targets.

## Mutation results, and the gap they found

Twelve deliberate defects were introduced into `harnesslearn.py` one at a time and the suite
re-run against each. Eleven were caught on the first pass. The twelfth was not, and it is
recorded here because the gap was real rather than cosmetic:

    separating=len(governed.live)  ->  len(governed.live) + len(governed.unknown)

Folding "the vacuity sweep ran out of budget" into "this rule has teeth" passed the entire
suite. Every rule-liveness assertion measured the permit log, where no verdict is ever
`unknown`, so the tests were passing without ever having been in a position to fail — the
vacuity defect, in the test file, about the vacuity detector. Closed by
`test_an_unknown_rule_verdict_counts_as_neither_live_nor_dead`, which constructs the
unsettled verdict instead of waiting for a log that produces one, and the mutation now fails.

Two other mutations are worth naming because they are the ones a reviewer would most likely
make. Replacing `quality`'s rule share with the objective's peak — the obvious meta-objective
— is caught, because peak is maximised at the pathological end. And dropping `and not
self.regressed` from `Admission.ok`, which reduces the gate to its objective half, is caught
by the `WEIGHT_FLOOR` control, which is the whole reason that control exists.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.types.graph import GradFeedback, ParameterNode, ThreadNode
from paths import PERMITS, needs_permits

from pneuma.casestudy import eventlog
from pneuma.casestudy.harnesslearn import (
    SEED_WEIGHT,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    Admission,
    Harness,
    HarnessKnobs,
    HarnessProposal,
    HarnessProposer,
    HarnessTraining,
    Round,
    admit,
    compose,
    emptying_margin,
    evidence_for,
    rule_liveness,
    substitute_score,
    weighted_score,
)
from pneuma.casestudy.minelearn import score_of
from pneuma.detect.objective import Domain, Space, probe
from pneuma.memory import TursoMemoryBackend

_LIVE = os.environ.get("PNEUMA_LIVE_HARNESS") == "1"
_live = pytest.mark.skipif(
    not _LIVE,
    reason="needs Bedrock credentials; set PNEUMA_LIVE_HARNESS=1 to measure the proposer",
)

# Measured on the permit log by an exhaustive 21-point sweep of the delegated domain, and
# asserted below rather than trusted. Named constants so a drift in either shows up as one
# failure naming the quantity instead of a bare number mismatching.
SEED_QUALITY = 0.75
CEILING_QUALITY = 0.8125
SEED_THRESHOLD = 17


@pytest.fixture(scope="module")
def permits() -> pl.DataFrame:
    return eventlog.parse_xes(PERMITS)


# ── The seed harness is the shipped harness, exactly ──


def test_the_seed_weight_reproduces_attempt_score_exactly() -> None:
    """`weighted_score(w=0.5)` is not approximately `Attempt.score`; it is that function.

    This is the test that makes every other number in this module honest. If the seed of the
    learnable family were merely close to the shipped objective, a measured improvement could
    be an artifact of the re-derivation rather than of the learning, and the two are
    indistinguishable from the outside.

    Swept over out-of-domain inputs on purpose. The interesting agreements are at negative
    coverage and at `edge_share` above 1, because those are where the shipped function's
    clamps and its `total <= 0` branch do the work, and a re-derivation that forgot either
    would still match on the well-behaved interior. The pole that once scored a garbage model
    at 319.386 lived exactly there.
    """
    values = (-2.0, -0.9, -0.3, 0.0, 0.001, 0.25, 0.5, 0.75, 1.0, 1.5, 3.0)
    checked = 0
    for coverage in values:
        for share in values:
            for invented in (0.0, 0.3, 1.0, 1.5, -0.5):
                checked += 1
                assert weighted_score(coverage, share, invented, weight=SEED_WEIGHT) == score_of(
                    coverage, share, invented
                ), (
                    f"the seed weight diverges from Attempt.score at coverage={coverage}, "
                    f"edge_share={share}, invented_share={invented}"
                )
    assert checked == 605


def test_the_weight_moves_the_tradeoff_without_touching_either_bound() -> None:
    """The weight changes ranking and nothing else, which is the safety property in miniature.

    Two halves. The weight must actually do something, or it is not a parameter; and it must
    not be able to push the score outside the range the shipped one produces, or it is a
    parameter that widens what counts as a good answer.
    """
    ranked = [weighted_score(0.9, 0.4, weight=w) for w in (0.1, 0.3, 0.5, 0.7, 0.9)]
    assert len(set(ranked)) == len(ranked), "the weight does not move the score at all"

    # No weight, however extreme, can make an invented-edge model score above an honest one
    # or push a legitimate score above 1. Both are properties the clamps establish, and the
    # weight is arithmetically incapable of reaching them.
    for weight in (0.001, 0.05, 0.5, 0.95, 0.999):
        assert weighted_score(1.0, 0.0, 0.0, weight=weight) <= 1.0
        assert weighted_score(1.0, 0.0, 0.5, weight=weight) < weighted_score(
            1.0, 0.0, 0.0, weight=weight
        )
        assert weighted_score(0.9, 0.3, 1.0, weight=weight) < 0.0


# ── The safety property, enforced by the schema rather than by a check ──


def test_the_non_delegable_parameters_are_absent_rather_than_defended(tmp_path: Path) -> None:
    """The structural half of the safety property, asserted through the error it produces.

    `HarnessKnobs` declares one field. The threshold window, the sweep resolution, the
    vacuity budget, the rule support floor and every finding's severity are not fields, so
    `_resolve_field` raises `KeyError` on both read and write. That is the numeric analogue
    of rules living in the verified IR: the optimizer's reach is bounded by a data structure,
    not by a guard somebody has to remember to run.

    Asserting the `KeyError` rather than asserting the field list is deliberate. A field list
    assertion says "we did not add that today" and would keep passing if a future field were
    added with a plausible name; this says the *store* cannot address it.
    """
    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        assert list(HarnessKnobs.model_fields) == ["coverage_weight"]
        assert memory.numeric_value("coverage_weight") == SEED_WEIGHT

        for forbidden in (
            "threshold_window",
            "sweep_resolution",
            "vacuity_limit",
            "min_support",
            "score_floor",
            "severity",
        ):
            with pytest.raises(KeyError):
                memory.fetch(forbidden)
            with pytest.raises(KeyError):
                memory.save(forbidden, "1")
    finally:
        memory.close()


def test_a_numeric_parameter_does_not_move_without_a_measurement(tmp_path: Path) -> None:
    """A text-only gradient leaves the value byte-identical, so the loop cannot invent movement.

    `_consolidate` returns early on a numeric field when no gradient carries a score. That
    matters here because the whole claim of this module is that the harness moves on
    *measurements*: a numeric parameter that drifted on prose would be a harness rewritten by
    assertion, and no round would be able to say which.
    """
    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        before = memory.numeric_value("coverage_weight")
        memory.consolidate(
            "coverage_weight",
            [GradFeedback(text="the weight should clearly be much higher")],
        )
        assert memory.numeric_value("coverage_weight") == before
        assert memory.observations("coverage_weight") == []

        memory.consolidate("coverage_weight", [GradFeedback(text="measured", score=0.4)])
        assert memory.numeric_value("coverage_weight") != before
        assert [round(v, 4) for v, _, _ in memory.observations("coverage_weight")] == [0.5]
    finally:
        memory.close()


# ── The negative controls: the gate must be able to reject ──


@needs_permits
def test_the_gate_rejects_a_weight_that_makes_the_empty_model_win(permits: pl.DataFrame) -> None:
    """Negative control one: a pathological objective, caught by the objective half.

    `coverage_weight = 0` weights out the only term that punishes an empty answer, so the
    score reduces to selectivity and the emptiest model wins outright. The gate must refuse,
    and it must refuse *with the cause named*: a rejection that said only "rejected" would
    give the proposer nothing to move on, and re-asking with an uninformative message is how
    a retry budget gets burned.
    """
    verdict = admit(permits, 0.0)

    assert not verdict.ok
    assert verdict.quality == 0.0, "a rejected harness must score zero, not partial credit"
    assert "emptying-is-free" in verdict.refusals
    assert "degenerate-optimum" in verdict.refusals
    assert verdict.emptying.discriminates is False
    assert "emptying-is-free" in verdict.report_text()

    # The pathology is not a knife-edge at exactly zero, which matters because a gate that
    # only caught the single worst value would be trivially evadable.
    assert not admit(permits, 0.001, baseline_rules=verdict.baseline_rules).ok


@needs_permits
def test_the_gate_rejects_a_weight_that_kills_a_rule_the_score_is_happy_with(
    permits: pl.DataFrame,
) -> None:
    """Negative control two: the safety half rejecting what the objective half admits.

    The most important test in this module, and a correction to an earlier draft of the
    module docstring that claimed the whole 0.05 to 0.95 range passes. It does pass the
    *objective* probe. It is nonetheless rejected, because weighting selectivity that heavily
    moves the selected threshold from 17 to 114, and at 114 one of the three derived
    precedences can no longer be violated by any reachable state.

    A vacuous rule is worse than no rule: TLC explores the whole space, reports no error, and
    the green verdict is about the shape of the mined graph rather than about the rule. So
    this asserts the conjunction directly — the objective probe says PASS and the gate says
    REJECT — because if those two were ever the same verdict, one of the detectors would be
    redundant and this whole design would be one detector wearing a hat.
    """
    verdict = admit(permits, WEIGHT_FLOOR)

    assert verdict.report.ok, "the objective probe is content, which is the whole point"
    assert verdict.refusals == ()
    assert not verdict.ok, "and the gate still rejects, on the safety half alone"
    assert verdict.regressed
    assert verdict.threshold > SEED_THRESHOLD
    assert verdict.rules.separating < verdict.baseline_rules
    assert verdict.quality == 0.0
    assert "compliance rules" in verdict.report_text()


@needs_permits
def test_the_gate_admits_the_seed_and_the_improved_weight(permits: pl.DataFrame) -> None:
    """The gate is not a refusal machine, which is the other half of having teeth.

    A detector that rejected everything would pass every rejection test above while being
    useless, and this project has already shipped one check that fired on nothing. So the
    seed and the measured optimum must both be admitted, with all three rules live.
    """
    seed = admit(permits, SEED_WEIGHT)
    assert seed.ok, seed.report_text()
    assert seed.threshold == SEED_THRESHOLD
    assert seed.quality == SEED_QUALITY
    assert seed.rules.discriminates is True
    assert seed.rules.separating == seed.rules.observations == 3

    better = admit(permits, 0.905, baseline_rules=seed.baseline_rules)
    assert better.ok, better.report_text()
    assert better.quality == CEILING_QUALITY
    assert better.quality > seed.quality


# ── The quality signal must itself discriminate ──


@needs_permits
def test_the_quality_signal_separates_harnesses_rather_than_being_flat(
    permits: pl.DataFrame,
) -> None:
    """The gate's own primitive, applied to the gate's own score. It caught a bad signal once.

    A quality signal that is constant across the domain it scores cannot tell a good harness
    from a bad one, which is exactly `Discrimination`'s question one level up. The first
    candidate signal tried here failed it: the separation between the honest optimum and the
    best degenerate input is exactly 1.0 at every admitted weight, so it was flat and was
    thrown out.

    Both surviving components are asserted to move, separately, because a combined score that
    moved only because one half moved would leave the other half unmeasured.
    """
    baseline = None
    qualities: list[float] = []
    margins: set[float] = set()
    for weight in (0.14, 0.365, 0.5, 0.905):
        verdict = admit(permits, weight, baseline_rules=baseline)
        baseline = verdict.baseline_rules
        assert verdict.ok, verdict.report_text()
        qualities.append(verdict.quality)
        margins.add(verdict.emptying.separating / verdict.emptying.observations)

    assert len(set(qualities)) > 1, "quality is flat across admitted weights, so it is useless"
    assert len(margins) > 1, "the emptying margin does not move, so it carries no signal"
    assert qualities == sorted(qualities), (
        "quality should rise with the coverage weight on this log; a non-monotone signal "
        "here would mean the two components disagree and the mean is hiding it"
    )


@needs_permits
def test_the_emptying_margin_reads_the_gates_own_counters(permits: pl.DataFrame) -> None:
    """`emptying_margin` parses the note the detector wrote, rather than re-walking the grid.

    A second implementation of the adjacency walk could disagree with the one the refusal is
    based on, and the quality signal would then be rating an objective the gate did not
    judge. So this asserts the parsed counters against the probe that produced them.
    """
    harness = compose(permits, weight=SEED_WEIGHT)
    report = probe(
        harness.objective,
        (harness.axis,),
        space=Space.DECISION,
        structure=harness.structure,
        components=harness.components,
    )
    margin = emptying_margin(report)

    assert margin.discriminates is True
    assert margin.observations > 0
    assert 0 < margin.separating <= margin.observations
    assert str(margin.separating) in " ".join(report.notes)
    assert margin.unit == "shrinking pair"


def test_an_unmeasured_emptying_check_is_withheld_rather_than_scored_zero() -> None:
    """Three-valued, for the reason `discrimination.py` gives: a search that did not run is
    not evidence.

    A sweep where the check never ran produces no shrinking pairs at all. Scoring that as a
    zero margin would make an unmeasured harness look identical to one measured and found
    monotone in emptiness, and those have different fixes.

    Built from a metric-space probe, where `_check_emptying` declines to run by design, so
    the condition is produced by the real detector rather than by a hand-made `Probe`.
    """
    from pneuma.detect.objective import Structure

    report = probe(
        lambda x: x,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
        structure=Structure(size=lambda x: x, units="x"),
    )
    margin = emptying_margin(report)

    assert margin.discriminates is None, "an unrun check must not settle either way"
    assert margin.observations == 0
    assert margin.withheld
    assert "UNSETTLED" in str(margin)


# ── The score channel carries a measurement, not the backward model's opinion ──


def test_substitute_score_replaces_the_backward_models_rating_with_the_measurement() -> None:
    """The correction at the heart of this module, asserted on a hand-built graph.

    `_distribute` sets `GradFeedback.score` from the backward *model's* structured output. So
    `optimizer.step` alone would drive the trust-region search on a language model's
    impression of how useful a number was, read off a conversation trace — a plausible
    quantity standing in for a measured one, in a loop that cannot tell them apart because
    both are floats in `[0, 1]`.

    A hand-built graph rather than a live one, because the property is about which number
    survives into `consolidate` and that is decidable without a model.
    """
    node = ThreadNode(node_id="t", thread_id="t")
    target = ParameterNode(node_id="p", name="coverage_weight", value=0.5)
    other = ParameterNode(node_id="q", name="something_else", value="x")
    target.gradients = [GradFeedback(text="model thinks this was fine", score=0.11)]
    other.gradients = [GradFeedback(text="untouched", score=0.11)]
    node.parameters = [target, other]

    replaced = substitute_score(node, "coverage_weight", 0.8125)

    assert replaced == 1
    assert target.gradients[0].score == 0.8125
    assert target.gradients[0].text == "model thinks this was fine", (
        "the routing decision is the model's job and must survive; only the number is ours"
    )
    assert other.gradients[0].score == 0.11, "a different parameter must not be touched"


def test_substituting_into_a_graph_with_no_gradient_reports_zero() -> None:
    """Zero replacements is the silent no-op this project keeps finding, so it is observable.

    `learn` refuses on it. If `substitute_score` returned `None` or swallowed the case, a
    round where no gradient reached the parameter would consolidate nothing, the value would
    stay at seed, and the loop would report a round anyway.
    """
    node = ThreadNode(node_id="t", thread_id="t")
    node.parameters = [ParameterNode(node_id="p", name="coverage_weight", value=0.5)]
    assert substitute_score(node, "coverage_weight", 0.9) == 0


# ── The honest baseline, and whether the loop beats it ──


@needs_permits
def test_the_honest_baseline_and_the_search_horizon_that_bounds_it(
    permits: pl.DataFrame, tmp_path: Path
) -> None:
    """The measurement this task is judged on, and it is a negative result.

    The baseline, stated before any loop runs: the seed weight of 0.5 is exactly
    `Attempt.score` and scores 0.75. An exhaustive 21-point sweep of the delegated domain
    finds a ceiling of 0.8125 at weight 0.905. So the entire mechanism competes for 0.0625,
    and anything below that is the loop losing to a for-loop.

    The finding is that from the honest seed **the numeric search provably cannot reach the
    ceiling**, and this test asserts the proof rather than only the outcome. Quality is a
    plateau: 0.75 for every weight from about 0.365 to 0.86. On a plateau the score handed to
    `_numeric_update` is constant, so every round takes an explore step of
    `span * TRUST * (1 - score) * DECAY ** trials`. That series is geometric, so the *total*
    distance the search can ever travel is bounded by `span * TRUST * (1 - score) / (1 -
    DECAY)`, which from 0.5 is 0.1406 and cannot pass 0.6406. The 0.8125 region starts near
    0.87. The stall is structural, not a tuning accident, and no round budget fixes it.

    This is a property of the *search*, not of the gate or the score channel: the mechanism
    is sound and it loses on this objective's shape. Reported as the finding.
    """
    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        seed = admit(permits, SEED_WEIGHT)
        assert seed.quality == SEED_QUALITY
        best = admit(permits, 0.905, baseline_rules=seed.baseline_rules)
        assert best.quality == CEILING_QUALITY
        headroom = round(best.quality - seed.quality, 4)
        assert headroom == 0.0625

        trajectory: list[tuple[float, float]] = []
        for _ in range(10):
            weight = memory.numeric_value("coverage_weight")
            verdict = admit(permits, weight, baseline_rules=seed.baseline_rules)
            trajectory.append((weight, verdict.quality))
            memory.consolidate(
                "coverage_weight",
                [GradFeedback(text=f"quality {verdict.quality}", score=verdict.quality)],
            )

        reached = max(quality for _, quality in trajectory)
        assert reached == SEED_QUALITY, (
            "the search matched the seed and did not beat it; if this ever improves, the "
            "measurement changed and the horizon arithmetic below must be re-derived"
        )
        assert reached < CEILING_QUALITY

        # The horizon, from the search's own declared constants rather than from the numbers
        # observed above, so this is a prediction the trajectory has to satisfy.
        from pneuma.memory.turso_backend import _EXPLORE_DECAY, _TRUST_FRACTION

        span = WEIGHT_CEILING - WEIGHT_FLOOR
        horizon = SEED_WEIGHT + span * _TRUST_FRACTION * (1 - SEED_QUALITY) / (1 - _EXPLORE_DECAY)
        assert round(horizon, 4) == 0.6406
        assert all(weight <= horizon for weight, _ in trajectory), (
            "the search left its own geometric horizon, so the stall explanation is wrong"
        )
        assert memory.numeric_value("coverage_weight") <= horizon
    finally:
        memory.close()


@needs_permits
def test_the_search_does_reach_the_ceiling_when_the_optimum_is_inside_its_horizon(
    permits: pl.DataFrame, tmp_path: Path
) -> None:
    """The control that turns the negative result into a diagnosis rather than an excuse.

    "The search did not find the optimum" has two explanations: the mechanism does not work,
    or the mechanism works and the optimum was out of reach. Those are different findings and
    only a measurement separates them. Seeded at 0.8, whose horizon of 0.9406 does contain
    the optimum, the same search with the same constants and the same gate crosses onto the
    0.8125 plateau and stays there.

    So the score channel does move a real harness parameter to a better value. Its limit is
    reach on a plateau, which is a property of the objective's shape.
    """
    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        memory.save("coverage_weight", 0.8)
        seed = admit(permits, SEED_WEIGHT)
        best = 0.0
        for _ in range(6):
            weight = memory.numeric_value("coverage_weight")
            verdict = admit(permits, weight, baseline_rules=seed.baseline_rules)
            best = max(best, verdict.quality)
            memory.consolidate("coverage_weight", [GradFeedback(text="q", score=verdict.quality)])
        assert best == CEILING_QUALITY, (
            "the search failed even with the optimum inside its horizon, so the mechanism "
            "itself is broken rather than merely short-ranged"
        )
        assert memory.numeric_value("coverage_weight") > 0.86
    finally:
        memory.close()


# ── The post-condition wiring: a rejection is re-asked, and a bug is not a verdict ──


@needs_permits
def test_a_pathological_proposal_is_rejected_by_the_post_condition(
    permits: pl.DataFrame,
) -> None:
    """The gate as a post-condition, which is the difference between a gate and a convention.

    A manual check after the call is a check the loop can forget. A post-condition cannot be
    skipped, and `ai_thread` feeds its message back to the model as a validation failure, so
    the rejection reaches the thing that has to fix it.

    Asserted by calling the validator directly, because what needs proving is that it raises
    with a usable message; whether a model then proposes better is the live test.
    """
    proposer = HarnessProposer(permits)

    proposer.admits(HarnessProposal(coverage_weight=SEED_WEIGHT, evidence="seed"))
    assert proposer.rejected == []

    with pytest.raises(AssertionError) as raised:
        proposer.admits(HarnessProposal(coverage_weight=0.0, evidence="selectivity only"))

    message = str(raised.value)
    assert "emptying-is-free" in message
    assert "Propose a different coverage_weight" in message
    assert len(proposer.rejected) == 1
    assert proposer.rejected[0].weight == 0.0


@needs_permits
def test_a_fault_in_the_gate_does_not_masquerade_as_a_rejection(
    permits: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception inside a post-condition is reported to the model as a validation failure.

    So a `KeyError` in the validator looks identical to a rejected proposal and burns every
    retry on a bug the model cannot fix. The gate call is therefore wrapped and an internal
    failure is re-raised as a message that says it is internal.

    This is the one property here that is about the *test suite* as much as the code: without
    it, a future refactor that broke `admit` would show up as a loop that mysteriously never
    admits anything, which is the least debuggable failure shape available.
    """
    proposer = HarnessProposer(permits)

    def _explode(weight: float) -> Admission:
        raise KeyError("threshold_window")

    monkeypatch.setattr(proposer, "gate", _explode)

    with pytest.raises(AssertionError) as raised:
        proposer.admits(HarnessProposal(coverage_weight=SEED_WEIGHT, evidence="x"))

    message = str(raised.value)
    assert "fault in the gate rather than a verdict" in message
    assert "KeyError" in message
    assert proposer.rejected == [], "a gate fault is not evidence against the proposal"


@needs_permits
def test_the_post_condition_parameter_name_cannot_collide_with_a_call_argument(
    permits: pl.DataFrame,
) -> None:
    """A post-condition whose first parameter shares an `ai_function` parameter's name raises
    `TypeError: got multiple values for argument`, swallowed as a validation failure.

    `admits` names its parameter `response`, and `propose` takes `coverage_weight` and
    `evidence`. Asserted mechanically rather than by reading, because the failure is silent
    and the fix is a one-word rename that a future edit could undo.
    """
    import inspect

    validator = inspect.signature(HarnessProposer.admits).parameters
    method = inspect.signature(HarnessProposer.propose).parameters
    overlap = (set(validator) & set(method)) - {"self"}
    assert not overlap, f"post-condition parameter(s) collide with call arguments: {overlap}"


# ── `train`'s wiring, offline ──


@needs_permits
async def test_train_hands_the_optimizer_a_numeric_gradient_target(
    permits: pl.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole `train` path with no Bedrock call, asserting what the optimizer was handed.

    The unit tests above prove the library works; they do not prove `train` wires it up, and
    the wiring is where the two silent rules bite. A `ParameterView` reused across rounds
    carries a gradient target on the first traced call and none afterwards; a view
    interpolated into an f-string renders the same prompt and drops the edge. Both failures
    look like a loop that runs and learns nothing.

    So this intercepts the learning step in the real loop and asserts the parameter arrived on
    every round, not merely the first.
    """
    from pneuma.casestudy import harnesslearn

    seen: list[list[tuple[str, bool]]] = []
    scores: list[float] = []

    async def _record(
        optimizer: Any,
        traced: Any,
        store: Any,
        feedback: str,
        *,
        quality: float,
        name: str = "coverage_weight",
    ) -> int:
        del optimizer, feedback
        graph = await build_graph_from_result(traced, [store])
        seen.append([(p.name, p.requires_grad) for p in graph.parameters])
        scores.append(quality)
        replaced = harnesslearn.substitute_score(graph, name, quality)
        store.consolidate(name, [GradFeedback(text="measured", score=quality)])
        return replaced

    # The structured-output tool is named after the output type, not `final_answer`: this
    # method has no code executor, so the answer arrives as a tool call named `HarnessProposal`.
    script = [
        Turn(
            tool_calls=(
                (
                    "HarnessProposal",
                    {"coverage_weight": 0.62, "evidence": "trying a coverage-leaning weight"},
                ),
            )
        ),
    ] * 8
    original = harnesslearn.HarnessProposer.compiled

    def _scripted(self: Any, name: str, **overrides: Any) -> Any:
        return original(self, name, **overrides).replace(model=ScriptedModel(script))

    monkeypatch.setattr(harnesslearn, "learn", _record)
    monkeypatch.setattr(harnesslearn.HarnessProposer, "compiled", _scripted)

    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        training = await harnesslearn.train(
            permits, tmp_path / "unused.db", rounds=3, memory=memory
        )
    finally:
        memory.close()

    assert len(training.rounds) == 3
    assert training.seed_quality == SEED_QUALITY
    assert len(seen) == 2, "the last round is measured, not learned from"
    for round_index, parameters in enumerate(seen):
        assert parameters == [("coverage_weight", True)], (
            f"round {round_index} carried no numeric gradient target, so the view was "
            "reused or interpolated"
        )
    assert all(score > 0.0 for score in scores), "a measured quality never reached the channel"
    assert all(entry.admitted for entry in training.rounds)


@needs_permits
async def test_train_refuses_a_round_that_would_consolidate_nothing(
    permits: pl.DataFrame, tmp_path: Path
) -> None:
    """`learn` raises when no gradient reached the parameter, rather than reporting a round.

    The failure this guards is the one every loop in this project has hit: the value stays at
    seed, every round reports a number, and nothing says the learning did not happen. Driven
    through `learn` with a graph that has no gradients, which is exactly the shape a missing
    `RuntimeHarness` produces.
    """
    from pneuma.casestudy import harnesslearn

    class _Silent:
        def backward(self, graph: Any, feedback: str) -> None:
            del graph, feedback

        def consolidate(self, graph: Any) -> None:  # pragma: no cover - must not be reached
            raise AssertionError("consolidate ran despite there being no gradient")

    class _Traced:
        value = HarnessProposal(coverage_weight=0.6, evidence="x")

    async def _empty_graph(traced: Any, backends: list[Any]) -> ThreadNode:
        del traced, backends
        node = ThreadNode(node_id="t", thread_id="t")
        node.parameters = [ParameterNode(node_id="p", name="coverage_weight", value=0.5)]
        return node

    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    import ai_functions.optimizer as optimizer_module

    original = optimizer_module.build_graph_from_result
    optimizer_module.build_graph_from_result = _empty_graph  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="no gradient reached"):
            await harnesslearn.learn(_Silent(), _Traced(), memory, "feedback", quality=0.75)
    finally:
        optimizer_module.build_graph_from_result = original  # type: ignore[assignment]
        memory.close()


# ── Reporting: a rejected round and a losing loop must both be visible ──


def test_the_summary_says_when_the_loop_did_not_beat_the_seed() -> None:
    """A loop that ran and lost must say so, or the table is a flattering comparison.

    `HarnessTraining.summary` is the artifact a reader judges the mechanism by, and the
    measured outcome on the permit log is that the search matches the seed rather than beating
    it. A summary that printed the best admitted round without that comparison would read as
    a success.
    """
    training = HarnessTraining(seed_quality=0.75)
    training.rounds = [
        Round(
            index=0,
            weight=0.5,
            quality=0.75,
            threshold=17,
            emptying=0.5,
            rule_share=1.0,
            admitted=True,
        ),
        Round(
            index=1,
            weight=0.0,
            quality=0.0,
            threshold=323,
            emptying=0.0,
            rule_share=0.0,
            admitted=False,
            refusals=("emptying-is-free",),
            rejected_before=1,
        ),
    ]
    training.rejections = ["harness gate: REJECTED — coverage_weight=0"]

    rendered = training.summary()

    assert not training.beat_seed
    assert "does NOT beat the seed" in rendered
    assert "REJECT" in rendered
    assert "rejected 1 proposal" in rendered
    assert training.best is not None and training.best.weight == 0.5, (
        "a rejected round must never be selectable as the best"
    )


def test_the_evidence_states_the_quantity_the_search_is_scored_on() -> None:
    """Every round, unconditionally, for the reason `probe_feedback` exists.

    A previous loop here reported coverage while selecting on a harmonic mean, and the agent
    obediently walked its score from 0.804 to 0.706 over four rounds while the number it was
    shown improved. A message that names the height only sometimes leaves the silent rounds
    steered by something else, so this asserts the quality appears in both branches.
    """
    from pneuma.detect.discrimination import Discrimination
    from pneuma.detect.objective import Probe

    def _admission(*, ok: bool) -> Admission:
        return Admission(
            weight=0.6,
            report=Probe(),
            emptying=Discrimination(
                subject="emptying costs score",
                observations=8,
                separating=4 if ok else 0,
            ),
            rules=Discrimination(
                subject="derived compliance rules can fire",
                observations=3,
                separating=3 if ok else 0,
            ),
            threshold=17,
            baseline_rules=3,
        )

    admitted = _admission(ok=True)
    assert admitted.ok
    assert f"{admitted.quality:.4f}" in evidence_for(admitted, 0.75)

    rejected = _admission(ok=False)
    assert not rejected.ok
    text = evidence_for(rejected, 0.75)
    assert "0.0000" in text
    assert "REJECTED" in text


def test_an_unknown_rule_verdict_counts_as_neither_live_nor_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsettled vacuity sweep must not be counted as a rule that can fire.

    This test exists because mutation testing found its absence. Changing
    `separating=len(governed.live)` to `len(governed.live) + len(governed.unknown)` — folding
    "we ran out of budget" into "this rule has teeth" — passed the entire suite, because no
    verdict on the permit log is ever `unknown` and every other test measures that log.

    That is the vacuity defect one level up: the *test* was passing without ever having been
    in a position to fail. So the unknown case is constructed here rather than waited for. A
    sweep that hit its budget is not evidence about the states it never reached, and counting
    it as live would let a harness that made the verification unmeasurable read as one that
    kept it intact — which is precisely the trade the safety half exists to refuse.
    """
    from pneuma.casestudy import harnesslearn
    from pneuma.casestudy.rules import Governed, Precedence
    from pneuma.detect.vacuity import RuleVerdict

    live = Precedence(before="A", after="B", cases=500)
    unsettled = Precedence(before="A", after="C", cases=400)

    def _fake(*args: object, **kwargs: object) -> Governed:
        del args, kwargs
        return Governed(
            process=None,  # type: ignore[arg-type]
            applied=[live, unsettled],
            measured={
                live.rule_name: RuleVerdict(
                    invariant=live.rule_name,
                    reachable_states=9,
                    antecedent_states=4,
                    violating_states=2,
                    truncated=False,
                    limit=200_000,
                ),
                unsettled.rule_name: RuleVerdict(
                    invariant=unsettled.rule_name,
                    reachable_states=9,
                    antecedent_states=0,
                    violating_states=0,
                    truncated=True,
                    limit=200_000,
                ),
            },
        )

    monkeypatch.setattr(harnesslearn.rules, "apply_derived_rules", _fake)
    measured = rule_liveness(pl.DataFrame(), None)  # type: ignore[arg-type]

    assert measured.observations == 2
    assert measured.separating == 1, "an unknown verdict was counted as a live rule"
    assert len(measured.withheld) == 1
    assert unsettled.rule_name in measured.withheld[0]
    assert measured.discriminates is True, "one real live rule still settles the verdict"


@needs_permits
def test_rule_liveness_settles_on_the_permit_log_and_reports_the_finding(
    permits: pl.DataFrame,
) -> None:
    """Three-valued on the safety half too, which is `vacuity`'s own rule.

    On the permit log every verdict settles, so what is checked here is that a settled result
    withholds nothing, that the counts are consistent, and that a threshold high enough to
    strand the forbidden states reports the *finding* rather than an abstention. The unknown
    branch is covered by the test above, on a constructed verdict.
    """
    from pneuma.casestudy import miner

    process = miner.mine(permits, name="M", min_edge_cases=SEED_THRESHOLD).process
    live = rule_liveness(permits, process)

    assert live.withheld == ()
    assert live.discriminates is True
    assert live.separating <= live.observations
    assert live.unit == "attached rule"

    # And at a threshold high enough to strand the forbidden states, the same call reports
    # the finding rather than an abstention.
    stranded = rule_liveness(permits, miner.mine(permits, name="M", min_edge_cases=200).process)
    assert stranded.discriminates is False
    assert stranded.separating == 0
    assert stranded.observations > 0


@needs_permits
def test_compose_carries_the_window_as_data_that_no_gradient_can_reach(
    permits: pl.DataFrame,
) -> None:
    """The non-delegable window is visible in the record and absent from the schema.

    Both halves matter. Visible, so a report can state what search space was probed rather
    than leaving the bound silent. Absent from `HarnessKnobs`, so no gradient can move it.
    """
    harness = compose(permits, weight=SEED_WEIGHT)
    assert isinstance(harness, Harness)
    assert harness.window > 0
    assert harness.axis.high == harness.window
    assert harness.axis.feasible == (1.0, float(harness.window))

    narrowed = compose(permits, weight=SEED_WEIGHT, window=60)
    assert narrowed.window == 60, "a caller may narrow the window"
    assert "window" not in HarnessKnobs.model_fields, "an optimizer may not"


# ── Live: what no fake can answer ──


@_live
@needs_permits
async def test_live_the_proposer_recovers_from_a_rejection(
    permits: pl.DataFrame, tmp_path: Path
) -> None:
    """Does a real model, shown a real rejection, propose something admissible on retry?

    The offline tests prove the gate raises with a usable message. Whether that message is
    usable *by a model* is a property of the model, and no scripted stand-in can answer it: a
    script that returns an admissible weight on attempt two proves only that the script does.

    Seeded at a rejected weight so the model is shown a real refusal, then asserted on the
    outcome rather than on the path, since how many attempts a model needs is not ours to fix.

    An earlier version also asserted `proposer.rejected` was non-empty, which contradicted
    that sentence and failed live for the best possible reason: shown the rejection as
    evidence, the model proposed an admissible weight on its *first* attempt, so the
    post-condition never had to fire. Requiring a rejection would demand the model be wrong
    once before being right.
    """
    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        memory.save("coverage_weight", WEIGHT_FLOOR)
        async with RuntimeHarness():
            proposer = HarnessProposer(permits)
            compiled = proposer.compiled("propose", post_conditions=[proposer.admits])
            rejected = admit(permits, WEIGHT_FLOOR)
            assert not rejected.ok, "the seeded weight must be one the gate refuses"

            result: Any = await compiled.trace(WEIGHT_FLOOR, evidence_for(rejected, SEED_QUALITY))
            proposal: HarnessProposal = result.value

        final = proposer.gate(proposal.coverage_weight)
        assert final.ok, "the proposer never recovered from the rejection:\n" + final.report_text()
        assert proposal.coverage_weight != WEIGHT_FLOOR, (
            "the proposer returned the seeded weight unchanged, so it did not act on the "
            "rejection at all"
        )
    finally:
        memory.close()


@_live
@needs_permits
async def test_live_the_backward_pass_routes_a_gradient_to_the_numeric_parameter(
    permits: pl.DataFrame, tmp_path: Path
) -> None:
    """Does a real backward model target a numeric parameter at all?

    `substitute_score` replaces the score, but only on gradients the backward pass actually
    routed to this parameter. If a real model declines to target a bare float, `learn` raises
    and the loop is dead — and that is a question about the model, so it is measured here
    rather than assumed in either direction.
    """
    from pneuma.casestudy import harnesslearn

    memory = TursoMemoryBackend(HarnessKnobs, actor_id="harness", path=tmp_path / "h.db")
    try:
        training = await harnesslearn.train(
            permits, tmp_path / "unused.db", rounds=2, memory=memory
        )
        observations = memory.observations("coverage_weight")
        assert observations, (
            "the backward model routed no gradient to the numeric parameter, so no "
            "observation was recorded and the harness cannot be learned this way"
        )
        assert all(0.0 <= score <= 1.0 for _, score, _ in observations)
        assert training.rounds
    finally:
        memory.close()
