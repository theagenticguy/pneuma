"""Is the merged primitive real, and does the mechanism built on it have teeth?

Two detectors in `pneuma.detect` were asking one question and answering it in two
vocabularies. `vacuity` asks whether a *rule* can ever fire; `objective` asks whether a
*scoring function* rewards garbage. A rule catching zero reachable states cannot tell a
compliant run from a violation, and a term whose value never moves cannot tell a good answer
from a bad one. `detect.discrimination` is the shape they share, and this module is the
evidence that sharing it was justified rather than an abstraction invented to look unified.

Three things are pinned here, and the middle one is the deliverable:

- The primitive's own contract, especially the three-valued verdict. `vacuity` reported a
  truncated relaxation sweep as a confident finding of decoration once, and that bug is why
  `discriminates` is not a boolean. The same trap now exists in one place instead of two.
- A metric *component* with no discriminating power is reported, naming the cause rather than
  only the symptom, demonstrated on the transcript fixture through the real `grade` path.
- The mechanism has teeth, proved the way this session proved the other two: with a
  deliberately useless input as a negative control. Against a flat term the surface API looks
  entirely healthy and only the discrimination measurement reveals it.

## The negative controls, and why each is a *useless* input rather than a broken one

Three, one per direction the mechanism could be theatre in.

`test_a_constant_term_is_the_negative_control` hands the prober a sound objective with one
term pinned to a constant. Every other check passes: the argmax is interior, nothing is
non-finite, there is no pole, the function is bounded, and emptying costs score. The prober
returns `ok=True`. Only the component measurement fires, which is exactly the shape the
finding has on the real transcript fixture.

`test_a_term_that_moves_by_less_than_its_own_floor_is_idle` is the same control with the
useless input made subtle: the term does move, by 1e-7, under a declared floor of 1e-6. A
range test with no floor would call that discriminating and be right about the arithmetic and
wrong about the objective.

`test_the_measurement_can_report_discriminating_at_all` is the control in the other
direction, and it is the one that matters most: a check that *always* fires is as useless as
one that never does. The suite asserts both outcomes on the same mechanism, and the two
fixtures below assert them on real data.

## What is deliberately not unified, measured rather than assumed

`memory.turso_backend.Discrimination` is a third instance of this idea and is *not* expressed
through the primitive. Its verdict is a margin between two distance distributions, not a
count of separating observations, and `test_the_memory_probe_agrees_on_the_verdicts_shape`
asserts what actually generalises: the three-valued verdict, on the same tri-state contract.
Forcing the margin into a numerator would have produced a shared interface that only looked
shared, which is the negative result this module reports rather than hides.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pneuma.detect import Component, Discrimination, Domain, Space, Structure, probe
from pneuma.detect.objective import Severity
from pneuma.process.ir import Guard, Invariant, Process, State, Transition, Variable

ROOT = Path(__file__).resolve().parents[1]
FLEET = ROOT / "data" / "transcripts_fleet.json"
PERMITS = ROOT / "data" / "receipt.xes"

needs_fleet = pytest.mark.skipif(not FLEET.is_file(), reason="needs data/transcripts_fleet.json")
needs_permits = pytest.mark.skipif(not PERMITS.is_file(), reason="needs data/receipt.xes")


def checks(report: object, severity: Severity | None = None) -> set[str]:
    findings = report.findings  # type: ignore[attr-defined]
    return {f.check for f in findings if severity is None or f.severity is severity}


# ── The primitive's contract ──


def test_a_separating_observation_makes_the_verdict_true() -> None:
    report = Discrimination(subject="r", observations=10, separating=1)
    assert report.discriminates is True
    assert report.settled
    assert not report.idle


def test_a_finished_search_that_never_separated_is_the_finding() -> None:
    """False, not None. The search completed and the check was never in a position to fire,
    which is the whole defect this package detects."""
    report = Discrimination(subject="r", observations=10, separating=0)
    assert report.discriminates is False
    assert report.idle
    assert "DOES NOT DISCRIMINATE" in str(report)


def test_a_withheld_reason_makes_the_verdict_unsettled_rather_than_a_finding() -> None:
    """The bug that motivated the three-valued verdict, at the primitive.

    `vacuity` once reported a truncated relaxation sweep as a confident finding of
    decoration. An abandoned search is not evidence about what it never reached, so a bound
    turns the same zero count into None. It is not a pass either: `settled` is False and
    `idle` is False, so nothing downstream can read it as either verdict."""
    report = Discrimination(subject="r", observations=10, separating=0, withheld=("hit limit",))
    assert report.discriminates is None
    assert not report.settled
    assert not report.idle
    assert "UNSETTLED" in str(report) and "hit limit" in str(report)


def test_a_withheld_reason_does_not_withdraw_a_positive_verdict() -> None:
    """One witness is one witness. Truncation bounds what was *not* seen, so it cannot
    unfind something that was, and reporting None here would lose a real finding."""
    report = Discrimination(subject="r", observations=10, separating=3, withheld=("hit limit",))
    assert report.discriminates is True


def test_no_observations_and_no_bound_is_a_finding_not_an_abstention() -> None:
    """The asymmetry the module docstring argues for. An empty observation set with no
    withheld reason means the search finished and the subject was never reachable, which is
    `vacuity`'s `unreachable_scope`: decoration, and a finding. A caller whose set is empty
    because of its own bound has to say so, and then it is unsettled."""
    proved = Discrimination(subject="r", observations=0, separating=0)
    assert proved.discriminates is False
    assert "never in a position to fire" in str(proved)

    bounded = Discrimination(subject="r", observations=0, separating=0, withheld=("gave up",))
    assert bounded.discriminates is None


def test_a_bound_can_be_attached_while_a_sweep_runs() -> None:
    report = Discrimination(subject="r", observations=5, separating=0).because("hit limit")
    assert report.withheld == ("hit limit",)
    assert report.because("and again").withheld == ("hit limit", "and again")


def test_negative_counts_are_refused_rather_than_coerced() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Discrimination(subject="r", observations=-1, separating=0)


# ── The rule side: `vacuity` reports through the primitive and cannot disagree with itself ──


def pinned_process() -> Process:
    """`f` starts at 0 and nothing assigns it, so `NeverBWhenSet` cannot fire.

    The classic bug this package detects: a checker pins the initial value exactly as the
    model does, reports green, and the rule was never in a position to break.
    """
    return Process(
        name="Pinned",
        states=[State(name="A"), State(name="B", terminal=True)],
        initial_state="A",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[Transition(name="AB", source="A", target="B")],
        invariants=[
            Invariant(
                name="NeverBWhenSet",
                forbidden_state="B",
                forbidden_when=[Guard(variable="f", op="eq", value=1)],
            )
        ],
    )


def test_a_vacuous_rule_reports_idle_through_the_shared_primitive() -> None:
    """The rule half of the merge: `vacuous` and `discrimination.idle` are one computation.

    Not two agreeing computations. `vacuous` is derived from `idle`, so the flag, the
    checker's gate, and the shared record cannot drift apart the way two conjunctions
    written in two places do."""
    from pneuma.detect import audit_process

    verdict = audit_process(pinned_process()).verdicts["NeverBWhenSet"]

    assert verdict.vacuous
    assert verdict.discrimination.discriminates is False
    assert verdict.discrimination.idle
    assert verdict.discrimination.separating == verdict.witnesses == 0
    assert verdict.discrimination.kind == "rule"


def test_a_live_rule_reports_discriminating_through_the_same_primitive() -> None:
    """The other direction, on the same mechanism. A primitive that only ever reported
    `idle` would be a check that cannot pass, which is the mirror of the defect."""
    from pneuma.detect import audit_process

    live = Process(
        name="Live",
        states=[State(name="A"), State(name="B", terminal=True)],
        initial_state="A",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[Transition(name="AB", source="A", target="B")],
        invariants=[
            Invariant(
                name="NeverBAtAll",
                forbidden_state="B",
                forbidden_when=[Guard(variable="f", op="eq", value=0)],
            )
        ],
    )
    verdict = audit_process(live).verdicts["NeverBAtAll"]

    assert verdict.live is True
    assert verdict.discrimination.discriminates is True
    assert not verdict.vacuous


def test_a_truncated_sweep_is_unsettled_in_the_primitive_and_names_its_bound() -> None:
    """No silent caps: the limit that stopped the sweep appears in the record it produced.

    This is the same property `test_a_sweep_that_hits_its_limit_reports_unknown_not_safe`
    pins on `live`, asserted at the merged primitive so both detectors inherit it."""
    from pneuma.detect import audit_process

    verdict = audit_process(pinned_process(), limit=1).verdicts["NeverBWhenSet"]

    assert verdict.live is None
    assert verdict.discrimination.discriminates is None
    assert not verdict.vacuous, "an abandoned search is not a finding of decoration"
    assert any("limit=1" in reason for reason in verdict.discrimination.withheld)


def test_a_non_gating_rule_still_reports_its_idleness_honestly() -> None:
    """`gates` stays outside the primitive, and this is the reason. A `TypeOK` that cannot
    fail means the domains are sound rather than untested, so its *pass* stands; its
    *discrimination* is still idle, and hiding that would make the record dishonest to keep
    a policy decision tidy."""
    from pneuma.detect import TYPE_RULE, audit_process

    verdict = audit_process(pinned_process()).verdicts[TYPE_RULE]

    assert not verdict.gates
    assert not verdict.vacuous, "a sound type invariant keeps its pass"
    assert verdict.discrimination.idle, "and still reports that it never fired"


# ── The negative controls: does the component mechanism have teeth? ──


def sound_objective(threshold: float) -> float:
    """A harmonic mean of two terms that both really move with the threshold.

    Interior maximum, bounded in [0, 1], no pole, no escape reward, and emptying costs
    score. The prober passes it, which is what makes it usable as a control: whatever the
    component check says about it is the only thing the component check contributed.
    """
    coverage = max(0.0, 1.0 - (threshold - 1) / 60.0)
    selectivity = min(1.0, (threshold - 1) / 20.0)
    total = coverage + selectivity
    return 0.0 if total <= 0 else 2 * coverage * selectivity / total


SOUND_AXES = (Domain("threshold", 1, 40, integral=True, feasible=(1.0, 40.0)),)
SOUND_STRUCTURE = Structure(
    size=lambda threshold: max(0.0, 41.0 - threshold), units="handoffs kept"
)


def test_the_measurement_can_report_discriminating_at_all() -> None:
    """The control in the direction people forget: a check that always fires is useless too.

    Asserted before the idle cases, because every one of them is only evidence if this one
    holds. Both of the sound objective's terms move, both are reported as discriminating, and
    no finding is raised."""
    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(
            Component("coverage", lambda threshold: max(0.0, 1.0 - (threshold - 1) / 60.0)),
            Component("selectivity", lambda threshold: min(1.0, (threshold - 1) / 20.0)),
        ),
    )

    assert report.ok, report.report()
    assert [d.discriminates for d in report.discrimination] == [True, True]
    assert report.idle_components == ()
    assert "component-does-not-discriminate" not in checks(report)


def test_a_constant_term_is_the_negative_control() -> None:
    """A deliberately useless term, and the surface API looks entirely healthy.

    The pattern this session used three times: a constant embedder for retrieval, a pinned
    variable for vacuity, a flat term here. Every other check passes and the prober returns
    `ok=True`, so nothing about the report's verdict reveals the defect. Only the
    discrimination measurement does, which is the whole claim.

    A warning rather than a refusal, deliberately. An idle term is a fact about the *dataset*
    as much as about the objective — the permit log's coverage term discriminates and the
    transcript log's does not, with the same arithmetic — and refusing would make the prober
    reject a sound formula for the log it was handed. The refusal comes from what the idleness
    causes, which on the transcript fixture is `emptying-is-free`."""
    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(
            Component("selectivity", lambda threshold: min(1.0, (threshold - 1) / 20.0)),
            Component("a term that never moves", lambda threshold: 0.0227),
        ),
    )

    assert report.ok, "the surface verdict is unchanged, which is the point"
    assert "component-does-not-discriminate" in checks(report, Severity.WARN)

    idle = report.idle_components
    assert [d.subject for d in idle] == ["a term that never moves"]
    assert idle[0].separating == 0 and idle[0].observations > 1
    assert "0.0227" in report.report(), "and the report names the constant it read"


def test_a_term_that_moves_by_less_than_its_own_floor_is_idle() -> None:
    """The same control with the useless input made subtle rather than obvious.

    A bare range test would call a term moving by 1e-7 discriminating, be right about the
    arithmetic, and wrong about the objective. The floor is the caller's declaration of what
    counts as movement on the caller's own scale, and it is absolute for that reason: a
    relative floor would be the prober substituting its own opinion for a declared one."""
    twitchy = Component(
        "a term that twitches", lambda threshold: 0.5 + threshold * 1e-9, floor=1e-6
    )
    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(twitchy,),
    )

    assert [d.subject for d in report.idle_components] == ["a term that twitches"]

    # And the same term with no floor declared is *not* idle, so the floor is what decided it
    # rather than the measurement being unable to see 1e-9 at all.
    unfloored = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(Component(twitchy.name, twitchy.term),),
    )
    assert unfloored.idle_components == ()


def test_a_slow_ramp_discriminates_even_though_every_adjacent_step_is_under_the_floor() -> None:
    """A gap this module's own mutation testing found, and the reason the count is anchored
    on the term's minimum rather than on its neighbour.

    Replacing `v - min(values) > floor` with `abs(v[i] - v[i+1]) > floor` passed every other
    test here, and it is the weaker measurement. A term ramping by 1e-7 per grid step over a
    total range of 2e-6, against a floor of 1e-6, has no adjacent step above the floor and a
    range twice it. The adjacent-pair form calls that dead; it is not, and a term reported dead
    while it is quietly steering selection is this package's defect with the sign flipped.

    Anchoring on the extreme also cannot be fooled the other way, by a term that is flat in
    patches while still moving overall, which is the shape a coupled decision-space term
    usually has."""
    ramp = Component("a slow ramp", lambda threshold: threshold * 1e-7, floor=1e-6)
    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(ramp,),
    )

    (measured,) = report.discrimination
    assert measured.discriminates is True, "a range twice the floor is not no range"
    assert report.idle_components == ()

    # The counterfactual, so this test is about the anchoring and not about the numbers: the
    # same ramp under a floor above its whole range really is idle.
    steeper_floor = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(Component(ramp.name, ramp.term, floor=1e-3),),
    )
    assert [d.subject for d in steeper_floor.idle_components] == ["a slow ramp"]


def test_a_term_that_cannot_be_measured_everywhere_is_unsettled_not_idle() -> None:
    """Three-valued at the objective end too, and the bound is named in the record.

    A term that raises on some swept points was measured over fewer points than the sweep
    visited. Reporting that as idle would be the truncation bug rebuilt on the other
    detector, which is exactly what merging the two is supposed to make impossible."""

    def raises_high(threshold: float) -> float:
        if threshold > 20:
            raise ValueError("no model compiles here")
        return 0.5

    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(Component("a term that cannot always be read", raises_high),),
    )

    (measured,) = report.discrimination
    assert measured.discriminates is None
    assert report.idle_components == (), "unsettled is not the finding"
    assert "component-does-not-discriminate" not in checks(report)
    assert any("did not evaluate finitely" in reason for reason in measured.withheld)


def test_a_non_finite_term_is_unsettled_rather_than_constant() -> None:
    """`nan` is not a value a term holds, and a term of all-nan is unmeasured rather than
    flat. Separated because `max` over nan is order-dependent, so treating nan as a constant
    would report a finding whose truth depended on iteration order."""
    report = probe(
        sound_objective,
        SOUND_AXES,
        space=Space.DECISION,
        structure=SOUND_STRUCTURE,
        components=(Component("a term that is never a number", lambda threshold: math.nan),),
    )

    (measured,) = report.discrimination
    assert measured.discriminates is None
    assert measured.observations == 0


def test_declaring_no_component_is_reported_rather_than_passing_quietly() -> None:
    """The prober says what it did not check, which is the same discipline `Probe.notes`
    already applies to the enumeration and the emptying walk."""
    report = probe(sound_objective, SOUND_AXES, space=Space.DECISION)

    assert report.discrimination == ()
    assert any("no `components` were declared" in note for note in report.notes)


def test_the_component_check_runs_in_metric_space_too_and_that_asymmetry_is_deliberate() -> (
    None
):
    """Unlike the enumeration and the emptying walk, this check is space-agnostic.

    Those two ask about a *direction* in the space, so free metric axes make the move they
    test always available and the check meaningless there. This asks whether a quantity has
    any range at all, which is the same question in both spaces."""
    report = probe(
        lambda x: x,
        (Domain("x", 0.0, 1.0),),
        space=Space.METRIC,
        components=(Component("a flat term", lambda x: 1.0),),
    )

    assert [d.subject for d in report.idle_components] == ["a flat term"]


# ── The deliverable, on the transcript fixture, through the real pipeline ──


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


# ── The negative result: where the unification stops, asserted rather than asserted away ──


def test_the_memory_probe_agrees_on_the_verdicts_shape_and_not_on_the_measurement() -> None:
    """The third instance of this idea, and the honest limit of the merge.

    `memory.turso_backend.Discrimination` asks the same question about retrieval and its
    `discriminates` is three-valued for the same reason. It is deliberately *not* expressed
    through `detect.Discrimination`, because its verdict is a margin between two distance
    distributions rather than a count of separating observations. Forcing a margin into a
    numerator would either lose the margin or make `separating` a number with no meaning.

    What is asserted is what actually generalises: both records answer with the same
    tri-state contract, and both reach None for "not measured" rather than reporting it as a
    pass. That is the shape; the measurement does not transfer, and this test is the record
    of that being a measured decision rather than an omission."""
    from pneuma.memory import Discrimination as RetrievalDiscrimination

    unmeasured = RetrievalDiscrimination(
        relevant=(), controls=(("q", "e", 0.4),), hits=0, recalled=0, self_retrieval_failures=()
    )
    ours = Discrimination(subject="r", observations=0, separating=0, withheld=("not measured",))
    assert unmeasured.discriminates is None
    assert ours.discriminates is None, "the same tri-state contract, reached the same way"

    broken = RetrievalDiscrimination(
        relevant=(("q", "e", "other", 0.4),),
        controls=(("c", "e", 0.1),),
        hits=0,
        recalled=0,
        self_retrieval_failures=("e",),
    )
    assert broken.discriminates is False

    assert RetrievalDiscrimination.discriminates.fget.__annotations__["return"] == "bool | None"
