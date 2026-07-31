"""Tests for the pre-flight objective prober.

The prober's whole claim is that it separates a sound objective from the ones this
project actually shipped, so the ground truth is not invented: the historical objectives
are reconstructed here verbatim from `git show ac57246` and from the coverage-only stage
described in `docs/case-study.md` section 10, and the current one is imported live.

The three anchors, all reproduced by running rather than quoted:

- the fixed objective PASSES, once the bounds it relies on are declared as established
- the coverage-only objective FAILS on a degenerate optimum at threshold 1
- the unbounded harmonic mean FAILS on a pole outside its declared domain

A prober that passed all three, or failed all three, would be useless in the same way a
check that cannot fire is useless, which is the defect class this module exists to catch.

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

The adversarial half lives in `test_adversary.py`. Its offline tests prove the wiring; the
live ones need Bedrock and skip without it.
"""

from __future__ import annotations

import io
import math

import polars as pl
import pytest
from paths import FLEET, PERMITS, needs_fleet, needs_permits

from pneuma.casestudy import eventlog
from pneuma.casestudy.aimine import Discovered, Edge, grade, to_csv
from pneuma.casestudy.minelearn import Attempt, feedback_for, score_edges, visible_handoffs
from pneuma.casestudy.miner import start_and_end_activities
from pneuma.detect.objective import (
    SPIKE_RATIO,
    Brief,
    Degenerate,
    Domain,
    ObjectiveRefused,
    Severity,
    Space,
    Structure,
    probe,
    probe_feedback,
)

# ── The three objectives under test ──


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


def historical_unbounded_harmonic(coverage: float, edge_share: float) -> float:
    """Attempt two's scoring, verbatim from `git show ac57246:.../minelearn.py`.

    No clamp on `edge_share`, and a guard on the denominator tested with exact float
    equality so it never fired anywhere near the pole.
    """
    selectivity = 1.0 - edge_share
    if coverage + selectivity == 0:
        return 0.0
    return round(2 * coverage * selectivity / (coverage + selectivity), 4)


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


# ── Regression fixture: attempt one, the degenerate optimum ──


def test_the_coverage_only_objective_fails_on_its_degenerate_optimum() -> None:
    """Failure one. Coverage was maximised by keeping every handoff including the thirty
    walked by one case out of 1434, and it reported 98.6% while describing no process. The
    prober must name the degenerate input that won, not merely that something did."""
    report = probe(
        historical_coverage_only, METRIC_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )

    assert not report.ok
    degenerate = [f for f in report.refusals if f.check == "degenerate-optimum"]
    assert degenerate, report.report()
    assert "keep every handoff" in degenerate[0].detail


def test_the_current_objective_is_not_maximised_by_keeping_everything() -> None:
    """The contrast that makes the previous test mean something: same probe, same
    degenerate inputs, and the fixed objective scores memorisation at zero."""
    assert current_objective(1.0, 1.0) == 0.0
    report = probe(
        current_objective, BOUNDED_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    assert "degenerate-optimum" not in checks(report)


def test_not_declaring_degenerate_inputs_is_reported_rather_than_passing_quietly() -> None:
    """The check that would otherwise not fire. A caller who declares no degenerate input
    gets a note saying the check did not run, because "PASS" from a prober that skipped
    the check silently is the exact defect this module was written against."""
    report = probe(historical_coverage_only, METRIC_AXES, space=Space.METRIC)

    assert "degenerate-optimum" not in checks(report)
    assert any("degenerate-winner not checked" in note for note in report.notes)


# ── Regression fixture: attempt two, the pole ──


def test_the_unbounded_harmonic_mean_fails_on_its_pole() -> None:
    """Failure five, the one that shipped. `edge_share` above 1 makes selectivity negative
    and the harmonic mean a rational function with a pole at `edge_share == 1 + coverage`.

    Reproduced first, then detected: at coverage 0.864 the historical score is 1.34e+16 on
    one side of 1.864 and -149297.472 a hundred-thousandth below it."""
    assert historical_unbounded_harmonic(0.864, 1.864) > 1e15
    assert historical_unbounded_harmonic(0.864, 1.86399) == pytest.approx(-149297.472)

    report = probe(
        historical_unbounded_harmonic,
        METRIC_AXES,
        space=Space.METRIC,
        degenerate=DEGENERATE,
    )

    assert not report.ok
    assert "escape-pole" in checks(report, Severity.REFUSE)
    assert "escape-rewarded" in checks(report, Severity.REFUSE)


def test_the_pole_is_caught_even_when_the_share_bound_is_claimed_to_be_established() -> None:
    """The hard case stated in the brief. A prober that only looked inside the declared
    range would pass this objective, because on `[0, 1]` it is a well-behaved F-score.

    Declaring the share bound as established downgrades that axis to a warning, so the
    refusal has to come from somewhere else, and it does: the historical objective is also
    unbounded below zero on coverage, which nothing claims to prevent."""
    axes = (
        Domain("coverage", 0.0, 1.0),
        Domain("edge_share", 0.0, 1.0, bounded_by="an intersection that did not exist yet"),
    )
    report = probe(
        historical_unbounded_harmonic, axes, space=Space.METRIC, degenerate=DEGENERATE
    )

    assert not report.ok, report.report()
    assert [f for f in report.findings if f.point and f.point["edge_share"] > 1.0]


def test_declaring_every_bound_lets_the_broken_objective_pass_and_the_report_says_so() -> None:
    """This module's own weakest point, measured rather than assumed.

    Declare `bounded_by` on every axis and the historical broken objective passes: all
    seven of its pathologies live outside the declared box, so all seven downgrade. That is
    the intended semantics, because the prober cannot verify a claim about another file. It
    is also a way to defeat the refusal by writing a false claim, so the report says which
    findings were downgraded and how to see them undowngraded, and the escape hatch is one
    keyword away."""
    trusting = (
        Domain("coverage", 0.0, 1.0, bounded_by="claimed"),
        Domain("edge_share", 0.0, 1.0, bounded_by="claimed"),
    )
    trusted = probe(
        historical_unbounded_harmonic, trusting, space=Space.METRIC, degenerate=DEGENERATE
    )

    assert trusted.ok, "the limitation this test documents has changed"
    assert len(trusted.warnings) >= 5
    assert "trust_declared_bounds=False" in trusted.report()

    paranoid = probe(
        historical_unbounded_harmonic,
        trusting,
        space=Space.METRIC,
        degenerate=DEGENERATE,
        trust_declared_bounds=False,
    )

    assert not paranoid.ok, paranoid.report()
    assert "escape-pole" in checks(paranoid, Severity.REFUSE)


def test_the_pole_is_distinguished_from_an_honest_zero_crossing() -> None:
    """The detector cannot just watch for a sign change, or it would fire on any objective
    that passes through zero. A straight line crosses zero and must not be called a pole;
    a reciprocal blows up and must be."""
    line = probe(
        lambda x: x - 0.5, (Domain("x", 0.0, 1.0),), space=Space.METRIC
    )
    assert "pole" not in checks(line)

    reciprocal = probe(
        lambda x: 1.0 / (x - 0.5) if x != 0.5 else math.inf,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
    )
    assert {"pole", "non-finite-value"} & checks(reciprocal, Severity.REFUSE), (
        reciprocal.report()
    )


# ── The other mechanical checks ──


def test_an_objective_that_raises_on_a_feasible_input_is_refused() -> None:
    """Failure three's shape, generalised. Tightening produced a graph with no terminal
    state and the compile step raised instead of degrading, so the round was lost rather
    than scored. An objective that can raise on a declared-feasible input has an input the
    loop cannot learn from."""

    def brittle(x: float) -> float:
        if x > 0.8:
            raise ValueError("no terminal state: the process could never complete")
        return x

    report = probe(brittle, (Domain("x", 0.0, 1.0),), space=Space.METRIC)

    assert not report.ok
    assert "raises-inside-the-domain" in checks(report, Severity.REFUSE)
    assert "no terminal state" in report.report()


def test_a_nan_inside_the_domain_is_refused_because_max_over_nan_is_order_dependent() -> None:
    report = probe(
        lambda x: math.nan if x > 0.5 else x, (Domain("x", 0.0, 1.0),), space=Space.METRIC
    )

    assert not report.ok
    assert "non-finite-value" in checks(report, Severity.REFUSE)


def test_a_singularity_that_never_changes_sign_is_still_caught() -> None:
    """An even-order pole is invisible to a sign test: `1 / abs(x - c)` is positive on both
    sides. A detector that only watched for sign flips would have a hole in exactly the
    place poles like to hide, so magnitude against grid neighbours is checked too."""
    report = probe(
        lambda x: 1.0 / abs(x - 0.5) if x != 0.5 else 1e308,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
    )

    assert not report.ok
    assert "pole" in checks(report, Severity.REFUSE), report.report()


def test_a_singularity_on_the_edge_of_the_domain_is_caught() -> None:
    """`-log(x)` is unbounded as x approaches 0 and its supremum sits on the domain's own
    lower edge. An earlier version of the spike scan skipped the endpoints and passed this,
    which is the worst place to have a hole: a denominator vanishing at zero is the common
    case, and zero is where a declared domain tends to start."""
    report = probe(
        lambda x: -math.log(x) if x > 0 else 1e300,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
    )

    assert not report.ok
    assert "pole" in checks(report, Severity.REFUSE), report.report()


def test_a_smooth_piecewise_objective_is_not_a_pole_however_its_zero_is_written() -> None:
    """A false positive that was live on this file, and the fix, in one place.

    `max(0, min(1, a - 0.5)) + 0.25*b - 0.25*c` is piecewise-linear, bounded in [0, 1], and
    has no division anywhere. It was refused, twice over, for two float reasons that are the
    same reason. Inside the declared box one grid point pairs an exact `0.0` neighbour with an
    ordinary `-0.0125`, three orders up. Outside it, `0.25*b - 0.25*c` returns
    `5.551115123125783e-17` where it is algebraically zero and an ordinary neighbouring `0.05`
    reads as fifteen orders. Neither is a singularity; both are ways of writing zero.

    One fix, scale-free and derived from the sweep's own values rather than a constant: the
    spiking point must also carry the sweep's largest finite magnitude, because a singularity
    dominates the space it sits in. Flooring the ratio's denominator relative to the peak was
    tried alongside it and removed, because mutation testing showed it never changes an
    outcome; see `SPIKE_PEAK_DOMINANCE`.

    The three `escape-rewarded` findings that remain are correct and are the reason this
    objective is not asserted `ok`: it really does score 1.25 outside its declared box against
    0.75 inside, which is precisely what the escape check exists to say."""

    def piecewise(a: float, b: float, c: float) -> float:
        return max(0.0, min(1.0, a - 0.5)) + 0.25 * b - 0.25 * c

    axes = (Domain("a", 0.0, 1.0), Domain("b", 0.0, 1.0), Domain("c", 0.0, 1.0))
    report = probe(piecewise, axes, space=Space.METRIC)

    assert "pole" not in checks(report), report.report()
    assert "escape-pole" not in checks(report), report.report()
    assert checks(report, Severity.REFUSE) == {"escape-rewarded"}, report.report()

    # The residue that used to be read as a denominator, at a grid point the escape sweep
    # really visits, so this test fails rather than silently stops being a regression if a
    # platform ever computes it exactly.
    residue = piecewise(0.55, -0.19999999999999996, 0.0)
    assert 0.0 < abs(residue) < 1e-15, (
        f"the float residue this regression is about is now {residue!r}"
    )
    assert abs(piecewise(0.55, 0.0, 0.0) / residue) > SPIKE_RATIO, (
        "and it is still large enough against an ordinary neighbour to have tripped the "
        "unfloored ratio test"
    )


def test_the_spike_fix_does_not_go_blind_on_a_small_scale_objective() -> None:
    """The fix must not buy its way out of the false positive by going blind where the whole
    objective is small.

    An objective whose ordinary values sit near 1e-6 and which has a genuine even-order pole
    is still refused. Both surviving conditions are scale-free, so this holds without the
    prober being told anything about the caller's units."""
    report = probe(
        lambda x: 1e-6 / abs(x - 0.5) if x != 0.5 else 1e-6 / 1e-30,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
    )

    assert not report.ok
    assert "pole" in checks(report, Severity.REFUSE), report.report()


def test_the_unbounded_check_fires_on_a_supremum_the_grid_never_attains() -> None:
    """Refinement-based rather than threshold-based: the peak rising by a factor every time
    the grid halves is what unboundedness looks like without the prober having to be told
    what "too big" is on the caller's scale."""
    report = probe(
        historical_unbounded_harmonic,
        (Domain("coverage", -1.0, 0.0), Domain("edge_share", 0.0, 1.0)),
        space=Space.METRIC,
    )

    assert not report.ok
    assert {"unbounded", "pole"} & checks(report, Severity.REFUSE), report.report()


def test_an_out_of_domain_input_that_degrades_is_not_a_finding() -> None:
    """The semantics stated in the module docstring: escaping is not forbidden, being
    *rewarded* for escaping is. An objective that decays outside its range is safe."""
    report = probe(
        lambda x: max(0.0, 1.0 - abs(x - 0.5)), (Domain("x", 0.0, 1.0),), space=Space.METRIC
    )

    assert report.ok, report.report()
    assert "escape-rewarded" not in checks(report)


def test_an_out_of_domain_input_that_raises_warns_rather_than_refusing() -> None:
    """Raising outside the range is not a reward, so it cannot be climbed. It is still
    worth saying, because the caller sees a crash where it expected a bad score."""

    def strict(x: float) -> float:
        if not 0.0 <= x <= 1.0:
            raise ValueError("out of range")
        return 1.0 - abs(x - 0.5)

    report = probe(strict, (Domain("x", 0.0, 1.0),), space=Space.METRIC)

    assert report.ok, report.report()
    assert "escape-raises" in checks(report, Severity.WARN)


def test_the_report_states_the_resolution_it_swept_at() -> None:
    """No silent caps. A prober that under-samples and says "looks fine" is this session's
    defect one level up, so the grid density is in the report text."""
    report = probe(
        lambda x: 1.0 - abs(x - 0.5), (Domain("x", 0.0, 1.0),), space=Space.METRIC, resolution=7
    )

    assert "xx7" in report.report()
    assert report.evaluations >= 7


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


def test_a_window_that_excludes_the_optimum_is_refused_in_decision_space() -> None:
    """Failure four, mechanically. The agent swept thresholds 1 to 24, optimised correctly
    inside that window, and settled short of a peak that sat outside it.

    A synthetic stand-in with the same shape: monotone rising to a peak at 40. Swept over
    1 to 24 the maximum lands on the window's own upper edge, and continuing past it
    improves, which is the refusal. Swept over the full feasible range the peak is interior
    and the same probe passes."""

    def peaked(threshold: float) -> float:
        return 1.0 - abs(threshold - 40.0) / 100.0

    narrow = probe(
        peaked,
        (Domain("threshold", 1, 24, integral=True, feasible=(1.0, 120.0)),),
        space=Space.DECISION,
    )
    assert not narrow.ok, narrow.report()
    assert "window-too-narrow" in checks(narrow, Severity.REFUSE)

    wide = probe(
        peaked,
        (Domain("threshold", 1, 120, integral=True, feasible=(1.0, 120.0)),),
        space=Space.DECISION,
    )
    assert wide.ok, wide.report()


def test_a_boundary_optimum_that_really_is_optimal_warns_instead_of_refusing() -> None:
    """A maximum on an edge is not automatically a defect: the true optimum can sit at a
    feasible extreme. The prober does what the agent did not and looks past the edge, so
    the two cases are separated by measurement rather than by assumption."""
    report = probe(
        lambda threshold: float(threshold),
        (Domain("threshold", 1, 24, integral=True, feasible=(1.0, 24.0)),),
        space=Space.DECISION,
    )

    assert report.ok, report.report()
    assert "boundary-optimum-at-feasible-limit" in checks(report, Severity.WARN)


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


def test_a_pathological_objective_stops_the_loop_before_it_starts() -> None:
    """The point of the whole module: refusing rather than reporting rounds. Run the same
    pre-flight against the historical coverage-only objective and it raises."""
    report = probe(
        historical_coverage_only, METRIC_AXES, space=Space.METRIC, degenerate=DEGENERATE
    )
    with pytest.raises(ObjectiveRefused, match="no training loop will be started"):
        report.raise_if_pathological("the miner's balanced score")


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


# ── Mechanical enumeration: the declared list was the defect one level up ──


def test_a_declared_list_of_degenerate_inputs_is_no_longer_what_the_prober_relies_on() -> None:
    """The contract change, stated as a property rather than as prose.

    Before, "no degenerate declared" meant no degenerate check at all: the prober emitted a
    note and moved on, so a call site that forgot the important one got a PASS. Now a
    `Structure` is enough, and the emptiest answer is derived from the space instead of
    remembered by the caller.

    The stand-in is the shape of the transcript-log defect with nothing else in it: an
    objective that only rewards emptiness. Declaring nothing and giving a structure refuses;
    the same probe with no structure and no declaration passes, which is exactly what the old
    contract allowed."""

    def only_selectivity(kept: float) -> float:
        return 1.0 - kept / 100.0

    axis = (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),)
    structure = Structure(size=lambda kept: kept, units="edges kept")

    refused = probe(only_selectivity, axis, space=Space.DECISION, structure=structure)
    assert not refused.ok, refused.report()
    assert {"degenerate-optimum", "emptying-is-free"} <= checks(refused, Severity.REFUSE)
    assert "[enumerated]" in refused.report()

    # The old contract, for contrast: no structure, no declaration, and the prober passes.
    old = probe(only_selectivity, axis, space=Space.DECISION)
    assert old.ok, old.report()
    assert any("no `structure` was declared" in note for note in old.notes)


def test_the_emptiest_enumerated_point_is_the_emptiest_one_that_is_still_an_answer() -> None:
    """Why `Structure.viable` is separate from `size > 0`, measured.

    An objective where the empty answer scores zero and the smallest non-empty one wins is
    the transcript log's shape. If the enumeration picked "the point with the smallest size"
    it would pick the empty one, which scores zero, lose the comparison, and pass — the check
    would fire on the wrong point and report a clean bill."""

    def keeps(kept: float) -> float:
        step = int(round(kept))
        return 0.0 if step < 1 else 1.0 - step / 200.0

    structure = Structure(
        size=lambda kept: max(0.0, float(round(kept))),
        viable=lambda kept: round(kept) >= 1,
        units="edges kept",
    )
    report = probe(
        keeps,
        (Domain("kept", 0, 100, integral=True, feasible=(0.0, 100.0)),),
        space=Space.DECISION,
        structure=structure,
    )

    assert not report.ok, report.report()
    emptiest = [
        f
        for f in report.refusals
        if f.check == "degenerate-optimum" and "emptiest answer" in f.detail
    ]
    assert emptiest, report.report()
    assert emptiest[0].point is not None
    assert emptiest[0].point["kept"] >= 1, "the empty point is not the emptiest *answer*"


def test_emptying_is_free_is_the_general_form_and_fires_where_the_point_test_cannot() -> None:
    """The stronger of the two derived checks, and an honest account of *how* it is stronger.

    Correcting a claim made while writing this: on the swept grid the two checks mostly
    coincide, and they have to. If emptying never costs score then the score is non-decreasing
    toward emptier, so the emptiest viable grid point holds the grid maximum and the point
    test fires too. Any test claiming to show the point test blind to a plain monotone case
    would be wrong about the arithmetic.

    Where they genuinely part is when the grid maximum is held by a point that is *not a
    viable answer at all*. Then no viable enumerated point can tie it, the point test on
    "emptiest answer" cannot fire, and only the emptying walk sees that shrinking a real
    answer is free. That is the shape of an objective that rewards returning nothing, which is
    the "keep nothing" failure with the reward on the wrong side, and it is not exotic.

    The other half of the general form's value is not about which fires but about what it
    says: it reports the mechanism — the score is monotone in emptiness across every pair
    walked — rather than one coordinate that happens to win. A caller who fixes the specific
    winning point without fixing the monotonicity has fixed nothing, and the point test alone
    would then go quiet."""

    def rewards_nothing(kept: float) -> float:
        step = int(round(kept))
        if step < 4:
            return 1.0  # returning nothing scores best, which is the defect
        return round(1.0 - step / 200.0, 4)

    structure = Structure(
        size=lambda kept: float(round(kept)),
        viable=lambda kept: round(kept) >= 4,
        units="edges kept",
    )
    report = probe(
        rewards_nothing,
        (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        structure=structure,
    )

    assert not report.ok, report.report()
    assert "emptying-is-free" in checks(report, Severity.REFUSE)
    assert not [
        f
        for f in report.refusals
        if f.check == "degenerate-optimum" and "emptiest answer" in f.detail
    ], "no viable point can tie a ceiling held by a non-viable one, so the point test is quiet"

    # The mechanism the general form reports, verified directly rather than taken from the
    # finding's own prose: every step toward emptier among real answers is free.
    scores = [rewards_nothing(float(k)) for k in range(4, 101)]
    assert all(a >= b for a, b in zip(scores, scores[1:], strict=False))


def test_emptying_is_free_does_not_fire_when_emptying_costs_score() -> None:
    """The control that makes the previous test mean something. An objective with a genuine
    interior peak pays for emptying past it, and the report says how many pairs it walked
    rather than only that it passed."""

    def peaked(kept: float) -> float:
        return 1.0 - abs(kept - 40.0) / 100.0

    report = probe(
        peaked,
        (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        structure=Structure(size=lambda kept: float(round(kept)), units="edges kept"),
    )

    assert report.ok, report.report()
    assert any("emptying-is-free passed" in note for note in report.notes)
    assert any("cost score" in note for note in report.notes)


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


def test_the_report_says_when_an_integral_axis_was_under_sampled() -> None:
    """A finding a live adversary produced that no enumeration would have. It swept the
    permit objective's integers directly, found 0.8274 at threshold 19, and observed the
    probe's stated ceiling was 0.8184 at 17 — because a 21-point grid over 1 to 323 lands on
    1, 17, 33 and skips 19.

    That makes the ceiling a lower bound, and every escape and degenerate comparison is made
    against it, so under-sampling makes the whole probe *lenient*. A note rather than a
    refusal: refusing over the caller's chosen resolution would substitute the prober's
    opinion about sampling for theirs, and there is no honest threshold for it."""
    coarse = probe(
        lambda t: float(t),
        (Domain("t", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        resolution=11,
    )
    assert any("under-sampled" in note for note in coarse.notes), coarse.report()
    assert "lower bound" in coarse.report()

    exhaustive = probe(
        lambda t: float(t),
        (Domain("t", 1, 11, integral=True, feasible=(1.0, 11.0)),),
        space=Space.DECISION,
        resolution=11,
    )
    assert not any("under-sampled" in note for note in exhaustive.notes)


def test_the_provenance_of_every_candidate_is_in_the_report() -> None:
    """A declared point, an enumerated one, and a searched one are different strengths of
    evidence, and a reviewer has to be able to tell which fired. So `found_by` is a field
    rather than something the report re-derives from its own prose.

    The searcher's candidate is deliberately one the enumeration cannot reach: an off-grid
    fractional value on an axis declared integral, which the objective rounds. That is a real
    class of finding a structure will never enumerate, and it is what the search is for."""

    def only_selectivity(kept: float) -> float:
        return 1.0 - round(kept) / 100.0

    def pretend_search(brief: object) -> list[Degenerate]:
        return [
            Degenerate(
                label="an off-grid fractional value the integral axis does not admit",
                point={"kept": 1.4},
                found_by="adversary/emptiness",
                worthless_because="rounds onto the emptiest model while violating integrality",
            )
        ]

    report = probe(
        only_selectivity,
        (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        structure=Structure(size=lambda kept: round(kept), units="edges kept"),
        degenerate=(Degenerate("a caller's own guess", {"kept": 1.0}),),
        search=pretend_search,
    )

    text = report.report()
    assert "[declared]" in text
    assert "[enumerated]" in text
    assert "[adversary/emptiness]" in text
    assert "adversarial search proposed 1 candidate" in text


def test_a_searchers_fabricated_candidate_produces_nothing() -> None:
    """The arithmetic half of adjudication, and why an LLM adversary cannot poison the
    prober. A searcher claiming a triumphant input is re-scored here; if it loses, no finding
    is recorded, whatever it argued. Nothing about that depends on trusting the searcher."""

    def peaked(kept: float) -> float:
        return 1.0 - abs(kept - 40.0) / 100.0

    def lying_search(brief: object) -> list[Degenerate]:
        return [
            Degenerate(
                label="this definitely wins, trust me",
                point={"kept": 100.0},
                found_by="adversary/hallucination",
                worthless_because="I am very confident about this",
            )
        ]

    report = probe(
        peaked,
        (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        structure=Structure(size=lambda kept: float(round(kept)), units="edges kept"),
        search=lying_search,
    )

    assert report.ok, report.report()
    assert "degenerate-optimum" not in checks(report)
    assert peaked(100.0) < peaked(40.0), "and the arithmetic is why"


def test_the_search_sees_the_objective_rather_than_a_description_of_it() -> None:
    """What makes a searcher a search. The `Brief` carries the callable, so a searcher probes
    and revises rather than reasoning about what the arithmetic probably does. This asserts
    the seam mechanically, with a "searcher" that is a bisection rather than a model."""
    seen: list[Brief] = []

    def bisecting_search(brief: Brief) -> list[Degenerate]:
        seen.append(brief)
        # A real search: walk the axis through the brief's own scorer and keep what ties.
        winners = [
            value
            for value in range(1, 101)
            if brief.score({"kept": float(value)}).value == brief.ceiling
        ]
        return [
            Degenerate(
                label=f"found by probing: kept={min(winners)}",
                point={"kept": float(min(winners))},
                found_by="bisection",
                worthless_because="keeps the fewest edges of everything that ties the best",
            )
        ]

    report = probe(
        lambda kept: 1.0 if kept <= 3 else 0.5,
        (Domain("kept", 1, 100, integral=True, feasible=(1.0, 100.0)),),
        space=Space.DECISION,
        structure=Structure(size=lambda kept: float(round(kept)), units="edges kept"),
        search=bisecting_search,
        source="def score(kept): return 1.0 if kept <= 3 else 0.5",
    )

    assert seen, "the search was called"
    assert seen[0].source is not None, "and it was handed the source"
    assert seen[0].structure is not None
    assert seen[0].ceiling == 1.0
    assert not report.ok, report.report()
    assert "found by probing: kept=1" in report.report()


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


# ── The historical failures, re-checked with nothing declared ──


def test_the_coverage_only_objective_is_refused_without_a_declared_degenerate() -> None:
    """Failure one again, and the point is what is *absent*: no `Degenerate`.

    The existing test for this failure hands the prober a list containing "keep every
    handoff", so it demonstrates the arithmetic and not the discovery. Here the same failure
    is posed in decision space with only a structure, and the prober finds the degenerate
    input itself. Coverage falls as the threshold rises, so the optimum is threshold 1: keep
    everything, replay the log, describe no process."""

    def coverage_only(threshold: float) -> float:
        return max(0.0, 1.0 - (threshold - 1) / 50.0)

    report = probe(
        coverage_only,
        (Domain("threshold", 1, 50, integral=True, feasible=(1.0, 50.0)),),
        space=Space.DECISION,
        structure=Structure(
            size=lambda threshold: max(0.0, 50.0 - threshold), units="handoffs kept"
        ),
    )

    assert not report.ok, report.report()
    assert "degenerate-optimum" in checks(report, Severity.REFUSE)
    assert "fullest answer" in report.report(), "it names memorisation as the winner"
