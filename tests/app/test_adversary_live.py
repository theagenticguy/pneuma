"""The live adversarial search, against the objectives the application actually optimises.

`tests/library/test_adversary.py` holds the offline tier: the adjudication arithmetic and the
wiring, provable with no model and no credentials. These are the other tier. They call Bedrock,
skip without `PNEUMA_LIVE=1`, and they need a real mined objective because both findings they
record depend on one.

The judge panel's first version told judges to reject "a restatement of the known-good
optimum". On the transcript log the optimum *is* the degenerate answer, so all three judges
rejected all ten true candidates: 100% rejection where 100% uphold was correct. The prompt now
says the sweep's best point is the incumbent rather than a blessed answer. Holding both logs
here is what keeps that honest — the panel must uphold on the transcript log and reject on the
permit log, or it is agreeing with whoever asked last.
"""

from __future__ import annotations

import inspect
import os

import pytest
from paths import FLEET, PERMITS, needs_fleet, needs_permits

from pneuma.detect.adversary import ANGLES, Verdict, adversarial_search
from pneuma.detect.objective import Domain, Space, probe

live = pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE=1 to run the adversarial search for real",
)


@live
@needs_fleet
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
@needs_permits
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
