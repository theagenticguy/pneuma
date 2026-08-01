"""The objective prober, run against the objective this project actually selects on.

The pure half of these tests lives in `tests/library/test_objective.py` and probes the
prober's own mechanics against hand-written objectives: poles, NaN, unboundedness, spike
detection, enumeration, provenance, adjudication. Those hold with `pneuma.casestudy`
absent, and a prober that only had them would be a mechanism nobody had pointed at
anything.

These are the tests that point it. Every one reads the live objective rather than a
reconstruction of it — through `Attempt.score`, through `feedback_for`, or through a real
log parsed by the case study's own loader — so they establish the two things the pure
tests cannot:

- the prober **clears** the objective in use, so it is a check that can be adopted rather
  than one that refuses everything
- the prober **names the defects still live in it**: coverage unclamped so 3.0 scores 1.5
  against an honest maximum of 1.0, the division pole relocated to the coverage axis where
  -0.9 with share 0.0999999 scores -16200001.8085, and `feedback_for` omitting the score on
  round zero

Reading the objective live is the whole point. A copy of the scoring formula pinned here
would keep passing after the formula changed, which is the defect class this project
detects one level up: a check that cannot fire.

## The fourth anchor, added after the declared-degenerate contract was replaced

`Degenerate` used to be the only way the prober learned what a bad answer looked like, and
a hand-written list of bad answers is written by the same hand as the scoring formula. The
measured proof is at the bottom of this file: the prober passed a genuinely degenerate
objective with zero findings, and the missing declaration was invisible on the permit log
by construction, because there the smallest surviving model scores 0.1496 against an
optimum of 0.8606.

So a fourth anchor is pinned: **the transcript log's composed objective is refused with
nothing declared**. Every point is enumerated from a `Structure`, and both fixtures are
exercised so the separation is measured rather than asserted.
"""

from __future__ import annotations

import io

import polars as pl
import pytest
from paths import FLEET, PERMITS, needs_fleet, needs_permits

from pneuma.casestudy import eventlog
from pneuma.casestudy.aimine import Discovered, Edge, grade, to_csv
from pneuma.casestudy.minelearn import Attempt, feedback_for, score_edges, visible_handoffs
from pneuma.casestudy.miner import start_and_end_activities
from pneuma.detect.objective import (
    Degenerate,
    Domain,
    ObjectiveRefused,
    Severity,
    Space,
    Structure,
    probe,
    probe_feedback,
)

# ── The objective under test, read live rather than reconstructed ──


def current_objective(coverage: float, edge_share: float) -> float:
    """pneuma's objective as it stands, read through the live `Attempt.score`."""
    return Attempt(
        index=0,
        coverage=coverage,
        matched_coverage=coverage,
        threshold=1,
        states=1,
        edges=100,
        guidance_chars=0,
        edge_share=edge_share,
    ).score


def historical_coverage_only(coverage: float, edge_share: float) -> float:
    """Attempt one. Coverage was the whole objective; `edge_share` was not scored at all.

    Reconstructed from section 10: "A jump from 93.2% to 98.6% in one round ... It also
    found the degenerate optimum." Threshold 1 keeps every handoff and replays the log.
    """
    return coverage


METRIC_AXES = (Domain("coverage", 0.0, 1.0), Domain("edge_share", 0.0, 1.0))

BOUNDED_AXES = (
    Domain("coverage", 0.0, 1.0, bounded_by="miner.conformance divides by the case count"),
    Domain("edge_share", 0.0, 1.0, bounded_by="score_edges intersects, Attempt.score clamps"),
)

DEGENERATE = (
    Degenerate("keep every handoff", {"coverage": 1.0, "edge_share": 1.0}),
    Degenerate("keep nothing", {"coverage": 0.0, "edge_share": 0.0}),
)


def checks(report: object, severity: Severity | None = None) -> set[str]:
    findings = report.findings  # type: ignore[attr-defined]
    return {f.check for f in findings if severity is None or f.severity is severity}


# ── Regression fixture: the current objective must pass ──


def test_the_current_objective_passes_once_its_bounds_are_declared() -> None:
    """The prober has to clear the objective that is actually in use, or it is a check
    nobody can adopt. Both bounds it relies on are established by code rather than merely
    intended, so both are declared with `bounded_by` and the escape findings downgrade to
    warnings the reviewer can weigh.

    Passing here is a statement about the *declared* domain plus the two `bounded_by`
    claims, and nothing more. The warnings this leaves are the subject of the next two
    tests, and one of them is a defect that is still live."""
    report = probe(current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE)

    assert report.ok, report.report()
    assert not report.refusals


def test_the_current_objective_leaks_above_one_on_coverage_and_the_probe_says_so() -> None:
    """A real finding, and the reason `bounded_by` warns rather than staying silent.

    `Attempt.score` clamps `edge_share` and does not clamp `coverage`. At coverage 3.0 and
    share 0.0 it returns 1.5, above the honest maximum of 1.0, and the supremum as coverage
    grows is 2.0. Nothing reachable produces it today because `conformance` divides by the
    case count, which is exactly the asymmetry worth a warning: the objective's own
    arithmetic does not enforce the bound its docstring assumes."""
    assert current_objective(3.0, 0.0) == pytest.approx(1.5)
    assert current_objective(1e9, 0.0) == pytest.approx(2.0)
    assert current_objective(1.0, 5.0) == 0.0  # the share clamp does hold

    report = probe(current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE)
    leaks = [
        f
        for f in report.warnings
        if f.check == "escape-rewarded" and f.point and f.point["coverage"] > 1.0
    ]
    assert leaks, report.report()


def test_the_pole_was_relocated_to_the_coverage_axis_rather_than_removed() -> None:
    """The prober's most useful finding on live code, and the exact defect class it was
    written for: a fix that closes the axis the failure arrived on and leaves the other one.

    `Attempt.score` clamps `edge_share` into `[0, 1]` and does not clamp `coverage`, and its
    denominator guard is `0.0 if total <= 0`. Negative coverage lands `total` on a tiny
    *positive* float, the guard misses, and the same division pole is back on the other
    axis. Measured: coverage -0.9 with share 0.0999999 scores -16200001.8085.

    This is why `bounded_by` demands a name rather than a boolean. Coverage really is
    bounded, by `miner.conformance` dividing by the case count, so this cannot be reached
    through the current pipeline and the finding is a warning. But the bound is a property
    of a different file, and the objective's arithmetic does not enforce it."""
    assert current_objective(-0.9, 0.0999999) == pytest.approx(-16200001.8085, abs=1e-3)
    assert current_objective(-0.9, 0.1000001) == 0.0  # the guard fires on the other side

    undeclared = probe(current_objective, METRIC_AXES, space=Space.METRIC, degenerate=DEGENERATE)
    assert not undeclared.ok
    assert "escape-pole" in checks(undeclared, Severity.REFUSE), undeclared.report()

    declared = probe(current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE)
    assert "escape-pole" in checks(declared, Severity.WARN), declared.report()


def test_undeclared_bounds_make_the_same_leak_a_refusal() -> None:
    """`bounded_by` is a claim the caller makes and has to write down. Without it the
    prober refuses, so the downgrade cannot happen by omission."""
    report = probe(current_objective, METRIC_AXES, space=Space.METRIC, degenerate=DEGENERATE)

    assert not report.ok
    assert "escape-rewarded" in checks(report, Severity.REFUSE)


def test_the_current_objective_is_not_maximised_by_keeping_everything() -> None:
    """The contrast that makes the previous test mean something: same probe, same
    degenerate inputs, and the fixed objective scores memorisation at zero."""
    assert current_objective(1.0, 1.0) == 0.0
    report = probe(
        current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    assert "degenerate-optimum" not in checks(report)


# ── Refusal, and the contrast that the live objective clears it ──


def test_refusal_raises_with_the_whole_report_attached() -> None:
    report = probe(
        historical_coverage_only, METRIC_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    with pytest.raises(ObjectiveRefused, match="degenerate-optimum"):
        report.raise_if_pathological("miner objective")

    clean = probe(
        current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    clean.raise_if_pathological("miner objective")  # must not raise


# ── Decision space: the two spaces are not interchangeable ──


def test_metric_space_says_it_did_not_check_for_a_boundary_maximum() -> None:
    """The single most important negative result in this module. In metric space the ideal
    corner is a boundary and is *supposed* to win: the fixed objective's grid maximum is
    coverage 1.0 with share 0.0, on the face of the box. A boundary-max check there fires
    on the good objective and the broken one alike, so it is not run, and the report says
    it was not run instead of quietly passing."""
    report = probe(
        current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    best = report.sweeps[0].best
    assert best is not None
    assert best.point == {"coverage": 1.0, "edge_share": 0.0}

    assert "window-too-narrow" not in checks(report)
    assert any("boundary-max not checked" in note for note in report.notes)


@needs_permits
def test_the_real_pipeline_objective_has_an_interior_optimum_over_the_full_range() -> None:
    """The end-to-end check, on the real log through the real `grade` and `score_edges`.

    Measured baseline: over the whole feasible threshold range the composed objective peaks
    in the interior, so the current harness has no degenerate optimum in the variable the
    loop actually moves. Both endpoints are bad, which is the shape a sound objective has:
    threshold 1 keeps everything and scores 0.0 by memorising."""
    events = eventlog.parse_xes(PERMITS)
    csv = to_csv(events, sample_cases=400)
    shown = visible_handoffs(csv)
    firsts, lasts = start_and_end_activities(pl.read_csv(io.StringIO(csv)))
    cache: dict[int, float] = {}

    def objective(threshold: float) -> float:
        step = max(1, int(round(threshold)))
        if step in cache:
            return cache[step]
        kept = shown.filter(
            (pl.col("cases") >= step) & (pl.col("activity") != pl.col("next_activity"))
        )
        if not kept.height:
            cache[step] = 0.0
            return 0.0
        model = Discovered(
            start_activity=firsts["activity"][0],
            terminal_activities=[lasts["activity"][0]],
            edges=[
                Edge(source=r["activity"], target=r["next_activity"], cases=r["cases"])
                for r in kept.iter_rows(named=True)
            ],
            threshold_used=step,
            method="probe",
        )
        graded = grade(events, model)
        audit = score_edges(model, visible_handoffs=shown)
        cache[step] = Attempt(
            index=0,
            coverage=graded.coverage,
            matched_coverage=graded.coverage,
            threshold=step,
            states=graded.states,
            edges=graded.edges,
            guidance_chars=0,
            edge_share=audit.edge_share,
            invented_edges=audit.invented,
        ).score
        return cache[step]

    assert objective(1) == 0.0  # keeping every handoff earns nothing

    top = int(shown["cases"].max())
    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        degenerate=(
            Degenerate("keep every handoff (threshold 1)", {"threshold": 1.0}),
            Degenerate("keep nothing (above max support)", {"threshold": float(top) + 1}),
        ),
    )

    assert report.ok, report.report()
    best = report.sweeps[0].best
    assert best is not None
    assert 1 < best.point["threshold"] < top, report.report()


# ── The feedback channel ──


def feedback_objective(coverage: float, edge_share: float) -> float:
    return current_objective(coverage, edge_share)


def rendered(point: dict[str, float], best: float | None) -> str:
    return feedback_for(
        Attempt(
            index=0,
            coverage=point["coverage"],
            matched_coverage=0.95,
            threshold=5,
            states=17,
            edges=29,
            guidance_chars=100,
            edge_share=point["edge_share"],
        ),
        best_so_far=best,
    )


# The four rounds of the documented failure-2 run, as (coverage, edge_share).
ROUNDS = (
    {"coverage": 0.932, "edge_share": 0.29},
    {"coverage": 0.956, "edge_share": 0.38},
    {"coverage": 0.969, "edge_share": 0.44},
    {"coverage": 1.0, "edge_share": 0.95},
)


def test_the_coverage_only_feedback_is_caught_reporting_the_wrong_quantity() -> None:
    """Failure two. Scoring was the harmonic mean and the feedback reported coverage, so
    the agent loosened the threshold every round while the score it was selected on fell.

    Reproduced from the documented run: coverage rises 0.932 to 0.969 across the rounds
    while the score falls 0.806 to 0.7098, and the prober both refuses and attaches the
    anti-correlation as evidence."""

    def coverage_only(point: dict[str, float], best: float | None) -> str:
        return (
            f"This model replayed {100 * point['coverage']:.1f}% of complete cases. "
            "Aim higher on coverage."
        )

    report = probe_feedback(
        coverage_only,
        feedback_objective,
        ROUNDS,
        components=("coverage", "edge_share"),
        best_so_far=0.804,
    )

    assert not report.ok
    assert "feedback-omits-the-score" in checks(report, Severity.REFUSE)
    assert "It does report coverage" in report.report()
    assert any("disagree about which" in note for note in report.notes)


def test_feedback_that_reports_the_score_only_sometimes_is_caught() -> None:
    """The narrowed form of the same defect, and the one the doc describes: the message
    only complained about memorisation above 60% edge share, so at 29% the agent heard
    nothing about the score at all."""

    def one_sided(point: dict[str, float], best: float | None) -> str:
        score = feedback_objective(**point)
        if point["edge_share"] > 0.6:
            return f"You kept {100 * point['edge_share']:.0f}% of handoffs; score {score:.3f}."
        return f"This model replayed {100 * point['coverage']:.1f}% of cases. You are behind."

    report = probe_feedback(
        one_sided, feedback_objective, ROUNDS, components=("coverage", "edge_share")
    )

    assert not report.ok
    assert "feedback-reports-the-score-conditionally" in checks(report, Severity.REFUSE)


def test_the_current_feedback_states_the_score_when_there_is_a_standing_best() -> None:
    """The positive control. Once a best exists, `feedback_for` reports the score, the
    standing best, and the direction, on every branch."""
    report = probe_feedback(
        rendered,
        feedback_objective,
        ROUNDS,
        components=("coverage", "edge_share"),
        best_so_far=0.804,
    )

    assert report.ok, report.report()


def test_the_current_feedback_omits_the_score_on_the_first_round() -> None:
    """A live finding the prober located rather than a fixture. `feedback_for` builds the
    score into the `standing` clause, and `standing` is empty when `best_so_far is None`,
    so round zero is steered entirely by coverage and the gap. That is failure two's exact
    shape surviving in one branch.

    Verified directly before asserting the prober catches it, so the test pins the defect
    and not just the detector's opinion of it."""
    first = Attempt(
        index=0,
        coverage=0.932,
        matched_coverage=0.95,
        threshold=5,
        states=17,
        edges=29,
        guidance_chars=100,
        edge_share=0.29,
    )
    message = feedback_for(first, best_so_far=None)
    assert first.score == pytest.approx(0.806)
    assert not any(f"{first.score:.{digits}f}" in message for digits in (1, 2, 3, 4))

    report = probe_feedback(
        rendered,
        feedback_objective,
        ROUNDS,
        components=("coverage", "edge_share"),
        best_so_far=None,
    )

    assert not report.ok
    assert "feedback-omits-the-score" in checks(report, Severity.REFUSE)


def test_the_feedback_probe_says_what_it_did_not_check() -> None:
    """The honest boundary of the mechanism. Presence of the number is decidable; whether
    the prose's advice points uphill is a semantic property of English and is not. The
    report says so rather than implying the stronger check happened."""
    report = probe_feedback(
        rendered, feedback_objective, ROUNDS, components=("coverage",), best_so_far=0.804
    )

    assert any("not that its advice points uphill" in note for note in report.notes)


def test_the_feedback_probe_refuses_to_run_on_a_single_round() -> None:
    """Conditional reporting is invisible with one sample, so one sample is rejected rather
    than silently answering a weaker question."""
    with pytest.raises(ValueError, match="at least two points"):
        probe_feedback(rendered, feedback_objective, (ROUNDS[0],))


# ── The consumer wiring ──


def test_the_loops_own_preflight_passes_and_reports_what_it_downgraded() -> None:
    """`minelearn.probe_objective` is the consumer, and `train` calls
    `raise_if_pathological` before its first round. It passes, and the report names the two
    findings it downgraded plus the flag that shows them undowngraded, so "PASS" here is a
    claim a reviewer can go and check rather than a green light."""
    from pneuma.casestudy.minelearn import probe_objective

    report = probe_objective()

    assert report.ok, report.report()
    assert len(report.warnings) >= 2
    assert "trust_declared_bounds=False" in report.report()
    report.raise_if_pathological("the miner's balanced score")


def test_the_preflight_says_the_decision_space_checks_did_not_run_without_a_log() -> None:
    """`probe_objective()` with no log is the metric probe alone, and that is not the whole
    pre-flight. The emptiest-answer and emptying-is-free checks are decision-space
    properties, so they cannot run, and this pins that the report says so rather than
    reporting a PASS that a reader would take as covering them.

    `train` passes the log for exactly this reason. The metric probe passing on its own is
    what left the transcript-log defect open all session."""
    from pneuma.casestudy.minelearn import probe_objective

    report = probe_objective()

    assert any("decision-space checks did not run" in note for note in report.notes)
    assert "emptying-is-free" not in checks(report)


def test_the_probed_callable_is_the_selected_one() -> None:
    """The check that stops this being theatre. If `score_of` re-implemented the objective
    instead of reading it, the probe would clear a function the loop does not use, which is
    a check that passes while never being able to fire on the real thing."""
    from pneuma.casestudy.minelearn import score_of

    for coverage, share in ((0.864, 0.1414), (1.0, 1.0), (0.8438, 0.1313), (0.0, 0.0)):
        assert score_of(coverage, share) == Attempt(
            index=0,
            coverage=coverage,
            matched_coverage=coverage,
            threshold=1,
            states=1,
            edges=100,
            guidance_chars=0,
            edge_share=share,
        ).score

    assert score_of(0.864, 0.1414) == pytest.approx(0.8613)  # the measured sweep peak


def test_a_percentage_rendering_of_the_score_counts_as_reporting_it() -> None:
    """The check must not be defeated by a formatting convention. Feedback in this project
    renders shares as percentages, so a prober that only looked for the raw float would
    miss a message that does state the score and would be a check that cannot fire."""

    def as_percent(point: dict[str, float], best: float | None) -> str:
        return f"Balanced score this round: {100 * feedback_objective(**point):.1f}%."

    report = probe_feedback(
        as_percent, feedback_objective, ROUNDS, components=("coverage",)
    )

    assert report.ok, report.report()


# ── Mechanical enumeration is decision-space only, on the live objective ──


def test_the_enumeration_is_decision_space_only_and_says_so_in_metric_space() -> None:
    """The correction a live adversary run forced, pinned as the property it produced.

    An earlier version enumerated the size-derived points in metric space too. That is
    unsound twice: 21 points tie for the smallest non-zero `edge_share` on the current
    objective's grid, so the pick was iteration-order dependent, and tiebreaking on score
    converges the emptiest point onto the ideal corner as the grid refines, which would
    eventually refuse a sound objective for having a good optimum.

    Both halves are checked here. The metric probe over the live objective enumerates
    nothing and says why; the same structure in decision space is what produces findings."""
    metric = probe(
        current_objective,
        BOUNDED_AXES,
        space=Space.METRIC,
        structure=Structure(
            size=lambda coverage, edge_share: edge_share,
            viable=lambda coverage, edge_share: edge_share > 0.0,
            units="share kept",
        ),
    )

    assert metric.ok, metric.report()
    assert "degenerate-optimum" not in checks(metric)
    assert any("not enumerated" in note for note in metric.notes)
    assert any("emptying-is-free not checked" in note for note in metric.notes)

    # The measurement behind the correction: at the smallest non-zero share, the best point
    # is the one holding coverage at its ideal, and it very nearly *is* the grid maximum.
    assert current_objective(1.0, 0.05) == pytest.approx(0.9744)
    assert current_objective(1.0, 0.0) == pytest.approx(1.0)


# ── The fourth anchor: both fixtures, nothing declared ──


@needs_permits
def test_the_permit_objective_still_passes_with_nothing_declared() -> None:
    """Half of the separation, and the half that has to hold or the prober is unusable.

    The permit log's composed objective is sound: emptying costs score, and the smallest
    surviving model loses badly. Measured through the same `threshold_objective` the loop's
    pre-flight uses, so this is the objective actually in play and not a stand-in."""
    from pneuma.casestudy.minelearn import threshold_objective

    events = eventlog.parse_xes(PERMITS)
    objective, structure, top, _components = threshold_objective(events)

    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
    )

    assert report.ok, report.report()
    assert any("emptying-is-free passed" in note for note in report.notes)

    # The measured ordering the pass rests on: emptying past the peak costs real score, and
    # the emptiest surviving model is far below it.
    best = report.sweeps[0].best
    assert best is not None
    smallest = objective(float(top))
    assert smallest < (best.value or 0.0) - 0.5, (
        f"emptiest scores {smallest:.4f} against {best.value:.4f}"
    )


@needs_fleet
def test_the_transcript_objective_is_refused_with_nothing_declared() -> None:
    """The other half, and the whole point of the change.

    This is the case the prober passed with zero findings all session, because the caller's
    declared degenerate inputs were the two the permit log needed. Nothing is declared here:
    every refusal comes from the enumeration or from the emptying walk.

    Measured through the real `grade` and `score_edges`, and the number that makes it a
    defect rather than a curiosity is that whole-trace coverage on this log is 0.0227 at
    *every* threshold from 1 to the maximum support. The score therefore reduces to a
    monotone function of selectivity and its optimum is a two-state, one-edge model replaying
    two cases out of 88."""
    from pneuma.casestudy import transcriptlog
    from pneuma.casestudy.minelearn import threshold_objective

    events, _ = transcriptlog.load_sample(FLEET)
    objective, structure, top, _components = threshold_objective(events)

    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
    )

    assert not report.ok, report.report()
    assert {"emptying-is-free", "degenerate-optimum"} <= checks(report, Severity.REFUSE)
    assert "emptiest answer" in report.report()
    assert "[enumerated]" in report.report()

    # And the reason, asserted rather than quoted: the emptiest surviving model ties the best.
    best = report.sweeps[0].best
    assert best is not None
    assert objective(float(top)) == pytest.approx(best.value)
    assert structure.measure({"threshold": float(top)}) == 1.0


@needs_permits
@needs_fleet
def test_the_loops_preflight_refuses_the_transcript_log_and_clears_the_permit_log() -> None:
    """The wiring, end to end, through the function `train` actually calls.

    `train` now passes its log so the decision half runs, and this is the assertion that the
    fix reaches the loop rather than only the prober. A pre-flight that caught the defect in
    a test and not at the call site would be the same defect one more level up."""
    from pneuma.casestudy import transcriptlog
    from pneuma.casestudy.minelearn import probe_objective

    permits = probe_objective(eventlog.parse_xes(PERMITS))
    assert permits.ok, permits.report()
    permits.raise_if_pathological("the miner's balanced score")

    fleet_events, _ = transcriptlog.load_sample(FLEET)
    fleet = probe_objective(fleet_events)
    assert not fleet.ok, fleet.report()
    with pytest.raises(ObjectiveRefused, match="emptying-is-free"):
        fleet.raise_if_pathological("the miner's balanced score")
