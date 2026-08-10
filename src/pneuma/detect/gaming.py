"""Probe a gate for rewarding gate-fitting, and an accepted set for being one mechanism.

Two probes, one defect: a check that can be satisfied without doing the work it stands for.
`probe_gate_fitting` asks whether a candidate can score near the gate's maximum while a
held-out evaluation the gate never saw scores it near the minimum — if so, the gate rewards
fitting the gate, and passing it is decoration. `probe_duplicate_mechanisms` asks whether a
checker's accepted set is materially one mechanism: accepted items that are pairwise
near-duplicates under a similarity function mean the checker admits one answer wearing many
coats, so its diversity of accepts is decoration. Both report through
`discrimination.Discrimination`, shared with `vacuity` and `objective`, because both are the
same question: can this check tell its two cases apart? The only pneuma import is
`.discrimination`; the rest is stdlib, so `tests/library/test_liftability.py` covers this
module automatically. Two things break if edited carelessly.

**A found exploit settles the verdict even under a bound.** The primitive returns None
whenever nothing separated and a reason was withheld, which protects a *negative* finding
from a truncated search. An exploit is a *positive* witness — a concrete candidate at the
gate's top with nothing behind it — and truncation cannot fake one, so `GateFitting`
reports it as False (decoration) with `withheld` cleared, the mirror of `vacuity` reporting
True for a witnessed violation under a truncated sweep. Letting the bound survive into the
verdict would let a caller shrink the budget until every gamed gate reads as unsettled.

**The bands are fractions of the observed spans, never absolute scores.** A gate scoring in
[0, 1] and one scoring in [0, 10000] must draw their near-maximum band at the same relative
place, so `edge_fraction` scales by `max - min` over the scored pool. The degenerate case is
a span of zero — a gate that scores every candidate identically — and that is reported as a
withheld reason rather than treated as a band, because a band of width zero at an undefined
edge silently makes every candidate an exploit or none of them one, depending on float luck.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import combinations, islice

from .discrimination import Discrimination

Evaluation = Callable[[object], float]
"""Called as `evaluation(candidate)`. May raise; an unscorable candidate is counted and
named in `withheld`, never silently skipped."""

Checker = Callable[[object], bool]
"""Called as `checker(item)`. True means the item is accepted."""

Similarity = Callable[[object, object], float]
"""Called as `similarity(left, right)`, returning a value in [0, 1] where 1 is identical."""

DEFAULT_EDGE_FRACTION = 0.05
"""Width of the near-maximum and near-minimum bands, as a fraction of the observed span.

Scale-free by construction: 0.05 means "within the top 5% of scores the gate actually
produced", whatever units the gate scores in.
"""

NEAR_DUPLICATE_JACCARD = 0.85
"""Token-set Jaccard at or above which two accepted items count as one mechanism.

Shepherd's CRO reflection contract used 0.85 as the near-duplicate threshold; kept as the
default so a caller who says nothing gets the measured value rather than an invented one.
"""

DEFAULT_CANDIDATE_BUDGET = 2048
"""Candidates examined before `probe_gate_fitting` stops and says so in `withheld`."""

DEFAULT_ITEM_BUDGET = 512
"""Items offered to the checker before `probe_duplicate_mechanisms` stops and says so.

Also caps the accepted set, so the pairwise comparison is at most C(512, 2) similarity
calls — bounded arithmetic, never a surprise quadratic on a caller's stream.
"""


@dataclass(frozen=True)
class Exploit:
    """One candidate that fits the gate without surviving the held-out evaluation.

    The candidate itself travels with the scores, because "the gate is gameable" without
    the input that games it leaves the caller re-running the search to see the defect.
    """

    candidate: object
    gate_score: float
    held_out_score: float

    def __str__(self) -> str:
        return f"gate={self.gate_score:g}, held-out={self.held_out_score:g} at {self.candidate!r}"


@dataclass(frozen=True)
class GateFitting:
    """Everything `probe_gate_fitting` looked at and everything it found.

    Attributes:
        subject: The gate, named as the report will name it.
        examined: Candidates taken from the pool, scorable or not.
        scored: Candidates that scored finitely on both evaluations.
        exploits: Candidates near the gate's maximum and near the held-out minimum.
        contained: Near-minimum held-out candidates the gate kept below its top band —
            the gate demonstrably telling a worthless candidate from a good one.
        gate_span: `max - min` of gate scores over the scored pool.
        held_out_span: `max - min` of held-out scores over the scored pool.
        withheld: Named reasons the verdict cannot be settled, per `Discrimination`.
    """

    subject: str
    examined: int
    scored: int
    exploits: tuple[Exploit, ...] = ()
    contained: int = 0
    gate_span: float = 0.0
    held_out_span: float = 0.0
    withheld: tuple[str, ...] = ()

    @property
    def gamed(self) -> bool:
        """True when a concrete candidate maximises the gate while failing held-out."""
        return bool(self.exploits)

    @property
    def discrimination(self) -> Discrimination:
        """This gate as the shared measurement: can it tell a worthless candidate apart?

        An observation is a scorable candidate; a separating one is a worthless candidate
        (near-minimum held-out) the gate held below its top band. A found exploit is the
        positive witness of decoration, so it reports zero separating with nothing
        withheld — settled False even if a budget was also hit; see the module docstring.

        The polarity is inverted from `vacuity`, and the containment count inherits the
        weak side of it: one exploit anywhere defeats any number of contained candidates,
        so containment is only evidence when the sweep completed. Under a bound with no
        exploit, `separating` is reported as zero — the primitive would otherwise let a
        truncated sweep's contained candidates settle True while the exploit sits in the
        unexamined tail.
        """
        if self.exploits:
            return Discrimination(
                subject=self.subject,
                observations=self.scored,
                separating=0,
                withheld=(),
                unit="candidate",
                kind="gate",
            )
        return Discrimination(
            subject=self.subject,
            observations=self.scored,
            separating=0 if self.withheld else self.contained,
            withheld=self.withheld,
            unit="candidate",
            kind="gate",
        )

    def report(self) -> str:
        lines = [str(self.discrimination)]
        lines.extend(f"  exploit: {exploit}" for exploit in self.exploits)
        if self.contained and not self.exploits:
            lines.append(
                f"  contained: {self.contained} near-minimum held-out candidate(s) "
                "held below the gate's top band"
            )
        return "\n".join(lines)


def _score(evaluation: Evaluation, candidate: object) -> float | None:
    """The evaluation at a candidate, or None if it raised or was not finite."""
    try:
        value = float(evaluation(candidate))
    except Exception:
        return None
    return value if math.isfinite(value) else None


def probe_gate_fitting(
    gate: Evaluation,
    held_out: Evaluation,
    candidates: Iterable[object],
    *,
    subject: str = "gate",
    edge_fraction: float = DEFAULT_EDGE_FRACTION,
    budget: int = DEFAULT_CANDIDATE_BUDGET,
) -> GateFitting:
    """Search the pool for a candidate that maximises the gate and fails held-out.

    The caller supplies the held-out evaluation, and it must be disjoint from the gate —
    a scorer over examples the gate never saw, or a different measurement entirely. This
    probe cannot check that disjointness; a held-out evaluation that is the gate under
    another name passes every gate by construction, which is the defect class this module
    exists to catch, so the report says what was compared and nothing more.

    Args:
        gate: The score the optimizer is selected on.
        held_out: The evaluation the gate is supposed to be a proxy for.
        candidates: The pool to sweep. Consumed lazily up to `budget`.
        subject: What the gate is, as the report will name it.
        edge_fraction: Band width as a fraction of each observed span. In (0, 0.5).
        budget: Candidates examined before stopping and saying so.
    """
    if not 0.0 < edge_fraction < 0.5:
        raise ValueError(f"edge_fraction must be in (0, 0.5), got {edge_fraction}")
    if budget < 1:
        raise ValueError(f"budget must be at least 1, got {budget}")

    pool = list(islice(candidates, budget + 1))
    truncated = len(pool) > budget
    pool = pool[:budget]

    scored: list[tuple[object, float, float]] = []
    unscorable = 0
    for candidate in pool:
        gate_score = _score(gate, candidate)
        held_out_score = _score(held_out, candidate)
        if gate_score is None or held_out_score is None:
            unscorable += 1
            continue
        scored.append((candidate, gate_score, held_out_score))

    withheld: list[str] = []
    if truncated:
        withheld.append(f"stopped at budget={budget} with candidates remaining in the pool")
    if unscorable:
        withheld.append(
            f"{unscorable} of {len(pool)} candidates could not be scored on both evaluations"
        )
    if not scored:
        withheld.append("no candidate scored finitely on both evaluations")
        return GateFitting(subject=subject, examined=len(pool), scored=0, withheld=tuple(withheld))

    gate_low = min(entry[1] for entry in scored)
    gate_high = max(entry[1] for entry in scored)
    held_low = min(entry[2] for entry in scored)
    held_high = max(entry[2] for entry in scored)
    gate_span = gate_high - gate_low
    held_span = held_high - held_low

    exploits: list[Exploit] = []
    contained = 0
    if gate_span <= 0.0:
        withheld.append(
            f"the gate scored all {len(scored)} scorable candidates identically "
            f"({gate_high:g}), so a near-maximum band cannot be drawn"
        )
    elif held_span <= 0.0:
        withheld.append(
            f"the held-out evaluation scored all {len(scored)} scorable candidates "
            f"identically ({held_high:g}), so it cannot name a worthless candidate"
        )
    else:
        gate_top = gate_high - edge_fraction * gate_span
        held_floor = held_low + edge_fraction * held_span
        for candidate, gate_score, held_out_score in scored:
            if held_out_score > held_floor:
                continue
            if gate_score >= gate_top:
                exploits.append(Exploit(candidate, gate_score, held_out_score))
            else:
                contained += 1

    return GateFitting(
        subject=subject,
        examined=len(pool),
        scored=len(scored),
        exploits=tuple(exploits),
        contained=contained,
        gate_span=gate_span,
        held_out_span=held_span,
        withheld=tuple(withheld),
    )


_TOKEN = re.compile(r"[a-z0-9]+")


def token_set_jaccard(left: object, right: object) -> float:
    """Jaccard similarity of the two texts' lowercased token sets.

    The stdlib default for `probe_duplicate_mechanisms`: case and punctuation do not make
    two accepted items different mechanisms, and token *sets* rather than sequences so a
    reordering does not either. Two empty token sets are identical, so they score 1.0.
    """
    left_tokens = set(_TOKEN.findall(str(left).lower()))
    right_tokens = set(_TOKEN.findall(str(right).lower()))
    if not left_tokens and not right_tokens:
        return 1.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


@dataclass(frozen=True)
class DuplicateMechanisms:
    """Everything `probe_duplicate_mechanisms` looked at and everything it found.

    Attributes:
        subject: The checker, named as the report will name it.
        examined: Items offered to the checker, up to the budget.
        accepted: Items the checker accepted.
        errors: Items the checker raised on, counted neither way.
        pairs: Accepted pairs compared.
        diverse: Pairs below the near-duplicate threshold — evidence of a second mechanism.
        threshold: The similarity at or above which a pair counts as one mechanism.
        most_distant: The least similar accepted pair seen, with its similarity, so a
            decoration verdict shows how close to diverse the set ever got.
        withheld: Named reasons the verdict cannot be settled, per `Discrimination`.
    """

    subject: str
    examined: int
    accepted: int
    errors: int = 0
    pairs: int = 0
    diverse: int = 0
    threshold: float = NEAR_DUPLICATE_JACCARD
    most_distant: tuple[object, object, float] | None = None
    withheld: tuple[str, ...] = ()

    @property
    def discrimination(self) -> Discrimination:
        """This checker as the shared measurement: does it admit more than one mechanism?

        An observation is an accepted pair; a separating one is a pair the similarity
        function puts below the threshold. Zero diverse pairs over a completed pool is the
        finding — the accepted set is materially one mechanism — and every bound this
        probe applied is in `withheld`, so a truncated pool reads as unsettled rather
        than as decoration.
        """
        return Discrimination(
            subject=self.subject,
            observations=self.pairs,
            separating=self.diverse,
            withheld=self.withheld,
            unit="accepted pair",
            kind="checker",
        )

    def report(self) -> str:
        lines = [str(self.discrimination)]
        if self.most_distant is not None:
            left, right, similarity = self.most_distant
            lines.append(
                f"  most distant accepted pair: {similarity:.3f} "
                f"(threshold {self.threshold:g}) between {left!r} and {right!r}"
            )
        return "\n".join(lines)


def probe_duplicate_mechanisms(
    checker: Checker,
    items: Iterable[object],
    *,
    similarity: Similarity | None = None,
    threshold: float = NEAR_DUPLICATE_JACCARD,
    subject: str = "checker",
    budget: int = DEFAULT_ITEM_BUDGET,
) -> DuplicateMechanisms:
    """Test whether the checker's accepted set is materially one mechanism.

    Offers each item to the checker, then compares every accepted pair under the
    similarity function. All pairs at or above the threshold means the checker's accepts
    collapse to near-duplicates of one answer — decoration. Any pair below it means a
    second mechanism got through — the checker's diversity is real. Fewer than two
    accepted items, or a pool cut by the budget, cannot settle the question and says so.

    Args:
        checker: Accepts or rejects an item. An item it raises on counts neither way
            and is named in `withheld`.
        items: The pool to offer. Consumed lazily up to `budget`.
        similarity: Pairwise similarity in [0, 1]. Defaults to `token_set_jaccard`.
        threshold: At or above this, a pair is one mechanism. In (0, 1].
        subject: What the checker is, as the report will name it.
        budget: Items offered before stopping and saying so.
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"threshold must be in (0, 1], got {threshold}")
    if budget < 1:
        raise ValueError(f"budget must be at least 1, got {budget}")
    measure = similarity if similarity is not None else token_set_jaccard

    pool = list(islice(items, budget + 1))
    truncated = len(pool) > budget
    pool = pool[:budget]

    accepted: list[object] = []
    errors = 0
    for item in pool:
        try:
            verdict = bool(checker(item))
        except Exception:
            errors += 1
            continue
        if verdict:
            accepted.append(item)

    withheld: list[str] = []
    if truncated:
        withheld.append(f"stopped at budget={budget} with items remaining in the pool")
    if errors:
        withheld.append(f"the checker raised on {errors} of {len(pool)} items")
    if len(accepted) < 2:
        withheld.append(
            f"only {len(accepted)} item(s) accepted, so pairwise diversity cannot be measured"
        )

    pairs = 0
    diverse = 0
    most_distant: tuple[object, object, float] | None = None
    for left, right in combinations(accepted, 2):
        score = float(measure(left, right))
        pairs += 1
        if score < threshold:
            diverse += 1
        if most_distant is None or score < most_distant[2]:
            most_distant = (left, right, score)

    return DuplicateMechanisms(
        subject=subject,
        examined=len(pool),
        accepted=len(accepted),
        errors=errors,
        pairs=pairs,
        diverse=diverse,
        threshold=threshold,
        most_distant=most_distant,
        withheld=tuple(withheld),
    )
