"""Mechanical detectors for properties that pass without ever being tested.

`discrimination` is the one question the two detectors here are both asking: can this check
tell its two cases apart, or does it pass because it was never in a position to fail? Read
that file first; it is the twenty lines they share, and its docstring records where the
shared shape stops being shared.

`vacuity` asks it about a *rule*, by enumerating reachable states and counting how many break
it. It imports nothing from pneuma. `adapter` binds pneuma's `Process` IR to it and is the
file to replace when lifting this elsewhere.

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
    Sweep,
    SweepError,
    System,
    Trace,
    Visit,
    audit,
    contradictory,
    sweep,
)

# `objective.Sweep` is deliberately not re-exported: `vacuity.Sweep` already owns that
# name here and the two are unrelated types. Import it from `pneuma.detect.objective`.
#
# `adversary` is deliberately not imported here either. It is the only file under `detect`
# that reaches for a model, and importing it eagerly would make `from pneuma.detect import
# probe` depend on `ai_functions` and, transitively, on having credentials to do anything
# interesting. Import it from `pneuma.detect.adversary` when you want the LLM half.
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
    "Relaxation",
    "Rule",
    "RuleVerdict",
    "Sample",
    "Search",
    "Severity",
    "Space",
    "Structure",
    "Sweep",
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
