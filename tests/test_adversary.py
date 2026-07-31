"""Tests for the LLM adversarial search over an objective.

Two tiers, and the split is the point rather than a convenience.

**Offline** tests prove the wiring and the adjudication arithmetic with no model at all.
They pin the properties that must hold whatever a model says: that a fabricated candidate
is harmless, that the panel's count is what upholds rather than a proposer's claim, that
the ballot ledger can report a panel which never rejected anything, and that the tool an
adversary calls is a closure rather than a decorated method. Every one of those runs in a
suite with no credentials, which is the reason the LLM half lives outside `objective.py`.

**Live** tests call Bedrock and skip without credentials. They are what measured the two
findings recorded below, and neither was predicted:

The adversaries found a defect in the *deterministic* half. An earlier version of the
enumeration ran in metric space, and five adversaries plus a three-judge panel unanimously
produced empty-answer candidates against a sound objective there. That is correct as far as
it goes — an empty answer scoring the maximum does look like a defect — and it is exactly
wrong, because with free axes the ideal corner *is* an empty answer and every sound
objective would eventually be refused. The LLM half found the flaw and then reproduced it.
Only the space discipline separates the two, and it is deterministic.

The judge panel had a presupposition that inverted on the case that matters. Its first
version told judges to reject "a restatement of the known-good optimum". On the transcript
log the optimum *is* the degenerate answer, so all three judges rejected all ten true
candidates, reasoning that a one-edge model tying the incumbent was merely the incumbent
restated. Measured: 100% rejection where 100% uphold was correct. The prompt now says the
sweep's best point is the incumbent and not a blessed answer, and the same run upholds 3/3
with judges independently back-solving coverage from the score. That is a check that could
not fire, found by running it — which is this session's whole subject arriving one level in.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from pneuma.detect.adversary import (
    ANGLES,
    Attack,
    Ballot,
    Candidate,
    Judged,
    Verdict,
    _brief_text,
    _score_tool,
    adversarial_search,
)
from pneuma.detect.objective import Brief, Domain, Space, Structure, probe

ROOT = Path(__file__).resolve().parents[1]
PERMITS = ROOT / "data" / "receipt.xes"
FLEET = ROOT / "data" / "transcripts_fleet.json"

live = pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE=1 to run the adversarial search for real",
)


def flat_objective(threshold: float) -> float:
    """An objective whose only live term is emptiness. The transcript log's shape, minimal."""
    kept = max(0, 40 - int(round(threshold)))
    return 0.0 if kept == 0 else round(1.0 - kept / 40.0 + 0.001, 4)


AXIS = (Domain("threshold", 1, 40, integral=True, feasible=(1.0, 40.0)),)
STRUCTURE = Structure(
    size=lambda threshold: float(max(0, 40 - int(round(threshold)))),
    units="edges kept",
)


def brief_for(objective=flat_objective) -> Brief:
    """A `Brief` built the way `probe` builds one, so offline tests exercise the real seam."""
    report = probe(objective, AXIS, space=Space.DECISION, structure=STRUCTURE)
    best = report.sweeps[0].best
    assert best is not None
    return Brief(
        objective=objective,
        axes=AXIS,
        space=Space.DECISION,
        ceiling=best.value or 0.0,
        best_point=dict(best.point),
        structure=STRUCTURE,
        samples=report.sweeps[0].samples,
        source=inspect.getsource(objective),
    )


def ballots(*votes: bool) -> tuple[Ballot, ...]:
    return tuple(Ballot(worthless=vote, reason="because") for vote in votes)


# ── Adjudication is a count and a comparison, never a claim ──


def test_a_candidate_needs_both_the_panel_and_the_arithmetic() -> None:
    """The two halves of adjudication, and that neither alone is enough.

    A proposer's own confidence appears nowhere in `upheld`. The panel decides worthlessness
    by counting ballots; `probe` decides whether the score reaches the ceiling by comparing
    numbers. Both must hold, and the truth table is asserted rather than described."""
    brief = brief_for()
    winner = Candidate(point={"threshold": 39.0}, label="one edge", worthless_because="one arrow")
    loser = Candidate(point={"threshold": 1.0}, label="everything", worthless_because="memorises")

    both = Judged(
        candidate=winner, ballots=ballots(True, True, False),
        sample=brief.score(winner.point), ceiling=brief.ceiling,
    )
    assert both.worthless and both.reaches_ceiling and both.upheld

    panel_says_no = Judged(
        candidate=winner, ballots=ballots(True, False, False),
        sample=brief.score(winner.point), ceiling=brief.ceiling,
    )
    assert panel_says_no.reaches_ceiling
    assert not panel_says_no.worthless
    assert not panel_says_no.upheld

    arithmetic_says_no = Judged(
        candidate=loser, ballots=ballots(True, True, True),
        sample=brief.score(loser.point), ceiling=brief.ceiling,
    )
    assert arithmetic_says_no.worthless
    assert not arithmetic_says_no.reaches_ceiling
    assert not arithmetic_says_no.upheld


def test_the_ledger_reports_a_panel_that_never_rejected_anything() -> None:
    """A panel that upheld everything it ever saw is a check that cannot reject, which is the
    defect class this whole module exists against. `rejection_rate` is how that is visible,
    and the report warns rather than leaving the reader to notice."""
    brief = brief_for()
    candidate = Candidate(point={"threshold": 39.0}, label="one edge", worthless_because="x")

    rubber_stamp = Verdict(angles=("emptiness",))
    rubber_stamp.judged.append(
        Judged(candidate=candidate, ballots=ballots(True, True, True),
               sample=brief.score(candidate.point), ceiling=brief.ceiling)
    )
    assert rubber_stamp.rejection_rate == 0.0
    assert "no evidence it can reject" in rubber_stamp.report()

    discriminating = Verdict(angles=("emptiness",))
    discriminating.judged.append(
        Judged(candidate=candidate, ballots=ballots(True, True, False),
               sample=brief.score(candidate.point), ceiling=brief.ceiling)
    )
    assert discriminating.rejection_rate == pytest.approx(1 / 3)
    assert "no evidence it can reject" not in discriminating.report()

    silent = Verdict(angles=("emptiness",))
    assert silent.rejection_rate is None
    assert "no ballot was cast" in silent.report()


def test_the_search_carries_its_own_caps_into_the_report() -> None:
    """No silent caps. Adversary count, candidates per adversary, panel size and the
    agreement threshold are all arguments, and all four are in the text a caller reads."""
    verdict = Verdict(
        angles=("emptiness", "escape"), panel_size=5, min_agreement=3, max_per_angle=7
    )
    text = verdict.report()

    assert "2 adversaries" in text
    assert "emptiness, escape" in text
    assert "at most 7 candidates each" in text
    assert "5 judges needing 3 to uphold" in text


def test_the_five_angles_are_five_different_mandates() -> None:
    """Diversity is doing the work, so it is pinned. Five identical prompts would explore one
    neighbourhood chosen by the prior they share, and the whole reason for a fan-out is to
    cover failure modes redundancy cannot. Each angle is also named after a historical
    failure in `docs/case-study.md` section 10 rather than invented."""
    names = [name for name, _ in ANGLES]
    mandates = [mandate for _, mandate in ANGLES]

    assert len(set(names)) == len(names) == 5
    assert len(set(mandates)) == 5
    assert set(names) == {"emptiness", "escape", "cancellation", "clamp", "tie"}
    for mandate in mandates:
        assert len(mandate) > 100, "a mandate is an instruction, not a label"


# ── What an adversary is shown ──


def test_the_brief_shows_the_source_the_grid_and_the_sizes() -> None:
    """An adversary that saw less than the prober would be searching a different problem.
    Reading the arithmetic is how the clamp and cancellation angles work at all, and 21
    sampled points do not show a clamp."""
    text = _brief_text(brief_for())

    assert "flat_objective" in text, "the source is in the brief"
    assert "threshold" in text
    assert "decision space" in text
    assert "edges kept" in text, "and the structure's units"
    assert "size" in text
    assert "incumbent" in text, "the ceiling is framed as an incumbent, not a blessed answer"


def test_the_brief_says_when_no_source_was_supplied() -> None:
    """A missing source is stated rather than left as an absence an adversary might read as
    "there is nothing to read". `source` is optional and the difference matters to a mandate
    that is about reading arithmetic."""
    brief = brief_for()
    without = Brief(
        objective=brief.objective, axes=brief.axes, space=brief.space,
        ceiling=brief.ceiling, best_point=brief.best_point,
        structure=brief.structure, samples=brief.samples, source=None,
    )
    assert "Not supplied" in _brief_text(without)


def test_the_scoring_tool_is_a_closure_and_not_a_decorated_method() -> None:
    """The trap this module was written around, pinned so a future refactor cannot walk into
    it. `AIFunction` is not a descriptor: `@ai_function` on a method returns the same object
    for the class and every instance, and `self` is stripped from the tool schema, so an
    agent handed it calls with zero arguments and Strands swallows the `TypeError` into a
    tool error. The tool is then permanently dead and the run merely degrades.

    Both halves are checked: the upstream behaviour, so the claim is measured rather than
    quoted, and that `_score_tool` avoids it by closing over the brief."""
    from ai_functions import AIFunction, ai_function

    assert not hasattr(AIFunction, "__get__"), "if this changes, the workaround can be dropped"

    class Trap:
        @ai_function
        def critique(self) -> str:
            """Critique something."""

    assert Trap().critique is Trap.critique, "no per-instance binding, so `self` is lost"

    scorer, _ = _score_tool(brief_for())
    assert scorer.tool_spec["name"] == "score_at"
    assert set(scorer.tool_spec["inputSchema"]["json"]["properties"]) == {"point"}
    assert "self" not in scorer.tool_spec["inputSchema"]["json"]["properties"]


def test_the_scoring_tool_evaluates_the_real_objective_and_reports_a_raise() -> None:
    """It is a search because the tool actually calls the objective. A tool returning a
    description would make every adversary a guesser."""
    _, measure = _score_tool(brief_for())

    assert "score = " in measure({"threshold": 39.0})
    assert str(flat_objective(39.0)) in measure({"threshold": 39.0})
    assert "ceiling" in measure({"threshold": 1.0})

    def brittle(threshold: float) -> float:
        raise ValueError("no terminal state")

    _, raising = _score_tool(
        Brief(
            objective=brittle, axes=AXIS, space=Space.DECISION, ceiling=1.0,
            best_point={"threshold": 1.0},
        )
    )
    assert "raised: ValueError: no terminal state" in raising({"threshold": 1.0})


def test_an_upheld_candidate_becomes_a_degenerate_carrying_its_argument() -> None:
    """The handover to `probe`. The prober's finding has to say *why* an input is worthless,
    not only where it is, and the panel's own reasons are what supply that — a proposer's
    account alone would be the unaudited half."""
    brief = brief_for()
    candidate = Candidate(
        point={"threshold": 39.0}, label="one edge", worthless_because="a single arrow"
    )
    verdict = Verdict(angles=("emptiness",))
    judged = Judged(
        candidate=candidate,
        ballots=(
            Ballot(worthless=True, reason="one transition is not a process"),
            Ballot(worthless=True, reason="replays almost nothing"),
            Ballot(worthless=False, reason="it is a legitimate small model"),
        ),
        sample=brief.score(candidate.point),
        ceiling=brief.ceiling,
    )
    verdict.judged.append(judged)
    verdict._angles[id(judged)] = "emptiness"

    (degenerate,) = verdict.degenerates()
    assert degenerate.found_by == "adversary/emptiness"
    assert "a single arrow" in degenerate.worthless_because
    assert "one transition is not a process" in degenerate.worthless_because
    assert "panel 2/3" in degenerate.worthless_because
    assert "legitimate small model" not in degenerate.worthless_because, "yes votes only"


def test_a_rejected_candidate_reaches_the_prober_not_at_all() -> None:
    """A panel that rejects has to actually stop the candidate, or its verdict is decoration."""
    brief = brief_for()
    candidate = Candidate(point={"threshold": 39.0}, label="x", worthless_because="y")
    verdict = Verdict(angles=("emptiness",))
    judged = Judged(
        candidate=candidate, ballots=ballots(True, False, False),
        sample=brief.score(candidate.point), ceiling=brief.ceiling,
    )
    verdict.judged.append(judged)
    verdict._angles[id(judged)] = "emptiness"

    assert verdict.degenerates() == []
    assert "[rejected]" in verdict.report()


def test_an_adversary_that_raises_does_not_end_the_search() -> None:
    """One dead adversary out of five must cost one angle, not the run. The error is recorded
    rather than swallowed, because an angle that never ran is an angle that found nothing for
    a reason the report should say."""
    verdict = Verdict(angles=("emptiness", "escape"))
    verdict.errors.append(("escape", "ThrottlingException: too many requests"))
    verdict.searches.append(("emptiness", "swept the whole range, found nothing"))

    text = verdict.report()
    assert "escape failed: ThrottlingException" in text
    assert "emptiness searched: swept the whole range" in text


def test_an_empty_attack_is_a_real_answer() -> None:
    """A negative result from an adversary is a finding about the objective, so the schema
    admits it and the report carries the account of the search rather than only its hits."""
    empty = Attack(candidates=[], searched="swept 1..40, nothing ties the ceiling")

    assert empty.candidates == []
    assert "nothing ties" in empty.searched


# ── The search plugs into `probe` without a model ──


def test_a_search_that_finds_nothing_leaves_the_probe_where_it_was() -> None:
    """The seam, exercised with a searcher that is a function rather than a model. `probe`
    must behave identically with a search that returns nothing and with no search at all,
    or the search is affecting the verdict by being present."""
    def finds_nothing(brief: Brief) -> list:
        return []

    def peaked(threshold: float) -> float:
        return 1.0 - abs(threshold - 20.0) / 100.0

    without = probe(peaked, AXIS, space=Space.DECISION, structure=STRUCTURE)
    with_search = probe(
        peaked, AXIS, space=Space.DECISION, structure=STRUCTURE, search=finds_nothing
    )

    assert without.ok and with_search.ok
    assert {f.check for f in without.findings} == {f.check for f in with_search.findings}
    assert "adversarial search proposed 0 candidate" in with_search.report()


def test_adversarial_search_returns_a_callable_probe_accepts() -> None:
    """`adversarial_search` is a factory, so its shape is checkable without a model call."""
    search = adversarial_search(angles=ANGLES[:1], max_per_angle=1, panel_size=1)
    assert callable(search)


# ── Live: what the adversaries actually found ──


@live
@pytest.mark.skipif(not FLEET.is_file(), reason="needs data/transcripts_fleet.json")
def test_live_the_panel_upholds_the_transcript_logs_real_degenerate() -> None:
    """The measurement that fixed the judge prompt, kept as the regression for it.

    The panel's first version rejected all ten true candidates here, reasoning that a
    one-edge model tying the incumbent was the incumbent restated. That inverts on exactly
    the case that matters, because when the optimum is itself degenerate, tying it is the
    finding. After the fix the same run upholds unanimously."""
    from pneuma.casestudy import transcriptlog
    from pneuma.casestudy.minelearn import Attempt, threshold_objective
    from pneuma.model import opus5

    events, _ = transcriptlog.load_sample(FLEET)
    objective, structure, top, _components = threshold_objective(events)
    verdicts: list[Verdict] = []
    search = adversarial_search(
        angles=ANGLES[:2], max_per_angle=2, model=opus5("high"), on_verdict=verdicts.append
    )

    probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
        search=search,
        source=inspect.getsource(Attempt.score.fget),
    )

    (verdict,) = verdicts
    assert verdict.judged, "the adversaries proposed something"
    assert verdict.upheld, "and the panel upheld a real degenerate rather than rejecting it"


@live
@pytest.mark.skipif(not PERMITS.is_file(), reason="needs data/receipt.xes")
def test_live_the_panel_rejects_on_a_sound_objective() -> None:
    """The control, and it is the more important of the two. A panel that upheld everything
    would refuse every objective and be worthless in the way a check that cannot fire is.

    Measured on the permit log, where nothing is degenerate: the adversaries proposed six
    candidates, several of them scoring *above* the reported ceiling, and the panel rejected
    all eighteen ballots because a model keeping nine frequent handoffs and replaying most
    cases is a real answer whatever its score."""
    from pneuma.casestudy import eventlog
    from pneuma.casestudy.minelearn import Attempt, threshold_objective
    from pneuma.model import opus5

    events = eventlog.parse_xes(PERMITS)
    objective, structure, top, _components = threshold_objective(events)
    verdicts: list[Verdict] = []
    search = adversarial_search(
        angles=ANGLES, max_per_angle=3, model=opus5("high"), on_verdict=verdicts.append
    )

    report = probe(
        objective,
        (Domain("threshold", 1, top, integral=True, feasible=(1.0, float(top))),),
        space=Space.DECISION,
        structure=structure,
        search=search,
        source=inspect.getsource(Attempt.score.fget),
    )

    (verdict,) = verdicts
    assert not verdict.upheld, verdict.report()
    assert verdict.rejection_rate == 1.0, verdict.report()
    assert report.ok, report.report()
