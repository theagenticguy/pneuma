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

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ir import Process, _render_tla

TLA_JAR = Path(__file__).resolve().parents[3] / "tools" / "tla2tools.jar"


@dataclass(frozen=True)
class CheckResult:
    """What TLC concluded."""

    ok: bool
    states_found: int
    distinct_states: int
    violated: str | None
    trace: list[str]
    raw: str

    @property
    def summary(self) -> str:
        if self.ok:
            return f"verified: {self.distinct_states} distinct states, no violation"
        return f"VIOLATED {self.violated}: trace of {len(self.trace)} states"


def render(process: Process) -> str:
    """Return a TLA+ module for `process`."""
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


def render_config(process: Process) -> str:
    """Return the .cfg naming the specification and every invariant to check."""
    invariants = ["TypeOK", "NoDeadlock", *[i.name for i in process.invariants]]
    lines = ["SPECIFICATION Spec"]
    lines += [f"INVARIANT {name}" for name in invariants]
    return "\n".join(lines) + "\n"


def tlc_available() -> bool:
    return TLA_JAR.is_file() and shutil.which("java") is not None


def check(process: Process, *, timeout: int = 180) -> CheckResult:
    """Model-check `process` with TLC.

    Raises:
        RuntimeError: java or tla2tools.jar is missing.
    """
    if not tlc_available():
        raise RuntimeError(f"TLC needs java and {TLA_JAR}")

    with tempfile.TemporaryDirectory() as work:
        directory = Path(work)
        (directory / f"{process.name}.tla").write_text(render(process))
        (directory / f"{process.name}.cfg").write_text(render_config(process))
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
    return _parse(completed.stdout + completed.stderr)


def _parse(output: str) -> CheckResult:
    violated: str | None = None
    states_found = distinct = 0
    trace: list[str] = []

    for line in output.splitlines():
        stripped = line.strip()
        if "Invariant" in stripped and "is violated" in stripped:
            violated = stripped.split("Invariant", 1)[1].split("is violated")[0].strip()
        elif stripped.startswith("State ") and ":" in stripped:
            trace.append(stripped)
        elif "states generated" in stripped and "distinct states found" in stripped:
            words = stripped.replace(",", "").split()
            states_found = int(words[0])
            for index, word in enumerate(words):
                if word == "distinct":
                    distinct = int(words[index - 1])
                    break

    ok = violated is None and "Model checking completed. No error has been found." in output
    return CheckResult(
        ok=ok,
        states_found=states_found,
        distinct_states=distinct,
        violated=violated,
        trace=trace,
        raw=output,
    )
