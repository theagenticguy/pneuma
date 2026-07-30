"""Derive precedence rules from any log, and attach them to any mined process.

This is what `pipeline.governed()` should have been. That function hardcodes two
activity names from the building-permit log, so on a different process it returns a
"governed" model whose invariant references a state that does not exist. The
invariant can never fire. Nothing raises. You get a green verification of a rule
that protects nothing, which is worse than no rule at all.

Here the rule comes from the log. `derive_precedences` scans every ordered activity
pair and keeps the ones that hold in *every* case where both appear, above a support
floor. A precedence that survives hundreds of cases without a single exception is the
organisation stating a control; one that holds in eleven cases is a coincidence, which
is what `min_support` is for.

`enforce` then compiles a derived precedence into the IR: a boolean flag per rule, an
effect on every transition entering the prerequisite, and an invariant forbidding the
dependent state while the flag is unset. Same shape the permit study used by hand,
except nothing is spelled out in advance.

Zero code changes are needed to point this at a new process. `apply_derived_rules`
takes a log and a mined process and returns a governed one.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ..process.ir import Effect, Guard, Invariant, Process, Transition, Variable
from .miner import _identifier


@dataclass(frozen=True)
class Precedence:
    """`before` precedes `after` in every case where both occur."""

    before: str
    after: str
    cases: int

    @property
    def flag(self) -> str:
        """Variable name recording that the prerequisite has happened."""
        return f"did_{_identifier(self.before).lower()}"[:40]

    @property
    def rule_name(self) -> str:
        return f"No{_identifier(self.after)}Without{_identifier(self.before)}"[:60]

    @property
    def stated_as(self) -> str:
        return f"'{self.after}' may not occur before '{self.before}'"

    @property
    def evidence(self) -> str:
        return f"held in {self.cases} of {self.cases} co-occurring cases (100%)"


def derive_precedences(
    events: pl.DataFrame,
    *,
    min_support: int = 100,
) -> list[Precedence]:
    """Find every activity pair where the first always precedes the second.

    Args:
        events: One row per event, as produced by `eventlog.parse_xes`.
        min_support: How many cases must contain both activities. The floor that
            separates a control from a coincidence — a precedence holding in a
            handful of cases says nothing about intent.
    """
    paths = (
        events.sort(["case_id", "position"])
        .group_by("case_id")
        .agg(pl.col("activity").alias("path"))
    )
    sequences = [row["path"] for row in paths.iter_rows(named=True)]
    activities = sorted(events["activity"].unique().to_list())

    found: list[Precedence] = []
    for before in activities:
        for after in activities:
            if before == after:
                continue
            both = ordered = 0
            for sequence in sequences:
                if before in sequence and after in sequence:
                    both += 1
                    if sequence.index(before) < sequence.index(after):
                        ordered += 1
            if both >= min_support and ordered == both:
                found.append(Precedence(before=before, after=after, cases=both))

    return sorted(found, key=lambda p: -p.cases)


def enforce(process: Process, precedence: Precedence) -> Process:
    """Attach one derived precedence to `process` as a checkable invariant.

    Returns the process unchanged in two cases, both of which would otherwise produce
    a rule that is worse than none.

    First, when either activity is absent from the model: mining drops infrequent
    activities, so a precedence derived from the full log can reference a state the
    thresholded model does not contain, and an invariant about a missing state can
    never fire.

    Second, when the prerequisite is the *initial* state. Every case begins there, so
    the log reports the precedence at a perfect 100% — and the flag is still 0 during
    the first step, before any transition has fired. The invariant then reports a
    violation on the opening move of a completely correct process. The strongest
    precedences in a log are always against the start activity, so this guard is doing
    real work rather than covering an edge case.
    """
    before = _identifier(precedence.before)
    after = _identifier(precedence.after)
    known = {state.name for state in process.states}
    if before not in known or after not in known:
        return process
    if before == process.initial_state:
        return process

    flag = precedence.flag
    transitions: list[Transition] = []
    for transition in process.transitions:
        if transition.target == before:
            effects = [*transition.effects, Effect(variable=flag, value=1)]
            transition = transition.model_copy(update={"effects": effects})
        transitions.append(transition)

    variables = [*process.variables]
    if flag not in {v.name for v in variables}:
        variables.append(Variable(name=flag, low=0, high=1, initial=0))

    invariant = Invariant(
        name=precedence.rule_name,
        stated_as=f"{precedence.stated_as} — {precedence.evidence}",
        forbidden_state=after,
        forbidden_when=[Guard(variable=flag, op="eq", value=0)],
    )

    return process.model_copy(
        update={
            "transitions": transitions,
            "variables": variables,
            "invariants": [*process.invariants, invariant],
        }
    )


def apply_derived_rules(
    events: pl.DataFrame,
    process: Process,
    *,
    min_support: int = 100,
    max_rules: int = 3,
) -> tuple[Process, list[Precedence]]:
    """Derive rules from `events` and attach the strongest ones to `process`.

    Returns the governed process and the precedences actually applied, so a caller
    can report what was enforced rather than assuming. `max_rules` is a state-space
    guard: each rule adds a boolean, so TLC's work doubles per rule.
    """
    candidates = derive_precedences(events, min_support=min_support)
    governed = process
    applied: list[Precedence] = []

    for precedence in candidates:
        if len(applied) >= max_rules:
            break
        updated = enforce(governed, precedence)
        if updated is not governed:
            governed = updated
            applied.append(precedence)

    return governed, applied
