# pneuma · Dead code

## Resolution

Acted on 2026-08-10. Seven of the CONFIRMED rows below were deleted from the source tree: `discover_and_grade` (`aimine.py`), `SEED_PLAYBOOK` (`learning.py`), `compliance_invariant` (`miner.py`), `FULL_GLOB` (`transcriptlog.py`), `RECOVERY_TS` (`incident.py`), `pick_first` (`interpreter.py`), and `transition_strategy` plus `start_strategy` (`properties.py`). `ruff` then removed five imports left unused by those deletions. `SPIKE_PEAK_DOMINANCE` was kept deliberately, for the reason given in its row.

Everything below this note is the pre-deletion audit, preserved as written. Its line numbers refer to the source tree as it stood before the deletions and no longer resolve against the current files; the reasoning that justified each removal is the point of the record.

Nine unreferenced exports in one file each, one unreferenced module, zero dead imports.

No dead-code analyzer is integrated: the dev dependency group lists `hypothesis`, `pm4py`, `pytest`, `pytest-asyncio`, and `ruff` only (`pyproject.toml:24-31`), and there is no `.github/workflows`, `Makefile`, or `justfile`. Findings below come from an AST pass over all 87 Python files, plus `ruff`'s own `F401` for the imports bucket.

Two dynamic-reference paths were resolved before any row was kept. `pythonpath = ["tests"]` (`pyproject.toml:46`) makes `tests/paths.py` importable as bare `paths`, and `pneuma = "pneuma.demo.cli:main"` (`pyproject.toml:22`) is a declared console script — both look unreferenced to an import-graph walk and neither is. The 740 `test_*` functions pytest collects by name convention are likewise excluded.

## Unreferenced exports

| Symbol | Path | Last modified |
| --- | --- | --- |
| `discover_and_grade` | `src/pneuma/casestudy/aimine.py:441` | 2026-07-31 |
| `SEED_PLAYBOOK` | `src/pneuma/casestudy/learning.py:63` | 2026-08-09 |
| `compliance_invariant` | `src/pneuma/casestudy/miner.py:244` | 2026-07-30 |
| `FULL_GLOB` | `src/pneuma/casestudy/transcriptlog.py:61` | 2026-07-30 |
| `RECOVERY_TS` | `src/pneuma/demo/incident.py:61` | 2026-07-31 |
| `SPIKE_PEAK_DOMINANCE` | `src/pneuma/detect/objective.py:935` | 2026-07-31 |
| `pick_first` | `src/pneuma/process/interpreter.py:405` | 2026-08-09 |
| `transition_strategy` | `src/pneuma/process/properties.py:36` | 2026-07-30 |
| `start_strategy` | `src/pneuma/process/properties.py:45` | 2026-07-30 |

All nine are CONFIRMED: AST-clean, undecorated, and unreachable by any dynamic-dispatch path in the repo. Each one carries information beyond "unused", and four of them are the same shape.

**Four are the unused half of a declared pair, where the twin is wired.** `FULL_GLOB` (`src/pneuma/casestudy/transcriptlog.py:61`) is the "scale test" corpus named alongside `FLEET_GLOB` (`:60`); `FLEET_GLOB` is the default at `src/pneuma/casestudy/transcriptlog.py:477` and is read by three tests, `FULL_GLOB` by nothing. `RECOVERY_TS` (`src/pneuma/demo/incident.py:61`) sits beside `ONSET_TS` (`:60`), which is read three times (`src/pneuma/demo/incident.py:1135`, `:1378`, `:1380`). `SEED_PLAYBOOK` (`src/pneuma/casestudy/learning.py:63`) was superseded in place: the module's real default comes from `SEED_ENTRIES` through `default_factory=lambda: list(SEED_ENTRIES)` (`src/pneuma/casestudy/learning.py:106`), while the identically-shaped `SEED_GUIDANCE` in the sibling module is properly wired (`src/pneuma/casestudy/minelearn.py:76` into `:116`). In each case the live twin is the evidence the dead one was meant to be reached.

**`SPIKE_PEAK_DOMINANCE` is unreferenced on purpose, and says so.** Its docstring reads "Not a tunable, a documented property of the check, kept named so the report and the tests can refer to one thing" (`src/pneuma/detect/objective.py:938-939`). The property is implemented as an unconditional guard, `if here < peak: continue` (`src/pneuma/detect/objective.py:1077`), which never reads the flag. Its only other occurrence anywhere in the repo is prose inside a test docstring (`tests/library/test_objective.py:280`). This row is a naming anchor, not a defect; deleting it would break a deliberate cross-reference.

**Two are unused seams in a module whose other seams are used.** `transition_strategy` (`src/pneuma/process/properties.py:36`) and `start_strategy` (`:45`) are public strategy builders in a Hypothesis module whose two other entry points are live — `run_sync` and `machine_for` at `tests/library/test_process.py:712`, `:721`, `:736` and `src/pneuma/casestudy/pipeline.py:184`. `machine_for` builds its start strategy inline as `st.sampled_from(range(len(initial)))` (`src/pneuma/process/properties.py:102`) and generates one rule per transition (`:148`), so it reimplements what both functions offer rather than calling them.

**Two are test-shaped helpers no test calls.** `pick_first` (`src/pneuma/process/interpreter.py:405`) is documented as "A deterministic stand-in for an agent, for tests and dry runs" (`:406`) and no test uses it. `discover_and_grade` (`src/pneuma/casestudy/aimine.py:441`) is an `async` wrapper over `Miner().compiled("discover")` and `grade`; `grade` alone is exercised repeatedly (`tests/app/test_aimine.py:189`, `:211`, `:232`), so the tests drive the halves and never the composition.

## Unreferenced files

| File | Lines | Last modified |
| --- | --- | --- |
| `src/pneuma/casestudy/benchmark.py` | 122 | 2026-07-30 |

`src/pneuma/casestudy/benchmark.py` has no inbound import from any file in the repo. It imports (`src/pneuma/casestudy/benchmark.py:24-25`) and is imported by nothing: searching for `from .benchmark`, `from pneuma.casestudy.benchmark`, `casestudy import benchmark`, `benchmark.Score`, `benchmark.evaluate`, and `benchmark.table` across `src/`, `tests/`, `docs/`, and `README.md` returns only its own definition sites and one prose mention in a sibling docstring (`src/pneuma/casestudy/ir_petri.py:3`).

Three things qualify the row rather than remove it. It is documented as a shipped file in the README's casestudy table — "Scores the mined model against the standard miners (manual script)" (`README.md:312`). It has a manual `__main__` guard marked `# pragma: no cover - manual benchmark run` (`src/pneuma/casestudy/benchmark.py:119`), so it is designed to run as a script, not to be imported. And its collaborator `ir_petri.py` was deliberately split out of it precisely so the conversion could be used without it — "Kept separate from `benchmark.py` because the conversion is useful on its own" (`src/pneuma/casestudy/ir_petri.py:3`) — and that split worked: `ir_to_petri` is used at `tests/app/test_casestudy.py:327-329` while `benchmark.py` is not.

The gap is coverage, not reachability. `tests/app/test_casestudy.py:337-346` asserts the benchmark's central claim — that a tighter threshold trades fitness for precision — by calling `miner.mine` twice directly, under a docstring that names what it is doing: "The benchmark's shape, asserted cheaply without re-running every miner" (`tests/app/test_casestudy.py:338`). So `benchmark.py`'s conclusion is tested while `benchmark.py` itself never executes in the suite. Confidence LIKELY, not CONFIRMED: a script invoked by hand leaves no in-repo reference either way.

## Dead imports

_none_

`uv run ruff check --select F401,F811,F841 --no-fix --output-format=concise .` reports `All checks passed!` over all 87 Python files. `F401` is already inside the project's configured lint selection — `select = ["E", "F", "I", "UP", "B", "SIM"]` (`pyproject.toml:53`) — so unused imports are gated in-repo rather than merely absent today. CONFIRMED.

## See also

- [Impact analysis][impact-analysis] — 16 shared source files
- [Processes][processes] — 12 shared source files
- [Module map][module-map] — 11 shared source files
- [Business logic][business-logic] — 10 shared source files
- [Contract map][contract-map] — 10 shared source files

[impact-analysis]: ../insights/impact-analysis.md
[processes]: ../behavior/processes.md
[module-map]: ../architecture/module-map.md
[business-logic]: ../insights/business-logic.md
[contract-map]: ../insights/contract-map.md
