"""What `rules.enforce` claims to enforce, checked against the reachable state space.

A separate module from `test_casestudy.py` because these are tests of one function's
contract rather than tests of the case study: `test_casestudy.py` never imports
`rules`, and `test_portability.py` exercises `rules` only as a means to the
portability claim. The defect class here is the project's own confessed one — a
property that verifies green over a state space where it can never fire — so it
needs tests that look past "did TLC pass" to "was there anything to pass".

Every number below comes from `data/receipt.xes` (1,434 real cases) or
`data/roadfines.xes`, so a change in mining or rule derivation that quietly moves
them fails here.
"""

from __future__ import annotations

import polars as pl
import pytest
from paths import FINES, PERMITS, needs_permits

from pneuma.casestudy import eventlog, miner, rules
from pneuma.process import tla
from pneuma.process.ir import Guard, Invariant, Process, State, Transition, Variable

pytestmark = needs_permits

# The rule the case study is built around: T02 checks, then T04 determines.
CHECK = "T02 Check confirmation of receipt"
DETERMINE = "T04 Determine confirmation of receipt"


@pytest.fixture(scope="module")
def permits() -> pl.DataFrame:
    return eventlog.parse_xes(PERMITS)


@pytest.fixture(scope="module")
def check_before_determine(permits: pl.DataFrame) -> rules.Precedence:
    found = [
        p
        for p in rules.derive_precedences(permits, min_support=100)
        if p.before == CHECK and p.after == DETERMINE
    ]
    assert found, "the log no longer yields the precedence this module is about"
    return found[0]


# ── The defect: a rule that verifies green over zero violating states ──


def test_the_same_rule_is_live_at_one_threshold_and_vacuous_at_another(
    permits: pl.DataFrame, check_before_determine: rules.Precedence
) -> None:
    """One precedence, two mined models, two entirely different amounts of protection.

    At `min_edge_cases=25` the model keeps the edge that reaches T04 without passing
    through T02, so the rule has something to forbid. At 200 that edge is below the
    threshold and T04 is reachable only after T02, so the invariant's condition holds
    in no reachable state. Both attach identically and both pass every structural
    check; only reachability tells them apart.
    """
    loose = miner.mine(permits, name="Loose", min_edge_cases=25).process
    tight = miner.mine(permits, name="Tight", min_edge_cases=200).process

    live = rules.liveness(rules.enforce(loose, check_before_determine), check_before_determine)
    vacuous = rules.liveness(rules.enforce(tight, check_before_determine), check_before_determine)

    assert live.live, "the rule should be violable in the loose model"
    assert live.violating_states == 1
    assert live.antecedent_states == 2

    assert not vacuous.live, "the rule cannot be violated in the tight model"
    assert vacuous.violating_states == 0
    assert vacuous.antecedent_states == 1


def test_enforce_reports_a_vacuous_rule_instead_of_attaching_it_quietly(
    permits: pl.DataFrame, check_before_determine: rules.Precedence
) -> None:
    """The rule is well-formed, passes `_not_vacuous`, and protects nothing.

    Attaching it silently is the failure this module exists to prevent, so `enforce`
    must say so, and must refuse outright when asked to.
    """
    tight = miner.mine(permits, name="Tight", min_edge_cases=200).process

    with pytest.warns(rules.RuleNotEnforced, match=rules.UNVIOLABLE):
        attached = rules.enforce(tight, check_before_determine)
    assert attached is not tight, "the default still attaches, so callers do not break"

    refused = rules.enforce(tight, check_before_determine, on_vacuous="refuse")
    assert refused is tight, "on_vacuous='refuse' must decline a rule that cannot fire"


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_reports_success_on_the_rule_that_protects_nothing(
    permits: pl.DataFrame, check_before_determine: rules.Precedence
) -> None:
    """The evidence that a green model-check is not evidence.

    TLC is given the same invariant twice. On the loose model it finds the
    counterexample in three states. On the tight model it explores the whole space
    and reports no error — over a state space in which the invariant's condition is
    reachable in zero states. Only the reachability count distinguishes the two
    verdicts, which is why it belongs next to the check rather than in a reviewer's
    head.
    """
    loose = rules.enforce(
        miner.mine(permits, name="Loose", min_edge_cases=25).process, check_before_determine
    )
    tight = rules.enforce(
        miner.mine(permits, name="Tight", min_edge_cases=200).process,
        check_before_determine,
        on_vacuous="ignore",
    )

    violated = tla.check(loose, timeout=300)
    assert not violated.ok
    assert violated.violated == check_before_determine.rule_name
    assert len(violated.trace) >= 3

    passed = tla.check(tight, timeout=300)
    assert passed.ok, passed.raw[-1200:]
    assert passed.distinct_states > 0
    # TLC's green and zero violating states, from the same object.
    assert rules.liveness(tight, check_before_determine).violating_states == 0


# ── The second defect: rules that vanish without a trace ──


def test_dropped_rules_are_reported_with_a_reason(permits: pl.DataFrame) -> None:
    """`enforce` legitimately declines most derived precedences, and used to do it
    by returning its argument. A caller who derived nine rules and attached two had
    no way to learn which seven went missing or why."""
    process = miner.mine(permits, name="P", min_edge_cases=25).process
    report = rules.apply_derived_rules(permits, process, min_support=100, max_rules=2)

    assert len(report.applied) == 2
    assert report.skipped, "declining a rule must leave a record"
    reasons = {skip.reason for skip in report.skipped}
    assert rules.PREREQUISITE_IS_START in reasons
    assert all(skip.precedence.rule_name for skip in report.skipped)
    # Nothing is lost: every candidate considered is either applied or explained.
    assert len(report.applied) + len(report.skipped) == report.considered


def test_the_report_still_unpacks_as_the_pair_callers_expect(permits: pl.DataFrame) -> None:
    """Existing callers write `governed, applied = apply_derived_rules(...)`."""
    process = miner.mine(permits, name="P", min_edge_cases=25).process
    governed, applied = rules.apply_derived_rules(permits, process, min_support=100, max_rules=2)

    assert isinstance(governed, Process)
    assert len(governed.invariants) == len(applied) == 2


def test_enforce_warns_rather_than_silently_returning_its_argument(
    permits: pl.DataFrame,
) -> None:
    """The strongest precedences in any log are against the start activity, and every
    one of them is declined. Silently."""
    process = miner.mine(permits, name="P", min_edge_cases=25).process
    start = process.state_map[process.initial_state].description
    against_start = [
        p for p in rules.derive_precedences(permits, min_support=100) if p.before == start
    ]
    assert against_start, "expected a precedence rooted at the start activity"

    with pytest.warns(rules.RuleNotEnforced, match=rules.PREREQUISITE_IS_START):
        assert rules.enforce(process, against_start[0]) is process


def test_a_precedence_naming_an_unmined_activity_is_reported(permits: pl.DataFrame) -> None:
    """Mining drops infrequent activities, so a rule derived from the whole log can
    name a state the thresholded model does not contain."""
    tight = miner.mine(permits, name="Tight", min_edge_cases=200).process
    absent = rules.Precedence(
        before="T02 Check confirmation of receipt", after="Nonexistent", cases=999
    )

    with pytest.warns(rules.RuleNotEnforced, match=rules.ACTIVITY_NOT_MINED):
        assert rules.enforce(tight, absent) is tight


# ── Truncated names collide into one TLA+ operator ──


def test_no_two_derived_rules_on_the_real_log_collide_after_truncation(
    permits: pl.DataFrame,
) -> None:
    """A negative result, measured rather than assumed, plus the margin.

    `rule_name` truncates to 60 characters and 8 of the 9 precedences derived from
    receipt.xes sit at exactly 60, which looks alarming. It is not: enumerating all
    702 ordered activity pairs in the log produces zero colliding names, and lowering
    `min_support` to 1 (156 precedences) still produces zero. So this is a latent
    hazard, not a live defect, and the guard below is insurance rather than a fix.

    What is thin is the margin. Four real activities have identifiers at the 40-char
    cap, and for those the rule name spends 42 of its 60 characters before it reaches
    the prerequisite at all, leaving 11.
    """
    names = [p.rule_name for p in rules.derive_precedences(permits, min_support=1)]
    assert len(names) == 156
    assert len(set(names)) == len(names), "no collision on the real log"

    at_cap = [
        s.name
        for s in miner.mine(permits, name="Loose", min_edge_cases=1).process.states
        if len(s.name) == 40
    ]
    assert len(at_cap) == 4, "four real activities sit at the identifier cap"
    assert 60 - len(f"No{at_cap[0]}Without") == 11


def test_two_precedences_that_truncate_to_one_name_are_refused() -> None:
    """The latent hazard, made concrete with a real 40-char activity from the log.

    `T09-1 Process or receive external advice from party 1` leaves 11 characters for
    the prerequisite, so two plausibly-named prerequisites compile to one rule name.
    The IR checks duplicate state, variable and transition names but not invariant
    names, so the collision reaches TLC as two definitions of one operator and TLC
    fails to *parse*. `tla.check` reports that as `ok=False` with `violated=None`,
    which is indistinguishable at a glance from a real counterexample.
    """
    after = "T09-1 Process or receive external advice from party 1"
    first = rules.Precedence(
        before="Determine eligibility for expedited review", after=after, cases=100
    )
    second = rules.Precedence(
        before="Determine eligibility for standard review", after=after, cases=100
    )
    assert first.rule_name == second.rule_name, "the premise: two rules, one name"
    assert first.flag != second.flag, "but genuinely different prerequisites"

    target = miner._identifier(after)
    one, two = miner._identifier(first.before), miner._identifier(second.before)
    process = Process(
        name="Collide",
        states=[
            State(name="Begin"),
            State(name=one),
            State(name=two),
            State(name=target),
            State(name="End", terminal=True),
        ],
        initial_state="Begin",
        transitions=[
            Transition(name="T1", source="Begin", target=one),
            Transition(name="T2", source="Begin", target=two),
            Transition(name="T3", source="Begin", target=target),
            Transition(name="T4", source=one, target=target),
            Transition(name="T5", source=two, target=target),
            Transition(name="T6", source=target, target="End"),
        ],
    )

    once = rules.enforce(process, first)
    assert once is not process
    with pytest.warns(rules.RuleNotEnforced, match=rules.DUPLICATE_RULE_NAME):
        assert rules.enforce(once, second) is once


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_a_name_collision_would_reach_tlc_as_a_parse_failure() -> None:
    """Why the guard is in `enforce` rather than left to the model-checker.

    Two invariants with one name is a TLA+ semantic error, so TLC never gets as far as
    checking anything. Nothing in the IR rejects it, and the reported result carries no
    violated invariant, so a caller reading `ok` alone learns only that something went
    wrong somewhere.
    """
    collision = Process(
        name="Dup",
        states=[State(name="A"), State(name="B", terminal=True)],
        initial_state="A",
        variables=[Variable(name="f", low=0, high=1, initial=0)],
        transitions=[Transition(name="AB", source="A", target="B")],
        invariants=[
            Invariant(
                name="Same",
                forbidden_state="B",
                forbidden_when=[Guard(variable="f", op="eq", value=0)],
            ),
            Invariant(
                name="Same",
                forbidden_state="A",
                forbidden_when=[Guard(variable="f", op="eq", value=1)],
            ),
        ],
    )
    assert [i.name for i in collision.invariants] == ["Same", "Same"], "the IR permits it"

    result = tla.check(collision, timeout=200)
    assert not result.ok
    assert result.violated is None, "no property was checked, so none was violated"
    assert "already defined or declared" in result.raw


# ── Honest unknowns ──


def test_a_search_that_hits_its_limit_reports_unknown_not_vacuous(
    permits: pl.DataFrame, check_before_determine: rules.Precedence
) -> None:
    """An exhausted budget is not evidence of safety.

    Reporting "no violating state found" after giving up would recreate the defect
    one level higher, so a truncated search says so and `enforce` attaches the rule
    rather than refusing on unknown grounds.
    """
    loose = miner.mine(permits, name="Loose", min_edge_cases=25).process
    governed = rules.enforce(loose, check_before_determine)

    truncated = rules.liveness(governed, check_before_determine, limit=2)
    assert truncated.truncated
    assert truncated.live is None, "an unknown must not read as either live or vacuous"

    exhaustive = rules.liveness(governed, check_before_determine, limit=10_000)
    assert not exhaustive.truncated
    assert exhaustive.live is True


# ── The shipped defaults, on the real logs ──


def test_the_stock_default_on_the_permit_log_does_protect_something(
    permits: pl.DataFrame,
) -> None:
    """The reassuring half of the audit, stated as a number rather than a hope.

    At the threshold the pipeline actually ships (`min_edge_cases=25`) all three
    attached rules are violable, so the green-or-red verdict on the shipped
    configuration is about real behaviour. This test is what would fail if a future
    threshold change quietly emptied them out.
    """
    process = miner.mine(permits, name="PermitIntake", min_edge_cases=25).process
    report = rules.apply_derived_rules(permits, process, min_support=100, max_rules=3)

    assert len(report.applied) == 3
    assert len(report.live) == 3, report.summary()
    assert not report.vacuous
    assert not report.unknown
    assert all(report.measured[p.rule_name].violating_states > 0 for p in report.applied)


def test_refusing_vacuous_rules_leaves_the_tight_model_ungoverned(
    permits: pl.DataFrame,
) -> None:
    """The honest outcome of asking for only rules with teeth, on a model that has none.

    Every candidate is either against the start activity or unviolable, so a caller
    that refuses vacuous rules gets a process with zero invariants and nine stated
    reasons. That is a far better artifact than three green invariants forbidding
    nothing, because it cannot be mistaken for a verified control.
    """
    tight = miner.mine(permits, name="Tight", min_edge_cases=200).process
    report = rules.apply_derived_rules(
        permits, tight, min_support=100, max_rules=3, on_vacuous="refuse"
    )

    assert report.applied == []
    assert len(report.skipped) == 9
    assert report.process.invariants == []
    assert rules.UNVIOLABLE in {s.reason for s in report.skipped}


def test_the_second_log_derives_no_rule_that_can_ever_fire() -> None:
    """A negative result worth pinning: on roadfines the mined graph already orders
    every derived precedence topologically, so every attachable rule is vacuous.
    `apply_derived_rules` reports a governed model whose invariants forbid nothing."""
    if not FINES.is_file():
        pytest.skip("needs data/roadfines.xes")

    fines = eventlog.parse_xes(FINES)
    process = miner.mine(fines, name="RoadFines", min_edge_cases=5).process
    report = rules.apply_derived_rules(fines, process, min_support=20, max_rules=2)

    assert report.applied, "rules do attach"
    assert not report.live, "and not one of them can be violated"
    assert len(report.vacuous) == len(report.applied)
