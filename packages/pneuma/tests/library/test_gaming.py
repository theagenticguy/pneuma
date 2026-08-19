"""Tests for the gate-fitting and duplicate-mechanism probes' own mechanics.

Every gate, held-out evaluation, and checker here is written out in this module — a lambda
or a closure over plain data, no models, no pneuma application imports — so the probes'
mechanics are checked with everything else absent, per the same discipline as
`test_objective.py`.

The planted cases are the ground truth: a length-only gate against a disjoint held-out
scorer (the gate is gameable by padding), and a checker that accepts only paraphrases of
one sentence (its accepted set is one mechanism). Each probe is pinned at all three
verdicts — works (True), decoration (False), and could-not-tell (None) naming the limit —
because a probe that cannot produce one of the three would be a check that cannot fire,
which is the defect class the module under test exists to catch. Every planted invariant
here was broken once during authoring to prove its guard test can fail; the packet's work
log records each break.
"""

from __future__ import annotations

import math

import pytest

from pneuma.detect.gaming import (
    DEFAULT_EDGE_FRACTION,
    NEAR_DUPLICATE_JACCARD,
    DuplicateMechanisms,
    GateFitting,
    probe_duplicate_mechanisms,
    probe_gate_fitting,
    token_set_jaccard,
)

# ── The planted gate-fitting fixture ──
#
# Candidates are answer strings. The held-out evaluation counts how many of five keywords
# the answer actually contains; the gameable gate scores length alone, so a long string of
# padding maximises the gate while containing nothing.

KEYWORDS = ("coverage", "threshold", "handoff", "selectivity", "witness")


def held_out_keywords(candidate: object) -> float:
    """The evaluation the gate is supposed to be a proxy for: substance, not size."""
    text = str(candidate).lower()
    return sum(keyword in text for keyword in KEYWORDS) / len(KEYWORDS)


def gameable_length_gate(candidate: object) -> float:
    """The planted defect: rewards length, which padding buys for free."""
    return float(len(str(candidate)))


def honest_gate(candidate: object) -> float:
    """A gate that measures the same substance the held-out evaluation does."""
    return held_out_keywords(candidate)


GOOD_ANSWER = "coverage rises when the threshold keeps a handoff; selectivity is the witness"
PADDED_JUNK = "x" * (2 * len(GOOD_ANSWER))
SHORT_JUNK = "nothing here"

POOL = (GOOD_ANSWER, PADDED_JUNK, SHORT_JUNK, "threshold and witness", "coverage handoff")


# ── probe_gate_fitting: the three verdicts ──


def test_a_length_only_gate_is_reported_as_gate_fittable() -> None:
    """Decoration. The padded junk string maximises the gate and scores zero held-out, so
    the gate rewards fitting the gate and `discriminates` is False — with the exploit
    named, because a finding without the input that produced it is not actionable."""
    result = probe_gate_fitting(gameable_length_gate, held_out_keywords, POOL)

    assert result.gamed
    assert result.discrimination.discriminates is False
    assert any(e.candidate == PADDED_JUNK for e in result.exploits), result.report()
    assert not result.withheld or result.discrimination.withheld == ()


def test_an_honest_gate_that_contains_worthless_candidates_works() -> None:
    """Works. The honest gate scores substance, so every near-minimum held-out candidate
    sits below the gate's top band and each one is a separating observation."""
    result = probe_gate_fitting(honest_gate, held_out_keywords, POOL)

    assert not result.gamed
    assert result.discrimination.discriminates is True
    assert result.contained >= 1, result.report()


def test_a_truncated_pool_with_no_exploit_is_unsettled_and_names_the_budget() -> None:
    """Could-not-tell. A budget that cuts the pool before the gameable candidate is seen
    must not read as a finding of soundness; the bound is named in `withheld`."""
    honest_only = (GOOD_ANSWER, "threshold and witness", "coverage handoff", SHORT_JUNK)
    result = probe_gate_fitting(
        gameable_length_gate, held_out_keywords, iter(honest_only + (PADDED_JUNK,)), budget=2
    )

    assert result.discrimination.discriminates is None
    assert any("budget=2" in reason for reason in result.discrimination.withheld), result.report()


def test_an_exploit_settles_the_verdict_even_when_the_budget_was_also_hit() -> None:
    """The module docstring's first invariant: truncation cannot fake a positive witness,
    so a found exploit reports False rather than hiding behind the bound."""
    result = probe_gate_fitting(
        gameable_length_gate, held_out_keywords, iter(POOL * 3), budget=len(POOL)
    )

    assert result.gamed
    assert result.discrimination.discriminates is False
    assert result.discrimination.withheld == ()


def test_a_flat_gate_cannot_be_banded_and_says_so() -> None:
    """A gate scoring everything identically has no top band to draw. That is unsettled
    with the flatness named, not a pass and not a finding of decoration."""
    result = probe_gate_fitting(lambda _: 1.0, held_out_keywords, POOL)

    assert result.discrimination.discriminates is None
    assert any("identically" in reason for reason in result.discrimination.withheld)


def test_unscorable_candidates_are_counted_and_named() -> None:
    """A held-out evaluation that raises on some candidates must surface in `withheld`,
    because a silently skipped candidate is a silently shrunk search."""

    def brittle_held_out(candidate: object) -> float:
        if candidate == PADDED_JUNK:
            raise ValueError("cannot score padding")
        return held_out_keywords(candidate)

    result = probe_gate_fitting(gameable_length_gate, brittle_held_out, POOL)

    assert any("could not be scored" in reason for reason in result.withheld), result.report()


def test_a_non_finite_gate_score_is_unscorable_not_an_exploit() -> None:
    """An infinite gate score would otherwise be the top of every band."""
    result = probe_gate_fitting(
        lambda c: math.inf if c == PADDED_JUNK else float(len(str(c))),
        held_out_keywords,
        POOL,
    )

    assert not any(e.candidate == PADDED_JUNK for e in result.exploits)
    assert any("could not be scored" in reason for reason in result.withheld)


def test_gate_fitting_rejects_a_degenerate_band_or_budget() -> None:
    with pytest.raises(ValueError, match="edge_fraction"):
        probe_gate_fitting(honest_gate, held_out_keywords, POOL, edge_fraction=0.5)
    with pytest.raises(ValueError, match="budget"):
        probe_gate_fitting(honest_gate, held_out_keywords, POOL, budget=0)


def test_the_gate_fitting_bands_are_scale_free() -> None:
    """The same pool judged by the same gate scaled 10000x must produce the same verdict,
    or the threshold is absolute and the probe only works in one unit system."""
    plain = probe_gate_fitting(gameable_length_gate, held_out_keywords, POOL)
    scaled = probe_gate_fitting(
        lambda c: 10_000.0 * gameable_length_gate(c), held_out_keywords, POOL
    )

    assert plain.discrimination.discriminates is scaled.discrimination.discriminates is False


# ── The planted duplicate-mechanism fixture ──
#
# A checker that only accepts paraphrases of one sentence: every accept shares nearly all
# its tokens with every other accept, so the accepted set is materially one mechanism.

ONE_MECHANISM = (
    "the threshold keeps every handoff and coverage rises",
    "the threshold keeps every handoff and coverage rises fast",
    "coverage rises and the threshold keeps every handoff",
)

TWO_MECHANISMS = ONE_MECHANISM + ("witness counts fall when selectivity gates the model",)


def accepts_all(item: object) -> bool:
    del item
    return True


# ── probe_duplicate_mechanisms: the three verdicts ──


def test_a_checker_whose_accepts_are_paraphrases_is_decoration() -> None:
    """Decoration. Every accepted pair is at or above the Jaccard threshold, so the
    checker admits one mechanism wearing three coats and `discriminates` is False."""
    result = probe_duplicate_mechanisms(accepts_all, ONE_MECHANISM)

    assert result.pairs == 3
    assert result.diverse == 0
    assert result.discrimination.discriminates is False
    assert result.most_distant is not None
    assert result.most_distant[2] >= NEAR_DUPLICATE_JACCARD, result.report()


def test_a_checker_admitting_a_second_mechanism_works() -> None:
    """Works. One genuinely different accept puts pairs below the threshold, and each
    such pair is a separating observation."""
    result = probe_duplicate_mechanisms(accepts_all, TWO_MECHANISMS)

    assert result.diverse >= 1
    assert result.discrimination.discriminates is True


def test_fewer_than_two_accepts_is_unsettled_and_names_the_thin_set() -> None:
    """Could-not-tell. One accept has no pair to compare, so diversity is unmeasurable;
    the verdict is None with the thin accepted set named, never a quiet pass."""
    result = probe_duplicate_mechanisms(lambda item: item == ONE_MECHANISM[0], TWO_MECHANISMS)

    assert result.accepted == 1
    assert result.discrimination.discriminates is None
    assert any("1 item(s) accepted" in reason for reason in result.discrimination.withheld)


def test_a_truncated_item_pool_is_unsettled_and_names_the_budget() -> None:
    """Could-not-tell. Near-duplicate accepts under a budget that cut the pool must not
    read as a finding of decoration: the diverse item may be in the unexamined tail."""
    result = probe_duplicate_mechanisms(accepts_all, iter(TWO_MECHANISMS), budget=3)

    assert result.diverse == 0
    assert result.discrimination.discriminates is None
    assert any("budget=3" in reason for reason in result.discrimination.withheld), result.report()


def test_a_raising_checker_is_counted_and_named() -> None:
    """An item the checker cannot judge counts neither way and lands in `withheld`."""

    def brittle(item: object) -> bool:
        if item == TWO_MECHANISMS[-1]:
            raise RuntimeError("cannot judge this one")
        return True

    result = probe_duplicate_mechanisms(brittle, TWO_MECHANISMS)

    assert result.errors == 1
    assert result.discrimination.discriminates is None
    assert any("raised on 1" in reason for reason in result.discrimination.withheld)


def test_a_caller_supplied_similarity_replaces_the_default() -> None:
    """The seam: a similarity that calls everything identical turns the diverse set into
    decoration, which proves the default was actually replaced."""
    result = probe_duplicate_mechanisms(
        accepts_all, TWO_MECHANISMS, similarity=lambda left, right: 1.0
    )

    assert result.discrimination.discriminates is False


def test_duplicate_mechanisms_rejects_a_degenerate_threshold_or_budget() -> None:
    with pytest.raises(ValueError, match="threshold"):
        probe_duplicate_mechanisms(accepts_all, TWO_MECHANISMS, threshold=0.0)
    with pytest.raises(ValueError, match="budget"):
        probe_duplicate_mechanisms(accepts_all, TWO_MECHANISMS, budget=0)


# ── The default similarity's own contract ──


def test_token_set_jaccard_ignores_case_punctuation_and_order() -> None:
    assert token_set_jaccard("The Threshold, kept!", "kept the threshold") == 1.0


def test_token_set_jaccard_separates_disjoint_texts_and_handles_empty() -> None:
    assert token_set_jaccard("alpha beta", "gamma delta") == 0.0
    assert token_set_jaccard("", "") == 1.0
    assert token_set_jaccard("alpha", "") == 0.0


def test_the_planted_fixtures_sit_on_the_right_side_of_the_threshold() -> None:
    """Guard the fixtures: if an edit to ONE_MECHANISM drifted a pair below 0.85, the
    decoration test above would fail for a fixture reason and point at the wrong code."""
    paraphrase_floor = min(
        token_set_jaccard(left, right)
        for i, left in enumerate(ONE_MECHANISM)
        for right in ONE_MECHANISM[i + 1 :]
    )
    outsider_ceiling = max(token_set_jaccard(text, TWO_MECHANISMS[-1]) for text in ONE_MECHANISM)
    assert paraphrase_floor >= NEAR_DUPLICATE_JACCARD, paraphrase_floor
    assert outsider_ceiling < NEAR_DUPLICATE_JACCARD, outsider_ceiling


# ── The result types stay frozen, like every probe result in detect/ ──


def test_the_result_dataclasses_are_frozen() -> None:
    gate = probe_gate_fitting(honest_gate, held_out_keywords, POOL)
    dup = probe_duplicate_mechanisms(accepts_all, ONE_MECHANISM)
    assert isinstance(gate, GateFitting) and isinstance(dup, DuplicateMechanisms)
    for result in (gate, dup):
        with pytest.raises(AttributeError):
            result.subject = "mutated"  # type: ignore[misc]


def test_the_reports_render_the_verdict_and_the_evidence() -> None:
    """The report is what a human reads; it must carry the verdict word and the witness."""
    gamed = probe_gate_fitting(gameable_length_gate, held_out_keywords, POOL)
    assert "DOES NOT DISCRIMINATE" in gamed.report()
    assert "exploit" in gamed.report()

    dup = probe_duplicate_mechanisms(accepts_all, ONE_MECHANISM)
    assert "DOES NOT DISCRIMINATE" in dup.report()
    assert "most distant accepted pair" in dup.report()

    assert DEFAULT_EDGE_FRACTION < 0.5  # the band constant the docstrings promise
