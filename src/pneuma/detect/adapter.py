"""Bind pneuma's `Process` IR to `vacuity`'s two protocols.

This is the only file in `detect/` that knows what a `Process` is, and it is the file
you replace to point the detector at a different formalism. `vacuity` itself needs a
`System` (how to start, how to step) and `Rule`s (name, scope, breach); everything
below is the translation, and it is about a hundred lines.

The walk uses the IR's own `Transition.enabled` and `Effect.apply` rather than
reimplementing the semantics, and accumulates effects into the successor dict the way
`interpreter.run` does, so two effects on one variable agree with execution rather
than with a second reading of the spec.

Two rules are supplied that no user writes and that a per-invariant check would miss:
`NoDeadlock` and `TypeOK`, named to match what `tla.render_config` puts in the .cfg so
a witness map lines up with the checker's own invariant list. Both are marked
non-gating. A type invariant that cannot fail means the domains are sound, not that
nothing was tested, and treating it as a vacuous pass would withdraw every verdict.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from ..process.ir import Invariant, Process, Variable
from .vacuity import (
    DEFAULT_LIMIT,
    RELAXATIONS,
    Assignment,
    Audit,
    Relaxation,
    Rule,
    RuleVerdict,
    Value,
    Visit,
    audit,
    contradictory,
)

DEADLOCK_RULE = "NoDeadlock"
TYPE_RULE = "TypeOK"


@dataclass(frozen=True)
class ProcessSystem:
    """A `Process` as a guarded transition system, at one relaxation level.

    `free_guards` steps every outgoing transition regardless of its guards, and
    `free_initial` starts every variable at every value in its domain. Neither
    rewrites the process: the relaxation lives here, in the walk, so the object the
    checker sees and the object the detector sweeps are the same object.
    """

    process: Process
    free_guards: bool = False
    free_initial: bool = False

    def starts(self) -> Iterator[tuple[str, dict[str, Value]]]:
        variables = self.process.variables
        if not variables:
            yield (self.process.initial_state, {})
            return
        names = [v.name for v in variables]
        choices = [self._domain(v) for v in variables]
        for combination in itertools.product(*choices):
            yield (self.process.initial_state, dict(zip(names, combination, strict=True)))

    def _domain(self, variable: Variable) -> list[Value]:
        if self.free_initial or variable.nondeterministic:
            return list(variable.domain)
        return [variable.initial]  # type: ignore[list-item]

    def successors(
        self, location: str, assignment: Assignment
    ) -> Iterator[tuple[str, str, dict[str, Value]]]:
        for transition in self.process.outgoing(location):
            if not self.free_guards and not transition.enabled(assignment):
                continue
            successor = dict(assignment)
            for effect in transition.effects:
                successor[effect.variable] = effect.apply(successor)
            yield (transition.name, transition.target, successor)


def system_for(process: Process, relaxation: Relaxation) -> ProcessSystem:
    """The `System` for one relaxation level. This is `audit`'s `build` argument."""
    return ProcessSystem(
        process=process,
        free_guards=relaxation in ("free_guards", "free_both"),
        free_initial=relaxation in ("free_initial", "free_both"),
    )


def rule_for(invariant: Invariant) -> Rule:
    """One IR invariant as a detector rule.

    `scope` is the forbidden state alone and `broken` is the whole conjunction, which
    is the split that separates "the situation never arose" from "the situation arose
    and the condition never held". An invariant with no forbidden state is in scope
    everywhere, since it constrains the assignment at every location.
    """
    forbidden = invariant.forbidden_state

    def in_scope(visit: Visit) -> bool:
        return forbidden is None or visit.location == forbidden

    def is_broken(visit: Visit) -> bool:
        return invariant.violated_by(visit.location, dict(visit.assignment))

    return Rule(
        name=invariant.name,
        broken=is_broken,
        scope=in_scope,
        stated_as=invariant.stated_as,
    )


def structural_rules(process: Process) -> list[Rule]:
    """`NoDeadlock` and `TypeOK`, the two the checker adds and no user writes.

    Non-gating, because neither asserts a control. A model where no state can deadlock
    is a good model; reporting that as a vacuous pass would make the gate fire on
    every sound process and therefore mean nothing.
    """
    terminal = {state.name for state in process.states if state.terminal}
    domains = {v.name: set(v.domain) for v in process.variables}
    known = {state.name for state in process.states}

    def stuck(visit: Visit) -> bool:
        return visit.location not in terminal and not visit.successors

    def mistyped(visit: Visit) -> bool:
        if visit.location not in known:
            return True
        return any(
            name not in domains or value not in domains[name]
            for name, value in visit.assignment.items()
        )

    return [
        Rule(
            name=DEADLOCK_RULE,
            broken=stuck,
            scope=lambda visit: visit.location not in terminal,
            stated_as="a non-terminal state must have an enabled transition",
            gates=False,
        ),
        Rule(
            name=TYPE_RULE,
            broken=mistyped,
            stated_as="pc and every variable stay inside their declared domain",
            gates=False,
        ),
    ]


def rules_for(process: Process, *, structural: bool = True) -> list[Rule]:
    """Every rule in `process`: the user invariants, plus the checker's own two."""
    compiled = [rule_for(invariant) for invariant in process.invariants]
    return compiled + structural_rules(process) if structural else compiled


def contradictions_in(process: Process) -> dict[str, str]:
    """Per-invariant note for any conjunction provably unsatisfiable on its own.

    Runs before enumeration, so `f = 0 /\\ f = 1` is named as a contradiction rather
    than discovered as zero hits over a state space that was walked for nothing.
    """
    found: dict[str, str] = {}
    for invariant in process.invariants:
        note = contradictory((g.variable, g.op, g.value) for g in invariant.forbidden_when)
        if note is not None:
            found[invariant.name] = note
    return found


def audit_process(
    process: Process,
    *,
    limit: int = DEFAULT_LIMIT,
    relaxations: Sequence[Relaxation] = RELAXATIONS,
    structural: bool = True,
) -> Audit:
    """Sweep every invariant in `process`, including the checker's structural two.

    The whole-process form is the one to call. Measuring one invariant on request is
    how the original defect survived: the rule someone thought to ask about got a
    number, and the rest got a green verdict.

    Args:
        process: A validated IR. Not modified; relaxation happens in the walk.
        limit: States per sweep, reported on every result and never applied silently.
        relaxations: Levels to consider. `("exact",)` gives a checker-equivalent count
            with no counterfactual and one sweep.
        structural: Include `NoDeadlock` and `TypeOK`. On by default, because they are
            what the .cfg checks and leaving them out would measure less than TLC does.
    """
    return audit(
        lambda relaxation: system_for(process, relaxation),
        rules_for(process, structural=structural),
        limit=limit,
        relaxations=relaxations,
        contradictions=contradictions_in(process),
    )


def witness_counts(process: Process, *, limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """Per-invariant witness counts, shaped for `tla.CheckResult.with_witnesses`.

    The one-line integration: `tla.check(p).with_witnesses(witness_counts(p))` returns
    `vacuous` rather than `verified` when any gating invariant has no witness state.
    """
    return audit_process(process, limit=limit).witness_counts()


def verdict_for(
    process: Process, invariant_name: str, *, limit: int = DEFAULT_LIMIT
) -> RuleVerdict:
    """One named invariant's verdict, still measured by a whole-process sweep.

    For a caller that wants a single rule's number. The sweep is the same; only the
    reporting narrows, so asking about one invariant cannot quietly skip the others.
    """
    verdicts = audit_process(process, limit=limit).verdicts
    if invariant_name not in verdicts:
        raise ValueError(f"{invariant_name} is not an invariant of {process.name}")
    return verdicts[invariant_name]
