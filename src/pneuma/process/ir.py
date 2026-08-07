"""The typed IR: one artifact, three consumers.

A mined business process becomes *data*, never generated code. The model emits an
instance of `Process`, Pydantic validates it, and three consumers read the same
validated object:

- `tla.py` renders it to TLA+ so TLC can exhaustively check the skeleton.
- `interpreter.py` walks it, dispatching each state to an `@ai_method`.
- `properties.py` renders it to a Hypothesis state machine that drives the real
  interpreter and shrinks any failing trace.

Generating data rather than code is what makes the whole thing checkable. Code
needs a sandbox and cannot be verified before it runs; a validated IR can be
model-checked first, and the interpreter that executes it stays hand-written and
reviewed.

Guards and effects are deliberately *not* free-form expressions. A guard compares
one named variable against a literal, and an effect assigns one. That keeps the
IR translatable to TLA+ without an expression compiler, and keeps the reachable
state space small enough for TLC to exhaust. Natural language earns its place in
`description`, which reaches the agent's prompt and never the verifier.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Comparison = Literal["eq", "ne", "lt", "le", "gt", "ge"]

_TLA_OPERATOR: dict[Comparison, str] = {
    "eq": "=",
    "ne": "#",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}

_PYTHON_COMPARE: dict[Comparison, str] = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}

# Identifiers the TLA+ renderer defines itself. A transition, invariant, or
# variable with one of these names would redefine it and break the generated
# module — loudly (TLC parse error), but better rejected here with a real
# message. State names are exempt: a state renders only as a string ("Done"),
# never as a definition. The module name is also safe; TLC accepts a module
# that shares its name with a definition inside it.
_TLA_RESERVED = frozenset(
    {"Spec", "Init", "Next", "Done", "States", "TypeOK", "NoDeadlock", "Termination", "pc", "vars"}
)


class Variable(BaseModel):
    """A process variable with a finite domain.

    Finite is the point: TLC enumerates every combination, so an unbounded integer
    would make the state space infinite. `low`/`high` bound an integer domain and
    `values` gives a symbolic one.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    low: int | None = None
    high: int | None = None
    values: list[str] | None = None
    initial: int | str | None = Field(
        default=None,
        description="Fixed starting value, or None for any value in the domain",
    )

    @model_validator(mode="after")
    def _one_domain(self) -> Variable:
        integral = self.low is not None and self.high is not None
        symbolic = self.values is not None
        if integral == symbolic:
            raise ValueError(f"{self.name}: give either low+high or values, not both")
        if integral and self.low > self.high:  # type: ignore[operator]
            raise ValueError(f"{self.name}: low exceeds high")
        if self.initial is not None and self.initial not in self.domain:
            raise ValueError(f"{self.name}: initial {self.initial!r} outside domain")
        return self

    @property
    def nondeterministic(self) -> bool:
        """True when the value comes from outside the process.

        A claim's size is decided by the claimant, not the workflow. Pinning such a
        variable to one starting value makes every branch that depends on it
        unreachable, and any invariant about those branches passes *vacuously* —
        TLC reports success having never visited the case you cared about.
        """
        return self.initial is None

    @property
    def domain(self) -> list[int | str]:
        if self.values is not None:
            return list(self.values)
        return list(range(self.low, self.high + 1))  # type: ignore[arg-type]


class Guard(BaseModel):
    """A condition on one variable. The natural-language original stays attached."""

    variable: str
    op: Comparison
    value: int | str
    stated_as: str = Field(default="", description="The natural-language condition this came from")

    def to_tla(self) -> str:
        return f"{self.variable} {_TLA_OPERATOR[self.op]} {_render_tla(self.value)}"

    def holds(self, state: dict[str, int | str]) -> bool:
        current = state[self.variable]
        if self.op == "eq":
            return current == self.value
        if self.op == "ne":
            return current != self.value
        if not isinstance(current, int) or not isinstance(self.value, int):
            raise TypeError(f"ordering compare needs integers: {self.variable}")
        return {
            "lt": current < self.value,
            "le": current <= self.value,
            "gt": current > self.value,
            "ge": current >= self.value,
        }[self.op]

    def __str__(self) -> str:
        return f"{self.variable} {_PYTHON_COMPARE[self.op]} {self.value!r}"


class Effect(BaseModel):
    """An assignment to one variable, or an increment of an integer one."""

    variable: str
    value: int | str | None = None
    increment: int | None = None

    @model_validator(mode="after")
    def _one_action(self) -> Effect:
        if (self.value is None) == (self.increment is None):
            raise ValueError(f"{self.variable}: give either value or increment")
        return self

    def apply(self, state: dict[str, int | str]) -> int | str:
        if self.increment is not None:
            current = state[self.variable]
            if not isinstance(current, int):
                raise TypeError(f"cannot increment non-integer {self.variable}")
            return current + self.increment
        assert self.value is not None
        return self.value


class Transition(BaseModel):
    """One edge: from a state to a state, when the guards hold, applying effects."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    source: str
    target: str
    guards: list[Guard] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)

    def enabled(self, state: dict[str, int | str]) -> bool:
        return all(guard.holds(state) for guard in self.guards)


class State(BaseModel):
    """A step in the process, optionally handled by an agent.

    `agent_method` names the `@ai_method` the interpreter dispatches to. States
    with no agent are pure control points, which is what keeps the verified
    skeleton small.
    """

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = ""
    agent_method: str | None = None
    terminal: bool = False


class Invariant(BaseModel):
    """A safety property: this conjunction must hold in every reachable state.

    `forbidden_state` plus guards expresses "never in state X while condition Y",
    which is the shape most business rules take ("no payment before two approvals").
    """

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    stated_as: str = ""
    forbidden_state: str | None = None
    forbidden_when: list[Guard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_vacuous(self) -> Invariant:
        if self.forbidden_state is None and not self.forbidden_when:
            raise ValueError(f"{self.name}: an invariant with no condition forbids nothing")
        return self

    def violated_by(self, current: str, state: dict[str, int | str]) -> bool:
        if self.forbidden_state is not None and current != self.forbidden_state:
            return False
        return all(guard.holds(state) for guard in self.forbidden_when)


class Process(BaseModel):
    """A whole mined process: states, transitions, variables, invariants."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = ""
    states: list[State]
    initial_state: str
    variables: list[Variable] = Field(default_factory=list)
    transitions: list[Transition]
    invariants: list[Invariant] = Field(default_factory=list)

    @model_validator(mode="after")
    def _referentially_sound(self) -> Process:
        """Reject a process TLC would reject, but with a better message.

        Catching dangling references here means the model-checker only ever sees
        well-formed specs, so a TLC failure is a real property violation rather
        than a typo in generated data.
        """
        names = [s.name for s in self.states]
        if len(names) != len(set(names)):
            raise ValueError("duplicate state names")
        known = set(names)
        if self.initial_state not in known:
            raise ValueError(f"initial_state {self.initial_state!r} is not a declared state")

        variables = {v.name for v in self.variables}
        if len(variables) != len(self.variables):
            raise ValueError("duplicate variable names")

        transition_names = [t.name for t in self.transitions]
        if len(transition_names) != len(set(transition_names)):
            raise ValueError("duplicate transition names")

        for kind, owned in (
            ("transition", transition_names),
            ("invariant", [i.name for i in self.invariants]),
            ("variable", variables),
        ):
            for owner in owned:
                if owner in _TLA_RESERVED:
                    raise ValueError(
                        f"{kind} name {owner!r} is reserved: the TLA+ renderer defines it"
                    )

        for transition in self.transitions:
            for end in (transition.source, transition.target):
                if end not in known:
                    raise ValueError(f"{transition.name}: unknown state {end!r}")
            for guard in transition.guards:
                _check_variable(guard.variable, guard.value, self, f"{transition.name} guard")
            for effect in transition.effects:
                _check_variable(effect.variable, effect.value, self, f"{transition.name} effect")

        for invariant in self.invariants:
            if invariant.forbidden_state is not None and invariant.forbidden_state not in known:
                raise ValueError(f"{invariant.name}: unknown state {invariant.forbidden_state!r}")
            for guard in invariant.forbidden_when:
                _check_variable(guard.variable, guard.value, self, f"{invariant.name}")

        if not any(s.terminal for s in self.states):
            raise ValueError("no terminal state: the process could never complete")
        return self

    @property
    def state_map(self) -> dict[str, State]:
        return {s.name: s for s in self.states}

    @property
    def variable_map(self) -> dict[str, Variable]:
        return {v.name: v for v in self.variables}

    def initial_assignments(self) -> list[dict[str, int | str]]:
        """Every starting assignment: the cross product over free variables.

        A variable with a fixed `initial` contributes one value; a nondeterministic
        one contributes its whole domain, so the interpreter and Hypothesis explore
        the same starting states TLC does.
        """
        import itertools

        names = [v.name for v in self.variables]
        choices = [v.domain if v.nondeterministic else [v.initial] for v in self.variables]
        return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*choices)]

    def outgoing(self, state: str) -> list[Transition]:
        return [t for t in self.transitions if t.source == state]

    def unreachable_states(self) -> set[str]:
        """States no transition can reach — ignoring guards, so this is topological.

        A cheap structural check. TLC finds the semantic version, where a state is
        reachable on paper but no guard assignment ever gets there.
        """
        reached = {self.initial_state}
        frontier = [self.initial_state]
        while frontier:
            for transition in self.outgoing(frontier.pop()):
                if transition.target not in reached:
                    reached.add(transition.target)
                    frontier.append(transition.target)
        return {s.name for s in self.states} - reached


def _check_variable(name: str, value: int | str | None, process: Process, where: str) -> None:
    variable = process.variable_map.get(name)
    if variable is None:
        raise ValueError(f"{where}: unknown variable {name!r}")
    if value is not None and value not in variable.domain:
        raise ValueError(f"{where}: {value!r} outside the domain of {name!r}")


def _render_tla(value: int | str) -> str:
    return str(value) if isinstance(value, int) else f'"{value}"'
