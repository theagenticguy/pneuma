"""Tests for the learned miner: its objective, its feedback, and its `Procedural` toolkit.

Every objective test here pins a failure that a live run actually produced. The
mechanism — backprop over a text parameter — worked on the first attempt; what went
wrong twice was the objective and then the feedback, and neither failure raised
anything. Both looked like a training loop reporting rounds.

## What the toolkit tests prove offline, and what needs a live run

The toolkit half is a `Procedural` parameter, and almost all of it is verifiable with
no model call at all, because the properties that would fail *silently* are properties
of the plumbing rather than of a model's taste.

Offline, against a real `LocalPythonExecutorTool` and a `ScriptedModel`:

- The toolkit is executed at sandbox setup, so its helpers are callable — not merely
  present as a string. Asserted by having the scripted agent call one.
- The runtime advertises each helper by signature and docstring in the prompt
  preamble, which is how an agent finds a helper the prompt never names.
- Comments and module docstrings in the toolkit are **not** advertised. This is the
  measurement that decides prose and code must be separate parameters, so it is
  asserted rather than believed.
- The recalled value arrives as a call argument, reaches the reconstructed graph as a
  `ParameterNode` with `procedural=True` and `requires_grad=True`, and so is a genuine
  gradient target. Passing it interpolated instead drops the edge, and that is
  asserted too, because the failure is silent.
- A rewrite persists across rounds: what round one's consolidation stored is what
  round two's sandbox defines.
- Every way a bad rewrite can break the sandbox degrades to a rollback rather than
  destroying accumulated progress — and the rollback is recorded, not hidden.
- `io` is not an authorised import, which is the specific trap `load_log` exists for.

Live, needing Bedrock (marked, skipped without credentials):

- Whether a real backward model routes code feedback to the code parameter and
  judgment feedback to the prose one. The crosstalk question is a question about the
  model, and no fake can answer it. `test_live_two_parameters_receive_distinct_gradients`
  is the shape that measurement takes.
- Whether the agent's use of the accumulated toolkit beats the seed toolkit's own
  mechanical argmax. `test_live_toolkit_beats_its_own_seed_baseline` states the
  baseline and skips by default.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from ai_functions import Procedural
from ai_functions.optimizer._graph import build_graph_from_result
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from ai_functions.tools.local_python_executor import (
    LocalPythonExecutorTool,
    procedural_signatures,
)
from ai_functions.types.graph import GradFeedback
from pydantic import BaseModel, Field

from pneuma.casestudy import eventlog
from pneuma.casestudy.aimine import ANALYSIS_IMPORTS, Discovered, Edge, grade, to_csv
from pneuma.casestudy.minelearn import (
    Attempt,
    Guidance,
    LearningMiner,
    Training,
    check_toolkit,
    feedback_for,
    rehearse,
    score_edges,
    visible_handoffs,
)
from pneuma.casestudy.miner import directly_follows
from pneuma.casestudy.toolkit import (
    BOOTSTRAP,
    REHEARSAL_LOG,
    SEED_TOOLKIT,
    missing_bootstrap,
    rehearsal_probe,
)
from pneuma.memory import TursoMemoryBackend

LOG = Path(__file__).resolve().parents[1] / "data" / "receipt.xes"

_LIVE = os.environ.get("PNEUMA_LIVE_MINE") == "1"
_live = pytest.mark.skipif(
    not _LIVE,
    reason="needs Bedrock credentials; set PNEUMA_LIVE_MINE=1 to measure gradient routing",
)


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


def discovered(pairs: list[tuple[str, str]], *, threshold: int = 1) -> Discovered:
    return Discovered(
        start_activity=pairs[0][0],
        terminal_activities=[pairs[-1][1]],
        edges=[Edge(source=s, target=t, cases=1) for s, t in pairs],
        threshold_used=threshold,
        method="test",
    )


# ── The objective must not have a degenerate optimum ──


def test_keeping_every_handoff_scores_zero() -> None:
    """The first live run maximised coverage by driving the threshold to 1, keeping every
    distinct handoff including those walked by a single case. It scored 98.6% coverage and
    described no process. Perfect memorisation must score zero, not near-perfect.

    This case was unreachable while the denominator was the full log's 99 handoffs and the
    agent only ever saw a 400-case sample's 69: keeping everything it was shown measured
    0.697. Now that both sides count the same population, share 1.0 is exactly what a
    threshold-of-1 model measures, so the test pins a state a run can actually enter."""
    memorised = attempt(coverage=1.0, edge_share=1.0)
    assert memorised.score == 0.0


def test_the_score_never_exceeds_the_honest_maximum() -> None:
    """`edge_share` is `edges the agent returned / distinct handoffs in the log`, and
    nothing constrained the numerator to be a subset of the denominator, so share > 1 was
    reachable. Past 1 the selectivity term goes negative and the harmonic mean stops being
    a mean: it becomes a rational function whose value is unbounded. On the honest domain
    the maximum is 1.0, so no input may score above it."""
    for share in (1.0, 1.05, 1.2, 1.5, 1.8687, 2.0, 5.0, 40.0):
        scored = attempt(coverage=0.864, edge_share=share)
        assert scored.score <= 1.0, f"edge_share={share} scored {scored.score}"


def test_the_division_pole_is_unreachable() -> None:
    """`coverage + selectivity` hits zero at `edge_share == 1 + coverage`, and the guard
    tested it with exact float equality so it never fired near the pole. At coverage
    0.864 a live model measured 1.34e+16 on one side and -149297.472 on the other. Sweep
    across the pole densely: nothing may leave the honest range."""
    coverage = 0.864
    pole = 1.0 + coverage
    for offset in (-1e-2, -1e-3, -1e-5, -1e-9, 0.0, 1e-9, 1e-5, 1e-3, 1e-2):
        scored = attempt(coverage=coverage, edge_share=pole + offset)
        assert -1.0 <= scored.score <= 1.0, f"edge_share={pole + offset!r} scored {scored.score}"


def test_a_hallucinating_model_cannot_set_a_record() -> None:
    """The concrete failing case: 185 returned edges against the log's 99 real handoffs
    at 86.4% coverage scored 319.386. `Training.best` selected it, and the feedback told
    the optimizer it was a record while scolding it for memorising in the same sentence.
    An honest attempt scoring 0.861 must still win."""
    hallucinated = attempt(coverage=0.864, edges=185, edge_share=round(185 / 99, 4))
    honest = attempt(coverage=0.8438, edges=13, edge_share=0.1313)

    assert honest.score > hallucinated.score
    assert honest.score == pytest.approx(0.8561, abs=5e-4)


def test_an_invented_edge_ranks_below_perfect_memorisation() -> None:
    """Memorising is keeping every real handoff; inventing is returning a handoff no case
    ever walked, which puts behaviour in the model that the log does not support. That is
    the worse defect, so it has to sort below the memoriser's zero."""
    memorised = attempt(coverage=1.0, edges=99, edge_share=1.0, invented_edges=0)
    inventing = attempt(coverage=1.0, edges=99, edge_share=1.0, invented_edges=1)
    assert inventing.score < memorised.score


def test_inventing_more_edges_scores_worse() -> None:
    few = attempt(coverage=0.9, edges=100, edge_share=0.5, invented_edges=5)
    many = attempt(coverage=0.9, edges=100, edge_share=0.5, invented_edges=60)
    assert many.score < few.score


def test_best_does_not_select_a_hallucinating_round() -> None:
    """`Training.best` is what picks the round whose approach is kept, and it picked the
    319.386 attempt on score alone."""
    honest = attempt(index=0, coverage=0.8438, edges=13, edge_share=0.1313)
    hallucinated = attempt(index=1, coverage=0.864, edges=185, edge_share=1.0, invented_edges=86)
    training = Training(attempts=[honest, hallucinated])

    assert training.best is honest


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


# ── Numerator and denominator must describe the same population ──


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_edge_share_is_measured_against_handoffs_the_agent_could_see() -> None:
    """The loop sent the agent a 400-case sample but divided by the 1434-case log's 99
    distinct handoffs. The sample contains 69 non-self handoffs, so an agent that returned
    every single handoff it was shown measured 0.697 rather than 1.0 — selectivity was
    systematically overstated by construction, and no input could reach share 1."""
    events = eventlog.parse_xes(LOG)
    shown = visible_handoffs(to_csv(events, sample_cases=400))
    real = shown.filter(pl.col("activity") != pl.col("next_activity"))
    assert real.height == 69
    assert directly_follows(events).height == 99

    every_visible = [(r["activity"], r["next_activity"]) for r in real.iter_rows(named=True)]
    scored = score_edges(discovered(every_visible), visible_handoffs=shown)

    assert scored.edge_share == 1.0
    assert scored.invented == 0


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_an_edge_no_case_ever_walked_is_counted_as_invented() -> None:
    events = eventlog.parse_xes(LOG)
    shown = visible_handoffs(to_csv(events, sample_cases=400))
    pairs = shown.filter(pl.col("activity") != pl.col("next_activity"))
    real = [(r["activity"], r["next_activity"]) for r in pairs.iter_rows(named=True)][:10]
    fabricated = ("T02 Check confirmation of receipt", "T99 A step nobody ever took")

    scored = score_edges(discovered([*real, fabricated]), visible_handoffs=shown)

    assert scored.invented == 1
    assert scored.kept == 10
    assert scored.edge_share == round(10 / 69, 4)


def test_edge_share_is_bounded_even_when_every_edge_is_invented() -> None:
    """The pole was reachable because the numerator was not constrained to the
    denominator's population. With the audit, 50 fabricated edges against 2 real handoffs
    is share 0.0 and 50 invented, not share 25."""
    shown = pl.DataFrame({"activity": ["a", "b"], "next_activity": ["b", "c"]})
    made_up = [(f"x{i}", f"y{i}") for i in range(50)]
    scored = score_edges(discovered(made_up), visible_handoffs=shown)

    assert scored.invented == 50
    assert scored.kept == 0
    assert 0.0 <= scored.edge_share <= 1.0


# ── The feedback must be two-sided ──


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_feedback_does_not_congratulate_a_hallucinating_model() -> None:
    """The recorded string scolded the agent for memorising and told it 319.386 was a
    record, in one message. Whatever it says, it must not claim a hallucinated attempt is
    the best yet."""
    hallucinated = attempt(coverage=0.864, edges=185, edge_share=1.0, invented_edges=86)
    message = feedback_for(hallucinated, best_so_far=0.861)

    assert "best yet" not in message
    assert "invented" in message


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


# ── The feedback must be able to reach both parameters ──


def test_feedback_names_both_channels_it_can_be_routed_to() -> None:
    """The backward model sees both parameters and decides which one a gradient is for.

    Feedback that only ever says "say in the guidance" trains it to route everything to
    the prose target, and the code parameter then never learns anything while the loop
    reports rounds — the two-parameter design failing quietly rather than the crosstalk
    it would be mistaken for. So every non-invented message says what belongs in code
    and what belongs in prose.
    """
    for scored in (
        attempt(coverage=0.90, edge_share=0.29),  # behind
        attempt(coverage=0.95, edge_share=0.20, matched_coverage=0.90),  # won
        attempt(coverage=1.0, edge_share=0.95),  # memorising
    ):
        message = feedback_for(scored, best_so_far=0.8)
        assert "toolkit" in message, message
        assert "prose guidance" in message, message


def test_a_rolled_back_round_says_so_before_reporting_its_score() -> None:
    """A round that ran on restored code did not test the rewrite it was meant to test.

    Reading its score as evidence about that rewrite is the mistake, and the only
    ordering that prevents it is saying so ahead of the number. Asserted on position
    rather than on presence for exactly that reason.
    """
    message = feedback_for(
        attempt(rolled_back=True, rehearsal_error="Import of os is not allowed"),
        best_so_far=0.9,
    )
    assert message.index("failed to load") < message.index("score")
    assert "Import of os is not allowed" in message


def test_an_unrolled_round_says_nothing_about_rollback() -> None:
    """The negative control: the clause must not appear when nothing was rolled back."""
    assert "failed to load" not in feedback_for(attempt(), best_so_far=0.9)


def test_the_summary_table_separates_code_size_from_prose_size() -> None:
    """One combined size column made the two-parameter design unmeasurable.

    A round that grew the toolkit and a round that grew the prose printed identically,
    and so did the crosstalk failure where the optimizer wrote a paragraph of advice
    into the code parameter.
    """
    table = Training(attempts=[attempt(toolkit_chars=8000, guidance_chars=40, helpers=9)]).summary()
    assert "helpers" in table and "code" in table and "advice" in table
    assert "8000" in table and "40" in table


def test_the_summary_table_marks_a_rolled_back_round() -> None:
    table = Training(attempts=[attempt(rolled_back=True, rehearsal_error="boom")]).summary()
    assert "!" in table
    assert "rolled back" in table


def test_the_summary_table_names_helpers_the_rehearsal_could_not_check() -> None:
    """ "We did not check this" and "this passed" must not read the same."""
    table = Training(attempts=[attempt(unrehearsed=("cluster_variants",))]).summary()
    assert "unrehearsed" in table
    assert "cluster_variants" in table


# ── The seed toolkit is real code, and it runs in the real sandbox ──


def test_the_seed_toolkit_is_parseable_python() -> None:
    """`Procedural`'s validator runs `ast.parse`, so an unparseable seed is a hard stop."""
    ast.parse(SEED_TOOLKIT)


def test_every_seed_helper_is_advertised_to_the_agent() -> None:
    """Advertisement is how the agent finds a helper the prompt never names.

    A helper the runtime does not advertise is, from the agent's side, a helper that
    does not exist: the prompt never lists them and nothing else tells it they are
    there.
    """
    advertised = procedural_signatures(SEED_TOOLKIT)
    assert len(advertised) == 9
    assert any("def load_log(log_csv" in block for block in advertised)
    assert any("def sweep_thresholds(" in block for block in advertised)
    for block in advertised:
        assert '"""' in block, f"helper advertised without a docstring: {block.splitlines()[0]}"


def test_comments_and_module_docstrings_are_not_advertised() -> None:
    """The measurement that decides code and prose must be separate parameters.

    If comments were advertised, folding the prose guidance into the toolkit as
    comments would be the simpler design. They are not, so a policy written as a
    comment in the code parameter is invisible to the agent that would have to follow
    it — and the two-parameter design is forced rather than chosen.
    """
    code = (
        '"""A module docstring stating a policy."""\n'
        "# A comment stating another policy.\n"
        "\n"
        "POLICY = 'a third'\n"
        "\n"
        "def visible():\n"
        '    """This is shown."""\n'
        "    return 1\n"
        "\n"
        "def _hidden():\n"
        '    """This is not."""\n'
    )
    advertised = "\n".join(procedural_signatures(code))
    assert "This is shown" in advertised
    assert "module docstring stating a policy" not in advertised
    assert "A comment stating another policy" not in advertised
    assert "POLICY" not in advertised
    assert "_hidden" not in advertised


def test_io_is_not_importable_in_the_sandbox() -> None:
    """The specific trap `load_log` exists for, asserted so a library change surfaces here.

    `aimine`'s own prompt recommends `polars.read_csv(io.StringIO(log_csv))` and that
    raises. If `io` is ever added to the allowlist this test fails, which is the right
    place to notice that `load_log`'s docstring has become wrong.
    """
    executor = LocalPythonExecutorTool(
        output_type=Discovered, additional_authorized_imports=list(ANALYSIS_IMPORTS)
    )
    outcome = executor._execute_code("import io")  # noqa: SLF001
    assert not outcome.success
    assert "io" in (outcome.error or "")


def test_the_seed_toolkit_loads_and_every_helper_runs() -> None:
    """The whole toolkit, in a real executor, with every helper actually called.

    Parsing proves nothing about execution: a module-level `import os` parses fine and
    aborts the cycle. This is the rehearsal the loop runs before every round, and it
    passing on the seed is the precondition for the seed being a usable default.
    """
    report = rehearse(SEED_TOOLKIT)
    assert report.ok, report.error
    assert report.helpers == 9
    assert len(report.rehearsed) == 9
    assert report.unrehearsed == ()


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_the_seed_toolkit_reproduces_the_documented_baseline() -> None:
    """The baseline no toolkit number should be reported without.

    Driving `final_answer` from `sweep_thresholds`' argmax with no model judgment at
    all measures threshold 19, 13 edges, 84.38% coverage, share 0.1884, score 0.8274 on
    a 400-case sample of the receipt log. That is what the *helpers alone* achieve, so
    it is the honest floor for any claim about what the agent does with them — and
    pinning it means a change to a helper that quietly moves the floor is visible.
    """
    events = eventlog.parse_xes(LOG)
    log_csv = to_csv(events, sample_cases=400)
    executor = LocalPythonExecutorTool(
        output_type=Discovered,
        initial_state={"log_csv": log_csv},
        initial_code=[SEED_TOOLKIT],
        additional_authorized_imports=list(ANALYSIS_IMPORTS),
    )
    outcome = executor._execute_code(  # noqa: SLF001
        "frame = load_log(log_csv)\n"
        "counted = handoff_support(frame)\n"
        "start = start_activity(frame)\n"
        "best = sweep_thresholds(frame, counted, start)[0]\n"
        "kept = [(s, t, c) for s, t, _, c in counted if s != t and c >= best[0]]\n"
        "final_answer(start_activity=start, "
        "terminal_activities=terminal_candidates([(s, t) for s, t, _ in kept]), "
        'edges=[{"source": s, "target": t, "cases": c} for s, t, c in kept], '
        'threshold_used=best[0], method="argmax of sweep_thresholds")\n'
    )
    assert outcome.success, outcome.error
    assert outcome.final_answer is not None
    discovered = Discovered(**outcome.final_answer)
    audit = score_edges(discovered, visible_handoffs=visible_handoffs(log_csv))

    assert discovered.threshold_used == 19
    assert audit.kept == 13
    assert audit.invented == 0
    assert audit.edge_share == pytest.approx(0.1884, abs=5e-4)
    scored = attempt(
        coverage=0.8438,
        matched_coverage=0.8438,
        edges=13,
        edge_share=audit.edge_share,
        invented_edges=0,
    )
    assert scored.score == pytest.approx(0.8274, abs=5e-4)


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_the_seed_toolkit_only_beats_the_frozen_miner_by_tuning_its_threshold() -> None:
    """The negative finding, pinned so nobody reads the baseline as a win.

    `handoff_support` plus a threshold *is* `miner.directly_follows` plus a threshold, so
    the seed toolkit reimplements the frozen algorithm. Run on the same 400-case sample
    at the cutoff the sweep chooses, the frozen miner produces the identical 13 edges and
    the identical score. The only thing the toolkit adds over the frozen miner at its
    default 25 is choosing the cutoff by measurement, and that is worth +0.0064.

    Reporting 0.8274 without this test would let it read as evidence the helpers are
    cleverer than the baseline. They are not; they are the baseline with its one free
    parameter tuned, which is a smaller and true claim.
    """
    from pneuma.casestudy.miner import conformance, mine

    events = eventlog.parse_xes(LOG)
    log_csv = to_csv(events, sample_cases=400)
    sample = pl.read_csv(log_csv.encode())
    visible = visible_handoffs(log_csv).filter(pl.col("activity") != pl.col("next_activity")).height

    def scored(threshold: int) -> Attempt:
        mined = mine(sample, min_edge_cases=threshold)
        identifiers = {s.description: s.name for s in mined.process.states}
        coverage = conformance(events, mined.process, identifiers)
        edges = len(mined.process.transitions)
        return attempt(
            coverage=coverage,
            matched_coverage=coverage,
            threshold=threshold,
            edges=edges,
            edge_share=round(edges / visible, 4),
            invented_edges=0,
        )

    at_sweep_argmax, at_default = scored(19), scored(25)

    assert at_sweep_argmax.edges == 13
    assert at_sweep_argmax.score == pytest.approx(0.8274, abs=5e-4)
    assert at_default.score == pytest.approx(0.8210, abs=5e-4)
    assert at_sweep_argmax.score - at_default.score == pytest.approx(0.0064, abs=1e-3)


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
def test_no_cutoff_beats_the_argmax_so_the_live_bar_needs_a_non_uniform_model() -> None:
    """What `test_live_toolkit_beats_its_own_seed_baseline` is actually asking for.

    That test's bar is >0.8274, and a bar nothing can clear would measure the objective
    rather than the agent. This pins which it is. Graded the way the loop grades the
    agent, 0.8274 at threshold 19 is the exact maximum over *every* candidate cutoff —
    so the agent cannot win by choosing a better threshold, only by leaving the
    "keep every handoff with support >= k" family altogether.

    Pinned because the live test ties at 0.8274 and the tempting reading is that the bar
    is impossible. It is not: a greedy search over arbitrary edge subsets reaches 0.8292
    by keeping the 13 argmax edges and adding one support-7 handoff. What the tie means
    is that the agent found the argmax and stopped, which is a finding about the agent.
    """
    events = eventlog.parse_xes(LOG)
    log_csv = to_csv(events, sample_cases=400)
    sample = pl.read_csv(log_csv.encode())
    shown = visible_handoffs(log_csv)
    pairs = directly_follows(sample).filter(pl.col("activity") != pl.col("next_activity"))
    start = (
        sample.sort(["case_id", "position"])
        .group_by("case_id")
        .agg(pl.col("activity").first().alias("a"))["a"]
        .mode()[0]
    )

    def graded(edges: list[tuple[str, str, int]]) -> float:
        built = [Edge(source=s, target=t, cases=c) for s, t, c in edges]
        targets = {e.target for e in built}
        sources = {e.source for e in built}
        model = Discovered(
            start_activity=start,
            terminal_activities=sorted(targets - sources) or sorted(targets),
            edges=built,
            threshold_used=min(c for _, _, c in edges),
            method="enumerated",
        )
        scored = grade(events, model, baseline_threshold=25)
        audit = score_edges(model, visible_handoffs=shown)
        return attempt(
            coverage=scored.coverage,
            matched_coverage=scored.matched_coverage,
            edges=scored.edges,
            edge_share=audit.edge_share,
            invented_edges=audit.invented,
        ).score

    candidates = [
        (s, t, int(c)) for s, t, c in pairs.select("activity", "next_activity", "cases").rows()
    ]
    by_cutoff = {
        cutoff: graded(kept)
        for cutoff in sorted({c for _, _, c in candidates})
        if (kept := [e for e in candidates if e[2] >= cutoff])
    }

    best_cutoff = max(by_cutoff, key=lambda k: by_cutoff[k])
    assert best_cutoff == 19
    assert by_cutoff[19] == pytest.approx(0.8274, abs=5e-4)
    assert max(v for k, v in by_cutoff.items() if k != 19) < 0.8274

    # Leaving the threshold family does clear the live bar, so it is not impossible.
    argmax_edges = [e for e in candidates if e[2] >= 19]
    added = next(
        e
        for e in candidates
        if (e[0], e[1])
        == (
            "T06 Determine necessity of stop advice",
            "T04 Determine confirmation of receipt",
        )
    )
    assert graded([*argmax_edges, added]) > 0.8274


def test_the_sweep_finds_the_cutoff_a_gap_alone_would_miss() -> None:
    """The helpers have to earn their place, so the two cutoff methods must disagree.

    If the widest support gap and the balanced argmax always agreed, `sweep_thresholds`
    would be `support_gaps` with extra steps and the toolkit would be a demonstration
    rather than leverage. On this log they do not: the widest gap is at support 69 and
    the objective peaks at 19.
    """
    events = eventlog.parse_xes(LOG) if LOG.is_file() else None
    if events is None:
        pytest.skip("needs data/receipt.xes")
    log_csv = to_csv(events, sample_cases=400)
    executor = LocalPythonExecutorTool(
        output_type=Discovered,
        initial_state={"log_csv": log_csv},
        initial_code=[SEED_TOOLKIT],
        additional_authorized_imports=list(ANALYSIS_IMPORTS),
    )
    outcome = executor._execute_code(  # noqa: SLF001
        "frame = load_log(log_csv)\n"
        "counted = handoff_support(frame)\n"
        "start = start_activity(frame)\n"
        "gap = support_gaps([c for _, _, _, c in counted])[0]\n"
        "peak = sweep_thresholds(frame, counted, start)[0]\n"
        "print('gap', gap[0], 'peak', peak[0])\n"
    )
    assert outcome.success, outcome.error
    reported = outcome.stdout.split()
    gap_cutoff, peak_cutoff = int(reported[1]), int(reported[3])
    assert gap_cutoff != peak_cutoff


# ── The mechanism: executed at setup, advertised, and a real gradient target ──


class _Toolkit(BaseModel):
    """The parameter shape under test, kept local so the test owns its schema."""

    toolkit: Procedural = Field(default=SEED_TOOLKIT, description="Mining helpers.")


_ANSWER = (
    'final_answer(start_activity="Alpha", terminal_activities=["Gamma"], '
    'edges=[{"source": "Alpha", "target": "Gamma", "cases": 3}], '
    'threshold_used=1, method="{note}")\n'
)


def _script(code: str) -> ScriptedModel:
    """One python_executor turn running `code`, then a closing turn."""
    return ScriptedModel([Turn(tool_calls=(("python_executor", {"code": code}),)), Turn(text="ok")])


async def test_the_toolkit_is_executed_at_setup_so_helpers_are_callable() -> None:
    """Not merely present as a string: *defined*, and callable without being redefined.

    The sandbox forbids `exec`, so a toolkit injected as an ordinary string variable
    would be inert and the agent could not call anything in it. The scripted agent
    here calls `handoff_support` with no definition of its own, which only works if the
    runtime ran the toolkit at setup.
    """
    compiled = LearningMiner().compiled("discover")
    async with RuntimeHarness() as harness:
        code = "frame = load_log(log_csv)\ncounted = handoff_support(frame)\n" + _ANSWER.replace(
            "{note}", "called helpers I never defined"
        )
        handle = await harness.spawn(compiled.replace(model=_script(code)))
        result = await handle.run(SEED_TOOLKIT, "advice", REHEARSAL_LOG, 3, 3)

    assert result.method == "called helpers I never defined"


async def test_the_helpers_are_advertised_in_the_prompt_preamble() -> None:
    """Signature and docstring, in the messages the model actually saw.

    This is the channel that makes an accumulated toolkit discoverable: the prompt
    never names a single helper, so an agent can only find one by reading the
    advertisement.
    """
    compiled = LearningMiner().compiled("discover")
    async with RuntimeHarness() as harness:
        handle = await harness.spawn(
            compiled.replace(model=_script(_ANSWER.replace("{note}", "m")))
        )
        await handle.run(SEED_TOOLKIT, "advice", REHEARSAL_LOG, 3, 3)
        transcript = str(harness.agent_messages(handle.id))

    assert "def sweep_thresholds(" in transcript
    assert "the cutoff to defend" in transcript
    # The toolkit must not also be dumped as a plain variable: it is `initial_code`,
    # not `initial_state`, and a copy in the variable list would be dead weight.
    assert "- toolkit:" not in transcript


async def test_the_recalled_toolkit_is_a_gradient_target(tmp_path: Path) -> None:
    """The link the whole design rests on, and the one that fails silently.

    A `Procedural` value that does not reach the graph as a `ParameterNode` is not
    learnable, and nothing raises — the round runs, the answer is fine, and
    consolidation has nothing to consolidate. So the node is asserted directly:
    present, procedural, grad-enabled, and holding the code.
    """
    memory = TursoMemoryBackend(_Toolkit, actor_id="miner", path=tmp_path / "m.db")
    compiled = LearningMiner().compiled("discover")
    try:
        async with RuntimeHarness():
            traced: Any = await compiled.replace(
                model=_script(_ANSWER.replace("{note}", "m"))
            ).trace(await memory.recall("toolkit"), "advice", REHEARSAL_LOG, 3, 3)
            graph = await build_graph_from_result(traced, [memory])
    finally:
        memory.close()

    nodes = [p for p in graph.parameters if p.name == "toolkit"]
    assert len(nodes) == 1, "the recalled toolkit did not reach the graph"
    node = nodes[0]
    assert node.procedural is True, "rendered as prose, so the optimizer would rewrite text"
    assert node.requires_grad is True
    assert node.backend is memory
    assert "def sweep_thresholds(" in str(node.value)


async def test_interpolating_the_toolkit_drops_the_gradient_edge(tmp_path: Path) -> None:
    """The negative control, because this failure is invisible.

    `f"{view}"` computes the identical prompt and the round succeeds identically. The
    only difference is that nothing is learnable, so a test asserting the positive case
    alone cannot tell the two apart.
    """
    memory = TursoMemoryBackend(_Toolkit, actor_id="miner", path=tmp_path / "m.db")
    compiled = LearningMiner().compiled("discover")
    try:
        async with RuntimeHarness():
            view = await memory.recall("toolkit")
            traced: Any = await compiled.replace(
                model=_script(_ANSWER.replace("{note}", "m"))
            ).trace(f"{view}", "advice", REHEARSAL_LOG, 3, 3)
            graph = await build_graph_from_result(traced, [memory])
    finally:
        memory.close()

    assert [p for p in graph.parameters if p.name == "toolkit"] == []


async def test_a_rewrite_persists_into_the_next_round(tmp_path: Path) -> None:
    """Accumulation, which is the entire point: round two runs round one's code.

    The consolidating model is stubbed, because whether a real model writes good code
    is a different question from whether what it writes survives to the next sandbox.
    This asserts the survival, which is the part that would fail silently.
    """
    memory = TursoMemoryBackend(_Toolkit, actor_id="miner", path=tmp_path / "m.db")
    added = SEED_TOOLKIT + (
        "\n\ndef variant_frequency(frame):\n"
        '    """Count whole traces by their activity sequence."""\n'
        "    from collections import Counter\n"
        "    paths = {}\n"
        "    for case, activity in frame.select('case_id', 'activity').rows():\n"
        "        paths.setdefault(case, []).append(activity)\n"
        "    return Counter(tuple(p) for p in paths.values()).most_common()\n"
    )

    class _Rewriter:
        def replace(self, **overrides: object) -> _Rewriter:
            del overrides
            return self

        def run_sync(self, **kwargs: object) -> str:
            del kwargs
            return added

    memory._rewrite_code_fn = _Rewriter()  # type: ignore[assignment]  # noqa: SLF001
    memory.consolidate("toolkit", [GradFeedback(text="add a variant counter", score=0.4)])

    compiled = LearningMiner().compiled("discover")
    try:
        async with RuntimeHarness() as harness:
            code = "print(variant_frequency(load_log(log_csv))[0][1])\n" + _ANSWER.replace(
                "{note}", "used the helper the last round added"
            )
            handle = await harness.spawn(compiled.replace(model=_script(code)))
            result = await handle.run(str(memory.fetch("toolkit")), "advice", REHEARSAL_LOG, 3, 3)
            transcript = str(harness.agent_messages(handle.id))
    finally:
        memory.close()

    assert result.method == "used the helper the last round added"
    assert "def variant_frequency(" in transcript, "the added helper was not advertised"


# ── A bad rewrite must degrade, not destroy ──


BROKEN = {
    "a forbidden import at module level": "import os\n\n" + SEED_TOOLKIT,
    "a module-level statement that raises": SEED_TOOLKIT + "\nraise ValueError('boom')\n",
    "a module-level name that does not exist": SEED_TOOLKIT + "\nWIDTH = undefined_name\n",
    "a helper that raises when it is called": SEED_TOOLKIT.replace(
        '    return pl.read_csv(log_csv.encode()).sort(["case_id", "position"])',
        '    return pl.read_csv(log_csv.encode()).sort(["case_id", "nonexistent"])',
    ),
}


@pytest.mark.parametrize("label", sorted(BROKEN))
def test_rehearsal_catches_every_way_a_rewrite_can_break_the_sandbox(label: str) -> None:
    """Each of these aborts a real round with `Failed to load procedural code`.

    Parsing catches none of them: all four are valid Python. That is why the guard is a
    rehearsal in a real executor rather than an `ast.parse`, and why each failure mode
    is enumerated rather than represented by one syntax error.
    """
    report = rehearse(BROKEN[label])
    assert not report.ok, f"{label} was not caught"
    assert report.error


@pytest.mark.parametrize("label", sorted(BROKEN))
def test_a_broken_rewrite_is_rolled_back_and_the_rollback_is_recorded(
    label: str, tmp_path: Path
) -> None:
    """Degrade, do not destroy — and say so.

    The stored toolkit goes back to the last one that ran, `rolled_back` is True, and
    the failing candidate's error is kept. A silent rollback would leave a loop that
    reverted the parameter it was supposed to be learning looking, from the outside,
    exactly like a loop that learned nothing.
    """
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        assert check_toolkit(memory).report.ok, "the seed must pass before a rollback can happen"
        memory.save("toolkit", BROKEN[label])

        outcome = check_toolkit(memory)

        assert outcome.rolled_back is True
        assert outcome.reason, "a rollback with no stated reason is a silent rollback"
        assert outcome.report.ok, "the restored toolkit was not itself rehearsed"
        assert str(memory.fetch("toolkit")) == SEED_TOOLKIT
    finally:
        memory.close()


def test_unparseable_code_never_reaches_the_store(tmp_path: Path) -> None:
    """The first line of defence, before rehearsal: `_save` re-parses a `Procedural` value.

    So the rollback path handles code that *runs* wrong, and the store handles code
    that does not parse. Both matter: a `Procedural` parameter holding unparseable text
    could not even be recalled without the validator raising somewhere less obvious.
    """
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        with pytest.raises(SyntaxError):
            memory.save("toolkit", "def broken(:\n")
        assert str(memory.fetch("toolkit")) == SEED_TOOLKIT
    finally:
        memory.close()


def test_a_toolkit_that_deletes_a_bootstrap_helper_is_rejected_with_a_clear_reason(
    tmp_path: Path,
) -> None:
    """The rehearsal's own boundary, stated rather than discovered.

    `load_log`, `handoff_support`, and `start_activity` are what the rehearsal builds
    its fixtures from, so a rewrite that renames one leaves nothing checkable. The
    honest response is to reject it and say which function is gone — not to skip every
    helper and report a pass, which is a guard certifying an empty check.
    """
    renamed = SEED_TOOLKIT.replace("def load_log(", "def parse_log(")
    assert missing_bootstrap(renamed) == ["load_log"]

    report = rehearse(renamed)
    assert not report.ok
    assert "load_log" in report.error

    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        assert check_toolkit(memory).report.ok
        memory.save("toolkit", renamed)
        assert check_toolkit(memory).rolled_back is True
        assert str(memory.fetch("toolkit")) == SEED_TOOLKIT
    finally:
        memory.close()


def test_both_broken_lets_the_round_fail_loudly_rather_than_claiming_a_rollback(
    tmp_path: Path,
) -> None:
    """When the fallback is broken too there is nothing to fall back to, and saying
    otherwise would be worse than failing. `rolled_back` stays False and the failing
    rehearsal is returned unchanged, so the round dies on the `Procedural` setup error
    it deserves rather than mining with code known to be broken."""
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        broken = "import os\n\n" + SEED_TOOLKIT
        memory.save("toolkit", broken)
        memory.save("last_good_toolkit", broken)

        outcome = check_toolkit(memory)

        assert outcome.rolled_back is False
        assert outcome.report.ok is False
    finally:
        memory.close()


def test_a_healthy_round_advances_the_last_good_toolkit(tmp_path: Path) -> None:
    """The fallback has to move forward, or a rollback three rounds in loses three rounds."""
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        grown = (
            SEED_TOOLKIT + '\n\ndef ratio(counted):\n    """Share."""\n    return len(counted)\n'
        )
        memory.save("toolkit", grown)
        assert check_toolkit(memory).report.ok
        assert str(memory.fetch("last_good_toolkit")) == grown
    finally:
        memory.close()


def test_the_last_good_toolkit_is_not_a_gradient_target(tmp_path: Path) -> None:
    """`Frozen` sets `requires_grad=False`, so the optimizer cannot corrupt the fallback.

    A rollback target the optimizer could rewrite is not a rollback target: the round
    that broke the toolkit could break the thing it falls back to in the same step.
    """
    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        assert memory._is_frozen("last_good_toolkit") is True  # noqa: SLF001
        assert memory._is_frozen("toolkit") is False  # noqa: SLF001
        assert memory._is_procedural("last_good_toolkit") is True  # noqa: SLF001
        assert memory._is_procedural("advice") is False  # noqa: SLF001
    finally:
        memory.close()


def test_a_helper_the_rehearsal_cannot_call_is_reported_not_skipped() -> None:
    """A helper whose parameter names the fixtures do not know is unrehearsed, and named.

    Reporting it as a pass would mean the rehearsal's coverage silently shrinks every
    time the agent adds a helper with an unfamiliar signature, and the guard would decay
    into a check on three functions while claiming to check the toolkit.
    """
    added = SEED_TOOLKIT + (
        '\n\ndef cluster_variants(distance_matrix):\n    """Needs something we cannot build."""\n'
        "    return distance_matrix\n"
    )
    _, rehearsed, skipped = rehearsal_probe(added)

    assert "cluster_variants" in skipped
    assert "cluster_variants" not in rehearsed

    report = rehearse(added)
    assert report.ok, report.error
    assert report.unrehearsed == ("cluster_variants",)
    assert report.helpers == 10
    assert len(report.rehearsed) == 9


def test_a_helper_the_agent_adds_with_a_known_signature_is_rehearsed() -> None:
    """The positive half: fixtures are matched by parameter *name*, so a helper that
    names its inputs the way the toolkit does is exercised without anything being
    registered. That is what makes the guard cover code nobody wrote by hand."""
    added = SEED_TOOLKIT + (
        '\n\ndef rare_handoffs(counted):\n    """Handoffs one case walked."""\n'
        "    return [(s, t) for s, t, _, c in counted if c == 1]\n"
    )
    report = rehearse(added)

    assert report.ok, report.error
    assert "rare_handoffs" in report.rehearsed
    assert report.unrehearsed == ()


def test_the_bootstrap_contract_matches_what_the_probe_actually_builds() -> None:
    """`BOOTSTRAP` is a documented promise, so it must match the probe's real
    dependencies rather than drift from them."""
    probe, _, _ = rehearsal_probe(SEED_TOOLKIT)
    for name in BOOTSTRAP:
        assert f"{name}(" in probe


# ── The loop itself, offline: two rounds with no model call outside the sandbox ──


@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
async def test_train_reaches_the_optimizer_with_both_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole `train` path, offline, asserting what the optimizer was actually handed.

    The earlier mechanism tests build the graph themselves, which proves the library
    works and not that `train` wires it correctly — and the wiring is where the
    single-use-`ParameterView` and call-argument rules are easy to get wrong. So this
    intercepts `optimizer.step` in the real loop and asserts both parameters arrived,
    with the code one flagged procedural.

    The forward model is scripted and the backward model is replaced, so nothing here
    calls Bedrock. What it cannot prove is what a real backward model does with the two
    targets, which is the live test below.
    """
    from pneuma.casestudy import minelearn

    captured: list[list[tuple[str, bool, bool]]] = []

    class _Recorder:
        async def step(self, traced: Any, feedback: str, backends: list[Any]) -> Any:
            del feedback
            graph = await build_graph_from_result(traced, backends)
            captured.append([(p.name, p.procedural, p.requires_grad) for p in graph.parameters])
            return graph

    code = (
        "frame = load_log(log_csv)\n"
        "counted = handoff_support(frame)\n"
        "start = start_activity(frame)\n"
        "best = sweep_thresholds(frame, counted, start)[0]\n"
        "kept = [(s, t, c) for s, t, _, c in counted if s != t and c >= best[0]]\n"
        "final_answer(start_activity=start, "
        "terminal_activities=terminal_candidates([(s, t) for s, t, _ in kept]), "
        'edges=[{"source": s, "target": t, "cases": c} for s, t, c in kept], '
        'threshold_used=best[0], method="argmax of sweep_thresholds")\n'
    )
    script = [Turn(tool_calls=(("python_executor", {"code": code}),)), Turn(text="ok")] * 6
    original = minelearn.LearningMiner.compiled

    def _scripted(self: Any, name: str, **overrides: Any) -> Any:
        return original(self, name, **overrides).replace(model=ScriptedModel(script))

    monkeypatch.setattr(minelearn, "TextGradOptimizer", _Recorder)
    monkeypatch.setattr(minelearn.LearningMiner, "compiled", _scripted)

    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    try:
        training = await minelearn.train(
            eventlog.parse_xes(LOG), tmp_path / "unused.db", rounds=2, memory=memory
        )
    finally:
        memory.close()

    assert len(training.attempts) == 2
    assert captured, "the optimizer was never stepped, so nothing could have been learned"
    assert sorted(captured[0]) == [("advice", False, True), ("toolkit", True, True)]
    assert training.final_toolkit == SEED_TOOLKIT
    assert all(not a.rolled_back for a in training.attempts)
    assert all(a.helpers == 9 and a.rehearsed == 9 for a in training.attempts)


# ── Live: does a real backward model route to the right parameter? ──


# A computation no seed helper provides: the distinct ordered activity sequence per
# case, and how many cases walk each one. The agent hand-rolls it below, which is the
# trace shape the toolkit parameter exists to absorb.
_HANDROLLED_VARIANTS = (
    "frame = load_log(log_csv)\n"
    "counted = handoff_support(frame)\n"
    "start = start_activity(frame)\n"
    "paths = {}\n"
    "for case, act in frame.select('case_id', 'activity').rows():\n"
    "    paths.setdefault(case, []).append(act)\n"
    "freq = {}\n"
    "for acts in paths.values():\n"
    "    key = '>'.join(acts)\n"
    "    freq[key] = freq.get(key, 0) + 1\n"
    "print('variants', sorted(freq.items(), key=lambda kv: -kv[1]))\n"
)


@_live
async def test_live_two_parameters_receive_distinct_gradients(tmp_path: Path) -> None:
    """The crosstalk question, which only a real backward model can answer.

    `TextGradOptimizer._distribute` shows one model both parameters and asks it to
    attribute. Whether it splits honestly is a property of the model, so this measures
    it rather than assuming either outcome.

    Both halves of the stimulus have to be real or the measurement is not about
    crosstalk. Measured over 24 live backward passes, an earlier version of this test
    asserted only that the toolkit node had *some* gradient while feeding a trace in
    which the agent called no helper at all and feedback claiming it "had no way to
    count how many cases each cutoff would cost" — a gap `sweep_thresholds` already
    closes. The backward model answered, correctly, "no code changes needed, the agent
    failed to use the helpers it has": 24/24 runs put a refusal in the toolkit node and
    0/24 put code there. The assertion passed on the refusal, so the test was green for
    the wrong reason and flaked whenever the model declined to say anything at all.

    So the trace now hand-rolls a computation the toolkit genuinely lacks (case
    variants), the feedback names that gap alongside the cutoff misjudgment, and the
    code target is asserted to receive *code* rather than merely a gradient. Measured
    6/6 on this shape, and 5/5 with the prose parameter removed, so routing competition
    is not what decides it.

    A failure here is a finding about the two-parameter design, not a broken test, and
    it should be reported as such rather than worked around.
    """
    from ai_functions import TextGradOptimizer

    memory = TursoMemoryBackend(Guidance, actor_id="miner", path=tmp_path / "m.db")
    optimizer = TextGradOptimizer()
    compiled = LearningMiner().compiled("discover")
    try:
        async with RuntimeHarness():
            traced: Any = await compiled.replace(
                model=_script(_HANDROLLED_VARIANTS + _ANSWER.replace("{note}", "m"))
            ).trace(
                await memory.recall("toolkit"),
                await memory.recall("advice"),
                REHEARSAL_LOG,
                3,
                3,
            )
            graph = await optimizer.step(
                traced,
                "No helper reports the distinct case variants (the ordered activity "
                "sequence per case) or how many cases walk each one, so the agent "
                "hand-wrote that grouping loop inline every round and could not tell a "
                "dominant happy path from a long tail of rare variants. It then chose "
                "the loosest cutoff rather than weighing coverage against selectivity.",
                backends=[memory],
            )
        by_name = {p.name: p for p in graph.parameters}
        assert by_name["toolkit"].gradients, "the code parameter received no gradient"
        assert by_name["advice"].gradients, "the prose parameter received no gradient"
        code_gradient = " ".join(g.text for g in by_name["toolkit"].gradients)
        assert "def " in code_gradient, (
            "the code parameter received a gradient with no code in it, so the "
            f"`type: code` label routed prose to the sandbox: {code_gradient!r}"
        )
    finally:
        memory.close()


@_live
@pytest.mark.skipif(not LOG.is_file(), reason="needs data/receipt.xes")
async def test_live_toolkit_beats_its_own_seed_baseline(tmp_path: Path) -> None:
    """The only comparison that is honest about this parameter.

    Not against the prose-only loop, which optimises a different parameter, and not
    against the frozen miner at its default, which is a different claim. The question
    the toolkit poses is whether an agent *using* these helpers beats the helpers'
    own mechanical argmax, which scores 0.8274 with no model judgment at all. Anything
    at or below that means the model added nothing and the toolkit is doing the work.

    ## The bar is reachable, which is what makes a tie a finding

    A bar nothing can clear measures the objective, not the agent, so it was checked
    before being trusted. Enumerating every candidate cutoff and grading each the way
    this loop grades the agent, 0.8274 at threshold 19 is the *exact maximum* of the
    whole pure-threshold family: the runner-up is 0.8266 at 28 and third is 0.8210 at
    27. So no choice of cutoff, however well judged, can beat the argmax — by
    construction, since the argmax is what it is.

    The bar is nevertheless clearable, because the agent is not confined to that family.
    A greedy search over arbitrary edge subsets, starting from the 13 argmax edges,
    finds 0.8292 by adding one edge of support 7 (`T06 Determine necessity of stop
    advice -> T04 Determine confirmation of receipt`). That is +0.0018, and it is
    the only kind of move that can win: keep the argmax skeleton and add a low-support
    handoff whose replay gain exceeds its selectivity cost.

    So a tie at 0.8274 is neither a broken test nor a ceiling artifact. It says the
    agent found the argmax and stopped there, which is the honest negative result and
    is left failing rather than papered over. Two live 2-round runs measured 0.8274 /
    0.8274 (agent reported reading the sweep argmax) and 0.7996 / 0.7888 (agent
    overrode the argmax to get a structural terminal, and lost). Neither cleared it.
    """
    from pneuma.casestudy.minelearn import train

    events = eventlog.parse_xes(LOG)
    training = await train(events, tmp_path / "m.db", rounds=2)

    best = training.best
    assert best is not None
    print(training.summary())
    assert best.score > 0.8274, (
        f"the agent scored {best.score} against the seed toolkit's own mechanical 0.8274, "
        "so the model contributed nothing over an argmax. The argmax is the maximum of "
        "the pure-threshold family, so clearing this bar requires leaving that family: "
        "0.8292 is reachable by keeping the 13 argmax edges and adding one support-7 "
        "handoff whose replay gain beats its selectivity cost."
    )
