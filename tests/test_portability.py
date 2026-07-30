"""Can a second, unrelated process go through the pipeline with no code changes?

`data/roadfines.xes` is a road-traffic-fines log: different domain, different
activities, 100 cases instead of 1,434. Nothing in `pneuma.process` or
`pneuma.casestudy.rules` mentions either process by name, and these tests are the
claim that the portability is real rather than asserted.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from pneuma.casestudy import eventlog, miner, pipeline, rules
from pneuma.process import interpreter, tla

PERMITS = Path(__file__).resolve().parents[1] / "data" / "receipt.xes"
FINES = Path(__file__).resolve().parents[1] / "data" / "roadfines.xes"
pytestmark = pytest.mark.skipif(
    not (PERMITS.is_file() and FINES.is_file()), reason="needs both logs in data/"
)


@pytest.fixture(scope="module")
def fines() -> pl.DataFrame:
    return eventlog.parse_xes(FINES)


@pytest.fixture(scope="module")
def permits() -> pl.DataFrame:
    return eventlog.parse_xes(PERMITS)


# ── The generic path: mine, derive, verify, execute ──


def test_a_second_process_mines_with_no_code_changes(fines: pl.DataFrame) -> None:
    discovery = miner.mine(fines, name="RoadFines", min_edge_cases=5)
    assert len(discovery.process.states) == 6
    assert discovery.coverage > 0.85
    assert discovery.process.unreachable_states() == set()


def test_rules_are_derived_from_whichever_log_you_pass(
    permits: pl.DataFrame, fines: pl.DataFrame
) -> None:
    """The rule comes from the data, so two logs yield two different rule sets."""
    permit_rules = rules.derive_precedences(permits, min_support=100)
    fine_rules = rules.derive_precedences(fines, min_support=20)

    assert permit_rules and fine_rules
    assert {p.before for p in permit_rules}.isdisjoint({f.before for f in fine_rules})

    # The permit rule I originally hardcoded is recovered by the generic scan.
    recovered = [
        p
        for p in permit_rules
        if p.before == pipeline.CHECK_ACTIVITY and p.after == pipeline.DETERMINE_ACTIVITY
    ]
    assert recovered, "the generic scan should find the rule the case study hardcoded"
    assert recovered[0].cases == 1303


def test_derived_rules_attach_and_verify_on_both_processes(
    permits: pl.DataFrame, fines: pl.DataFrame
) -> None:
    for events, threshold, support in ((permits, 25, 100), (fines, 5, 20)):
        discovery = miner.mine(events, name="P", min_edge_cases=threshold)
        governed, applied = rules.apply_derived_rules(
            events, discovery.process, min_support=support, max_rules=2
        )
        assert applied, "no rule was attached"
        assert len(governed.invariants) == len(applied)
        # Every invariant names a state the model actually contains.
        known = {s.name for s in governed.states}
        assert all(i.forbidden_state in known for i in governed.invariants)


async def test_a_new_process_executes_with_no_handlers_registered(
    fines: pl.DataFrame,
) -> None:
    """The interpreter needs no per-process code: handlers are an enrichment."""
    from pneuma.casestudy import handlers

    discovery = miner.mine(fines, name="RoadFines", min_edge_cases=5)
    governed, _ = rules.apply_derived_rules(fines, discovery.process, min_support=20, max_rules=2)
    assert handlers.coverage(governed) == (0, 6), "no handler should match this process"

    async def take_first(
        _state: str, enabled: list[interpreter.Transition], _v: dict[str, int | str]
    ) -> str:
        return enabled[0].name

    run = await interpreter.run(governed, take_first, max_steps=12)
    assert governed.state_map[run.final_state].terminal


# ── The bug this module exists to prevent ──


def test_the_hardcoded_rule_is_vacuous_on_another_process(fines: pl.DataFrame) -> None:
    """`pipeline.governed` names two permit activities, so on any other log it
    attaches an invariant about a state that does not exist. Nothing raises; the
    verification passes; the rule protects nothing. That is the failure mode
    `rules.py` replaces, and this test pins it so the difference stays visible."""
    discovery = miner.mine(fines, name="RoadFines", min_edge_cases=5)
    hardcoded = pipeline.governed(discovery.process)

    known = {s.name for s in hardcoded.states}
    assert hardcoded.invariants[0].forbidden_state not in known
    assert not any(t.effects for t in hardcoded.transitions), "no effect ever sets the flag"


def test_enforce_refuses_a_rule_whose_prerequisite_is_the_start_state(
    fines: pl.DataFrame,
) -> None:
    """Every case begins at the initial state, so the log reports those precedences at
    100% — but the flag is still 0 on the opening move, so the invariant would fire on
    step 1 of a correct process. The strongest precedences in any log are against the
    start activity, so this guard runs on real input, not a contrived case."""
    discovery = miner.mine(fines, name="RoadFines", min_edge_cases=5)
    start_activity = next(
        s.description for s in discovery.process.states if s.name == discovery.process.initial_state
    )
    against_start = [
        p for p in rules.derive_precedences(fines, min_support=20) if p.before == start_activity
    ]
    assert against_start, "expected at least one precedence rooted at the start activity"
    assert rules.enforce(discovery.process, against_start[0]) is discovery.process


@pytest.mark.skipif(not tla.tlc_available(), reason="needs java and tools/tla2tools.jar")
def test_tlc_checks_a_derived_rule_on_the_second_process(fines: pl.DataFrame) -> None:
    discovery = miner.mine(fines, name="RoadFines", min_edge_cases=5)
    governed, applied = rules.apply_derived_rules(
        fines, discovery.process, min_support=20, max_rules=2
    )
    result = tla.check(governed, timeout=250)
    assert result.ok, result.raw[-1200:]
    assert result.distinct_states >= len(governed.states)
