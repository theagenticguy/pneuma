"""Do the two detectors generalise to fixture two, or were they fitted to fixture one?

Both detectors were designed against `data/receipt.xes`: a curated human business
process, 27 activities, 6% of cases walking a trace nobody else walks. Fixture two is AI
coding-agent tool-use transcripts, where 91% of cases do. This module is the validation,
and the finding it exists to pin is negative: one detector generalises, one is partly
fitted, and one real bug in the first was found by pushing it here.

Every test either builds its own tiny process or reads a committed file:
`data/transcripts_sample.json` (75 events, 5 cases) for the shaping path, or
`data/transcripts_fleet.json` (3,055 events, 88 cases) when a whole objective curve is
needed. Nothing here calls `claude-sql`.

`data/transcripts_fleet.json` was generated from `transcriptlog.fetch(FLEET_GLOB)` at
2026-07-30 21:28 UTC, which read 9,850 events over 88 cases. Two bounds were applied and
both are stated rather than hidden: session ids and tool_use_ids are renumbered, and each
case is truncated to its first 60 events by timestamp order, which touched 30 of the 88
cases and dropped 6,795 events to keep the file at 0.36 MB. The cap was chosen because it
preserves the shape the tests are about: 88 cases, 30 activities, the same 97.6% of
distinct traces occurring once, and the same interior argmax. It does *not* preserve
magnitudes, and no test below asserts one that would differ from the live corpus, which
is the point.

That last point is the correction this module carries. An earlier version of it froze a
table of live-corpus measurements as module constants, including a claim that whole-trace
coverage sat at 0.0230 for every mining threshold from 1 to 51. The corpus is
live-appended: it went 9,081 to 9,662 to 9,850 events inside a single session, and 12 of
that table's 51 entries had already drifted when they were re-measured hours later. The
coverage claim did not reproduce at all. Re-swept on a 9,850-event read of the fleet
corpus taken at 2026-07-30 21:28 UTC, coverage moves monotonically from 0.5795 at
threshold 1 to 0.0682 above maximum support, and the composed objective peaks in the
interior at threshold 3 with a 24-state, 103-edge model scoring 0.4215.

0.0230 is reproducible, but only as 2/87 conforming cases, and only under
`max_cases=87, sampling="longest"` on the *full* corpus rather than the fleet one. A
1,080-configuration sweep over granularity, both globs, and every filter the adapter
exposes found that value nowhere else. It is the biased-sample path the adapter's own
docstring warns about, and even there it holds over thresholds 4 to 8, not 1 to 51.

The substantive finding survives the correction and is asserted below without live data,
because it never depended on the constant's value: with coverage *held fixed at any
constant*, the score is monotone in selectivity alone and its optimum is the smallest
surviving model. That is a property of the score's algebra, provable on a grid, and it
is the actual overfit in the prober's declared-degenerate contract.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from pathlib import Path

import polars as pl
import pytest

from pneuma import detect
from pneuma.casestudy import eventlog, miner, rules, transcriptlog
from pneuma.casestudy.minelearn import Attempt
from pneuma.detect import vacuity
from pneuma.detect.objective import Degenerate, Domain, Severity, Space, probe
from pneuma.process.ir import Guard, Invariant, Process, State, Transition, Variable

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "transcripts_sample.json"
FLEET = ROOT / "data" / "transcripts_fleet.json"
PERMITS = ROOT / "data" / "receipt.xes"

needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason="needs data/transcripts_sample.json")
needs_fleet = pytest.mark.skipif(not FLEET.is_file(), reason="needs data/transcripts_fleet.json")
needs_permits = pytest.mark.skipif(not PERMITS.is_file(), reason="needs data/receipt.xes")


# ── The bug this validation found: a truncated relaxed sweep read as vacuity ──


def guarded_with_padding(padding: int) -> Process:
    """`AB`'s guard on `f` is load-bearing, plus `padding` free booleans.

    The padding variables are guarded by nothing and assigned by nothing. They exist to
    make the `free_initial` start set 2^padding while `exact` keeps a single start, which
    is the size asymmetry between relaxation levels that the defect lives in.

    The same asymmetry arises on fixture two, where `rules.enforce` adds one boolean per
    distinct precedence antecedent: `data/transcripts_fleet.json` at mining threshold 4
    yields 14 flags, so `free_initial` starts from 2^14 states while `exact` starts from
    one. The count was stated as 15 here and measured against the live corpus, which had
    drifted by the time it was rechecked; the asymmetry it illustrates does not depend on
    the exact number, and `test_the_free_initial_asymmetry_is_real_on_fixture_two` now
    checks the direction against the committed file instead.
    """
    return Process(
        name="Padded",
        states=[State(name="A"), State(name="B"), State(name="C", terminal=True)],
        initial_state="A",
        variables=[
            Variable(name="f", low=0, high=1, initial=0),
            *[Variable(name=f"pad{i}", low=0, high=1, initial=0) for i in range(padding)],
        ],
        transitions=[
            Transition(
                name="AB", source="A", target="B", guards=[Guard(variable="f", op="eq", value=1)]
            ),
            Transition(name="AC", source="A", target="C"),
            Transition(name="BC", source="B", target="C"),
        ],
        invariants=[
            Invariant(
                name="NeverBWhenUnset",
                forbidden_state="B",
                forbidden_when=[Guard(variable="f", op="eq", value=0)],
            )
        ],
    )


def test_a_guarded_rule_whose_relaxed_sweep_truncated_is_unknown_not_vacuous() -> None:
    """The defect found by pushing the detector at fixture two, pinned.

    `audit` walks the relaxations in order and drops a rule from `pending` once a sweep
    truncates, so a `free_initial` sweep that exhausts the budget means `free_guards` is
    never swept. `free_guards` is the only level that can earn a guarded rule its pass.
    `RuleVerdict.truncated` reads the `exact` sweep alone, so before the fix the verdict
    reported `truncated=False`, `cause='unreachable_scope'` and `vacuous=True`: a
    finished search concluding the rule was decoration, when the search that would have
    exonerated it never ran.

    The control below is the same process with the budget available, where the rule is
    correctly `guarded` and keeps its pass. One field of the report differs between the
    two runs, and it has to be the honest one.
    """
    ample = detect.verdict_for(guarded_with_padding(0), "NeverBWhenUnset")
    assert ample.cause == "guarded"
    assert ample.witnesses == 1
    assert not ample.vacuous
    assert not ample.relaxation_truncated

    starved = detect.verdict_for(guarded_with_padding(6), "NeverBWhenUnset", limit=40)
    assert starved.relaxed == {"exact": 0, "free_initial": 0}, "free_guards was never swept"
    assert starved.relaxation_truncated
    assert starved.truncated is False, "the exact sweep did finish, which is why this was missed"
    assert starved.cause == "unknown"
    assert not starved.vacuous, "an abandoned search is not a finding of decoration"
    assert starved.violating_states == 0

    # The gate is deliberately stricter than the vacuity verdict: not knowing is not a
    # pass either, so the witness count stays zero and a checker's green is withdrawn.
    assert starved.witnesses == 0
    assert "CAUSE UNKNOWN" in str(starved)


def test_an_unsettled_rule_appears_in_exactly_one_bucket_of_the_audit() -> None:
    """A rule the search did not settle must be counted somewhere, or it vanishes.

    `Audit.live` / `.vacuous` / `.unknown` are how a caller reads the split. Before the
    fix a relaxation-truncated rule was in `vacuous`; a fix that only cleared the flag
    would have put it in none of the three, which is a quieter version of the same
    defect.
    """
    audited = detect.audit_process(guarded_with_padding(6), limit=40)
    name = "NeverBWhenUnset"

    assert name in audited.unknown
    assert name not in audited.vacuous
    assert name not in audited.live
    assert audited.truncated, "the audit as a whole did hit its budget"
    assert audited.witness_counts()[name] == 0


def test_the_gate_still_withdraws_a_pass_when_the_relaxed_search_gave_up() -> None:
    """The property that must not regress: a rule with no witness never reads as verified.

    The fix moved a rule out of `vacuous`, and the danger in that direction is turning an
    unsettled rule into a pass. It does not: `witness_counts` reports zero, and any
    consumer feeding that to `CheckResult.with_witnesses` gets its verdict withdrawn.
    """
    from pneuma.process.tla import CheckResult

    counts = detect.witness_counts(guarded_with_padding(6), limit=40)
    assert counts == {"NeverBWhenUnset": 0}

    pretend_verified = CheckResult(
        outcome="verified",
        returncode=0,
        states_found=2,
        distinct_states=2,
        initial_states=1,
        violated=None,
        failure=None,
        trace=[],
        raw="",
    )
    gated = pretend_verified.with_witnesses(counts)
    assert gated.outcome == "vacuous"
    assert not gated.ok


# ── The vacuity detector generalises: same mechanism, opposite verdicts ──


@needs_sample
def test_derived_transcript_rules_are_measured_the_same_way_permit_rules_are() -> None:
    """Fixture two derives rules and the detector splits them, with no code change.

    The mechanism is log-agnostic and this asserts that rather than a count: every
    attached rule gets a verdict, the two structural rules are swept alongside, and no
    rule is simultaneously live and vacuous. The sample is small, so the split it happens
    to produce is not the claim; the claim is that a split is produced and accounted for.
    """
    from pneuma.casestudy import transcriptlog

    events, _ = transcriptlog.load_sample(SAMPLE)
    mined = miner.mine(events, name="Sample", min_edge_cases=2).process
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rules.RuleNotEnforced)
        report = rules.apply_derived_rules(
            events, mined, min_support=2, max_rules=3, on_vacuous="ignore"
        )
    assert report.applied, "the sample should derive at least one attachable rule"

    audited = detect.audit_process(report.process)
    gating = {n: v for n, v in audited.verdicts.items() if v.gates}
    assert set(gating) == {p.rule_name for p in report.applied}
    assert set(audited.live).isdisjoint(audited.vacuous)
    for verdict in gating.values():
        assert (verdict.live is True) or verdict.vacuous or verdict.invariant in audited.unknown


@needs_fleet
def test_the_free_initial_asymmetry_is_real_on_fixture_two() -> None:
    """The state-space blow-up that `guarded_with_padding` stands in for, on real data.

    `guarded_with_padding` is a hand-built process, so on its own it leaves open whether the
    asymmetry it exercises ever occurs. It does. `rules.enforce` adds one boolean per
    distinct precedence antecedent, and this log's derived rules give enough of them that
    `free_initial` has to start from 2^flags states while `exact` starts from one. Under a
    budget the audit truncates and rules land in `unknown`, which is the state the fixed
    detector reports honestly and the unfixed one reported as `vacuous`.

    Asserted against the committed snapshot rather than the live corpus, and as an
    inequality rather than a state count, because both the flag count and the state count
    move with the log. An earlier docstring here quoted 15 flags and 72,704 states measured
    live; the flag count was 14 by the time it was rechecked hours later.
    """
    events, _ = transcriptlog.load_sample(FLEET)
    mined = miner.mine(events, name="Fleet", min_edge_cases=4).process
    # max_rules is bounded at 25 for runtime, not to make the finding look better: this log
    # derives 45 attachable rules and 14 flags, but `rules.apply_derived_rules` re-checks
    # each rule as it attaches and the full set costs 41s against 0.3s here. 25 already
    # truncates, and a larger set only truncates harder.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rules.RuleNotEnforced)
        report = rules.apply_derived_rules(
            events, mined, min_support=2, max_rules=25, on_vacuous="ignore"
        )

    flags = len(report.process.variables)
    assert flags >= 4, "the derived-rule path really does add several free booleans here"
    assert 2**flags >= 16, "so free_initial starts from many states where exact starts from one"

    audited = detect.audit_process(report.process, limit=200)
    assert audited.truncated, "and a modest budget is not enough for that start set"
    assert audited.unknown, "rules the search did not settle are reported, not called vacuous"
    assert not set(audited.unknown) & set(audited.vacuous)

    # `witness_counts` reports gating rules only, by design: a wellformedness property that
    # cannot fail is a sound model rather than an untested one. So the gate is checked over
    # the intersection, and the structural rules are asserted to be the difference rather
    # than left as an unexplained gap.
    counts = audited.witness_counts()
    for name in set(audited.unknown) & set(counts):
        assert counts[name] == 0, "an unsettled gating rule never earns a witness"
    unmetered = set(audited.unknown) - set(counts)
    assert all(not audited.verdicts[name].gates for name in unmetered)


@needs_permits
@needs_fleet
def test_the_same_threshold_number_is_a_different_fraction_on_each_log() -> None:
    """Why the honest comparison is fraction-matched rather than threshold-matched.

    `min_edge_cases` is an absolute count of distinct cases, so the shipped 25 is under 2%
    of the permit log's 1,434 cases and over a quarter of the fleet snapshot's 88. Any
    comparison at a matched *number* is therefore comparing two different amounts of
    aggression, and the flattering direction is available to whoever picks the number.

    Both case counts are read from the logs rather than written down, so the fractions
    move with the fixtures instead of pinning yesterday's corpus size.
    """
    permits = eventlog.parse_xes(PERMITS)
    fleet, _ = transcriptlog.load_sample(FLEET)
    permit_cases = permits["case_id"].n_unique()
    fleet_cases = fleet["case_id"].n_unique()

    assert 25 / permit_cases < 0.02, "the shipped threshold is under 2% of the permit cases"
    assert 25 / fleet_cases > 0.2, "and over a fifth of the fleet snapshot's"

    matched_fraction = max(2, round(25 / fleet_cases * permit_cases))
    tight = miner.mine(permits, name="Tight", min_edge_cases=matched_fraction)
    stock = miner.mine(permits, name="Stock", min_edge_cases=25)
    assert len(tight.process.states) < len(stock.process.states)
    assert tight.coverage < stock.coverage


# ── The objective prober is partly fitted: it passes a degenerate optimum here ──


def fleet_curve() -> tuple[pl.DataFrame, dict[int, int], dict[int, float], int]:
    """Everything the fleet objective is built from, derived from the committed snapshot.

    Returns the shaped log, surviving non-self-loop edges per threshold, mined coverage per
    threshold, and the highest per-edge case support. Nothing here is written down: an
    earlier version of this module froze these three tables against the live corpus and 12
    of 51 entries had drifted within hours of being taken, because the corpus is
    live-appended. Reading a committed file instead is what makes the exact numbers below
    legitimate rather than dated.

    Self-loops are dropped from the edge count because `score_edges` drops them from both
    sides of its ratio, so the `edge_share` this composes is the one the training loop
    actually divides. Keeping them would understate the share and put a non-zero score on
    threshold 1, the memorising model that has to score zero.
    """
    events, _ = transcriptlog.load_sample(FLEET)
    pairs = miner.directly_follows(events)
    handoffs = pairs.filter(pl.col("activity") != pl.col("next_activity"))
    top = int(handoffs["cases"].max())
    edges = {t: handoffs.filter(pl.col("cases") >= t).height for t in range(1, top + 2)}
    coverage = {
        t: miner.mine(events, name=f"T{t}", min_edge_cases=t).coverage for t in range(1, top + 2)
    }
    return events, edges, coverage, top


@pytest.fixture(scope="module")
def fleet() -> tuple[pl.DataFrame, dict[int, int], dict[int, float], int]:
    return fleet_curve()


def composed(
    edges: dict[int, int], coverage: dict[int, float], visible: int
) -> Callable[[float], float]:
    """`Attempt.score` as a function of mining threshold alone.

    Built through a real `Attempt` rather than re-derived, so the probed quantity is the
    selected one. A re-implementation could drift from `Attempt.score` and the probe would
    then be clearing an objective the loop does not use.
    """

    def objective(threshold: float) -> float:
        step = max(1, int(round(threshold)))
        kept = edges.get(step, 0)
        if not kept:
            return 0.0
        return Attempt(
            index=0,
            coverage=coverage[step],
            matched_coverage=coverage[step],
            threshold=step,
            states=2,
            edges=kept,
            guidance_chars=0,
            edge_share=kept / visible,
        ).score

    return objective


def pinned(edges: dict[int, int], constant: float, visible: int) -> Callable[[float], float]:
    """The same objective with the coverage term replaced by a constant.

    This is the counterfactual the finding lives in, and it is deliberately *not* a
    measurement of any log. It stands for a log on which whole-trace replay coverage fails
    to discriminate between models, which is the condition a mostly-unique-trace log
    approaches, and it isolates what the score does under that condition from what any
    particular corpus happened to read on any particular day.
    """
    return composed(edges, dict.fromkeys(edges, constant), visible)


@needs_fleet
def test_the_fleet_coverage_term_moves_and_moves_one_way(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The correction, pinned: coverage on this log is monotone, not flat.

    An earlier version of this module asserted coverage was 0.0230 at every threshold from
    1 to 51 on the fleet corpus. It is not, and this is the invariant that would have
    caught the claim: tightening the threshold removes edges, removing edges can only
    disqualify cases that were replaying, so coverage is non-increasing in the threshold
    and strictly falls somewhere. A genuinely flat curve would fail the second assertion.

    Asserted as monotonicity plus a floor on the drop rather than as the curve's values, so
    it survives regenerating the snapshot at a different size: checked by rebuilding the
    fixture at 2,358 and 4,293 events, where every test in this module still passes. The
    measured curve on the committed 3,055-event snapshot runs 0.5795 down to 0.1023 over
    thresholds 1 to 44.
    """
    _, edges, coverage, top = fleet
    walk = [coverage[t] for t in range(1, top + 1)]
    kept = [edges[t] for t in range(1, top + 1)]

    assert all(later <= earlier for earlier, later in zip(walk, walk[1:], strict=False)), (
        "removing edges cannot make a case start replaying"
    )
    assert max(walk) - min(walk) > 0.2, "and the coverage term genuinely discriminates here"
    assert all(later <= earlier for earlier, later in zip(kept, kept[1:], strict=False))
    assert edges[top + 1] == 0, "above maximum support no handoff survives"


@needs_fleet
def test_the_smallest_surviving_model_has_the_lowest_coverage(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The ordering the degenerate-optimum argument depends on.

    "A model that describes nothing wins" is only a defect if the winner really does
    describe less. This asserts that ordering structurally: the threshold at which the last
    handoff survives mines the fewest states and replays the fewest cases of any threshold
    that compiles a model at all.
    """
    events, edges, coverage, top = fleet
    smallest = max(t for t in range(1, top + 1) if edges[t])

    assert coverage[smallest] == min(coverage[t] for t in range(1, top + 1))
    assert edges[smallest] == 1, "one handoff left, so a two-state model"

    mined = miner.mine(events, name="Smallest", min_edge_cases=smallest)
    loosest = miner.mine(events, name="Loosest", min_edge_cases=1)
    assert len(mined.process.states) < len(loosest.process.states)
    assert len(mined.process.states) <= 2


@needs_fleet
def test_the_prober_passes_a_flat_objective_whose_optimum_is_the_empty_model(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The overfit, isolated from any corpus. The sharpest negative result in this module.

    The score is a harmonic mean of coverage and `1 - edge_share`. Hold coverage at any
    constant and the second term is the only one left moving, so the score rises as edges
    are dropped and its argmax is whatever the graph admits last: here a two-edge model
    replaying a tenth of the log. Every check the prober runs passes on that. The maximum
    is interior, so `window-too-narrow` cannot fire. Nothing is non-finite, nothing raises,
    there is no pole, and refining the grid does not raise the peak because the function is
    bounded. The optimum is degenerate in the sense that matters and the prober has no
    check for it, because `degenerate` is a list the caller supplies and the call site
    declares the two inputs *fixture one* had.

    The constant is a stand-in for a non-discriminating coverage term, not a measurement.
    An earlier version of this test used 0.0230 and presented it as the fleet corpus's
    coverage at every threshold, which was wrong twice over: that log's coverage moves, and
    a claim pinned to a live-appended corpus is not checkable later anyway. The finding
    needed neither, so the degenerate-winner half is asserted over four unrelated constants
    and rests on the score's algebra.

    Writing it that way also corrected a second claim. The earlier version said the
    objective was *flat*, and flatness is not constant-independent: with the real edge decay
    the non-zero range spans 0.0014 at coverage 0.0230 but 0.1352 at 0.35, so a
    two-points-of-range assertion holds only where the pinned constant is itself small. The
    plateau is therefore asserted only in the low-coverage regime, and labelled as
    conditional on it. What holds everywhere is the part that matters: the winner keeps at
    most two handoffs whatever the constant is.
    """
    _, edges, _, top = fleet
    visible = edges[1]
    axis = Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top)))
    as_declared_for_fixture_one = (
        Degenerate("keep every handoff (threshold 1)", {"threshold": 1.0}),
        Degenerate("keep nothing (above max support)", {"threshold": float(top) + 1}),
    )

    for constant in (0.0230, 0.1023, 0.35, 0.9):
        objective = pinned(edges, constant, visible)
        report = probe(
            objective,
            (axis,),
            space=Space.DECISION,
            degenerate=as_declared_for_fixture_one,
        )
        assert report.ok, report.report()
        assert not report.refusals

        best = report.sweeps[0].best
        assert best is not None
        assert 1 < best.point["threshold"] < top, "interior, so the boundary check cannot fire"

        # The argmax keeps almost no handoffs, which is the whole finding: the winner
        # describes nothing, and every check the prober runs is satisfied by it.
        scores = {t: objective(t) for t in range(1, top + 1)}
        argmax = max(scores, key=lambda t: scores[t])
        assert edges[argmax] <= 2, f"coverage pinned at {constant} picks {edges[argmax]} edges"

        # Selectivity is the only term left moving, so dropping an edge never costs score.
        # This is the mechanism behind the degenerate winner, and it is what "flat" was
        # reaching for. It holds at every constant, unlike flatness.
        for step in range(2, top + 1):
            if edges[step] < edges[step - 1]:
                assert scores[step] >= scores[step - 1] - 1e-9, (
                    f"at {constant}, dropping an edge at threshold {step} cost score"
                )

        # The plateau is real but only in the low-coverage regime, which is the honest
        # scope of the original claim. Fixture one's composed objective spans 0.71 over its
        # non-zero range and its peak is a single point.
        alive = [s for s in scores.values() if s > 0]
        if constant <= 0.11:
            assert max(alive) - min(alive) < 0.03, "the whole non-zero range is a few points"
            plateau = [t for t, s in scores.items() if s >= max(alive) - 0.001]
            assert len(plateau) > 0.5 * top, "over half the feasible range ties the maximum"


@needs_fleet
def test_declaring_the_real_degenerate_input_makes_the_prober_refuse(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The mechanism is sound; the *declaration* is what was fitted to fixture one.

    Adding one `Degenerate` naming the smallest surviving model turns the same probe over
    the same objective into a refusal. So this is not a hole in the prober's arithmetic, it
    is a hole in what a caller has to know to use it, and the caller cannot know it from
    fixture one: on the permit log the smallest model does *not* win, so the declaration
    was never needed there and its absence was invisible.
    """
    _, edges, _, top = fleet
    visible = edges[1]
    objective = pinned(edges, 0.0230, visible)
    axis = Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top)))
    smallest_surviving = max(t for t in range(1, top + 1) if objective(t) > 0)

    report = probe(
        objective,
        (axis,),
        space=Space.DECISION,
        degenerate=(
            Degenerate("keep every handoff (threshold 1)", {"threshold": 1.0}),
            Degenerate("keep nothing (above max support)", {"threshold": float(top) + 1}),
            Degenerate(
                "smallest model the graph still admits", {"threshold": float(smallest_surviving)}
            ),
        ),
    )
    assert not report.ok, report.report()
    refusals = {f.check for f in report.findings if f.severity is Severity.REFUSE}
    assert "degenerate-optimum" in refusals
    assert "smallest model" in report.report()


@needs_fleet
def test_the_pinned_coverage_term_is_what_degenerates_the_optimum_not_this_log(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The correction's other half, and the honest comparison the earlier version skipped.

    Two objectives over the same edge table and the same probe, differing only in whether
    the coverage term is the measured one or a constant. Pinned, the argmax is the smallest
    surviving model and declaring it refuses. Measured, the argmax is an interior peak with
    a real model behind it and that same declaration does not fire, because on this log the
    smallest model does not win.

    So fixture two does *not* by itself exhibit the degenerate optimum. What exhibits it is
    a coverage term that stops discriminating, which is the condition the earlier version
    asserted this log was already in. It is not, and the difference matters: the defect is
    in the prober's contract, reachable from any log whose coverage term goes flat, and it
    is not a property of AI transcripts.
    """
    _, edges, coverage, top = fleet
    visible = edges[1]
    axis = Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top)))
    baseline = (
        Degenerate("keep every handoff (threshold 1)", {"threshold": 1.0}),
        Degenerate("keep nothing (above max support)", {"threshold": float(top) + 1}),
    )

    measured = composed(edges, coverage, visible)
    peak = max(range(1, top + 1), key=measured)
    assert 1 < peak < top
    assert edges[peak] > 10, "the measured winner is a model with real structure in it"
    assert coverage[peak] > 2 * coverage[max(t for t in range(1, top + 1) if edges[t])]

    smallest_surviving = max(t for t in range(1, top + 1) if measured(t) > 0)
    declared = (
        *baseline,
        Degenerate(
            "smallest model the graph still admits", {"threshold": float(smallest_surviving)}
        ),
    )
    assert probe(measured, (axis,), space=Space.DECISION, degenerate=declared).ok, (
        "with a live coverage term the smallest model is not the optimum"
    )
    assert not probe(
        pinned(edges, 0.0230, visible), (axis,), space=Space.DECISION, degenerate=declared
    ).ok, "with coverage pinned it is"


@needs_permits
@needs_fleet
def test_the_permit_objective_trades_two_live_terms_and_the_fleet_one_barely_does(
    fleet: tuple[pl.DataFrame, dict[int, int], dict[int, float], int],
) -> None:
    """The baseline the flat-objective finding is negative against, on real data both sides.

    The earlier version of this test asserted only the permit half and left the fleet half
    as a prose claim about a live corpus, which is exactly the kind of half-checked
    comparison this session exists to find. Both halves are measured here, both from
    committed files.

    What separates the logs is not whether the argmax is interior; it is interior on both.
    It is how much of *both* terms a model can have at once. The permit log admits a model
    holding 86% coverage and 86% selectivity simultaneously. The fleet snapshot's best
    simultaneous pair is around 0.41, because almost every session is unique and whole-trace
    replay is close to the strongest possible demand on such a log. That is the real,
    weaker version of the original claim: the coverage term is not dead here, it is capped.
    """
    permits = eventlog.parse_xes(PERMITS)
    permit_pairs = miner.directly_follows(permits)
    permit_handoffs = permit_pairs.filter(pl.col("activity") != pl.col("next_activity"))
    permit_visible = permit_handoffs.height

    def best_simultaneous(
        events: pl.DataFrame, handoffs: pl.DataFrame, visible: int, thresholds: range
    ) -> float:
        """The largest value both terms can hold at once, over a threshold sweep."""
        best = 0.0
        for step in thresholds:
            kept = handoffs.filter(pl.col("cases") >= step).height
            if not kept:
                continue
            coverage = miner.mine(events, name=f"S{step}", min_edge_cases=step).coverage
            best = max(best, min(coverage, 1.0 - kept / visible))
        return best

    permit_best = best_simultaneous(permits, permit_handoffs, permit_visible, range(1, 60, 4))
    assert permit_best > 0.8, "the permit log admits a model strong on both terms at once"

    events, edges, coverage, top = fleet
    fleet_best = max(min(coverage[t], 1.0 - edges[t] / edges[1]) for t in range(1, top + 1))
    assert fleet_best < 0.6, "the fleet snapshot does not"
    assert permit_best > fleet_best + 0.3, "and the gap is the finding, measured both sides"

    # The permit coverage term is the one that moves a long way, which is why fixture one
    # never needed the smallest-model degenerate declared.
    assert coverage[1] < 0.7, "fleet: keeping every handoff still replays well under 70%"
    assert miner.mine(permits, name="Loose", min_edge_cases=1).coverage > 0.95


# ── Guard satisfiability: fixture two does not reach the stated limit ──


@needs_sample
def test_every_derived_transcript_condition_is_a_single_variable_comparison() -> None:
    """`contradictory` handles one variable at a time, and fixture two never needs more.

    Worth pinning as a not-reached limit rather than leaving unstated. `rules.enforce`
    compiles a precedence to exactly one clause, `flag = 0`, so the multi-variable case
    is unreachable through the derived-rule path on any log. The limit is real, but it is
    a limit on hand-written invariants, and fixture two cannot exercise it.
    """
    from pneuma.casestudy import transcriptlog

    events, _ = transcriptlog.load_sample(SAMPLE)
    mined = miner.mine(events, name="Guards", min_edge_cases=2).process
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rules.RuleNotEnforced)
        report = rules.apply_derived_rules(
            events, mined, min_support=2, max_rules=3, on_vacuous="ignore"
        )

    for invariant in report.process.invariants:
        assert len({g.variable for g in invariant.forbidden_when}) == 1
        assert {g.op for g in invariant.forbidden_when} == {"eq"}
    assert detect.contradictions_in(report.process) == {}

    # The shape it genuinely cannot decide, on this log's own flag name and domain.
    flag = report.process.variables[0].name
    assert vacuity.contradictory([(flag, "ne", 0), (flag, "ne", 1)]) is None
