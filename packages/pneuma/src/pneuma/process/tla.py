"""Render a `Process` to TLA+ and run TLC over it.

TLC verifies the *skeleton*: which states are reachable under which variable
assignments, whether the invariants hold in all of them, and whether the process
can deadlock. It knows nothing about whether the agent inside a state did its job
— that is what post-conditions and Hypothesis are for.

The translation is direct because the IR was designed for it. Each transition
becomes an action, guards become its enabling condition, effects become primed
assignments, and every unmentioned variable is explicitly unchanged. `Deadlock`
is expressed as a state predicate rather than left to TLC's own deadlock check, so
a violation names the stuck state.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .ir import Process, _render_tla

TLA_JAR = Path(__file__).resolve().parents[3] / "tools" / "tla2tools.jar"

Outcome = Literal["verified", "violated", "vacuous", "failed"]

_SUCCESS_LINE = "Model checking completed. No error has been found."

# TLC narrates a counterexample on lines that also begin with `Error:`. They are
# part of a violation report, not an independent failure.
_NARRATION = (
    "Error: The behavior up to this point is:",
    "Error: The following behavior constitutes a counter-example:",
    "Error: The error occurred when TLC was evaluating the nested",
)

# TLC's documented exit codes for "the spec is fine, the property is not". Measured
# against tla2tools 2.19: everything else means the run itself did not complete.
_TLC_INVARIANT_VIOLATED = 12
_TLC_DEADLOCK = 11
_TLC_TEMPORAL = 13

_INVARIANT_VIOLATED = re.compile(r"Invariant\s+(\S+)\s+is violated")
_COUNTS = re.compile(r"([\d,]+) states generated, ([\d,]+) distinct states found")
_INITIAL_COUNT = re.compile(r"Finished computing initial states: ([\d,]+) distinct state")


@dataclass(frozen=True)
class CheckResult:
    """What TLC concluded, and whether that verdict can be trusted.

    `ok` is derived rather than stored. A pass has to survive every gate at once:
    TLC exited cleanly, printed its success line, reported no violation, no error
    of any kind, and actually explored some states. Reading a broken run as a clean
    one is the worst failure this project has, so no single string decides it.
    """

    outcome: Outcome
    returncode: int
    states_found: int
    distinct_states: int
    initial_states: int
    violated: str | None
    failure: str | None
    trace: list[str]
    raw: str
    witness_counts: tuple[tuple[str, int], ...] | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "verified"

    @property
    def witnesses(self) -> dict[str, int] | None:
        """Per-invariant count of states that actually exercised its condition."""
        return None if self.witness_counts is None else dict(self.witness_counts)

    @property
    def vacuous_invariants(self) -> tuple[str, ...]:
        """Invariants that held only because nothing ever reached their condition."""
        if self.witness_counts is None:
            return ()
        return tuple(sorted(name for name, count in self.witness_counts if count <= 0))

    def with_witnesses(self, counts: Mapping[str, int]) -> CheckResult:
        """Attach per-invariant witness counts, downgrading a vacuous pass.

        The seam for reachability-vacuity analysis: TLC's own verdict says only
        that no invariant was violated, which a process can satisfy by never
        reaching the condition at all. An invariant with zero witnesses is not
        verified, so the pass is withdrawn.
        """
        attached = tuple(counts.items())
        starved = any(count <= 0 for _, count in attached)
        outcome = "vacuous" if starved and self.outcome == "verified" else self.outcome
        return replace(self, witness_counts=attached, outcome=outcome)

    @property
    def summary(self) -> str:
        if self.outcome == "verified":
            return f"verified: {self.distinct_states} distinct states, no violation"
        if self.outcome == "violated":
            # A lasso trace also carries a `Back to state` closing line; count states.
            steps = sum(1 for line in self.trace if line.startswith("State "))
            return f"VIOLATED {self.violated}: trace of {steps} states"
        if self.outcome == "vacuous":
            if self.vacuous_invariants:
                names = ", ".join(self.vacuous_invariants)
                return f"NOT VERIFIED: no witness state for {names}"
            return "NOT VERIFIED: TLC explored 0 distinct states, so nothing was checked"
        return f"CHECKER FAILED (exit {self.returncode}): {self.failure}"


def render(process: Process, *, liveness: bool = False) -> str:
    """Return a TLA+ module for `process`.

    With `liveness=True` the module also defines `Termination == <>(pc \\in
    {terminals})` and strengthens the spec to `Init /\\ [][Next]_vars /\\
    WF_vars(Next)`. See `check` for why that is opt-in.
    """
    variables = [v.name for v in process.variables]
    declared = ", ".join(["pc", *variables])

    lines = [
        f"---- MODULE {process.name} ----",
        "EXTENDS Integers, TLC",
        "",
        f"VARIABLES {declared}",
        "",
        f"vars == <<{declared}>>",
        "",
        "States == {" + ", ".join(f'"{s.name}"' for s in process.states) + "}",
        "",
        "TypeOK ==",
        "  /\\ pc \\in States",
    ]
    for variable in process.variables:
        if variable.values is not None:
            domain = "{" + ", ".join(f'"{v}"' for v in variable.values) + "}"
        else:
            domain = f"{variable.low}..{variable.high}"
        lines.append(f"  /\\ {variable.name} \\in {domain}")

    lines += [
        "",
        "Init ==",
        f'  /\\ pc = "{process.initial_state}"',
    ]
    for variable in process.variables:
        if variable.nondeterministic:
            # `\in` over the domain, not `=`: TLC then explores every starting value,
            # so a branch guarded on this variable is actually reached and an
            # invariant about it cannot pass vacuously.
            if variable.values is not None:
                domain = "{" + ", ".join(f'"{v}"' for v in variable.values) + "}"
            else:
                domain = f"{variable.low}..{variable.high}"
            lines.append(f"  /\\ {variable.name} \\in {domain}")
        else:
            lines.append(f"  /\\ {variable.name} = {_render_tla(variable.initial)}")
    lines.append("")

    for transition in process.transitions:
        lines.append(f"{transition.name} ==")
        lines.append(f'  /\\ pc = "{transition.source}"')
        for guard in transition.guards:
            lines.append(f"  /\\ {guard.to_tla()}")
        lines.append(f'  /\\ pc\' = "{transition.target}"')
        touched: set[str] = set()
        for effect in transition.effects:
            touched.add(effect.variable)
            if effect.increment is not None:
                lines.append(f"  /\\ {effect.variable}' = {effect.variable} + {effect.increment}")
            else:
                lines.append(f"  /\\ {effect.variable}' = {_render_tla(effect.value)}")  # type: ignore[arg-type]
        untouched = [v for v in variables if v not in touched]
        if untouched:
            lines.append("  /\\ UNCHANGED <<" + ", ".join(untouched) + ">>")
        lines.append("")

    # A terminal state must be able to stall, or TLC reports its own deadlock on
    # every completing run. Real stalls are caught by the Deadlock invariant below.
    terminal = [s.name for s in process.states if s.terminal]
    lines += [
        "Done ==",
        "  /\\ pc \\in {" + ", ".join(f'"{t}"' for t in terminal) + "}",
        "  /\\ UNCHANGED vars",
        "",
        "Next ==",
    ]
    for transition in process.transitions:
        lines.append(f"  \\/ {transition.name}")
    lines.append("  \\/ Done")

    if liveness:
        # `<>` needs one visit to a terminal, so `Done` stuttering there is harmless:
        # the property is already satisfied by the time the run can stall.
        terminals = ", ".join(f'"{t}"' for t in terminal)
        lines += ["", f"Termination == <>(pc \\in {{{terminals}}})"]
        # `WF_vars(Next)` only forbids stalling forever while a move is enabled. It
        # does not forbid taking moves forever, so a reachable cycle still refutes
        # `Termination` — correctly, since such a behavior never terminates.
        lines += ["", "Spec == Init /\\ [][Next]_vars /\\ WF_vars(Next)", ""]
    else:
        lines += ["", "Spec == Init /\\ [][Next]_vars", ""]

    # Stuck: a non-terminal state with no enabled outgoing transition.
    stuck_clauses: list[str] = []
    for state in process.states:
        if state.terminal:
            continue
        outgoing = process.outgoing(state.name)
        if not outgoing:
            stuck_clauses.append(f'  \\/ pc = "{state.name}"')
            continue
        enabled = []
        for transition in outgoing:
            if transition.guards:
                enabled.append("(" + " /\\ ".join(g.to_tla() for g in transition.guards) + ")")
            else:
                enabled.append("TRUE")
        stuck_clauses.append(f'  \\/ (pc = "{state.name}" /\\ ~(' + " \\/ ".join(enabled) + "))")

    lines.append("NoDeadlock ==")
    if stuck_clauses:
        lines.append("  ~(")
        lines.extend(f"  {clause}" for clause in stuck_clauses)
        lines.append("  )")
    else:
        lines.append("  TRUE")
    lines.append("")

    for invariant in process.invariants:
        conditions: list[str] = []
        if invariant.forbidden_state is not None:
            conditions.append(f'pc = "{invariant.forbidden_state}"')
        conditions.extend(guard.to_tla() for guard in invariant.forbidden_when)
        body = " /\\ ".join(conditions)
        lines += [f"{invariant.name} ==", f"  ~({body})", ""]

    lines.append("====")
    return "\n".join(lines)


def render_config(process: Process, *, liveness: bool = False) -> str:
    """Return the .cfg naming the specification and every invariant to check.

    At most one `PROPERTY` is ever named. TLC's violation report says only that
    "temporal properties were violated", never which one, so a second property
    would make the verdict unattributable.
    """
    invariants = ["TypeOK", "NoDeadlock", *[i.name for i in process.invariants]]
    lines = ["SPECIFICATION Spec"]
    lines += [f"INVARIANT {name}" for name in invariants]
    if liveness:
        lines.append("PROPERTY Termination")
    return "\n".join(lines) + "\n"


def tlc_available() -> bool:
    return TLA_JAR.is_file() and shutil.which("java") is not None


def check(process: Process, *, timeout: int = 180, liveness: bool = False) -> CheckResult:
    """Model-check `process` with TLC.

    The default checks safety only: every invariant holds in every reachable state,
    and no state is stuck. That says nothing about whether the process ever finishes
    — a run that cycles forever violates no invariant.

    `liveness=True` adds `PROPERTY Termination`, asking TLC whether every behavior
    eventually reaches a terminal state. It is opt-in because it is a different
    question, and a real one: a mined process whose log contains a rework loop has a
    genuine cycle, so it is legitimately non-terminating and safety-verified at the
    same time. Turning this on by default would recast those models as broken rather
    than as accurately mined.

    The fairness attached is `WF_vars(Next)`, which rules out only stalling forever
    while a move is enabled. It does not rule out *moving* forever, so a reachable
    cycle among real transitions still refutes `Termination`. That verdict is
    correct, not a limitation: such a behavior really does never terminate. Demanding
    that a loop eventually be exited is a strictly stronger assumption — strong
    fairness on each exit action — and is deliberately not made here.

    Two ways to misread a `liveness=True` result:

    - Safety masks liveness. If an invariant also fails, TLC stops at exit 12 and
      never evaluates the temporal property. Any `violated`, `vacuous`, or `failed`
      outcome means termination was *not* checked, so it must not be reported as
      "termination verified" — only `verified` carries that claim.
    - A `violated` result names `"TemporalProperty"`, not `Termination`. TLC does not
      say which temporal property failed, which is why only one is ever configured.

    Raises:
        RuntimeError: java or tla2tools.jar is missing.
    """
    if not tlc_available():
        raise RuntimeError(f"TLC needs java and {TLA_JAR}")

    with tempfile.TemporaryDirectory() as work:
        directory = Path(work)
        (directory / f"{process.name}.tla").write_text(render(process, liveness=liveness))
        (directory / f"{process.name}.cfg").write_text(render_config(process, liveness=liveness))
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "java",
                "-XX:+UseSerialGC",
                "-cp",
                str(TLA_JAR),
                "tlc2.TLC",
                "-config",
                f"{process.name}.cfg",
                "-workers",
                "1",
                "-cleanup",
                f"{process.name}.tla",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=directory,
        )
    return _parse(completed.stdout + completed.stderr, returncode=completed.returncode)


def _parse(output: str, *, returncode: int) -> CheckResult:
    """Turn TLC's stdout plus its exit status into a verdict.

    Both are needed. TLC prints its success line before the JVM can still die, and
    it exits non-zero for failures it describes only in prose, so neither the text
    nor the status is sufficient alone.
    """
    violated: str | None = None
    failure: str | None = None
    states_found = distinct = initial = 0
    trace: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        match = _INVARIANT_VIOLATED.search(stripped)
        if match:
            violated = match.group(1).rstrip(".")
        elif "Error: Deadlock reached" in stripped:
            violated = "Deadlock"
        elif "Error: Temporal properties were violated" in stripped:
            violated = violated or "TemporalProperty"
        elif stripped.startswith("Error:") and stripped not in _NARRATION:
            failure = failure or stripped.removeprefix("Error:").strip()
        elif stripped.startswith("State ") and ":" in stripped:
            trace.append(stripped)
        elif stripped.startswith("Back to state"):
            # A liveness counterexample is a lasso, and this line is where it closes.
            # Without it the trace reads as a finite prefix and the loop is invisible.
            trace.append(stripped)

        counts = _COUNTS.search(stripped)
        if counts and not stripped.startswith("Progress("):
            states_found = int(counts.group(1).replace(",", ""))
            distinct = int(counts.group(2).replace(",", ""))
        starting = _INITIAL_COUNT.search(stripped)
        if starting:
            initial = int(starting.group(1).replace(",", ""))

    outcome: Outcome
    property_codes = (0, _TLC_INVARIANT_VIOLATED, _TLC_DEADLOCK, _TLC_TEMPORAL)
    if failure is not None or returncode not in property_codes:
        # A checker that broke reports neither "holds" nor "violated". Reporting it
        # as a violation would blame the process for the harness's failure.
        outcome = "failed"
        if failure is None:
            failure = f"TLC exited {returncode} without an Error: line"
        violated = None
    elif violated is not None:
        outcome = "violated"
    elif returncode != 0 or _SUCCESS_LINE not in output:
        outcome = "failed"
        failure = f"TLC exited {returncode} without reporting completion"
    elif distinct <= 0 or initial <= 0:
        # No error found because nothing was explored: an unsatisfiable Init, or a
        # spec whose state space is empty. Green here means untested, not safe.
        outcome = "vacuous"
    else:
        outcome = "verified"

    return CheckResult(
        outcome=outcome,
        returncode=returncode,
        states_found=states_found,
        distinct_states=distinct,
        initial_states=initial,
        violated=violated,
        failure=failure,
        trace=trace,
        raw=output,
    )
