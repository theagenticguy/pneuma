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

Attaching a well-formed rule is not the same as protecting anything. A precedence
compiles into "never in state `after` while the flag is 0", and whether that can
ever hold depends on the *mined graph*, not the log the rule came from: raise the
mining threshold until the only edge into `after` runs through `before`, and the
condition is reachable in zero states. TLC then explores the whole space and reports
no error, which reads exactly like a rule being obeyed.

Measuring that is not this module's job. `pneuma.detect` owns it, knows nothing about
event logs or precedences, and reports per-invariant counts, a named cause and a
shortest witness trace over a whole-process sweep. This module is a consumer: it asks
the detector whether the rule it just compiled can fire, and reports the answer.
Everything `enforce` declines to attach is reported too, for the same reason: a rule
that vanishes silently is indistinguishable from one that was applied.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Literal

import polars as pl

from ..detect import DEFAULT_LIMIT, RuleVerdict, audit_process
from ..process.ir import Effect, Guard, Invariant, Process, Transition, Variable
from .miner import _identifier

ACTIVITY_NOT_MINED = "an activity the mined model does not contain"
PREREQUISITE_IS_START = "the prerequisite is the initial state"
DUPLICATE_RULE_NAME = "the rule name is already taken"
UNVIOLABLE = "no reachable state can violate it"


class RuleNotEnforced(UserWarning):
    """A derived rule was declined, or attached while unable to ever fire.

    A warning rather than an exception because declining is the common, correct case
    on real logs — most derived precedences are against the start activity — and
    raising would make `apply_derived_rules` unusable. What is not acceptable is
    doing it in silence.
    """


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


# Alias: a caller that annotates against `rules.Liveness` gets `detect.RuleVerdict`,
# the record with the three-valued `live`, a named cause, a shortest witness trace
# and per-relaxation counts.
Liveness = RuleVerdict


def liveness(
    process: Process, precedence: Precedence, *, limit: int = DEFAULT_LIMIT
) -> RuleVerdict:
    """Ask the detector whether the invariant `enforce` compiled can ever fire.

    A thin adapter from a precedence to a rule name. The sweep behind it is
    whole-process, so measuring one rule here cannot leave the others unmeasured; use
    `detect.audit_process` directly for the full report, which is what a caller
    presenting a verification should be reading.
    """
    if not any(i.name == precedence.rule_name for i in process.invariants):
        raise ValueError(f"{precedence.rule_name} is not attached to {process.name}")
    return audit_process(process, limit=limit).verdicts[precedence.rule_name]


def enforce(
    process: Process,
    precedence: Precedence,
    *,
    on_vacuous: Literal["warn", "refuse", "ignore"] = "warn",
) -> Process:
    """Attach one derived precedence to `process` as a checkable invariant.

    Returns the process unchanged, with a `RuleNotEnforced` warning naming the reason,
    in three cases, each of which would otherwise produce a rule worse than none.

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

    Third, when the compiled name is already taken. `rule_name` truncates to 60
    characters, and on receipt.xes eight of the nine derived names already sit at
    exactly 60, so two distinct precedences colliding is one activity rename away. The
    IR checks duplicate state, variable and transition names but not invariant names,
    so a collision reaches TLC as two definitions of one operator; TLC fails to parse
    and `tla.check` reports that as `ok=False` with no named violation, which looks
    like a real counterexample until someone reads the raw output.

    A fourth case is reported but, by default, still attached: the rule is well-formed
    and cannot be violated anywhere in the reachable state space. `on_vacuous`
    controls this. `"warn"` attaches and warns, which keeps the honest-but-useless rule
    visible in the spec where TLC's green verdict can be read against the count.
    `"refuse"` declines it, for a caller that wants only rules with teeth. `"ignore"`
    is for tests that need the unprotected artifact itself.
    """
    before = _identifier(precedence.before)
    after = _identifier(precedence.after)
    known = {state.name for state in process.states}
    if before not in known or after not in known:
        return _decline(process, precedence, ACTIVITY_NOT_MINED)
    if before == process.initial_state:
        return _decline(process, precedence, PREREQUISITE_IS_START)
    if any(i.name == precedence.rule_name for i in process.invariants):
        return _decline(process, precedence, DUPLICATE_RULE_NAME)

    flag = precedence.flag
    transitions: list[Transition] = []
    for transition in process.transitions:
        if transition.target == before and not any(e.variable == flag for e in transition.effects):
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

    governed = process.model_copy(
        update={
            "transitions": transitions,
            "variables": variables,
            "invariants": [*process.invariants, invariant],
        }
    )

    if on_vacuous == "ignore":
        return governed
    measured = audit_process(governed).verdicts[invariant.name]
    if measured.live is False:
        if on_vacuous == "refuse":
            return _decline(process, precedence, UNVIOLABLE)
        warnings.warn(
            f"{precedence.rule_name} attached but {UNVIOLABLE}: {measured}",
            RuleNotEnforced,
            stacklevel=2,
        )
    return governed


def _decline(process: Process, precedence: Precedence, reason: str) -> Process:
    warnings.warn(
        f"{precedence.rule_name} not enforced: {reason} "
        f"({precedence.stated_as}, {precedence.evidence})",
        RuleNotEnforced,
        stacklevel=3,
    )
    return process


@dataclass(frozen=True)
class Skipped:
    """One derived precedence that was not attached, and why."""

    precedence: Precedence
    reason: str

    def __str__(self) -> str:
        return f"{self.precedence.rule_name}: {self.reason}"


@dataclass(frozen=True)
class Governed:
    """What attaching derived rules actually produced.

    Iterable as `(process, applied)` so the pair every existing caller unpacks keeps
    working, while `skipped` and the liveness split are there for a caller that wants
    to report what it enforced rather than assume.
    """

    process: Process
    applied: list[Precedence]
    skipped: list[Skipped] = field(default_factory=list)
    measured: dict[str, Liveness] = field(default_factory=dict)

    def __iter__(self):  # noqa: ANN204 - tuple compatibility for `a, b = ...`
        return iter((self.process, self.applied))

    @property
    def considered(self) -> int:
        return len(self.applied) + len(self.skipped)

    @property
    def live(self) -> list[Precedence]:
        return [p for p in self.applied if self.measured[p.rule_name].live is True]

    @property
    def vacuous(self) -> list[Precedence]:
        return [p for p in self.applied if self.measured[p.rule_name].live is False]

    @property
    def unknown(self) -> list[Precedence]:
        return [p for p in self.applied if self.measured[p.rule_name].live is None]

    def summary(self) -> str:
        lines = [
            f"{len(self.applied)} of {self.considered} derived rules attached: "
            f"{len(self.live)} live, {len(self.vacuous)} vacuous, {len(self.unknown)} unknown"
        ]
        lines += [f"  applied  {self.measured[p.rule_name]}" for p in self.applied]
        lines += [f"  skipped  {s}" for s in self.skipped]
        return "\n".join(lines)


def apply_derived_rules(
    events: pl.DataFrame,
    process: Process,
    *,
    min_support: int = 100,
    max_rules: int = 3,
    on_vacuous: Literal["warn", "refuse", "ignore"] = "warn",
) -> Governed:
    """Derive rules from `events` and attach the strongest ones to `process`.

    Returns a `Governed` report: the process, the precedences applied, the ones
    declined with the reason, and per-rule liveness. Reporting the declines is the
    point — on receipt.xes five of the first nine candidates are declined, and a
    caller that only sees the pair cannot tell an enforced rule from a lost one.
    `max_rules` is a state-space guard: each rule adds a boolean, so TLC's work
    doubles per rule.
    """
    candidates = derive_precedences(events, min_support=min_support)
    governed = process
    applied: list[Precedence] = []
    skipped: list[Skipped] = []
    measured: dict[str, Liveness] = {}

    for precedence in candidates:
        if len(applied) >= max_rules:
            break
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuleNotEnforced)
            updated = enforce(governed, precedence, on_vacuous=on_vacuous)
        declines = [w for w in caught if issubclass(w.category, RuleNotEnforced)]

        if updated is governed:
            reason = _reason_of(declines[0]) if declines else "declined without a stated reason"
            skipped.append(Skipped(precedence=precedence, reason=reason))
            continue

        governed = updated
        applied.append(precedence)
        measured[precedence.rule_name] = liveness(governed, precedence)
        for warning in declines:
            warnings.warn(str(warning.message), RuleNotEnforced, stacklevel=2)

    return Governed(process=governed, applied=applied, skipped=skipped, measured=measured)


def _reason_of(warning: warnings.WarningMessage) -> str:
    """Recover the reason `_decline` stated, so the report does not restate the rules."""
    for reason in (ACTIVITY_NOT_MINED, PREREQUISITE_IS_START, DUPLICATE_RULE_NAME, UNVIOLABLE):
        if reason in str(warning.message):
            return reason
    return str(warning.message)
