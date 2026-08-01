"""Mechanical detectors for properties that pass without ever being tested.

`discrimination` is the one question the two detectors here are both asking: can this check
tell its two cases apart, or does it pass because it was never in a position to fail? Read
that file first; it is the twenty lines they share, and its docstring records where the
shared shape stops being shared.

`vacuity` asks it about a *rule*, by enumerating reachable states and counting how many break
it. It imports nothing from pneuma. `adapter` binds pneuma's `Process` IR to it and is the
file to replace when lifting this elsewhere; it is imported lazily through this module's
`__getattr__`, so importing this package does not pull the IR in. `discrimination`, `vacuity`,
and `objective` are pure stdlib and lift out of this project unchanged, which is a property
`tests/library/test_liftability.py` measures rather than asserts.

`objective` asks it about a *scoring function*, by sweeping the domain before a training loop
runs against it rather than after. It also imports nothing from pneuma; the consumer supplies
the callable and the declared domain. `Component` is where it asks the question about one term
of the arithmetic, which is what names the cause a degenerate optimum is the symptom of.

The one-liner a checker consumer wants:

    from pneuma.detect import audit_process, witness_counts

    report = audit_process(process)     # every invariant, with causes and traces
    tla.check(process).with_witnesses(witness_counts(process))

And the one a training loop wants, before its first round:

    from pneuma.detect import Domain, Space, Structure, probe

    probe(
        score, domains, space=Space.DECISION,
        structure=Structure(size=how_much_answer_is_this, units="handoffs kept"),
    ).raise_if_pathological()

`structure` rather than a list of degenerate inputs, because a hand-written list of bad
answers is written by the same hand as the scoring formula. `adversary.py` adds the LLM
half — a fan-out of adversaries searching for an input that scores well and is worthless —
and is imported separately because it is the only file here that needs a model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .discrimination import Discrimination
from .objective import (
    DEFAULT_REACH,
    DEFAULT_REFINE,
    DEFAULT_RESOLUTION,
    MAX_CORNERS,
    Brief,
    Component,
    Degenerate,
    Domain,
    Finding,
    Objective,
    ObjectiveRefused,
    Probe,
    Sample,
    Search,
    Severity,
    Space,
    Structure,
    Term,
    probe,
    probe_feedback,
)
from .vacuity import (
    DEFAULT_LIMIT,
    RELAXATIONS,
    Audit,
    Count,
    Relaxation,
    Rule,
    RuleVerdict,
    SweepError,
    System,
    Trace,
    Visit,
    audit,
    contradictory,
    sweep,
)
from .vacuity import Sweep as ReachabilitySweep

if TYPE_CHECKING:
    from .adapter import (
        DEADLOCK_RULE,
        TYPE_RULE,
        ProcessSystem,
        audit_process,
        contradictions_in,
        rule_for,
        rules_for,
        structural_rules,
        system_for,
        verdict_for,
        witness_counts,
    )

# Everything `adapter` exports, resolved on first attribute access rather than at import.
_ADAPTER_EXPORTS = frozenset(
    {
        "DEADLOCK_RULE",
        "TYPE_RULE",
        "ProcessSystem",
        "audit_process",
        "contradictions_in",
        "rule_for",
        "rules_for",
        "structural_rules",
        "system_for",
        "verdict_for",
        "witness_counts",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve the `adapter` names lazily, so importing this package stays liftable.

    `adapter` is the seam that binds `vacuity` to pneuma's `Process` IR, so it is the one
    file here that imports from `pneuma` and therefore drags in pydantic. Importing it
    eagerly made `from pneuma.detect import probe` depend on the whole IR, which contradicts
    the claim the three deterministic modules make: that they are pure stdlib and can be
    lifted out of this project unchanged. `tests/library/test_liftability.py` measures that claim.

    `__getattr__` rather than telling callers to import `pneuma.detect.adapter` themselves,
    because `audit_process` is half of this package's documented one-liner and moving it
    would be a breaking rename for a property no caller cares about. Callers keep the flat
    surface; the cost lands on the first attribute access instead of on import.
    """
    if name in _ADAPTER_EXPORTS:
        from . import adapter

        return getattr(adapter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# `Sweep` is not exported under that name: `vacuity.Sweep` (a reachability sweep over
# states) and `objective.Sweep` (a grid sweep over a scoring domain) are unrelated types
# that collide, and the flat re-export silently resolved to whichever module imported last.
# `vacuity.Sweep` is re-exported as `ReachabilitySweep`; reach `objective.Sweep` through
# `pneuma.detect.objective`. Neither module needs a rename to become its own package.
#
# `adversary` is deliberately not imported here at all, not even lazily. It is the only file
# under `detect` that reaches for a model, and it has no place in the flat surface. Import it
# from `pneuma.detect.adversary` when you want the LLM half.
__all__ = [
    "DEADLOCK_RULE",
    "DEFAULT_LIMIT",
    "DEFAULT_REACH",
    "DEFAULT_REFINE",
    "DEFAULT_RESOLUTION",
    "MAX_CORNERS",
    "RELAXATIONS",
    "TYPE_RULE",
    "Audit",
    "Brief",
    "Component",
    "Count",
    "Degenerate",
    "Discrimination",
    "Domain",
    "Finding",
    "Objective",
    "ObjectiveRefused",
    "Probe",
    "ProcessSystem",
    "ReachabilitySweep",
    "Relaxation",
    "Rule",
    "RuleVerdict",
    "Sample",
    "Search",
    "Severity",
    "Space",
    "Structure",
    "SweepError",
    "System",
    "Term",
    "Trace",
    "Visit",
    "audit",
    "audit_process",
    "contradictions_in",
    "contradictory",
    "probe",
    "probe_feedback",
    "rule_for",
    "rules_for",
    "structural_rules",
    "sweep",
    "system_for",
    "verdict_for",
    "witness_counts",
]
