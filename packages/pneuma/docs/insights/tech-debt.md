# pneuma · Tech debt

This register was assembled in four passes over `src/` (19,173 LOC across 47 files, 41 of them non-`__init__` modules) and `tests/` (20,939 LOC across 39 files), using the flattened codebase at `docs/.repomix/codebase.json` for breadth and `Read` for line-numbered verification.

1. **Explicit comment markers.** Case-sensitive grep for `\bTODO\b`, `\bFIXME\b`, `\bHACK\b`, `\bXXX\b`, `REFACTOR`, `DEPRECATED`, `WORKAROUND`, `KLUDGE`, `TBD`, `OPTIMIZE` across `src`, `tests`, `tools`, `pyproject.toml`, `README.md`. **Zero hits.** A case-insensitive sweep adding `for now`, `temporary`, `placeholder`, `stub`, `not implemented`, `revisit`, `band-aid`, `shim` returned only domain vocabulary — `Revisit` is a first-class dataclass (`src/pneuma/process/interpreter.py:30`) and `placeholders` is a SQL bind-parameter variable (`src/pneuma/memory/embedding.py:240`). This repo does not use TODO comments. The Explicit markers section below therefore reports the repo's *actual* known-exception annotations — `# noqa` suppressions — which is the closest thing it has to a marker convention.

2. **Deprecation and version pins.** Every module-level alias assignment in `src/`, plus `pyproject.toml` and `uv.lock`.

3. **Mechanical smell detection.** AST-based rather than grep-based, because grep overstates coupling: an AST pass hashed every normalized function body in `src/` to find duplicates, enumerated every class's method count and span, resolved every test-side import (including function-body imports **and** package `__init__.py` re-exports) to measure test reach per module, and counted repo-wide references to every module-level `UPPER_CASE` constant.

4. **Reviewer judgment.** Smells that survived verification, ranked by cost-to-fix against consequence-of-leaving. Items marked *judgment-call* are borderline and say so.

Two conventions to read the register with. **Category** is a closed vocabulary: `marker`, `wrong abstraction`, `error handling`, `dead code adjacent`, `deprecated pattern`, `version pin`, `duplicated logic`, `missing tests`. **Cost to fix** is `S` (under an hour), `M` (a focused session), `L` (a design decision plus migration).

Three findings from the negative results, because they change how the register should be read. The repo is unusually clean by the mechanical measures: `.venv/bin/ruff check src tests` reports `All checks passed!` on the committed config, there are no commented-out code blocks over five lines, no `legacy*`/`*Manager`/`*Wrapper`/`*Helper`/`*V1` naming anywhere in `src/`, and exactly one exact-duplicate function body in 19k lines. The debt that remains is concentrated in two places instead: **suppressions and annotations that no configured tool enforces**, and **two classes that grew past the point where their own docstrings describe them**.

## Ranked register

| Rank | Debt item | Category | Cost to fix | Citation |
| --- | --- | --- | --- | --- |
| 1 | 62 `# type: ignore[code]` suppressions maintained against no type checker — no `[tool.mypy]`/`[tool.pyright]`/`[tool.ty]` in the manifest, no config file in the repo root, no checker binary in `.venv/bin`, and the dev group lists only hypothesis, pm4py, pytest, pytest-asyncio, ruff | `deprecated pattern` | `M` | `pyproject.toml:24-31`, `src/pneuma/detect/adapter.py:72`, `src/pneuma/method.py:132`, `src/pneuma/casestudy/eventlog.py:133`, `src/pneuma/process/tla.py:180` |
| 2 | 40 of 44 `# noqa` directives are inert — 39 name rules that `select` never enables (BLE/SLF/ANN/S/PT prefixes) and one is genuinely unused, so the justifications they carry protect nothing; `ruff check --extend-select RUF100` reports all 40 while the committed config reports `All checks passed!` | `marker` | `S` | `pyproject.toml:53`, `src/pneuma/gated.py:186`, `src/pneuma/detect/objective.py:181`, `src/pneuma/team/hooks/hiring.py:244`, `src/pneuma/casestudy/minelearn.py:872` |
| 3 | RESOLVED 2026-08-10: the flat `Team` (876 LOC, 34 methods, three mixed responsibilities) was rebuilt hooks-first — a 463-LOC core plus one hook per capability under `src/pneuma/team/hooks/` | `wrong abstraction` | `L` | `src/pneuma/team/core.py:1`, `src/pneuma/team/hooks/__init__.py:1` |
| 4 | `TursoMemoryBackend` is 860 LOC across 39 methods with 22 inline SQL statements, mixing schema DDL, CRUD, embedding, retrieval-quality probes, optimizer callbacks, and LLM tool construction | `wrong abstraction` | `L` | `src/pneuma/memory/turso_backend.py:360`, `src/pneuma/memory/turso_backend.py:718`, `src/pneuma/memory/turso_backend.py:1028`, `src/pneuma/memory/turso_backend.py:1134` |
| 5 | `FLEET_GLOB` hardcodes `/home/lalsaado/...` and is the **default argument** of the public `load()`, so the library's default data source resolves on one developer's machine only — while `tests/paths.py` already demonstrates the root-relative pattern | `wrong abstraction` | `S` | `src/pneuma/casestudy/transcriptlog.py:60`, `src/pneuma/casestudy/transcriptlog.py:475`, `tests/paths.py:20` |
| 6 | `casestudy/benchmark.py` has zero test reach: `evaluate()` computes the published fitness/precision/F-score comparison table and nothing executes it; the nearest test states in its own docstring that it asserts the shape "without re-running every miner" | `missing tests` | `M` | `src/pneuma/casestudy/benchmark.py:45`, `src/pneuma/casestudy/benchmark.py:119`, `tests/app/test_casestudy.py:337-346`, `docs/case-study.md:53-58` |
| 7 | ~40 lines of objective-construction copy-pasted between two modules — identical `surviving` closure, identical `objective` head through the `terms[step]` assignment, identical `term(index)` factory and `Component` names; only three tokens differ | `duplicated logic` | `M` | `src/pneuma/casestudy/harnesslearn.py:209-253`, `src/pneuma/casestudy/minelearn.py:641-695` |
| 8 | RESOLVED 2026-08-10: the README's component table published stale LOC counts (`team.py` listed at 968 lines against an actual 1655); the README rewrite re-measured every count, and the hooks rebuild re-measured them again (`README.md:109-114`) | `dead code adjacent` | `S` | `README.md:109-114`, `src/pneuma/team/core.py`, `src/pneuma/gated.py`, `src/pneuma/process/agent.py` |
| 9 | Two comments direct readers to `rules.liveness_of`, a symbol that does not exist anywhere in `src/` — one claims two tests "moved to `test_vacuity.py` with" it | `dead code adjacent` | `S` | `tests/app/test_rules.py:324-328`, `tests/library/test_vacuity.py:264` |
| 10 | `probe()` is 175 LOC with 13 parameters orchestrating nine `_check_*` siblings that return five different tuple shapes, so its body is mostly bespoke unpack-and-extend glue | `wrong abstraction` | `L` | `src/pneuma/detect/objective.py:669`, `src/pneuma/detect/objective.py:794-843`, `src/pneuma/detect/objective.py:952`, `src/pneuma/detect/objective.py:1497` |
| 11 | `FULL_GLOB` is defined with a justifying comment ("the scale test") and never referenced anywhere | `dead code adjacent` | `S` | `src/pneuma/casestudy/transcriptlog.py:57-63` |
| 12 | `RECOVERY_TS` is dead while its adjacent sibling `ONSET_TS` has three real uses — the asymmetry marks it as leftover rather than reserved | `dead code adjacent` | `S` | `src/pneuma/demo/incident.py:62`, `src/pneuma/demo/incident.py:1134`, `src/pneuma/demo/incident.py:1377-1379` |
| 13 | `SEED_PLAYBOOK` is dead while its adjacent sibling `SEED_ENTRIES` is wired into a `default_factory` | `dead code adjacent` | `S` | `src/pneuma/casestudy/learning.py:65`, `src/pneuma/casestudy/learning.py:104` |
| 14 | `Liveness = RuleVerdict` back-compat alias kept "so a caller that annotates against `rules.Liveness` still type-checks" — no such external caller exists, and the alias is documented from both ends so removal touches two files | `deprecated pattern` | `S` | `src/pneuma/casestudy/rules.py:130-135`, `src/pneuma/detect/vacuity.py:332`, `tests/app/test_vacuity_on_real_logs.py:235` |
| 15 | `strands-ai-functions`, the project's foundational dependency, pinned to a bare git commit SHA with no version range; the recorded reason is a lagging PyPI release, a condition that expires silently | `version pin` | `M` | `pyproject.toml:16-19` |
| 16 | `_score` collapses "raised" and "non-finite" into one `None`, and its caller counts both in a single `unscorable` field — while the sibling probe in the same module keeps an explicit `errors` count | `error handling` | `S` | `src/pneuma/detect/gaming.py:169-175`, `src/pneuma/detect/gaming.py:215-223`, `src/pneuma/detect/gaming.py:312` |
| 17 | `demo/cli.py` and `demo/warroom.py` have zero test reach, and `cli:main` is the shipped console-script entry point | `missing tests` | `M` | `pyproject.toml:22`, `src/pneuma/demo/cli.py:18`, `src/pneuma/demo/warroom.py:3` |

## Explicit markers

**This repo contains no TODO, FIXME, HACK, XXX, REFACTOR, WORKAROUND, KLUDGE, TBD, or OPTIMIZE comments.** A case-sensitive word-boundary grep across `src/`, `tests/`, `tools/`, `pyproject.toml`, `README.md`, and `.gitignore` returns no output. `tools/` holds a single binary, `tools/tla2tools.jar`, so nothing was missed there.

The repo's real known-exception annotation is the `# noqa` directive, and unlike a bare marker each one carries a written justification. Every such comment in `src/`, verbatim:

- `# noqa: S608 — placeholders only` — `src/pneuma/memory/embedding.py:243`
- `# noqa: BLE001 — an extractor bug must not read as a verdict either` — `src/pneuma/gated.py:186`
- `# noqa: BLE001 — see the docstring: a bug must not read as a verdict` — `src/pneuma/gated.py:191`
- `# noqa: BLE001 — a bug must not read as a verdict` — `src/pneuma/gated.py:225`
- `# noqa: BLE001 — a broken verdict is a fault, not a verdict` — `src/pneuma/gated.py:234`
- `# noqa: BLE001 — a broken verdict is a fault, not a verdict` — `src/pneuma/gated.py:252`
- `# noqa: BLE001 — an extractor bug must not read as a verdict` — `src/pneuma/gated.py:413`
- `# noqa: BLE001 - re-raised with the state that caused it` — `src/pneuma/detect/vacuity.py:256`
- `# noqa: BLE001 - a rule that cannot be evaluated is a bug` — `src/pneuma/detect/vacuity.py:273`
- `# noqa: BLE001 — the model can retry or re-scope` — `src/pneuma/team/hooks/hiring.py:244`
- `# noqa: BLE001 — one dead teammate must not stop the rest` — `src/pneuma/team/hooks/worklog.py:139`
- `# noqa: BLE001 — any setup failure is the finding` — `src/pneuma/casestudy/minelearn.py:872`
- `# noqa: SLF001` — `src/pneuma/casestudy/minelearn.py:875`
- `# noqa: ANN204 - tuple compatibility for \`a, b = ...\`` — `src/pneuma/casestudy/rules.py:275`
- `# noqa: BLE001 — one adversary dying must not end the search` — `src/pneuma/detect/adversary.py:537`
- `# noqa: BLE001` — `src/pneuma/detect/adversary.py:570`
- `# noqa: S603 - fixed argv, no shell` — `src/pneuma/process/tla.py:309`
- `# noqa: BLE001 — a structure that cannot measure a point is not a finding` — `src/pneuma/detect/objective.py:181`
- `# noqa: BLE001` — `src/pneuma/detect/objective.py:188`
- `# noqa: BLE001` — `src/pneuma/detect/objective.py:241`
- `# noqa: BLE001 — a raising objective is a finding, not a crash` — `src/pneuma/detect/objective.py:488`

Two comments in `tests/` point at a removed symbol and are the clearest cleanup targets in the repo, quoted verbatim:

- `# Two tests that used to sit here — effect accumulation, and a self-contradictory` / `# conjunction — moved to \`test_vacuity.py\` with \`rules.liveness_of\`. They were tests of` — `tests/app/test_rules.py:324-325`
- `here rather than beside \`rules.liveness_of\`, because it is a test of the walk.` — `tests/library/test_vacuity.py:264`

## Pattern-level smells

### Suppression comments outrunning the tools that would honor them

The repo annotates exceptions carefully and then never wires up the checker that would read them. `pyproject.toml:53` selects `["E", "F", "I", "UP", "B", "SIM"]`, so every `BLE001`, `SLF001`, `ANN20x`, `S6xx`, and `PT011` directive names a rule that is not enabled — 39 of the 44 `# noqa` comments in the repo. `ruff check --extend-select RUF100` reports 40 findings, 39 `non-enabled` plus one genuinely unused (`tests/library/test_vacuity.py:589`), while `ruff check src tests` on the committed config reports `All checks passed!`. Only four directives in the repo did real work at audit time, and all four sat in `tests/` (`tests/library/test_liftability.py:259`, `tests/library/test_recall.py:341`, plus two in the pre-rebuild team suites that the 2026-08-10 hooks rebuild rewrote) — every single one of the 21 in `src/` is inert. The same pattern holds for typing at larger scale: 62 `# type: ignore[...]` comments name specific error codes (`list-item`, `attr-defined`, `union-attr`, `method-assign`, `arg-type`), yet there is no `[tool.mypy]`, `[tool.pyright]`, or `[tool.ty]` section in the manifest, no `mypy.ini` or `pyrightconfig.json` in the repo root, and no checker binary in `.venv/bin`. The specificity of the codes is the tell — they were written under a checker that has since left the toolchain, so each is now an unverified claim that reads like a verified one. The cost is inverted from ordinary debt: the annotations are load-bearing documentation, and the fix is to enable the tools rather than delete the comments.

Shows up in:

- `pyproject.toml:53` — the `select` list that leaves BLE/SLF/ANN/S unenabled
- `pyproject.toml:24-31` — dev group with no type checker
- `src/pneuma/gated.py:186` — a carefully justified `BLE001` suppressing nothing
- `src/pneuma/detect/adapter.py:72` — `# type: ignore[list-item]` with no checker to satisfy
- `src/pneuma/process/tla.py:180` — `# type: ignore[arg-type]`, same

Cost: `M` — mechanical to resolve (enable `RUF100` plus one of BLE/SLF/ANN, add a type checker to the dev group and a gate), but the first type-checker run over 19k lines with 62 pre-existing ignores will surface real work.

### Two objects that grew past their own docstrings (one since resolved)

`Team` was the first: the flat `team.py` reached 876 LOC and 34 methods, mixing prose rendering, run lifecycle, and its own validation suite. RESOLVED 2026-08-10 by the hooks rebuild — the core is now 463 LOC owning exactly the pipeline (`src/pneuma/team/core.py:176`), and each former responsibility is one hook module under `src/pneuma/team/hooks/`. `TursoMemoryBackend` (`src/pneuma/memory/turso_backend.py:360`) remains: 860 LOC and 39 methods with 22 SQL statements inline, spanning schema DDL, entry CRUD, embedding, retrieval-quality measurement (`probe_retrieval` at `:718`, `calibrate_ceiling` at `:784`), optimizer callbacks (`_save`/`_recall`/`_consolidate` at `:963-1091`), and LLM tool construction (`tool_provider` at `:1134`). The register ranks it below the suppression smell deliberately: it is internally coherent and well documented, so the consequence of leaving it is slow (every new feature lands in an already-crowded class) rather than sharp. The measurable symptom was that the documentation lost track — an earlier README described `team.py` at 968 lines against an actual 1655, corrected in the 2026-08-10 rewrite (`README.md:114`).

Shows up in:

- `src/pneuma/memory/turso_backend.py:360` — `TursoMemoryBackend`, 860 LOC / 39 methods
- `src/pneuma/memory/turso_backend.py:867` — `_numeric_update`, 66 LOC
- `README.md:114` — the LOC claim the growth outran, now re-measured

Cost: `L` — splitting it means moving a public surface.

### Constants and comments that outlived their referents

Small, cheap, and unambiguous because each has a live sibling to compare against. Three module-level constants are referenced exactly once, at their own definition: `FULL_GLOB` (`src/pneuma/casestudy/transcriptlog.py:63`) beside the live `FLEET_GLOB`; `RECOVERY_TS` (`src/pneuma/demo/incident.py:63`) beside `ONSET_TS`, which has three real uses; `SEED_PLAYBOOK` (`src/pneuma/casestudy/learning.py:65`) beside `SEED_ENTRIES`, which feeds a `default_factory`. The sibling asymmetry is what rules out "reserved for future use." The comment half of the pattern is worse because it actively misdirects: `tests/app/test_rules.py:324-328` tells a reader two tests moved "to `test_vacuity.py` with `rules.liveness_of`", and `tests/library/test_vacuity.py:264` repeats the reference — but `rules.liveness_of` exists nowhere in `src/`. A reader following either comment searches for a symbol that was removed. This sits inside a broader house style of narrating prior states (25 sites, 8 of them in `tests/app/test_fixture_two.py` alone); the style is deliberate and mostly valuable, which is exactly why the decayed instances are hard to spot.

Shows up in:

- `src/pneuma/casestudy/transcriptlog.py:57-63` — `FULL_GLOB`, justified and unused
- `src/pneuma/demo/incident.py:62` — `RECOVERY_TS`, dead sibling of a live constant
- `src/pneuma/casestudy/learning.py:65` — `SEED_PLAYBOOK`, same shape
- `tests/app/test_rules.py:324-328` — pointer to the non-existent `rules.liveness_of`
- `tests/library/test_vacuity.py:264` — the same phantom reference, restated

Cost: `S` — five deletions and two comment rewrites, no behavior change.

### Untested code that produces published numbers

Resolving every test-side import through package re-exports leaves three `src/` modules with zero test reach: `casestudy/benchmark.py`, `demo/cli.py`, and `demo/warroom.py`. Two initially looked untested and were cleared — `detect/adapter.py` is reached via `from pneuma.detect import audit_process` (`tests/library/test_discrimination.py:162`) and `memory/turso_backend.py` via `memory/__init__.py`, both through re-export rather than direct import. The remaining three matter unevenly. `demo/cli.py` and `demo/warroom.py` are demo surface, though `cli:main` is the shipped console script (`pyproject.toml:22`). `benchmark.py` is the sharp one: `evaluate()` (`src/pneuma/casestudy/benchmark.py:45`) computes the fitness/precision/F-score comparison against four pm4py miners that `docs/case-study.md:53-58` publishes as a table, it is reachable only through its own `__main__` block (`:119`), and the nearest test says in its own docstring that it asserts "the benchmark's shape, asserted cheaply without re-running every miner" (`tests/app/test_casestudy.py:337-346`) — it exercises `miner.mine` directly and never enters `benchmark.py`. The comparison this project uses to substantiate its central claim is the one path with no coverage.

Shows up in:

- `src/pneuma/casestudy/benchmark.py:45` — `evaluate()`, unexercised
- `tests/app/test_casestudy.py:337-346` — the near-miss test, explicit about what it skips
- `docs/case-study.md:53-58` — the published table those numbers feed
- `src/pneuma/demo/cli.py:18` — shipped entry point, no test
- `src/pneuma/demo/warroom.py:3` — cited in test prose but never imported

Cost: `M` — a `pytest.importorskip("pm4py")` test over a small committed log would cover `evaluate()`; the demo modules need a smoke test each.

### Copy-paste at the seam between two learning loops

*judgment-call* — worth flagging because the duplicated region is domain arithmetic that defines a score, not framework boilerplate, so the two copies can drift into computing different things under the same name. An AST hash of every function body in `src/` found exactly one exact duplicate: the nested `read` closure at `src/pneuma/casestudy/harnesslearn.py:242` and `src/pneuma/casestudy/minelearn.py:684`. Diffing outward shows the shared region is ~40 lines: an identical `surviving` closure, an identical `objective` head through the `Discovered` construction and the `terms[step] = (graded.coverage, 1.0 - min(max(audit.edge_share, 0.0), 1.0))` assignment, and an identical `term(index)` factory emitting the same two `Component` names. Only three tokens differ — a qualified versus bare `start_and_end_activities`, `method="harness probe"` versus `method="probe"`, and the scoring tail (`weighted_score(...)` versus `Attempt(...).score`). The duplicated `raise ValueError(f"no model compiles at threshold {step}")` guard appears in both with its explanatory comment reworded rather than shared, which is the signature of hand-copied code.

Shows up in:

- `src/pneuma/casestudy/harnesslearn.py:209-253` — the source region
- `src/pneuma/casestudy/minelearn.py:641-695` — the copy
- `src/pneuma/casestudy/harnesslearn.py:242` and `src/pneuma/casestudy/minelearn.py:684` — the byte-identical `read` closures
- `src/pneuma/casestudy/harnesslearn.py:246-250` and `src/pneuma/casestudy/minelearn.py:688-692` — the same guard, comment reworded

Cost: `M` — the shared part factors into one helper taking the scoring tail as a callback; both call sites are covered by tests, so the change is verifiable.

## See also

- [Impact analysis][impact-analysis] — 30 shared source files
- [Module map][module-map] — 24 shared source files
- [Processes][processes] — 21 shared source files
- [Business logic][business-logic] — 21 shared source files
- [Debugging guide][debugging-guide] — 19 shared source files

[impact-analysis]: impact-analysis.md
[module-map]: ../architecture/module-map.md
[processes]: ../behavior/processes.md
[business-logic]: business-logic.md
[debugging-guide]: debugging-guide.md
