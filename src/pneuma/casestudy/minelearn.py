"""Improve the miner's own instructions by backpropagation.

The first `aimine` run lost to the fixed implementation, and the diagnosis was in the
agent's stated method: it counted directly-follows pairs ranked by distinct cases,
which is exactly what the frozen code does. It reproduced the algorithm rather than
improving on it, because the prompt described that algorithm as the obvious starting
point and then asked for a judgment call.

So the thing to optimise is the prompt. `Guidance` is a text parameter the miner reads
before analysing, and the loop rewrites it from measured feedback: mine, score the
coverage against the fixed implementation at a matched threshold, phrase the shortfall
in plain English, let the optimizer rewrite the guidance, mine again.

Two details make this work at all, both learned the hard way elsewhere in this project.
The guidance arrives as a **call argument**, because gradient targets are discovered in
call arguments and anything hidden on `self` is invisible to the optimizer. And the
recall happens per call, because a `ParameterView` is emitted once — reuse one across a
batch and only the first traced call carries a gradient target.

The safety property is unchanged. Guidance influences how the agent analyses; the
structure it returns is still validated by Pydantic, still model-checked by TLC, and
still executed by the same interpreter. A bad rewrite produces a worse model, never an
unverified one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
from ai_functions import JSONMemoryBackend, TextGradOptimizer
from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from .aimine import ANALYSIS_IMPORTS, Discovered, grade, to_csv
from .miner import directly_follows

SEED_GUIDANCE = "# No guidance learned yet.\n"


class Guidance(BaseModel):
    """The learnable parameter: how to approach process discovery.

    Plain `str` rather than `Procedural`. `Procedural` marks a parameter as reusable
    *code* the sandbox should define, which would be a reasonable future move — the
    agent could accumulate its own analysis helpers. Prose is the smaller change and
    the one that tests whether better instructions are enough.
    """

    advice: str = Field(
        default=SEED_GUIDANCE,
        description=(
            "Accumulated guidance on discovering a process model from an event log: "
            "what to compute, how to choose a support cutoff, and what to avoid."
        ),
    )


class LearningMiner(MethodAgent):
    """A miner whose approach is a gradient target."""

    name = "learning-miner"

    @ai_method(
        Discovered,
        description="Discover a process model, guided by what previous attempts learned",
        code_execution_mode="local",
        code_executor_additional_imports=ANALYSIS_IMPORTS,
        max_attempts=3,
    )
    def discover(
        self,
        guidance: str,
        log_csv: str,
        activity_count: int,
        case_count: int,
    ) -> Discovered:
        """Discover the process behind this event log.

        Guidance learned from previous attempts:
        {guidance}

        The variable `log_csv` holds a CSV with columns case_id, position, activity —
        one row per event, sorted by case then position. It covers {case_count} cases
        and {activity_count} distinct activities.

        Write Python in the executor to analyse it. `polars`, `numpy`, `statistics`,
        `collections`, `itertools`, and `math` are available.

        Decide for yourself what belongs in the model, how to rank candidate handoffs,
        and where to cut. Report the cutoff in `threshold_used`. The model must be
        connected: every activity in an edge reachable from `start_activity`, and at
        least one terminal activity reachable.

        You are scored on two things at once. Coverage: how many complete real cases the
        model can replay end to end. Selectivity: how small a fraction of the log's
        distinct handoffs you needed to get there. Keeping every handoff scores perfect
        coverage and fails, because a model containing every one-off deviation describes
        the log rather than the process.

        In `method`, say what you computed. Return via `final_answer`.
        """


@dataclass
class Attempt:
    """One mining attempt and how it scored."""

    index: int
    coverage: float
    matched_coverage: float
    threshold: int
    states: int
    edges: int
    guidance_chars: int
    method: str = ""

    edge_share: float = 1.0
    """Fraction of the log's distinct handoffs this model kept."""

    @property
    def gap(self) -> float:
        """How far behind the fixed implementation, at the same threshold."""
        return round(self.matched_coverage - self.coverage, 4)

    @property
    def score(self) -> float:
        """Coverage balanced against generalisation, as an F-score.

        Coverage alone has a degenerate optimum: keep every handoff, including the
        thirty walked by exactly one case, and you replay the log perfectly while
        describing no process. A first run optimising coverage alone found exactly
        that — it drove the threshold to 1 and reported 98.6%.

        `1 - edge_share` stands in for generalisation: a model using a small share of
        the log's distinct handoffs has abstracted, one using all of them has
        memorised. The harmonic mean forces both to be respectable.
        """
        selectivity = 1.0 - self.edge_share
        if self.coverage + selectivity == 0:
            return 0.0
        return round(2 * self.coverage * selectivity / (self.coverage + selectivity), 4)

    @property
    def won(self) -> bool:
        return self.coverage > self.matched_coverage


@dataclass
class Training:
    """The whole run."""

    attempts: list[Attempt] = field(default_factory=list)
    final_guidance: str = ""

    @property
    def best(self) -> Attempt | None:
        """Best on the balanced score, not on raw coverage."""
        return max(self.attempts, key=lambda a: a.score) if self.attempts else None

    def summary(self) -> str:
        lines = [
            f"{'round':>5} {'thr':>4} {'states':>7} {'edges':>6} {'kept':>6} "
            f"{'coverage':>9} {'matched':>8} {'score':>7} {'guidance':>9}"
        ]
        for attempt in self.attempts:
            lines.append(
                f"{attempt.index:>5} {attempt.threshold:>4} {attempt.states:>7} "
                f"{attempt.edges:>6} {100 * attempt.edge_share:>5.0f}% "
                f"{100 * attempt.coverage:>8.1f}% {100 * attempt.matched_coverage:>7.1f}% "
                f"{attempt.score:>7.3f} {attempt.guidance_chars:>9}"
            )
        return "\n".join(lines)


def feedback_for(attempt: Attempt, best_so_far: float | None = None) -> str:
    """Turn the score into feedback the optimizer can route.

    Two earlier versions of this function each taught the agent the wrong lesson, and
    both failures were in the feedback rather than the mechanism.

    Reporting coverage alone drove the threshold to 1: the agent kept every one-off
    handoff, scored 98.6%, and produced a model indistinguishable from the raw log.

    Reporting the balanced score but only *complaining* about memorisation above 60%
    edge share was worse, because it was silently one-sided. At 29% share the agent
    heard nothing but "you are behind on coverage", so it loosened the threshold every
    round and walked its score from 0.804 down to 0.706 — away from its own best
    attempt, obediently, while the objective it was being scored on got worse.

    So the score itself is now reported every round, along with whether it moved up or
    down against the best attempt so far. An optimizer cannot climb a hill it is not
    told the height of.
    """
    kept = f"{100 * attempt.edge_share:.0f}% of the log's distinct handoffs"
    standing = (
        ""
        if best_so_far is None
        else (
            f" Your balanced score this round is {attempt.score:.3f}; the best so far is "
            f"{best_so_far:.3f}."
            + (
                " You moved backwards — whatever changed since the best attempt made "
                "things worse, so revert toward it."
                if attempt.score < best_so_far
                else " That is the best yet."
            )
        )
    )

    if attempt.edge_share > 0.6:
        return (
            f"This model replayed {100 * attempt.coverage:.1f}% of complete cases, but it "
            f"kept {kept} at a threshold of {attempt.threshold}. That is memorising the "
            "log rather than describing the process: a handoff walked by one case out of "
            "hundreds is an exception, and a model containing all of them permits every "
            "one-off deviation as normal practice. Coverage is not the only goal. Aim for "
            "high coverage using a small fraction of the distinct handoffs, and say in "
            "the guidance how to tell a load-bearing rare edge from noise — for example "
            "by asking whether the cases needing it share anything, or whether dropping "
            "it disconnects part of the graph." + standing
        )

    if attempt.won:
        return (
            f"This model replayed {100 * attempt.coverage:.1f}% of complete cases using "
            f"only {kept}, beating the reference implementation's "
            f"{100 * attempt.matched_coverage:.1f}% at the same threshold. Keep what "
            "produced that. Do not add caution that would drop more edges, and do not "
            "loosen the threshold to chase the last points of coverage." + standing
        )

    return (
        f"This model replayed {100 * attempt.coverage:.1f}% of complete cases using "
        f"{kept}. The reference implementation, a plain directly-follows count at the "
        f"same threshold of {attempt.threshold}, reached "
        f"{100 * attempt.matched_coverage:.1f}%. You are {100 * attempt.gap:.1f} points "
        "behind at equal selectivity, so the model is missing handoffs real cases walk "
        "while keeping others that are not needed. Coverage is measured on whole traces: "
        "a case counts only if every consecutive pair in it is an edge, so one missing "
        "handoff disqualifies an otherwise ordinary case. Improve which edges you keep "
        "at a given threshold rather than lowering the threshold. Raising the threshold "
        "costs coverage but buys selectivity, and the balanced score often improves "
        "when you keep fewer edges, so tighter is worth testing before looser." + standing
    )


async def train(
    events: pl.DataFrame,
    db_path: Path,
    *,
    rounds: int = 3,
    sample_cases: int | None = 400,
    baseline_threshold: int = 25,
) -> Training:
    """Run the improvement loop over the miner's guidance."""
    from ai_functions.testing import RuntimeHarness

    memory = JSONMemoryBackend(Guidance, actor_id="miner", path=str(db_path))
    optimizer = TextGradOptimizer()
    agent = LearningMiner()
    compiled = agent.compiled("discover")
    log_csv = to_csv(events, sample_cases=sample_cases)
    # Denominator for selectivity: every distinct handoff the log contains.
    total_handoffs = directly_follows(events).height
    activities = events["activity"].n_unique()
    cases = events["case_id"].n_unique()
    training = Training()

    try:
        async with RuntimeHarness():
            for index in range(rounds):
                advice = await memory.recall("advice")
                traced: Any = await compiled.trace(advice, log_csv, activities, cases)
                discovered: Discovered = traced.value

                scored = grade(events, discovered, baseline_threshold=baseline_threshold)
                attempt = Attempt(
                    edge_share=round(scored.edges / max(total_handoffs, 1), 4),
                    index=index,
                    coverage=scored.coverage,
                    matched_coverage=scored.matched_coverage,
                    threshold=discovered.threshold_used,
                    states=scored.states,
                    edges=scored.edges,
                    guidance_chars=len(str(advice)),
                    method=discovered.method,
                )
                training.attempts.append(attempt)

                if index == rounds - 1:
                    break
                best = max((a.score for a in training.attempts[:-1]), default=None)
                await optimizer.step(
                    traced, feedback_for(attempt, best_so_far=best), backends=[memory]
                )

            training.final_guidance = str(await memory.recall("advice"))
    finally:
        memory.close()

    return training
