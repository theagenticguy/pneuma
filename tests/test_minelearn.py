"""Tests for the learned-miner objective and its feedback.

Every test here pins a failure that a live run actually produced. The mechanism —
backprop over a text parameter — worked on the first attempt; what went wrong twice was
the objective and then the feedback, and neither failure raised anything. Both looked
like a training loop reporting rounds.
"""

from __future__ import annotations

from pneuma.casestudy.minelearn import Attempt, feedback_for


def attempt(**overrides: object) -> Attempt:
    base: dict[str, object] = {
        "index": 0,
        "coverage": 0.9,
        "matched_coverage": 0.95,
        "threshold": 5,
        "states": 17,
        "edges": 29,
        "guidance_chars": 100,
        "edge_share": 0.29,
    }
    return Attempt(**{**base, **overrides})  # type: ignore[arg-type]


# ── The objective must not have a degenerate optimum ──


def test_keeping_every_handoff_scores_zero() -> None:
    """The first live run maximised coverage by driving the threshold to 1, keeping all
    99 distinct handoffs including 30 walked by a single case. It scored 98.6% coverage
    and described no process. Perfect memorisation must score zero, not near-perfect."""
    memorised = attempt(coverage=1.0, edge_share=1.0)
    assert memorised.score == 0.0


def test_the_score_rewards_abstraction_over_raw_coverage() -> None:
    """A model with less coverage and far fewer edges should win, or the objective is
    just coverage with extra steps."""
    broad = attempt(coverage=0.988, edge_share=0.70)  # threshold 2
    tight = attempt(coverage=0.844, edge_share=0.13)  # threshold 50
    assert tight.score > broad.score


def test_the_score_still_punishes_a_model_that_covers_nothing() -> None:
    """Selectivity alone is equally degenerate: keep one edge, cover almost no cases."""
    empty = attempt(coverage=0.02, edge_share=0.01)
    balanced = attempt(coverage=0.898, edge_share=0.20)
    assert balanced.score > empty.score


# ── The feedback must be two-sided ──


def test_feedback_reports_the_score_and_the_direction() -> None:
    """The second live run failed here. The feedback only complained about memorisation
    above 60% edge share, so at 29% the agent heard nothing but "you are behind on
    coverage" — it loosened the threshold every round and walked its score from 0.804
    down to 0.706, away from its own best attempt. An optimizer cannot climb a hill it
    is not told the height of."""
    regressed = attempt(coverage=0.969, edge_share=0.44)
    message = feedback_for(regressed, best_so_far=0.804)

    assert f"{regressed.score:.3f}" in message
    assert "0.804" in message
    assert "moved backwards" in message


def test_feedback_says_so_when_the_attempt_is_the_best_yet() -> None:
    message = feedback_for(attempt(coverage=0.898, edge_share=0.20), best_so_far=0.70)
    assert "best yet" in message


def test_feedback_names_memorisation_when_edge_share_is_high() -> None:
    message = feedback_for(attempt(coverage=1.0, edge_share=0.95), best_so_far=0.8)
    assert "memorising the log" in message


def test_a_losing_attempt_is_told_that_tighter_is_worth_testing() -> None:
    """Without this the agent only ever hears that it is behind on coverage, and the
    only lever it reaches for is a looser threshold."""
    message = feedback_for(attempt(coverage=0.90, edge_share=0.29), best_so_far=0.90)
    assert "tighter is worth testing" in message


def test_feedback_works_before_there_is_a_best_score() -> None:
    """Round zero has no history, and the message must not read as a regression."""
    message = feedback_for(attempt(), best_so_far=None)
    assert "moved backwards" not in message
    assert "best so far" not in message
