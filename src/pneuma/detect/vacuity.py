"""Detect rules that pass because nothing ever tested them.

A model-checker answers one question: did any reachable state break this rule? It
does not answer the question a reviewer actually has, which is whether the rule was
ever in a position to break. Those two look identical from the outside — both print
green — and the whole point of this module is to make them different objects.

The mechanism is a reachability sweep plus *relaxation*. Sweeping the reachable
`(location, assignment)` pairs and counting how many break each rule reproduces the
checker's verdict. Re-sweeping the same rules over a deliberately weakened system
answers the counterfactual the verdict cannot: if the rule still cannot break when
the guards are ignored and the initial values are freed, then nothing about the
model's *logic* was keeping it safe, and the green verdict was about the shape of
the graph rather than about the rule.

Four relaxations, and the level at which a rule first becomes breakable is its
diagnosis:

    exact         the system as written. Agrees with the model-checker.
    free_initial  every variable starts at every value in its domain.
    free_guards   transitions fire whether or not their guards hold.
    free_both     both at once. The weakest system the formalism admits.

    broken at exact         the rule can fire; the checker will report it
    only at free_initial    a pinned initial value hid the case  <-- the classic bug
    only at free_guards     a guard is load-bearing; the pass is real
    only at free_both       needs both, so a pinned value is still load-bearing
    broken at no level      the rule cannot fire under any assignment: decoration

The order is not arbitrary. Freeing an initial value is a *sound* widening: it is
exactly what TLA+'s `x \\in Domain` does in `Init`, and the model's own guards keep
enforcing themselves throughout. Freeing guards is unsound, since it walks edges the
system forbids. So the sound relaxation is tried first, and a rule that becomes
breakable under it is diagnosed as hidden by a pinned variable even though ignoring
guards would also have surfaced it.

That ordering is what makes the gate correct. A checker pins initial values exactly
as the model does, so a rule rescued only by `free_initial` was never checked at all
and its pass must be withdrawn. A rule rescued by `free_guards` under the model's own
initial values has a guard doing real work, and its pass stands.

Nothing here imports its host project. The consumer supplies a `System` (how to
start, how to step) and a list of `Rule`s (name, scope, broken), and gets counts,
shortest witness traces, and a named cause back. Lifting this into another project
means writing one adapter against those two protocols; see `adapter.py` for the
process-IR one and treat it as the file you replace rather than the file you edit.

Every bound is reported. `limit` caps the states any one sweep will visit, and a
sweep that hits it says `truncated`, which makes `live` and `vacuous` three-valued
rather than optimistic. A search that gave up is not evidence of safety, and
recording it as such would rebuild the defect one level up.

That applies to the *relaxed* sweeps too, and their truncation must be tracked separately
from `exact`'s, because the levels bind at wildly different sizes. `free_initial` starts
every variable at every value, so with n free booleans its start set is 2^n against
`exact`'s one. A model can therefore finish at `exact` and exhaust the budget at
`free_initial`, and when it does, `free_guards` is never swept at all. Since `free_guards`
is the level that earns a guarded rule its pass, without a separate flag such a rule reads
as "0 violating, search finished, no witness", which is indistinguishable from decoration.
`relaxation_truncated` separates that case out: the cause becomes `unknown`, `vacuous`
stays False, and the witness count stays 0 so the checker's pass is still withdrawn. Not
knowing is not the same finding as decoration, and neither of them is a pass.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .discrimination import Discrimination

Value = int | str
Assignment = Mapping[str, Value]
Key = tuple[str, tuple[tuple[str, Value], ...]]

Relaxation = Literal["exact", "free_guards", "free_initial", "free_both"]

# Ordered weakest-constraint-last, with the sound widening (`free_initial`) ahead of
# the unsound one. `audit` walks this order and stops at the first level where a rule
# becomes breakable, so the level that resolves it is the diagnosis.
RELAXATIONS: tuple[Relaxation, ...] = ("exact", "free_initial", "free_guards", "free_both")

Cause = Literal[
    "unreachable_scope",
    "unsatisfiable",
    "pinned_variable",
    "guarded",
    "pinned_and_guarded",
    "unknown",
]

DEFAULT_LIMIT = 200_000


@dataclass(frozen=True)
class Visit:
    """One reachable state, as a rule sees it.

    `successors` carries the names of the edges enabled here, because a liveness-ish
    rule like "no non-terminal state is stuck" is a statement about out-degree and
    cannot be written as a predicate over the assignment alone.
    """

    location: str
    assignment: Assignment
    successors: tuple[str, ...]
    depth: int


@dataclass(frozen=True)
class Rule:
    """A property to check, split into the scope it talks about and the breach itself.

    Splitting them is what makes vacuity measurable. `broken` counts states that
    violate the rule, which is what a checker reports. `scope` counts states the rule
    has an opinion about at all, so a rule whose scope is never reached is separable
    from a rule whose scope is reached and whose breach is never satisfied. Those are
    different findings with different fixes.

    `gates` says whether a zero-witness verdict should withdraw a checker's pass. It
    is False for wellformedness properties: a type invariant that cannot fail is a
    sound model, not an untested one. It is True for anything asserting a control,
    where unfirable means absent.
    """

    name: str
    broken: Callable[[Visit], bool]
    scope: Callable[[Visit], bool] = lambda _visit: True
    stated_as: str = ""
    gates: bool = True


@runtime_checkable
class System(Protocol):
    """A guarded transition system, reduced to the two things a sweep needs.

    Deliberately smaller than any real IR. An adapter that can enumerate starting
    states and step one state to its successors is enough, which is why this survives
    being lifted away from the formalism it was written against.
    """

    def starts(self) -> Iterator[tuple[str, dict[str, Value]]]:
        """Yield every initial `(location, assignment)`. May be large; keep it lazy."""
        ...

    def successors(
        self, location: str, assignment: Assignment
    ) -> Iterator[tuple[str, str, dict[str, Value]]]:
        """Yield `(edge_name, target_location, successor_assignment)` for one state."""
        ...


@dataclass(frozen=True)
class Trace:
    """A shortest path from a starting state to a state that breaks a rule.

    A count says a rule can fire; a trace says how, which is the difference between
    a metric and a bug report. Shortest because the sweep is breadth-first, so the
    first breach found sits at minimum depth.
    """

    locations: tuple[str, ...]
    edges: tuple[str, ...]
    start: tuple[tuple[str, Value], ...]
    end: tuple[tuple[str, Value], ...]

    @property
    def depth(self) -> int:
        return len(self.edges)

    def __str__(self) -> str:
        path = " -> ".join(self.locations)
        via = f" via {', '.join(self.edges)}" if self.edges else ""
        opening = dict(self.start)
        closing = dict(self.end)
        where = f" with {closing}" if closing else ""
        prefix = f"from {opening} " if opening and opening != closing else ""
        return f"{prefix}{path}{via}{where}"


@dataclass(frozen=True)
class Count:
    """What one sweep found for one rule."""

    rule: str
    scope_states: int
    broken_states: int
    trace: Trace | None


@dataclass(frozen=True)
class Sweep:
    """One reachability pass, and every bound it hit.

    `truncated` is not a detail. A sweep that stopped early proves nothing about the
    states it never saw, so a caller that reads `broken_states == 0` without reading
    this flag has learned nothing.
    """

    relaxation: Relaxation
    reachable_states: int
    start_states: int
    limit: int
    truncated: bool
    counts: Mapping[str, Count]

    def __str__(self) -> str:
        bound = f", TRUNCATED at limit={self.limit}" if self.truncated else ""
        return (
            f"{self.relaxation}: {self.reachable_states} reachable "
            f"from {self.start_states} starts{bound}"
        )


class SweepError(RuntimeError):
    """The system could not be walked, with the state that broke it named.

    Raised rather than swallowed. A guard that cannot be evaluated is a modelling
    error, and treating it as "not enabled" would silently shrink the state space,
    which is the same failure this module detects.
    """


def sweep(
    system: System,
    rules: Sequence[Rule],
    *,
    limit: int = DEFAULT_LIMIT,
    relaxation: Relaxation = "exact",
) -> Sweep:
    """Breadth-first enumerate reachable states, counting scope and breach per rule.

    Multi-source: every starting state is seeded at depth 0 before any expansion, so
    a recorded trace really is a shortest one. Seeding is itself budgeted, because a
    system with many free variables can have more starting states than the whole
    reachable space of another, and a cap applied only to expansion would be a cap
    that lies.

    This is a whole-space count, not a path search. `scope_states` is how often each
    rule's subject is reachable at all, `broken_states` how often it is reachable
    with the breach holding. Zero breaches over a non-zero scope is the interesting
    shape: the situation arises, the rule names it, and no assignment ever satisfies
    it.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    seen: set[Key] = set()
    parent: dict[Key, tuple[Key, str]] = {}
    frontier: deque[tuple[str, dict[str, Value], int]] = deque()

    start_states = 0
    truncated = False
    for location, assignment in system.starts():
        if len(seen) >= limit:
            truncated = True
            break
        key = _key(location, assignment)
        if key in seen:
            continue
        seen.add(key)
        start_states += 1
        frontier.append((location, assignment, 0))

    scope = dict.fromkeys((rule.name for rule in rules), 0)
    broken = dict.fromkeys((rule.name for rule in rules), 0)
    traces: dict[str, Trace] = {}

    reachable = 0
    while frontier:
        location, assignment, depth = frontier.popleft()
        reachable += 1

        try:
            outgoing = list(system.successors(location, assignment))
        except Exception as error:  # noqa: BLE001 - re-raised with the state that caused it
            raise SweepError(f"stepping {location} with {dict(assignment)}: {error}") from error

        visit = Visit(
            location=location,
            assignment=assignment,
            successors=tuple(name for name, _target, _next in outgoing),
            depth=depth,
        )
        for rule in rules:
            try:
                if rule.scope(visit):
                    scope[rule.name] += 1
                if rule.broken(visit):
                    broken[rule.name] += 1
                    if rule.name not in traces:
                        traces[rule.name] = _trace(parent, _key(location, assignment))
            except Exception as error:  # noqa: BLE001 - a rule that cannot be evaluated is a bug
                raise SweepError(
                    f"evaluating {rule.name} at {location} with {dict(assignment)}: {error}"
                ) from error

        for name, target, successor in outgoing:
            key = _key(target, successor)
            if key in seen:
                continue
            if len(seen) >= limit:
                truncated = True
                continue
            seen.add(key)
            parent[key] = (_key(location, assignment), name)
            frontier.append((target, successor, depth + 1))

    return Sweep(
        relaxation=relaxation,
        reachable_states=reachable,
        start_states=start_states,
        limit=limit,
        truncated=truncated,
        counts={
            rule.name: Count(
                rule=rule.name,
                scope_states=scope[rule.name],
                broken_states=broken[rule.name],
                trace=traces.get(rule.name),
            )
            for rule in rules
        },
    )


def _key(location: str, assignment: Assignment) -> Key:
    return (location, tuple(sorted(assignment.items())))


def _trace(parent: Mapping[Key, tuple[Key, str]], target: Key) -> Trace:
    locations = [target[0]]
    edges: list[str] = []
    cursor = target
    while cursor in parent:
        cursor, edge = parent[cursor]
        locations.append(cursor[0])
        edges.append(edge)
    return Trace(
        locations=tuple(reversed(locations)),
        edges=tuple(reversed(edges)),
        start=cursor[1],
        end=target[1],
    )


@dataclass(frozen=True)
class RuleVerdict:
    """One rule, and whether its pass would mean anything.

    `live` / `violating_states` / `antecedent_states` / `truncated` are a compatibility
    surface: `casestudy.rules.Liveness` is an alias for this record, so a caller that
    annotates against that name reads those four fields and must keep working. The rest is
    this record's own: the level-by-level breach counts, the named cause, the shortest
    witness trace, and the guard-satisfiability note.

    `live` is three-valued and must stay so. False means the sweep finished and found
    nothing, None means it ran out of budget, and collapsing those two would report an
    abandoned search as a safe one.
    """

    invariant: str
    reachable_states: int
    antecedent_states: int
    violating_states: int
    truncated: bool
    limit: int
    relaxed: Mapping[Relaxation, int] = field(default_factory=dict)
    trace: Trace | None = None
    relaxed_trace: Trace | None = None
    contradiction: str | None = None
    stated_as: str = ""
    gates: bool = True

    relaxation_truncated: bool = False
    """A relaxed sweep this rule's diagnosis needed hit the budget and stopped.

    Separate from `truncated`, which is about the `exact` sweep alone. The two bind at
    very different sizes: `free_initial` starts every variable at every value, so on a
    process with n free booleans its start set is 2^n while `exact` has one. A model
    can therefore finish at `exact` and exhaust the budget at `free_initial`, and the
    levels after it are never swept at all.

    That matters because `free_guards` is where a load-bearing guard earns its pass.
    Without this flag such a rule reads as `0 violating, search finished, no witness`,
    which is indistinguishable from decoration and is the same "an abandoned search is
    not evidence" defect this module exists to catch, one relaxation level down.
    """

    @property
    def live(self) -> bool | None:
        """Can this rule be broken in the system as written?

        None when the sweep was truncated, because an exhausted budget is not a
        finding about the states it never reached. Only the `exact` sweep decides this:
        `live` is a statement about the system as written, and a relaxed sweep giving
        up says nothing about it. What a relaxed truncation does withdraw is the
        *cause* and the vacuity verdict, which is `relaxation_truncated`'s job.
        """
        if self.violating_states:
            return True
        return None if self.truncated else False

    @property
    def witnesses(self) -> int:
        """States that count as this rule having been exercised, for a checker's gate.

        Zero withdraws a pass. Two levels contribute, and `free_initial` is not one of
        them: a checker pins initial values exactly as the model does, so a rule that
        only becomes breakable once they are freed was genuinely never checked, and
        that is the pass most worth withdrawing.

        `exact` counts real violations, which a checker would have reported anyway.
        `free_guards` counts states reachable under the model's own initial values that
        break the rule once guards are ignored, which means a guard is load-bearing and
        the pass was earned. Either is a witness; neither being present means the rule
        is decoration.
        """
        return max(self.relaxed.get("exact", 0), self.relaxed.get("free_guards", 0))

    @property
    def unfirable(self) -> bool:
        """True when the finished sweep found no state breaking this rule."""
        return self.live is False

    @property
    def discrimination(self) -> Discrimination:
        """This rule as the shared measurement: could it separate compliant from not?

        An observation is a reachable state the rule had an opinion about, and a
        separating one is a witness: a state that breaks the rule under the model's own
        initial values. So `discriminates` is exactly the question `vacuous` answers,
        expressed in the vocabulary `objective` uses for the same question about a
        scoring term. See `discrimination.py` for why they are the same question.

        The two bounds this module applies both land in `withheld`, so an unsettled
        verdict names which search gave up rather than reading as a quiet pass.
        """
        withheld: list[str] = []
        if self.truncated:
            withheld.append(
                f"the exact sweep stopped at limit={self.limit} after "
                f"{self.reachable_states} states"
            )
        if self.relaxation_truncated:
            withheld.append(
                f"the relaxed sweep that would explain it stopped at limit={self.limit}, "
                "so the level that could earn this rule its pass never ran"
            )
        return Discrimination(
            subject=self.invariant,
            observations=self.antecedent_states,
            separating=self.witnesses,
            withheld=tuple(withheld),
            unit="reachable state in scope",
            kind="rule",
        )

    @property
    def vacuous(self) -> bool:
        """True when a green verdict on this rule would mean nothing.

        Derived from `discrimination.idle` rather than re-deriving the same conjunction,
        so the flag, the gate, and the shared primitive can never disagree about a rule.
        `idle` is "examined in full and never fired", which is exactly "no witness and
        neither search was truncated".

        `gates` is required and stays outside the primitive, because it is a statement
        about what a *pass* would mean rather than about discrimination. An unfirable
        wellformedness property means the domains are sound rather than untested, and
        calling that vacuous would make the word useless: every correct process has a
        `TypeOK` that cannot fail. Its `discrimination` still reports honestly, which is
        what lets a report show a non-gating rule's idleness without withdrawing a pass
        for it.
        """
        return self.gates and self.discrimination.idle

    @property
    def cause(self) -> Cause | None:
        """Why the rule cannot fire, named by the relaxation that makes it fire.

        None when it can fire in the system as written, which needs no explanation.
        """
        if self.violating_states:
            return None
        if self.truncated or self.relaxation_truncated:
            return "unknown"
        if self.relaxed.get("free_initial", 0):
            return "pinned_variable"
        if self.relaxed.get("free_guards", 0):
            return "guarded"
        if self.relaxed.get("free_both", 0):
            return "pinned_and_guarded"
        return "unreachable_scope" if self.antecedent_states == 0 else "unsatisfiable"

    def __str__(self) -> str:
        if self.live is None:
            return (
                f"{self.invariant}: UNKNOWN, search stopped at limit={self.limit} "
                f"after {self.reachable_states} states"
            )
        if self.live:
            witness = f"; shortest {self.trace}" if self.trace else ""
            return (
                f"{self.invariant}: live - {self.violating_states} violating of "
                f"{self.antecedent_states} in scope, {self.reachable_states} "
                f"reachable{witness}"
            )
        if self.vacuous:
            verdict = "VACUOUS"
        elif not self.gates:
            verdict = "unfirable, does not gate"
        elif self.relaxation_truncated:
            return (
                f"{self.invariant}: unfirable, CAUSE UNKNOWN - 0 violating of "
                f"{self.antecedent_states} in scope, {self.reachable_states} reachable; the "
                f"relaxed search that would explain it stopped at limit={self.limit}, so the "
                "pass is neither earned nor withdrawn"
            )
        else:
            verdict = "unfirable but a guard earned it"
        detail = _CAUSE_PROSE[self.cause] if self.cause else ""
        note = f"; {self.contradiction}" if self.contradiction else ""
        breakable = (
            f"; breakable under {_first_breaking(self.relaxed)}"
            if _first_breaking(self.relaxed)
            else ""
        )
        return (
            f"{self.invariant}: {verdict} - 0 violating of {self.antecedent_states} "
            f"in scope, {self.reachable_states} reachable ({detail}){note}{breakable}"
        )


_CAUSE_PROSE: dict[str, str] = {
    "unreachable_scope": "the rule's subject is not reachable at all",
    "unsatisfiable": "the subject is reachable but the condition never holds",
    "pinned_variable": "a pinned initial value makes the case unreachable",
    "guarded": "a transition guard is what prevents it",
    "pinned_and_guarded": "a pinned initial value and a guard together prevent it",
    "unknown": "the search was truncated",
}


def _first_breaking(relaxed: Mapping[Relaxation, int]) -> str | None:
    for level in RELAXATIONS:
        if relaxed.get(level, 0):
            return level
    return None


@dataclass(frozen=True)
class Audit:
    """Every rule in one system, swept at every relaxation it needed.

    The whole-system sweep is the point. Checking one rule on demand is how a vacuous
    rule survives: the rule someone thought to ask about gets measured, and the ones
    nobody asked about do not.
    """

    verdicts: Mapping[str, RuleVerdict]
    sweeps: Mapping[Relaxation, Sweep]
    limit: int

    @property
    def reachable_states(self) -> int:
        exact = self.sweeps.get("exact")
        return exact.reachable_states if exact else 0

    @property
    def truncated(self) -> bool:
        return any(sweep.truncated for sweep in self.sweeps.values())

    @property
    def live(self) -> list[str]:
        return [name for name, v in self.verdicts.items() if v.live is True]

    @property
    def vacuous(self) -> list[str]:
        return [name for name, v in self.verdicts.items() if v.vacuous]

    @property
    def unknown(self) -> list[str]:
        """Rules whose verdict the search did not settle, at either level.

        `live is None` covers a truncated `exact` sweep. `relaxation_truncated` covers
        the other way a rule ends up unsettled: `exact` finished and found nothing, and
        the relaxed sweep that would have said whether a guard earned the pass never
        completed. Both are unresolved, and a rule missing from every bucket would be a
        rule the summary quietly dropped.
        """
        return [
            name
            for name, v in self.verdicts.items()
            if v.live is None or (v.gates and v.relaxation_truncated)
        ]

    def witness_counts(self) -> dict[str, int]:
        """Per-rule witness counts for a checker's vacuity gate.

        Only gating rules appear. A wellformedness property that cannot fail is a
        sound model rather than an untested one, so including it would withdraw every
        pass and the gate would carry no information.

        A rule whose search was truncated at any level reports zero here, so a checker's
        pass is withdrawn. That is deliberately stricter than `vacuous`, which stays
        False for those rules: "we do not know" is not the same finding as "this rule is
        decoration", but neither of them is a pass. The two can therefore disagree, and
        the direction they disagree in is the safe one.
        """
        return {name: v.witnesses for name, v in self.verdicts.items() if v.gates}

    def summary(self) -> str:
        gating = [v for v in self.verdicts.values() if v.gates]
        lines = [
            f"{len(self.verdicts)} rules over {self.reachable_states} reachable states"
            + (f" (TRUNCATED at limit={self.limit})" if self.truncated else "")
            + f": {len(self.live)} live, {len(self.vacuous)} vacuous, "
            f"{len(self.unknown)} unknown, {len(gating)} gating"
        ]
        lines += [f"  {verdict}" for verdict in self.verdicts.values()]
        return "\n".join(lines)


def audit(
    build: Callable[[Relaxation], System],
    rules: Sequence[Rule],
    *,
    limit: int = DEFAULT_LIMIT,
    relaxations: Sequence[Relaxation] = RELAXATIONS,
    contradictions: Mapping[str, str] | None = None,
) -> Audit:
    """Sweep every rule, relaxing the system only as far as the answer requires.

    `build` is asked for a system per relaxation level, and only for the levels that
    are still needed: once a rule is breakable at `exact` there is nothing to explain,
    and once every rule is resolved the remaining levels are not built at all. That
    keeps the common case a single sweep while leaving the diagnosis available.

    Args:
        build: Returns a `System` for one relaxation level. The adapter's job.
        rules: Every rule to measure, including the wellformedness ones. Sweeping the
            whole set is what makes this a detector rather than a query.
        limit: States per sweep. Reported on every result and never applied silently.
        relaxations: Levels to consider, weakest last. Pass `("exact",)` for a
            checker-equivalent count with no counterfactual.
        contradictions: Optional per-rule note from `contradictory`, attached to the
            verdict so a self-contradictory condition is named rather than merely
            counted.
    """
    if "exact" not in relaxations:
        raise ValueError(f"relaxations must include 'exact', got {list(relaxations)}")

    sweeps: dict[Relaxation, Sweep] = {}
    relaxed: dict[str, dict[Relaxation, int]] = {rule.name: {} for rule in rules}
    relaxed_trace: dict[str, Trace] = {}
    abandoned: set[str] = set()
    pending = list(rules)

    for level in relaxations:
        if not pending:
            break
        result = sweep(build(level), pending, limit=limit, relaxation=level)
        sweeps[level] = result
        still: list[Rule] = []
        for rule in pending:
            count = result.counts[rule.name]
            relaxed[rule.name][level] = count.broken_states
            if count.broken_states:
                if level != "exact" and rule.name not in relaxed_trace and count.trace:
                    relaxed_trace[rule.name] = count.trace
            elif not result.truncated:
                still.append(rule)
            elif level != "exact":
                # An unresolved rule at a truncated relaxed level loses every level after
                # it, including the `free_guards` sweep that is the only thing that can
                # earn its pass. `exact` is excluded because `truncated` already carries
                # it and reports the stronger finding.
                abandoned.add(rule.name)
        pending = still

    exact = sweeps["exact"]
    notes = contradictions or {}
    verdicts = {
        rule.name: RuleVerdict(
            invariant=rule.name,
            reachable_states=exact.reachable_states,
            antecedent_states=exact.counts[rule.name].scope_states,
            violating_states=exact.counts[rule.name].broken_states,
            truncated=exact.truncated,
            limit=limit,
            relaxed=dict(relaxed[rule.name]),
            trace=exact.counts[rule.name].trace,
            relaxed_trace=relaxed_trace.get(rule.name),
            contradiction=notes.get(rule.name),
            stated_as=rule.stated_as,
            gates=rule.gates,
            relaxation_truncated=rule.name in abandoned,
        )
        for rule in rules
    }
    return Audit(verdicts=verdicts, sweeps=sweeps, limit=limit)


# ── Guard-level satisfiability, before any enumeration ──

_NEGATED = {"eq": "ne", "ne": "eq"}


def contradictory(clauses: Iterable[tuple[str, str, Value]]) -> str | None:
    """Name a conjunction that no assignment can satisfy, without enumerating.

    Cheaper and sharper than discovering zero hits. A sweep reports that a condition
    never held over the states it happened to reach; this reports that it could not
    hold anywhere, and says which variable is responsible. Comparisons are
    `(variable, op, value)` with `op` in eq/ne/lt/le/gt/ge, matching the shape a
    single-variable guard language has.

    Returns None when no contradiction is provable from the clauses alone. That is
    deliberately weak: this cannot see the domains or the reachable space, so None
    means "not provably empty here", never "satisfiable".
    """
    equal: dict[str, set[Value]] = {}
    unequal: dict[str, set[Value]] = {}
    low: dict[str, int] = {}
    high: dict[str, int] = {}

    for variable, op, value in clauses:
        if op == "eq":
            equal.setdefault(variable, set()).add(value)
        elif op == "ne":
            unequal.setdefault(variable, set()).add(value)
        elif isinstance(value, int):
            if op == "gt":
                low[variable] = max(low.get(variable, value + 1), value + 1)
            elif op == "ge":
                low[variable] = max(low.get(variable, value), value)
            elif op == "lt":
                high[variable] = min(high.get(variable, value - 1), value - 1)
            elif op == "le":
                high[variable] = min(high.get(variable, value), value)

    for variable, values in equal.items():
        if len(values) > 1:
            listed = ", ".join(repr(v) for v in sorted(values, key=repr))
            return f"{variable} is required to equal {listed} at once"
        pinned = next(iter(values))
        if pinned in unequal.get(variable, set()):
            return f"{variable} is required to equal and not equal {pinned!r}"
        if isinstance(pinned, int):
            if variable in low and pinned < low[variable]:
                return f"{variable} = {pinned} contradicts {variable} >= {low[variable]}"
            if variable in high and pinned > high[variable]:
                return f"{variable} = {pinned} contradicts {variable} <= {high[variable]}"

    for variable, floor in low.items():
        ceiling = high.get(variable)
        if ceiling is not None and floor > ceiling:
            return f"{variable} must be both >= {floor} and <= {ceiling}"

    return None
