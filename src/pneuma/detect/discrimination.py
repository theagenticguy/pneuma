"""One question both detectors in this package are asking: does this check discriminate?

`vacuity` sweeps reachable states and counts how many break a rule. `objective` sweeps a
scoring function's domain and asks whether shrinking the answer ever costs score. Those
read as different problems and they are not. A rule that no reachable state can break
cannot tell a compliant run from a violation. A scoring term whose value never moves across
the space the loop searches cannot tell a good answer from a bad one. Both are checks that
pass without ever having been in a position to fail, and reporting either as a pass is the
same defect.

This module is the shape they share, and it is deliberately small: three integers-and-strings
worth of state and one three-valued verdict. Nothing here knows what a state or a score is.

## The three-valued verdict, and why it is not a boolean

`discriminates` is True / False / None, never a bare boolean, and this is the one design
point that matters. The third value is not optional: under a boolean, "the search found no
witness" and "the search gave up before it could find one" collapse into the same False,
which reports a truncated sweep as a confident finding of decoration. An abandoned search
is not evidence about the states it never reached, so the two have to be different objects.

    True   at least one observation separated the cases. The check has teeth here.
    False  every observation was examined and none separated them. That is the finding.
    None   the measurement did not settle. Either a bound was hit, or the question could
           not be posed at all. Not a pass, and not a finding either.

`withheld` is what makes None reachable, and it is a tuple of *named reasons* rather than a
flag. Every bound this package applies has to be visible in the result it produced, so a
reader can tell "we swept 200,000 states and stopped" from "the grid was too coarse to
change the answer's size", which are unrelated failures with unrelated fixes.

## observations == 0 is a finding, not an abstention, and that asymmetry is deliberate

An empty observation set with no withheld reason means the search *completed* and the check
was never in a position to apply. That is `vacuity`'s `unreachable_scope`: the rule's
subject is not reachable, the sweep proved it, and the rule is decoration. So it reports
False.

A caller whose observation set is empty *because of its own bound* must say so in
`withheld`, and then it reports None. The difference is whether the emptiness is a property
of the subject or of the harness, and only the caller knows which.

## Relationship to `memory.Discrimination`

`pneuma.memory.turso_backend.Discrimination` is a third instance of this idea, on retrieval:
it asks whether an embedding lands queries the corpus answers closer than queries it does
not, and its `discriminates` is three-valued for exactly the reason above. It is not
expressed through this primitive, and that is a deliberate limit rather than an oversight.
Its verdict is not a count of separating observations but a *margin* between two distance
distributions, plus an internal-consistency check (an entry that does not retrieve itself).
Forcing a margin into a numerator would either lose the margin or make `separating` a
number with no meaning, and inventing a shared interface that only looks unified is worse
than two records that agree on the verdict's shape. The shape is what generalises; the
measurement does not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Discrimination:
    """A measurement of whether one check can tell its two cases apart.

    Attributes:
        subject: What was measured, named as the report will name it.
        observations: How many opportunities the check was examined over. A reference
            scale, not a strict denominator: a caller may gather `separating` at a wider
            level than it counted `observations` at, which `vacuity` does when a relaxed
            sweep reaches more states than the exact one. So this is not validated
            against `separating`, and no ratio is exposed that would imply it was.
        separating: How many of them the check actually distinguished. Zero is the finding.
        withheld: Named reasons the verdict cannot be settled. Non-empty makes
            `discriminates` None whenever nothing separated. Every bound a caller applies
            belongs here, because a bound that does not appear in its own result is a
            silent cap.
        unit: What an observation is, for the report. "reachable state" reads better
            than "observation".
        kind: What sort of thing the subject is, for a report grouping several.
    """

    subject: str
    observations: int
    separating: int
    withheld: tuple[str, ...] = ()
    unit: str = "observation"
    kind: str = "check"

    def __post_init__(self) -> None:
        if self.observations < 0 or self.separating < 0:
            raise ValueError(
                f"{self.subject}: counts must be non-negative, got "
                f"observations={self.observations}, separating={self.separating}"
            )

    @property
    def discriminates(self) -> bool | None:
        """Can this check tell its two cases apart? See the module docstring."""
        if self.separating:
            return True
        if self.withheld:
            return None
        return False

    @property
    def settled(self) -> bool:
        """Whether the measurement produced a verdict at all."""
        return self.discriminates is not None

    @property
    def idle(self) -> bool:
        """True only for the finding: examined in full, and it never fired."""
        return self.discriminates is False

    def because(self, reason: str) -> Discrimination:
        """The same measurement with one more withheld reason. Used while a sweep runs."""
        return Discrimination(
            subject=self.subject,
            observations=self.observations,
            separating=self.separating,
            withheld=(*self.withheld, reason),
            unit=self.unit,
            kind=self.kind,
        )

    def __str__(self) -> str:
        plural = self.unit if self.observations == 1 else f"{self.unit}s"
        counted = f"{self.separating} of {self.observations} {plural} separating"
        if self.discriminates is None:
            return f"{self.subject}: UNSETTLED ({counted}); " + "; ".join(self.withheld)
        if self.discriminates:
            return f"{self.subject}: discriminates ({counted})"
        if not self.observations:
            return f"{self.subject}: DOES NOT DISCRIMINATE (never in a position to fire)"
        return f"{self.subject}: DOES NOT DISCRIMINATE ({counted})"
