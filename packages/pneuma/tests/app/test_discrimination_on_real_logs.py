"""Does the discrimination primitive name the *cause* on processes mined from real logs?

The synthetic tests in `tests/library/test_discrimination.py` establish the primitive's
contract: a three-valued verdict, and a term whose value never moves reported as
undiscriminating rather than as a pass. They cannot establish that the mechanism finds a real
idle term in a real pipeline, because a hand-built flat term is one somebody wrote flat.

These do, through `casestudy.minelearn.threshold_objective` and the real `grade` path. The
separation is the point: on the transcript log whole-trace replay coverage reads 0.0227 at
every swept threshold while selectivity moves, so coverage is named as the cause; on the
permit log both terms move, so the idleness is a property of that dataset rather than of the
score. A check reporting idleness on both would be measuring the formula and calling it the
data.
"""

from __future__ import annotations

from paths import FLEET, PERMITS, needs_fleet, needs_permits

from pneuma.detect import Domain, Space, probe
from pneuma.detect.objective import Severity


def checks(report: object, severity: Severity | None = None) -> set[str]:
    findings = report.findings  # type: ignore[attr-defined]
    return {f.check for f in findings if severity is None or f.severity is severity}


@needs_fleet
def test_the_transcript_logs_coverage_term_is_named_as_the_cause() -> None:
    """The finding this whole task exists for, measured on the real `grade` path.

    The prober already refused this log, with `emptying-is-free` and two enumerated
    `degenerate-optimum` findings. Every one of those is the *symptom*: a degenerate input
    wins. None of them says that one term of the metric has no discriminating power on this
    dataset, which is the cause, and a caller who fixes the winning point without fixing the
    measurement has fixed nothing.

    Measured through `threshold_objective`, so the terms read are the ones the composed
    objective actually divided rather than a re-derivation that could drift: whole-trace
    replay coverage is 0.0227 at every swept threshold while selectivity separates all but
    one of them. The 21-point grid over 1 to 44 is the prober's own sampling and the report
    says so; the full 44-point curve was swept independently and is flat throughout."""
    from pneuma.casestudy import transcriptlog
    from pneuma.casestudy.minelearn import threshold_objective

    events, _ = transcriptlog.load_sample(FLEET)
    objective, structure, top, components = threshold_objective(events)
    assert [c.name for c in components] == ["replay coverage", "selectivity (1 - edge share)"]

    # The cause, on the whole feasible range rather than only the prober's grid.
    coverage, selectivity = components
    walk = [coverage.measure({"threshold": float(t)}) for t in range(1, top + 1)]
    assert None not in walk
    assert len(set(walk)) == 1, f"coverage moves on this log after all: {sorted(set(walk))}"
    moved = [selectivity.measure({"threshold": float(t)}) for t in range(1, top + 1)]
    assert len(set(moved)) > 1, "and the other term does move, so the log is not degenerate"

    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
        components=components,
    )

    assert [d.subject for d in report.idle_components] == ["replay coverage"]
    assert "component-does-not-discriminate" in checks(report, Severity.WARN)
    assert "replay coverage" in report.report()

    # The symptom is still reported, and the cause does not replace it.
    assert not report.ok
    assert {"emptying-is-free", "degenerate-optimum"} <= checks(report, Severity.REFUSE)


@needs_permits
def test_the_permit_logs_terms_both_discriminate_so_the_separation_is_measured() -> None:
    """The baseline the fleet number is honest against, and the comparison that makes it a
    finding rather than a curiosity.

    Same arithmetic, same code path, different log: on the permit log both terms move, so the
    idleness on the transcript log is a property of that dataset rather than of the score. A
    check that reported idleness on both would be measuring the formula and calling it the
    data."""
    from pneuma.casestudy import eventlog
    from pneuma.casestudy.minelearn import threshold_objective

    events = eventlog.parse_xes(PERMITS)
    objective, structure, top, components = threshold_objective(events)

    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
        components=components,
    )

    assert report.ok, report.report()
    assert report.idle_components == ()
    assert all(d.discriminates is True for d in report.discrimination), report.report()


@needs_fleet
@needs_permits
def test_the_loops_preflight_carries_the_named_cause_to_the_call_site() -> None:
    """The wiring, end to end, through the function `train` actually calls.

    A cause named in a test and not at the call site would be the same silent-harness defect
    one level up, which is the mistake `probe_objective` was already corrected for once."""
    from pneuma.casestudy import eventlog, transcriptlog
    from pneuma.casestudy.minelearn import probe_objective

    permits = probe_objective(eventlog.parse_xes(PERMITS))
    assert permits.ok, permits.report()
    assert permits.idle_components == ()

    events, _ = transcriptlog.load_sample(FLEET)
    fleet = probe_objective(events)
    assert not fleet.ok
    assert [d.subject for d in fleet.idle_components] == ["replay coverage"]


@needs_fleet
def test_two_different_quantities_are_both_called_coverage_and_only_one_is_flat() -> None:
    """A correction to the claim that drove this task, kept because the confusion is live.

    The briefing said coverage is 0.0227 at every threshold from 1 to 44 on this fixture.
    `test_fixture_two.py` asserts coverage moves 0.5795 down to 0.1023 over the same range
    and would fail if it were flat. Both are true, of *different functions*: `grade` measures
    whole-trace conformance of the mined process and is the one `Attempt.score` divides, while
    `miner.mine(...).coverage` is the miner's own and is monotone. Asserting the split here
    keeps the next reader from concluding one of the two is wrong."""
    import polars as pl

    from pneuma.casestudy import miner, transcriptlog
    from pneuma.casestudy.minelearn import threshold_objective

    events, _ = transcriptlog.load_sample(FLEET)
    _objective, _structure, top, components = threshold_objective(events)
    graded = {components[0].measure({"threshold": float(t)}) for t in range(1, top + 1)}
    assert graded == {0.0227}, "the term the score divides is flat"

    pairs = miner.directly_follows(events)
    handoffs = pairs.filter(pl.col("activity") != pl.col("next_activity"))
    mined = [
        miner.mine(events, name=f"T{t}", min_edge_cases=t).coverage
        for t in (1, int(handoffs["cases"].max()))
    ]
    assert mined[0] - mined[1] > 0.2, "the miner's own coverage is not, on the same log"
