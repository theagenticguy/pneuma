"""Does the vacuity detector's verdict hold on processes mined from real logs?

`tests/library/test_vacuity.py` establishes the mechanism over hand-built processes small
enough to count by hand. That is necessary and not sufficient: a hand-built process is
one the test author already understood, so it cannot show that the detector's causes and
counts survive a graph nobody designed. These tests run the detector through the
application — `eventlog.parse_xes`, `miner.mine`, `rules.apply_derived_rules` — over
`data/receipt.xes` (1,434 real cases), `data/roadfines.xes`, and the transcript corpora,
and assert the pair that is the point of the whole detector: a bare `tla.check` says
`verified` and the gated one says `vacuous` over the same mined process.

Two properties are only measurable here. First, mining threshold is what moves a rule
between live and vacuous, so the same precedence at `min_edge_cases=25` and 200 gives the
detector two different amounts of protection to report. Second, the sweep's state count
must equal TLC's `distinct states found` on a real mined model — a sweep that visited a
different number of states than the checker would be measuring a different system, and
no synthetic process is large enough for that agreement to mean anything.
"""

from __future__ import annotations

import polars as pl
import pytest
from paths import FINES, PERMITS, SAMPLE, needs_fines, needs_permits, needs_sample

from pneuma import detect
from pneuma.casestudy import eventlog, miner, rules
from pneuma.process import tla
from pneuma.process.ir import Process

CHECK = "T02 Check confirmation of receipt"
DETERMINE = "T04 Determine confirmation of receipt"

needs_tlc = pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")


def transcript_corpus_available() -> bool:
    """Is the live transcript corpus reachable? Imported lazily so this file loads
    even if a sibling's adapter is mid-edit."""
    try:
        from pneuma.casestudy import transcriptlog
    except ImportError:
        return False
    return transcriptlog.available()


@pytest.fixture(scope="module")
def permits() -> pl.DataFrame:
    return eventlog.parse_xes(PERMITS)


@pytest.fixture(scope="module")
def precedence(permits: pl.DataFrame) -> rules.Precedence:
    found = [
        p
        for p in rules.derive_precedences(permits, min_support=100)
        if p.before == CHECK and p.after == DETERMINE
    ]
    assert found, "the log no longer yields the precedence these tests are about"
    return found[0]


def governed_at(permits: pl.DataFrame, threshold: int, p: rules.Precedence) -> Process:
    mined = miner.mine(permits, name=f"Mined{threshold}", min_edge_cases=threshold).process
    return rules.enforce(mined, p, on_vacuous="ignore")


# ── Reproducing the verified ground truth on the real logs ──


@needs_permits
def test_the_same_rule_is_live_at_one_threshold_and_vacuous_at_another(
    permits: pl.DataFrame, precedence: rules.Precedence
) -> None:
    """The audit's headline pair, reproduced by the detector rather than assumed.

    One precedence, two mining thresholds, two entirely different amounts of
    protection. At `min_edge_cases=25` the model keeps an edge reaching T04 without
    passing through T02, so the rule has something to forbid. At 200 that edge is below
    threshold and the condition holds in no reachable state.
    """
    live = detect.verdict_for(governed_at(permits, 25, precedence), precedence.rule_name)
    assert live.live is True
    assert (live.antecedent_states, live.violating_states) == (2, 1)
    assert live.reachable_states == 19
    assert live.trace is not None and live.trace.locations[-1] == miner._identifier(DETERMINE)

    vacuous = detect.verdict_for(governed_at(permits, 200, precedence), precedence.rule_name)
    assert vacuous.live is False
    assert (vacuous.antecedent_states, vacuous.violating_states) == (1, 0)
    assert vacuous.reachable_states == 8
    assert vacuous.vacuous
    # Not a pinned variable and not a guard: the mined graph alone forbids the case, so
    # no relaxation the formalism allows makes this rule fire.
    assert vacuous.cause == "unsatisfiable"
    assert vacuous.trace is None and vacuous.relaxed_trace is None


@needs_permits
@needs_tlc
def test_the_gate_splits_tlcs_two_identical_green_verdicts(
    permits: pl.DataFrame, precedence: rules.Precedence
) -> None:
    """Two models, two `verified` results from TLC, one of which means nothing.

    The tight governed model and the ungoverned one both pass. Only the witness count
    separates "this rule was obeyed" from "this rule was never in a position to be
    obeyed", which is why it belongs next to the check rather than in a reviewer's head.
    """
    tight = governed_at(permits, 200, precedence)
    bare = tla.check(tight, timeout=300)
    assert bare.outcome == "verified", bare.raw[-1200:]
    assert bare.distinct_states == 8

    gated = bare.with_witnesses(detect.witness_counts(tight))
    assert gated.outcome == "vacuous"
    assert gated.vacuous_invariants == (precedence.rule_name,)

    # The same threshold with no rule attached is a genuine pass, and must survive the
    # gate. Otherwise the gate would only be a way of failing everything.
    ungoverned = miner.mine(permits, name="Bare", min_edge_cases=200).process
    honest = tla.check(ungoverned, timeout=300)
    assert honest.outcome == "verified"
    assert honest.with_witnesses(detect.witness_counts(ungoverned)).outcome == "verified"


@needs_permits
@needs_tlc
@pytest.mark.parametrize("threshold", [25, 200])
def test_the_sweep_counts_the_same_states_tlc_does(
    permits: pl.DataFrame, precedence: rules.Precedence, threshold: int
) -> None:
    """The detector's soundness check: agree with the model-checker on state count.

    A sweep that visited a different number of states than TLC would be measuring a
    different system, and every count it reported would be about that other system.
    Compared on the mined models with no violation, where TLC explores the whole space
    and its `distinct states found` is directly comparable.
    """
    ungoverned = miner.mine(permits, name="Bare", min_edge_cases=threshold).process
    swept = detect.audit_process(ungoverned)
    checked = tla.check(ungoverned, timeout=300)
    assert checked.outcome == "verified", checked.raw[-1200:]
    assert swept.reachable_states == checked.distinct_states


@needs_fines
def test_the_second_log_derives_no_rule_the_detector_calls_live() -> None:
    """The negative result on roadfines, now with a cause attached to each rule.

    Every attachable rule is vacuous because the mined graph already orders the
    precedence topologically. `tests/app/test_portability.py` passes today over exactly
    these rules, which is the point: a green suite over rules that forbid nothing.
    """
    fines = eventlog.parse_xes(FINES)
    mined = miner.mine(fines, name="RoadFines", min_edge_cases=5).process
    report = rules.apply_derived_rules(
        fines, mined, min_support=20, max_rules=2, on_vacuous="ignore"
    )
    assert len(report.applied) == 2, "rules do attach"

    audited = detect.audit_process(report.process)
    assert audited.reachable_states == 8
    assert not audited.live, audited.summary()
    assert len(audited.vacuous) == 2
    assert all(audited.verdicts[p.rule_name].cause == "unsatisfiable" for p in report.applied)
    # Every count is zero, so a checker's pass over this process is withdrawn entirely.
    assert set(audited.witness_counts().values()) == {0}


# ── Whole-process sweep: the structural rules the checker adds ──


@needs_permits
def test_the_sweep_covers_nodeadlock_and_typeok_not_only_user_invariants(
    permits: pl.DataFrame, precedence: rules.Precedence
) -> None:
    """A per-invariant check measures the rule someone thought to ask about.

    The checker's .cfg names `TypeOK` and `NoDeadlock` alongside every user invariant,
    so a detector that skipped them would be measuring less than the thing it gates.
    """
    audited = detect.audit_process(governed_at(permits, 25, precedence))
    assert set(audited.verdicts) == {precedence.rule_name, detect.DEADLOCK_RULE, detect.TYPE_RULE}
    named = tla.render_config(governed_at(permits, 25, precedence))
    for rule in audited.verdicts:
        assert f"INVARIANT {rule}" in named, "every swept rule is one the checker checks"


# ── Honest bounds: a truncated sweep is not a finding ──


@needs_permits
def test_a_sweep_that_hits_its_limit_reports_unknown_not_safe(
    permits: pl.DataFrame, precedence: rules.Precedence
) -> None:
    """An exhausted budget is not evidence of safety.

    Three-valued on purpose. Reporting "no violating state found" after giving up would
    rebuild the defect one level up, so the limit is carried on the result and `live`
    goes to None rather than False.
    """
    governed = governed_at(permits, 25, precedence)

    truncated = detect.verdict_for(governed, precedence.rule_name, limit=2)
    assert truncated.truncated
    assert truncated.live is None, "an unknown must read as neither live nor vacuous"
    assert not truncated.vacuous
    assert truncated.cause == "unknown"
    assert truncated.limit == 2, "the bound is on the result, never applied silently"
    assert "TRUNCATED" in detect.audit_process(governed, limit=2).summary()

    exhaustive = detect.verdict_for(governed, precedence.rule_name, limit=10_000)
    assert not exhaustive.truncated
    assert exhaustive.live is True


# ── The consumer still behaves identically ──


@needs_permits
def test_rules_liveness_still_returns_the_record_its_callers_read(
    permits: pl.DataFrame, precedence: rules.Precedence
) -> None:
    """`rules.liveness` delegates to the detector, and its four fields are unchanged.

    The seam the previous author named. `enforce`'s `on_vacuous` parameter and the
    `Governed` report read `live`, `violating_states`, `antecedent_states` and
    `truncated`, so delegation has to preserve all four rather than merely a verdict.
    """
    governed = governed_at(permits, 25, precedence)
    delegated = rules.liveness(governed, precedence)
    direct = detect.verdict_for(governed, precedence.rule_name)

    assert isinstance(delegated, rules.Liveness)
    for field in ("live", "violating_states", "antecedent_states", "truncated"):
        assert getattr(delegated, field) == getattr(direct, field)
    assert delegated.invariant == precedence.rule_name

    with pytest.raises(ValueError, match="is not attached"):
        rules.liveness(miner.mine(permits, name="Bare", min_edge_cases=25).process, precedence)


# ── Fixture two: does the sweep survive a structurally messier model? ──


@needs_sample
def test_the_sweep_handles_the_transcript_fixture() -> None:
    """The second fixture is bigger and messier, and the sweep has to survive it.

    Transcript-mined models carry genuinely unreachable states, which neither XES log
    does, so this is the first fixture where `unreachable_scope` can arise from real
    data rather than a hand-built process. Nothing here asserts a magnitude beyond the
    sample's own, because the live corpus grows while the suite runs.
    """
    from pneuma.casestudy import transcriptlog

    events, _ = transcriptlog.load_sample(SAMPLE)
    mined = miner.mine(events, name="TranscriptSample", min_edge_cases=2).process
    report = rules.apply_derived_rules(
        events, mined, min_support=2, max_rules=3, on_vacuous="ignore"
    )

    audited = detect.audit_process(report.process)
    # Every attached rule gets a verdict, and the structural pair is in there too.
    assert set(audited.witness_counts()) == {p.rule_name for p in report.applied}
    assert detect.DEADLOCK_RULE in audited.verdicts
    assert not audited.truncated, "the default budget is not close to binding here"
    # Whatever the split, no rule may be simultaneously live and vacuous, and none may
    # be silently absent from the report.
    assert set(audited.live).isdisjoint(audited.vacuous)
    assert len(audited.verdicts) == len(report.applied) + 2


@pytest.mark.skipif(not transcript_corpus_available(), reason="needs claude-sql on PATH")
def test_the_sweep_and_tlc_agree_on_the_live_transcript_corpus() -> None:
    """Measured, not assumed: the fleet corpus is far past the permit model's size.

    A sibling declined to run TLC on a transcript model for that reason. It does run,
    in seconds, and the sweep agrees with it on the state count. Asserting agreement
    rather than a magnitude, because the corpus grows between runs.
    """
    from pneuma.casestudy import transcriptlog

    events, _ = transcriptlog.load(transcriptlog.FLEET_GLOB)
    mined = miner.mine(events, name="BonkFleet", min_edge_cases=10).process
    assert len(mined.states) >= 6

    audited = detect.audit_process(mined)
    assert not audited.truncated
    if tla.tlc_available():
        checked = tla.check(mined, timeout=300)
        assert checked.outcome == "verified", checked.raw[-1200:]
        assert audited.reachable_states == checked.distinct_states


@needs_permits
def test_the_stock_default_still_protects_something(permits: pl.DataFrame) -> None:
    """The reassuring half, re-measured through the detector.

    At the threshold the pipeline ships (`min_edge_cases=25`) all three attached rules
    are violable, and each now carries a witness trace. This is what would fail if a
    future threshold change quietly emptied them out.
    """
    mined = miner.mine(permits, name="PermitIntake", min_edge_cases=25).process
    report = rules.apply_derived_rules(permits, mined, min_support=100, max_rules=3)
    assert len(report.applied) == 3

    audited = detect.audit_process(report.process)
    assert len(audited.live) == 3, audited.summary()
    assert not audited.vacuous
    assert not audited.unknown
    assert all(v > 0 for v in audited.witness_counts().values())
    assert all(audited.verdicts[p.rule_name].trace is not None for p in report.applied)
