"""Adversaries that search for an input scoring well while being obviously worthless.

`objective.py` enumerates the degenerate inputs that follow from a declared `Structure`.
That is mechanical and it is strong where it applies: it found the transcript-log defect
that a hand-written list of degenerate inputs missed for the whole session. But it can only
find what the structure implies. An objective can be gamed in a way nobody put in the
declaration, and the only mechanism that can find those is a search.

This module is that search, as a `Search` callable `probe` accepts. It lives beside the
prober rather than inside it for one reason worth stating: `objective.py` imports nothing
from pneuma, has no network dependency, and a suite with no credentials runs every check it
has. Putting a Bedrock call in that file would make the arithmetic half of the prober
untestable offline. `probe(..., search=None)` is the whole prober minus this, and that is
the seam.

## What an adversary sees

Everything the prober does, and one thing more. The `Brief` carries the axes, the swept
grid, the in-domain ceiling every candidate is measured against, the structure if there is
one, and — this is the part that matters — a tool that *calls the objective*. That is what
makes it a search rather than a guess. An adversary that could only read a description
would be reasoning about what the arithmetic probably does; one that can evaluate it
probes, gets a number back, and revises.

`Brief.source` carries the scoring code when the caller supplies it. Reading the source
is how an adversary finds a clamp that hides a mis-measurement, a guard tested with exact
float equality, a term that cancels. Sampling 21 points does not show any of that.

## How "obviously worthless" is adjudicated without a human

Two mechanisms, and neither of them is the proposer's own opinion.

**Arithmetic, in `probe`, which cannot be argued with.** A candidate becomes a
`degenerate-optimum` finding only if re-scoring it in `_check_degenerate` puts it at or
above the grid maximum. `Verdict.upheld` is not a vote, it is a comparison. So an adversary
that hallucinates a triumphant input produces nothing: the point is scored, it loses, and
the report says so. This is the half that makes a fabricated candidate harmless.

**A judge panel, for the half that is a judgment call.** Whether an input is *worthless*
is not decidable from its score — that is the entire premise, since a worthless input that
scored badly would not be a defect. So `judge` asks a panel, and the panel's ballot is
structured: a judge answers `worthless: bool` plus a reason, and `MIN_AGREEMENT` of them
must say yes. A single judge is the failure mode this session is about, because one judge
agreeing with one proposer is two samples of the same prior.

The judges' guard against rubber-stamping is that they are asked to reject, with a
concrete example of what rejecting looks like, and the panel is measured: `Verdict` keeps
every ballot, so `Panel.rejection_rate` reports how often judges actually said no across a
run. A panel that has never rejected anything is a check that cannot fire, and the number
is in the report rather than assumed. This is a measurement, not a proof — see "What this
cannot do".

## Why the adversaries get distinct angles

`ANGLES` is five prompts, not one prompt run five times. Diversity is doing the work here:
identical adversaries at temperature-equivalent sampling explore the same neighbourhood,
and the neighbourhood is chosen by the prior they share. The five angles correspond to the
five ways this project's own objective was historically wrong, which is the only grounded
basis available for picking them: emptiness, escape, cancellation, clamp exploitation, and
tie-seeking. `docs/case-study.md` section 10 is the source.

No silent caps. The fan-out is `len(angles)` adversaries, each proposing at most
`MAX_PER_ANGLE` candidates, judged by `PANEL_SIZE` judges needing `MIN_AGREEMENT` yes
votes. Every one of those is an argument to `adversarial_search` and every one lands in the
`Verdict` the caller can read.

## What this cannot do

The judge panel is an LLM panel, so "obviously worthless" is adjudicated by models sharing
a great deal of prior with the proposers. `rejection_rate` measures that the panel *does*
reject, which is weaker than proving it rejects the right things, and there is no
mechanism here that would notice a panel and a proposer being wrong in the same direction.
The arithmetic half has no such hole, which is why it is the half that decides whether a
finding is recorded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from strands import tool

from .objective import Brief, Degenerate, Sample

logger = logging.getLogger(__name__)


class Candidate(BaseModel):
    """One input an adversary claims scores well while being worthless."""

    point: dict[str, float] = Field(
        description="The input, one value per domain name, exactly as the objective takes it"
    )
    label: str = Field(description="Short name for this input, e.g. 'keeps a single handoff'")
    worthless_because: str = Field(
        description=(
            "Why this input is worthless as an answer, in terms of what it represents rather "
            "than what it scores. A high score is the premise, not the argument."
        )
    )
    observed_score: float | None = Field(
        default=None, description="The score you measured with score_at, if you called it"
    )


class Attack(BaseModel):
    """One adversary's report."""

    candidates: list[Candidate] = Field(
        default_factory=list,
        description=(
            "Inputs that score at or above the ceiling while being worthless. May be empty."
        ),
    )
    searched: str = Field(
        default="",
        description="What you tried, including what did not work. An empty result with a "
        "description of the search is a real answer.",
    )


class Ballot(BaseModel):
    """One judge's verdict on one candidate."""

    worthless: bool = Field(
        description=(
            "True only if this input is worthless as an answer to the problem. False if it "
            "is a legitimate answer that happens to score well, or if the claim is unsupported."
        )
    )
    reason: str = Field(
        description="One sentence. What makes it worthless, or why the claim fails."
    )


ANGLES: tuple[tuple[str, str], ...] = (
    (
        "emptiness",
        "Find the emptiest possible answer that still scores at or above the ceiling. An "
        "answer that represents almost nothing — the fewest items kept, the smallest model, "
        "the narrowest selection — while scoring as well as a full one. This is the failure "
        "where a term that was supposed to punish an empty answer has stopped discriminating.",
    ),
    (
        "escape",
        "Find an input outside the declared range that scores above everything inside it. A "
        "training loop searches for the argmax, so a reward outside the declared domain is a "
        "reward whether or not anyone believes it is reachable. Try values just past each "
        "bound, then far past, then negative.",
    ),
    (
        "cancellation",
        "Read the arithmetic and find where terms cancel, a factor becomes zero, a "
        "denominator nearly vanishes, or a product collapses. Then find the input that lands "
        "there. Look for guards tested with exact equality: they miss by one float.",
    ),
    (
        "clamp",
        "Find where a clamp, a `min`, a `max`, a `round`, or an `abs` makes two genuinely "
        "different answers score identically, and pick the worse of the two. A clamp hides "
        "a mis-measurement rather than repairing it, so the input it hides is the target.",
    ),
    (
        "tie",
        "Do not look for a higher score, look for a tie. Selection by `max` picks whichever "
        "tied input it saw first, so an input that merely equals the optimum is already a "
        "win for the optimizer and is invisible to a check looking for 'scores higher'. Find "
        "the worst answer that ties the best one.",
    ),
)
"""Five adversaries with five distinct mandates, one per historical failure mode.

Grounded in `docs/case-study.md` section 10 rather than invented: emptiness is failure one,
cancellation and clamp are the pole that shipped and the clamp added for it, escape is the
out-of-domain reward, and tie is the property `_check_degenerate` compares with `>=`.
"""

MAX_PER_ANGLE = 3
"""Candidates one adversary may propose. A cap, and it is reported in the `Verdict`."""

PANEL_SIZE = 3
"""Judges per candidate. Odd, and more than one, because one judge plus one proposer is
two samples of the same prior."""

MIN_AGREEMENT = 2
"""Yes votes needed to uphold a candidate as worthless. A majority of `PANEL_SIZE`."""


@dataclass(frozen=True)
class Judged:
    """One candidate, its ballots, and whether the arithmetic agreed."""

    candidate: Candidate
    ballots: tuple[Ballot, ...]
    sample: Sample
    ceiling: float

    @property
    def yes(self) -> int:
        return sum(1 for b in self.ballots if b.worthless)

    @property
    def worthless(self) -> bool:
        """Did the panel uphold it. A count of ballots, never a proposer's own claim."""
        return self.yes >= MIN_AGREEMENT

    @property
    def reaches_ceiling(self) -> bool:
        """Does the arithmetic agree it scores well. The half that cannot be argued with."""
        return self.sample.finite and (self.sample.value or 0.0) >= self.ceiling - 1e-9

    @property
    def upheld(self) -> bool:
        return self.worthless and self.reaches_ceiling

    def __str__(self) -> str:
        scored = f"{self.sample.value:.4f}" if self.sample.finite else str(self.sample.error)
        return (
            f"{self.candidate.label!r} at {self.candidate.point}: scored {scored} against "
            f"ceiling {self.ceiling:.4f}, panel {self.yes}/{len(self.ballots)} worthless"
        )


@dataclass
class Verdict:
    """Everything the search did, including what it failed to find.

    A negative result is the point of keeping this: "five adversaries with distinct
    mandates found nothing enumeration did not" is a publishable measurement about the
    search, and it is only available if the search records its misses.
    """

    judged: list[Judged] = field(default_factory=list)
    searches: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    angles: tuple[str, ...] = ()
    panel_size: int = PANEL_SIZE
    min_agreement: int = MIN_AGREEMENT
    max_per_angle: int = MAX_PER_ANGLE

    @property
    def upheld(self) -> list[Judged]:
        return [j for j in self.judged if j.upheld]

    @property
    def ballots(self) -> list[Ballot]:
        return [b for j in self.judged for b in j.ballots]

    @property
    def rejection_rate(self) -> float | None:
        """Share of ballots that said "not worthless". None when no ballot was cast.

        The measurement that says whether the panel can fire at all. A panel that upheld
        everything it ever saw is a check that cannot reject, which is the defect class this
        whole module was written against, so the number is reported rather than assumed.
        """
        cast = self.ballots
        if not cast:
            return None
        return sum(1 for b in cast if not b.worthless) / len(cast)

    def degenerates(self) -> list[Degenerate]:
        """Upheld candidates, as the prober's own unit. Re-scored there, not trusted here."""
        return [
            Degenerate(
                label=j.candidate.label,
                point=dict(j.candidate.point),
                found_by=f"adversary/{self.angle_of(j)}",
                worthless_because=(
                    f"{j.candidate.worthless_because} "
                    f"(panel {j.yes}/{len(j.ballots)}: "
                    + "; ".join(b.reason for b in j.ballots if b.worthless)
                    + ")"
                ),
            )
            for j in self.upheld
        ]

    def angle_of(self, judged: Judged) -> str:
        return self._angles.get(id(judged), "unknown")

    _angles: dict[int, str] = field(default_factory=dict)

    def report(self) -> str:
        lines = [
            f"adversarial search: {len(self.angles)} adversaries "
            f"({', '.join(self.angles)}), at most {self.max_per_angle} candidates each, "
            f"{self.panel_size} judges needing {self.min_agreement} to uphold"
        ]
        rate = self.rejection_rate
        lines.append(
            f"  {len(self.judged)} candidate(s) proposed, {len(self.upheld)} upheld; "
            + (
                f"judges rejected {rate:.0%} of {len(self.ballots)} ballots"
                if rate is not None
                else "no ballot was cast"
            )
        )
        if rate == 0.0 and self.ballots:
            lines.append(
                "  warning: the panel upheld every candidate it saw, so on this run there is "
                "no evidence it can reject. Treat its verdicts as unmeasured."
            )
        for judged in self.judged:
            mark = "upheld" if judged.upheld else "rejected"
            lines.append(f"  [{mark}] {judged}")
        for angle, searched in self.searches:
            if searched:
                lines.append(f"  {angle} searched: {searched}")
        for angle, error in self.errors:
            lines.append(f"  {angle} failed: {error}")
        return "\n".join(lines)


def _brief_text(brief: Brief) -> str:
    """What the adversary reads. The prober's own view rendered, plus the source."""
    axes = "\n".join(
        f"- `{axis.name}`: declared [{axis.low:g}, {axis.high:g}]"
        + (", integral" if axis.integral else "")
        + (f", feasible {axis.feasible}" if axis.feasible else "")
        + (f", bound claimed established by: {axis.bounded_by}" if axis.bounded_by else "")
        for axis in brief.axes
    )
    finite = [s for s in brief.samples if s.finite]
    ranked = sorted(finite, key=lambda s: -(s.value or 0.0))
    def show(sample: Sample) -> str:
        return f"  {json.dumps(sample.point)} -> {sample.value:.6g}"
    top = "\n".join(show(s) for s in ranked[:6])
    bottom = "\n".join(show(s) for s in ranked[-4:])
    structure = ""
    if brief.structure is not None:
        sized = [
            (s.point, brief.structure.measure(s.point), brief.structure.is_viable(s.point))
            for s in ranked[:6]
        ]
        structure = (
            f"\n## The answer's size\n\n`size` counts {brief.structure.units}. Smaller means "
            "the answer represents less. At the highest-scoring points:\n"
            + "\n".join(
                f"  {json.dumps(p)} -> size {m}"
                + ("" if v else " (not a viable answer at all)")
                for p, m, v in sized
                if m is not None
            )
        )
    source = (
        f"\n## The scoring code\n\n```python\n{brief.source}\n```"
        if brief.source
        else "\n## The scoring code\n\nNot supplied. You can only probe it with `score_at`."
    )
    return f"""## The axes ({brief.space.value} space)

{axes}

## The ceiling you must reach

The best in-domain score found by sweeping is **{brief.ceiling:.6g}**, at
{json.dumps(brief.best_point)}. A candidate is only interesting if it scores at or above
that. A tie counts: selection by `max` picks whichever tied input it saw first.

That point is the *incumbent*, not a blessed answer, and this distinction is load-bearing.
Whether the objective's optimum is any good is the entire question under review, so
"scores the same as the sweep's best" is not by itself evidence that an input is
acceptable. If the incumbent is itself a worthless answer, everything tying it is too.

## Highest-scoring swept points

{top}

## Lowest-scoring swept points

{bottom}
{structure}
{source}"""


def _score_tool(brief: Brief) -> tuple[object, Callable[[dict[str, float]], str]]:
    """The tool that makes this a search, plus the plain function behind it.

    A plain `@tool` closure rather than an `@ai_function` on a class method, and this is
    load-bearing rather than stylistic. `AIFunction` is not a descriptor, so
    `@ai_function` on a method returns the same object for the class and every instance
    and `self` is stripped from the tool schema. An agent handed such a method calls it
    with zero arguments, gets a `TypeError`, and Strands swallows that into a tool error:
    the tool is then permanently dead and the run merely degrades. Verified against
    `ai_functions` at the pinned revision — `AIFunction.__get__` does not exist and
    `instance.method is Class.method` is True. `examples/compose_research_team.py:103`
    does the broken thing.
    """

    def measure(point: dict[str, float]) -> str:
        """What the tool returns. Kept as a plain function so a test can call it.

        `@tool` produces a `DecoratedFunctionTool`, which is not callable and exposes no
        handle on the wrapped function, so a test that only had the decorated object could
        assert the schema and never that the tool returns the objective's real value.
        """
        sample = brief.score(point)
        if sample.error is not None:
            return f"raised: {sample.error}"
        return f"score = {sample.value!r} (ceiling to beat or tie: {brief.ceiling!r})"

    @tool(name="score_at")
    def score_at(point: dict[str, float]) -> str:
        """Score the objective at one input. Keys must be the axis names.

        Call this as often as you like. Returns the score, or the error if it raised.
        """
        return measure(point)

    return score_at, measure


def adversarial_search(
    *,
    angles: Sequence[tuple[str, str]] = ANGLES,
    max_per_angle: int = MAX_PER_ANGLE,
    panel_size: int = PANEL_SIZE,
    min_agreement: int = MIN_AGREEMENT,
    model: object | None = None,
    on_verdict: object | None = None,
):
    """Build a `Search` that fans out LLM adversaries and adjudicates with a judge panel.

    Returns a callable `probe(search=...)` accepts. It is synchronous because `probe` is,
    and it bridges through `ai_functions.run_blocking`, which handles being called from
    inside a running loop as well as outside one.

    Args:
        angles: `(name, mandate)` pairs. One adversary per entry, each with a distinct
            mandate; see `ANGLES` for why distinctness rather than count is the design.
        max_per_angle: Candidates one adversary may propose. Reported in the `Verdict`.
        panel_size: Judges per candidate.
        min_agreement: Yes votes needed to uphold a candidate as worthless.
        model: A `strands` model for every thread. `None` takes the library default.
        on_verdict: Called with the `Verdict` when the search finishes, so a caller can
            read the search's own account of itself including what it failed to find.
    """

    def search(brief: Brief) -> list[Degenerate]:
        from ai_functions import run_blocking

        verdict = run_blocking(
            lambda: _run(
                brief,
                angles=tuple(angles),
                max_per_angle=max_per_angle,
                panel_size=panel_size,
                min_agreement=min_agreement,
                model=model,
            )
        )
        if callable(on_verdict):
            on_verdict(verdict)
        logger.info("adversarial search verdict:\n%s", verdict.report())
        return verdict.degenerates()

    return search


async def _run(
    brief: Brief,
    *,
    angles: tuple[tuple[str, str], ...],
    max_per_angle: int,
    panel_size: int,
    min_agreement: int,
    model: object | None,
) -> Verdict:
    """Fan out the adversaries in parallel, then judge every candidate in parallel.

    Two phases rather than one because judging is the adjudication and a proposer must not
    be able to influence it. An adversary never sees another's candidates or any ballot.
    """
    import asyncio

    from ai_functions import ai_function

    overrides: dict[str, object] = {} if model is None else {"model": model}
    context = _brief_text(brief)
    scorer, _ = _score_tool(brief)

    @ai_function[Attack](
        description="Search for an input that scores well and is worthless",
        tools=[scorer],
        **overrides,
    )
    def adversary(angle: str, mandate: str, context: str, budget: int) -> None:
        """You are an adversary reviewing a scoring function that is about to be used as the
        objective of a training loop. Your job is to break it.

        Find an input that **scores at or above the ceiling while being an obviously
        worthless answer**. Worthless means: if a model or a decision produced this, a
        reasonable reviewer would say it answers nothing, whatever the score says.

        Your specific mandate is **{angle}**:

        {mandate}

        {context}

        RULES:
        - Use `score_at` to actually evaluate inputs. A candidate you have not scored is a
          guess, and a guess will be re-scored and thrown out. Probe freely.
        - Report at most {budget} candidates, and only ones you measured at or above the
          ceiling. Fewer is better than padded.
        - If you find nothing, return an empty list and say what you tried in `searched`.
          An honest empty result is a real answer and is worth more than a weak candidate.
        - `worthless_because` must argue from what the input *represents*, not from its
          score. "It scores 0.9" is not a reason. "It keeps one handoff out of 166, so the
          model describes a single transition" is.
        """

    @ai_function[Ballot](
        description="Judge whether a proposed input is genuinely a worthless answer",
        **overrides,
    )
    def judge(claim: str, context: str) -> None:
        """You are judging one claim about a scoring function, made by an adversary trying to
        break it. Adversaries are rewarded for finding things, so they overclaim.

        {context}

        The claim:

        {claim}

        Decide one thing only: **is this input a worthless answer?** Not whether it scores
        well — that has already been measured independently and is not your question.

        Answer `worthless: true` only if a reasonable reviewer would look at what this input
        represents and say it answers nothing.

        Answer `worthless: false` if any of these hold, and expect to be answering false
        often:
        - it is a legitimate answer that happens to score well, which is what a sound
          objective's optimum looks like
        - the argument for worthlessness is only that the score is high
        - the argument is about what *could* be wrong rather than what this input is

        One reason that is **not** valid, and it is the one that is easiest to reach for:
        "this merely ties the sweep's best point, so it is a restatement of the known-good
        optimum". The sweep's best point is the incumbent, and whether the incumbent is any
        good is the whole question under review. Judge what the input *represents* against
        the problem, never against the incumbent. If the incumbent is itself worthless then
        everything tying it is worthless, and answering false there is precisely the failure
        you are here to prevent: it would clear an objective whose optimum answers nothing.

        For a concrete example of a false: on an objective over a mining threshold, "the
        threshold at an interior peak, keeping 15 of 166 handoffs and replaying 86% of
        cases" is a *good* answer, and an adversary calling it degenerate because 15 is a
        small number is overclaiming. Reject that one — 86% of cases replayed is a real
        result.

        For a concrete example of a true: on the same objective, "a threshold keeping 1
        handoff out of 166, replaying 2% of cases" is worthless. It is one transition; it
        expresses no ordering, no branching, and no process. It stays worthless if the
        sweep's best point also replays 2% of cases — that would mean the objective has
        stopped discriminating, which is the defect, not a defence.
        """

    async def attack(angle: str, mandate: str) -> tuple[str, Attack | None, str | None]:
        try:
            result = await adversary(
                angle=angle, mandate=mandate, context=context, budget=max_per_angle
            )
        except Exception as error:  # noqa: BLE001 — one adversary dying must not end the search
            return angle, None, f"{type(error).__name__}: {error}"
        return angle, result, None

    attacks = await asyncio.gather(*(attack(name, mandate) for name, mandate in angles))

    verdict = Verdict(
        angles=tuple(name for name, _ in angles),
        panel_size=panel_size,
        min_agreement=min_agreement,
        max_per_angle=max_per_angle,
    )
    proposals: list[tuple[str, Candidate]] = []
    for angle, result, error in attacks:
        if error is not None:
            verdict.errors.append((angle, error))
            continue
        if result is None:
            continue
        verdict.searches.append((angle, result.searched))
        for candidate in result.candidates[:max_per_angle]:
            proposals.append((angle, candidate))

    async def vote(candidate: Candidate) -> tuple[Ballot, ...]:
        claim = (
            f"input: {json.dumps(candidate.point)}\n"
            f"label: {candidate.label}\n"
            f"the adversary's argument: {candidate.worthless_because}"
        )

        async def one() -> Ballot | None:
            try:
                return await judge(claim=claim, context=context)
            except Exception as error:  # noqa: BLE001
                logger.warning("a judge failed: %s", error)
                return None

        cast = await asyncio.gather(*(one() for _ in range(panel_size)))
        return tuple(b for b in cast if b is not None)

    ballots = await asyncio.gather(*(vote(candidate) for _, candidate in proposals))

    for (angle, candidate), cast in zip(proposals, ballots, strict=True):
        judged = Judged(
            candidate=candidate,
            ballots=cast,
            sample=brief.score(candidate.point),
            ceiling=brief.ceiling,
        )
        verdict.judged.append(judged)
        verdict._angles[id(judged)] = angle
    return verdict
