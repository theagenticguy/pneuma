"""One question both detectors in this package are asking: does this check discriminate?

A rule no reachable state can break cannot tell a compliant run from a violation. A scoring
term whose value never moves across the space the loop searches cannot tell a good answer
from a bad one. `vacuity` and `objective` report through this one shape because those are the
same defect: a check that passes without ever having been in a position to fail. Deliberately
small — three integers-and-strings of state and one verdict, and nothing here knows what a
state or a score is. Rationale: `docs/design/discrimination.md`.

Two things break if edited carelessly.

**`discriminates` is three-valued and must never become a bare boolean.** Under a boolean,
"the search found no witness" and "the search gave up before it could find one" collapse into
the same False, reporting a truncated sweep as a confident finding of decoration. An
abandoned search is not evidence about the states it never reached.

    True   at least one observation separated the cases. The check has teeth here.
    False  every observation was examined and none separated them. That is the finding.
    None   the measurement did not settle. Either a bound was hit, or the question could
           not be posed at all. Not a pass, and not a finding either.

`withheld` is what makes None reachable, and it is a tuple of *named reasons* rather than a
flag, so a reader can tell "swept 200,000 states and stopped" from "the grid was too coarse
to change the answer's size" — unrelated failures with unrelated fixes.

**`observations == 0` with no `withheld` reason is a finding, not an abstention.** It means
the search completed and the check was never in a position to apply — `vacuity`'s
`unreachable_scope` — so it reports False. A caller whose observation set is empty because of
its *own* bound must name that in `withheld` and gets None. Only the caller knows whether the
emptiness belongs to the subject or to the harness.

`memory.turso_backend.Discrimination` is a third instance of this idea, on retrieval, and it
is deliberately not expressed through this primitive: its verdict is a *margin* between two
distance distributions plus a self-retrieval check, and forcing a margin into a numerator
would either lose the margin or make `separating` a number with no meaning. The verdict's
shape generalises; the measurement does not.
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
