"""The miner's accumulating toolkit: real functions, shipped as `Procedural` source.

The learnable parameter is a `str` of Python source, and the tempting shape is a triple-quoted
literal in `minelearn.py`. Source in a string is not importable, type-checkable, lintable, or
callable by a test, so the seed helpers would be the only unverified part of this project and
they are what the agent's first round depends on entirely. Written as real functions they are
all four, and `SEED_TOOLKIT` is assembled from `inspect.getsource`. Verified: `getsource`
round-trips through `ast.parse` and loads into the sandbox namespace unchanged, annotations and
all, because the sandbox never evaluates them. Rationale: `docs/design/toolkit.md`.

**`io` is not an authorised import.** `SAFE_BUILTINS` in
`ai_functions.tools.local_python_executor` does not list it and `aimine.ANALYSIS_IMPORTS` does
not add it, so `polars.read_csv(io.StringIO(log_csv))` raises `InterpreterError: Import of io
is not allowed` — despite being the route `aimine`'s own prompt recommends.
`polars.read_csv(log_csv.encode())` works and is what `load_log` does. That is most of
`load_log`'s reason to exist and the design rule here: a helper the agent cannot get wrong
beats an instruction it has to remember. `polars.__version__` also raises, because the
interpreter forbids dunder attribute access, so anything reaching for a dunder fails at call
time rather than load time — which is why `minelearn`'s rehearsal exists.

**Only `def` lines and docstrings are advertised.** `procedural_signatures` walks top-level
`def`s, skips `_`-prefixed names, and emits the signature plus docstring; module docstrings,
comments, and module-level constants are silently dropped. Measured, not inferred. So anything
the agent must read has to be a docstring on a function it can call, which is why `minelearn`'s
prose parameter is a separate gradient target rather than comments in here.

These helpers are the primitives a support-threshold argument is made of. `sweep_thresholds`
has the leverage, turning "choose a threshold" into an argmax over a curve computing the same
coverage-versus-selectivity trade `minelearn.Attempt.score` grades.

**The seed baseline, and the correction it needs.** On `data/receipt.xes` at a 400-case sample,
driving `final_answer` from `sweep_thresholds`' argmax with no model judgment: threshold 19, 13
edges, 84.38% coverage, edge share 0.1884, no invented edges, `Attempt.score` 0.8274. The
argmax does *not* beat the frozen miner's method — `miner.mine` on the same sample at
`min_edge_cases=19` produces the identical 13 edges and the identical 0.8274, because
`handoff_support` plus a threshold *is* the directly-follows count. The frozen miner at its
default 25 scores 0.8210, so choosing the cutoff by argmax is worth +0.0064 and nothing else.
The honest claim is narrow: the frozen method with its one free parameter tuned.
`tests/app/test_minelearn.py::test_live_toolkit_beats_its_own_seed_baseline` uses 0.8274 as the bar
because the prose-only loop or the frozen default would be easier comparisons.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

# ── The seed helpers ──
#
# Each one is advertised to the agent by its signature and docstring, so the
# docstrings are prompt text and are written as such: they say when to reach for the
# helper, and they name the trap it exists to avoid.


def load_log(log_csv: str) -> Any:
    """Parse the event-log CSV into a polars DataFrame sorted by case then position.

    Use this instead of `polars.read_csv(io.StringIO(log_csv))`. `io` is not an
    authorised import in this environment, so the StringIO route raises
    `Import of io is not allowed`. Encoding the string is the route that works.
    """
    import polars as pl

    return pl.read_csv(log_csv.encode()).sort(["case_id", "position"])


def handoff_support(frame: Any) -> list[tuple[str, str, int, int]]:
    """Count every consecutive activity pair within a case.

    Returns `(source, target, occurrences, cases)` sorted by cases descending.
    `cases` is distinct cases that walked the pair, which is the support a threshold
    is applied to, and it is the number to rank by: `occurrences` double-counts a
    case that walked the same handoff twice.

    Self-loops are included. Drop them before counting anything you will be scored
    on: the model compiler drops them, so a self-loop kept here inflates your own
    edge count against a denominator that never had it.
    """
    from collections import defaultdict

    occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
    case_sets: defaultdict[tuple[str, str], set[Any]] = defaultdict(set)
    rows = frame.select("case_id", "activity").rows()
    for index in range(len(rows) - 1):
        case, source = rows[index]
        next_case, target = rows[index + 1]
        if case != next_case:
            continue
        occurrences[(source, target)] += 1
        case_sets[(source, target)].add(case)
    counted = [
        (source, target, count, len(case_sets[(source, target)]))
        for (source, target), count in occurrences.items()
    ]
    counted.sort(key=lambda row: (-row[3], -row[2], row[0], row[1]))
    return counted


def start_activity(frame: Any) -> str:
    """The activity most cases begin at, counted from first position within each case."""
    from collections import Counter

    firsts: Counter[str] = Counter()
    seen: set[Any] = set()
    for case, activity in frame.select("case_id", "activity").rows():
        if case not in seen:
            seen.add(case)
            firsts[activity] += 1
    return firsts.most_common(1)[0][0] if firsts else ""


def support_gaps(support: list[int], top: int = 8) -> list[tuple[int, int, float]]:
    """Find where the support distribution actually breaks.

    `support` is the list of per-handoff case counts. Returns
    `(cutoff, edges_kept, ratio)` for the largest multiplicative jumps between
    consecutive distinct support values, widest gap first. Keeping edges with support
    at or above `cutoff` sits above that gap.

    A wide ratio is evidence the log itself separates routine handoffs from
    exceptions at that point, which is a defensible reason to cut there. Ratios that
    are all near 1 are the opposite finding — the distribution is smooth and no
    cutoff is natural — and that is worth knowing before you claim one is.
    """
    distinct = sorted({int(value) for value in support if value > 0}, reverse=True)
    if len(distinct) < 2:
        return []
    gaps: list[tuple[int, int, float]] = []
    for index in range(len(distinct) - 1):
        high, low = distinct[index], distinct[index + 1]
        kept = sum(1 for value in support if value >= high)
        gaps.append((high, kept, round(high / max(low, 1), 3)))
    gaps.sort(key=lambda row: -row[2])
    return gaps[:top]


def reachable_from(edges: list[tuple[str, str]], start: str) -> set[str]:
    """Activities reachable from `start` by following `edges`, `start` included."""
    successors: dict[str, list[str]] = {}
    for source, target in edges:
        successors.setdefault(source, []).append(target)
    reached = {start}
    frontier = [start]
    while frontier:
        for target in successors.get(frontier.pop(), ()):
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    return reached


def islands(edges: list[tuple[str, str]], start: str) -> set[str]:
    """Activities in `edges` that no path from `start` reaches.

    Must be empty. A non-empty result means the model you are about to submit will
    be rejected as disconnected, so check it before calling `final_answer` rather
    than after being sent back.
    """
    activities: set[str] = set()
    for source, target in edges:
        activities.add(source)
        activities.add(target)
    return activities - reachable_from(edges, start)


def terminal_candidates(edges: list[tuple[str, str]]) -> list[str]:
    """Activities that appear as a target and never as the source of a real handoff.

    Where cases structurally drain, so this is the list to report as
    `terminal_activities`. Self-loops are ignored: an activity that only loops to
    itself still ends the case.

    An empty result means every activity in your edge set has a successor, so the
    graph is all cycles and no exit. That model is rejected. Cut tighter, or name a
    terminal from the observed last activities.
    """
    sources = {source for source, target in edges if source != target}
    targets = {target for _, target in edges}
    return sorted(targets - sources)


def replay_coverage(frame: Any, edges: list[tuple[str, str]], start: str) -> float:
    """Share of whole cases this edge set can replay end to end.

    The same quantity the grader measures, computed here so a cutoff can be chosen
    by measurement rather than by argument. A case counts only if its first activity
    is `start` and every consecutive pair is in `edges`, so one missing handoff
    disqualifies an otherwise ordinary case — which is why coverage falls faster
    than edge count as you tighten.
    """
    allowed = set(edges)
    paths: dict[Any, list[str]] = {}
    for case, activity in frame.select("case_id", "activity").rows():
        paths.setdefault(case, []).append(activity)
    if not paths:
        return 0.0
    conforming = 0
    for path in paths.values():
        if path[0] != start:
            continue
        if all((a, b) in allowed for a, b in zip(path, path[1:], strict=False)):
            conforming += 1
    return round(conforming / len(paths), 4)


def sweep_thresholds(
    frame: Any,
    counted: list[tuple[str, str, int, int]],
    start: str,
    cutoffs: list[int] | None = None,
) -> list[tuple[int, int, float, float, float]]:
    """Score every candidate cutoff, best first, on the trade you are graded on.

    Returns `(cutoff, kept_edges, edge_share, coverage, balanced)` sorted by
    `balanced` descending, where `balanced` is the harmonic mean of coverage and
    `1 - edge_share`. That is the objective, so its argmax is the cutoff to defend
    and reading it off this sweep is a measurement rather than a claim.

    Self-loops are dropped before anything is counted, matching how the model is
    compiled, and `edge_share` divides by the distinct non-self handoffs in the log
    you were given — the same denominator the grader uses.

    Do not stop at the first cutoff that looks reasonable. The curve is not
    monotonic in either term, and neighbouring cutoffs can differ by more in
    `balanced` than a wide change of approach.
    """
    real = [(s, t, n, c) for s, t, n, c in counted if s != t]
    visible = len({(s, t) for s, t, _, _ in real}) or 1
    if cutoffs is None:
        cutoffs = sorted({c for _, _, _, c in real})
    scored: list[tuple[int, int, float, float, float]] = []
    for cutoff in cutoffs:
        kept = [(s, t) for s, t, _, c in real if c >= cutoff]
        if not kept:
            continue
        share = len(kept) / visible
        coverage = replay_coverage(frame, kept, start)
        selectivity = 1.0 - min(max(share, 0.0), 1.0)
        total = coverage + selectivity
        balanced = 0.0 if total <= 0 else 2 * coverage * selectivity / total
        scored.append((cutoff, len(kept), round(share, 4), coverage, round(balanced, 4)))
    scored.sort(key=lambda row: -row[4])
    return scored


SEED_HELPERS: tuple[Any, ...] = (
    load_log,
    handoff_support,
    start_activity,
    support_gaps,
    reachable_from,
    islands,
    terminal_candidates,
    replay_coverage,
    sweep_thresholds,
)
"""The functions that make up the seed toolkit, in the order they are emitted.

Order matters only for readability: the sandbox executes the whole block before the
agent calls anything, so a helper may reference one defined below it. `islands`
calling `reachable_from` works either way.
"""


def seed_toolkit() -> str:
    """Render the seed helpers as one parseable Python source block.

    Imports live inside each function body rather than at module top level, and that
    is deliberate. A module-level import in the recalled toolkit runs at sandbox
    setup, and a setup failure raises `ValueError: Failed to load procedural code
    into the executor namespace` — losing the entire round rather than one helper.
    An import in a body fails only when that helper is called, which the round can
    report. Verified both ways: `import os` at module level in a recalled toolkit
    aborts the cycle; the same import inside a function body loads fine.
    """
    return "\n\n".join(inspect.getsource(helper).rstrip() for helper in SEED_HELPERS) + "\n"


SEED_TOOLKIT: str = seed_toolkit()
"""The seed value of the `Procedural` parameter."""


# ── Rehearsal: does a recalled toolkit survive being loaded and called? ──


REHEARSAL_LOG = (
    "case_id,position,activity\n"
    "1,0,Alpha\n1,1,Beta\n1,2,Gamma\n"
    "2,0,Alpha\n2,1,Beta\n2,2,Gamma\n"
    "3,0,Alpha\n3,1,Delta\n3,2,Gamma\n"
)
"""A three-case log with one rare handoff, small enough to rehearse against.

Three cases and not two: `support_gaps` needs at least two distinct support values
to return anything, so a log where every handoff has identical support would
rehearse the function without exercising the branch that matters.
"""


def _rehearsal_fixtures(log_csv: str) -> dict[str, str]:
    """Sandbox expressions that can stand in for a helper's parameters, by name.

    Keyed by parameter name, valued as source text evaluated inside the sandbox.
    Matching by name is the whole mechanism: it means a helper the agent *adds*
    gets rehearsed too, as long as it names its parameters the way the toolkit
    does, and a helper that invents a new parameter name is honestly reported as
    unrehearsed rather than quietly skipped.
    """
    del log_csv
    return {
        "log_csv": "_rehearsal_csv",
        "frame": "_rehearsal_frame",
        "counted": "_rehearsal_counted",
        "support": "[c for _, _, _, c in _rehearsal_counted]",
        "edges": "_rehearsal_edges",
        "start": "_rehearsal_start",
    }


def _callable_helpers(code: str) -> list[tuple[str, list[str], int]]:
    """Return `(name, positional parameter names, count without a default)` per helper.

    Underscore-prefixed definitions are excluded, matching what the runtime
    advertises: a helper the agent is never told about is not part of the contract.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found: list[tuple[str, list[str], int]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("_"):
            continue
        positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        required = len(positional) - len(node.args.defaults)
        found.append((node.name, positional, max(required, 0)))
    return found


BOOTSTRAP: tuple[str, ...] = ("load_log", "handoff_support", "start_activity")
"""Helpers the rehearsal builds its fixtures from, so they must keep existing.

A frame, a support count, and a start activity are what every other helper's
arguments are made of, so the rehearsal cannot construct anything without these
three. That makes them a contract: the optimizer may change their bodies, and a
rewrite that deletes or renames one fails rehearsal and is rolled back.

That is a real restriction and worth naming rather than discovering. The agent cannot
evolve away from these three signatures. The alternative is a rehearsal that skips
whatever it cannot construct arguments for, which on a toolkit that renamed
`load_log` would skip everything and report a pass — a guard that certifies an empty
check is worse than a guard with a stated boundary.
"""


def missing_bootstrap(code: str) -> list[str]:
    """Bootstrap helpers absent from `code`. Non-empty means rehearsal cannot run."""
    defined = {name for name, _, _ in _callable_helpers(code)}
    return [name for name in BOOTSTRAP if name not in defined]


def rehearsal_probe(code: str, log_csv: str = REHEARSAL_LOG) -> tuple[str, list[str], list[str]]:
    """Build the probe that calls every rehearsable helper in `code`.

    Returns `(probe source, rehearsed helper names, skipped helper names)`. A helper
    is skipped when a required parameter has no fixture of that name, and skipping is
    reported rather than swallowed: "we did not check this one" and "this one passed"
    must not be the same observation.

    The probe is deliberately not defensive. Every call is at the top level of the
    executed block, so the first helper that raises aborts the probe and the error
    text names it. Wrapping each call in a try/except would produce a probe that
    always succeeds, which is a rehearsal that cannot fail.

    A missing bootstrap helper produces a probe whose first statement is a bare
    `raise` naming what is gone. Letting the sandbox discover it instead reports
    `Forbidden function evaluation: 'handoff_support' is not among the explicitly
    allowed tools`, which is true and reads like a sandbox permission problem rather
    than like a deleted function.
    """
    absent = missing_bootstrap(code)
    if absent:
        message = (
            f"the toolkit no longer defines {', '.join(absent)}, which the rehearsal "
            "builds its fixtures from, so nothing can be checked"
        )
        return f"raise ValueError({message!r})\n", [], []

    fixtures = _rehearsal_fixtures(log_csv)
    lines = [
        "_rehearsal_csv = " + repr(log_csv),
        "_rehearsal_frame = load_log(_rehearsal_csv)",
        "_rehearsal_counted = handoff_support(_rehearsal_frame)",
        "_rehearsal_start = start_activity(_rehearsal_frame)",
        "_rehearsal_edges = [(s, t) for s, t, _, _ in _rehearsal_counted if s != t]",
    ]
    rehearsed = list(BOOTSTRAP)
    skipped: list[str] = []
    for name, positional, required in _callable_helpers(code):
        if name in rehearsed:
            continue
        needed = positional[:required]
        if any(parameter not in fixtures for parameter in needed):
            skipped.append(name)
            continue
        arguments = ", ".join(fixtures[parameter] for parameter in needed)
        lines.append(f"{name}({arguments})")
        rehearsed.append(name)
    lines.append("print('rehearsed', " + repr(len(rehearsed)) + ")")
    return "\n".join(lines) + "\n", rehearsed, skipped
