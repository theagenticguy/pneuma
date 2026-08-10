# pneuma · Risk hotspots

"Risk" here is a composed score, not a judgment about code quality. Two signals feed it. The first is **finding severity** from `ruff` 0.16.0, the only static analyzer the repo installs (`pyproject.toml:29`), run with an escalated selection because the repo's own configured gate is clean and therefore carries no discriminating signal — `select = ["E", "F", "I", "UP", "B", "SIM"]` at `pyproject.toml:47-48` yields zero findings across the tree. Escalating to `--select ALL --ignore D,CPY,ANN,COM,TD,FIX,ERA` yields 3481 findings, of which 2148 are `S101` (assert-in-test) and are discarded outright, since pytest asserts are the intended idiom and counting them would rank files by test length. The remainder splits into an `error` tier (correctness, security, complexity, exception naming: families `F`, `E`, `S`, `B`, `BLE`, `PLE`, `PLW` plus `C901`, `PLR0912`, `PLR0915`, `PLR0913`, `PLR0917`, `N818`, `TRY004`, `RUF007`, `RUF046`, `EXE001`, `PYI055`) and a `warn` tier (style, convention, typing-import placement). The second signal is **30-day activity trend**, computed per current path with `git log --follow --since=30.days.ago`, which matters here because 27 renames landed inside the window — the test suite was split into `tests/library/` and `tests/app/`, and seven modules moved into `src/pneuma/demo/` — so counts taken without `--follow` split each moved file's history across two names. The score is `2 × error_count + 0.5 × warn_count + 1 if rising`.

Three limitations bound what this file can tell you. **The trend column cannot report a falling slope.** The repo's entire history is 66 commits between 2026-07-29 and 2026-08-10, so the 30-day window covers the project's whole life and every one of the 86 tracked Python files has at least one commit in it; with median 2.0 and σ 1.609, the packet's `↓ falling` threshold of "fewer than 0.39 commits" is unreachable by construction. `↑ rising` means 4 or more commits, `→ flat` means 1 to 3. **Ownership carries no information.** `git log --format='%an'` returns 65 commits from Laith Al-Saadoon and 1 from `bgagent`; every file in the table below resolves to a 100% single owner, so the column is retained for schema compliance but should not be read as a bus-factor signal. **A high score is not a defect count.** The dominant contributors are magic-number literals in tests (`PLR2004`, 244 repo-wide), function-local imports (`PLC0415`, 162), and long exception messages (`TRY003`, 138) — deliberate idioms in this codebase, not bugs. What the ranking does surface reliably is *concentration of complexity and churn*, which is where a careless edit is most likely to go unnoticed. Zero `TODO`, `FIXME`, `HACK`, or `XXX` markers exist anywhere under `src/`, `tests/`, or `tools/`, so the marker-density fallback signal is both unnecessary and unavailable.

| File | Trend | Open findings | Top owner | Citation |
| --- | --- | --- | --- | --- |
| `tests/library/test_team.py` | → flat | 82 warn, 8 error | Laith Al-Saadoon (100%) | `tests/library/test_team.py:1` (723 LOC after the hooks rebuild; findings pre-date it) |
| `tests/library/test_turso_memory.py` | → flat | 62 warn, 1 error | Laith Al-Saadoon (100%) | `tests/library/test_turso_memory.py:1` (1349 LOC) |
| `src/pneuma/casestudy/harnesslearn.py` | ↑ rising | 31 warn, 6 error | Laith Al-Saadoon (100%) | `src/pneuma/casestudy/harnesslearn.py:1` (1053 LOC) |
| `src/pneuma/detect/objective.py` | ↑ rising | 16 warn, 9 error | Laith Al-Saadoon (100%) | `src/pneuma/detect/objective.py:1` (1878 LOC) |
| `src/pneuma/team/` (was the flat `team.py`) | ↑ rising | 33 warn, 4 error (pre-rebuild) | Laith Al-Saadoon (100%) | `src/pneuma/team/core.py:1` (2242 LOC across the package) |
| `tests/app/test_casestudy.py` | ↑ rising | 48 warn, 0 error | Laith Al-Saadoon (100%) | `tests/app/test_casestudy.py:1` (570 LOC) |
| `tests/library/test_objective.py` | ↑ rising | 20 warn, 7 error | Laith Al-Saadoon (100%) | `tests/library/test_objective.py:1` (739 LOC) |
| `src/pneuma/memory/turso_backend.py` | → flat | 28 warn, 5 error | Laith Al-Saadoon (100%) | `src/pneuma/memory/turso_backend.py:1` (1376 LOC) |
| `src/pneuma/casestudy/minelearn.py` | ↑ rising | 26 warn, 4 error | Laith Al-Saadoon (100%) | `src/pneuma/casestudy/minelearn.py:1` (1180 LOC) |
| `src/pneuma/casestudy/transcriptlog.py` | → flat | 24 warn, 5 error | Laith Al-Saadoon (100%) | `src/pneuma/casestudy/transcriptlog.py:1` (506 LOC) |
| `src/pneuma/process/ir.py` | → flat | 35 warn, 2 error | Laith Al-Saadoon (100%) | `src/pneuma/process/ir.py:1` (326 LOC) |
| `tests/app/test_harnesslearn.py` | ↑ rising | 35 warn, 1 error | Laith Al-Saadoon (100%) | `tests/app/test_harnesslearn.py:1` (1017 LOC) |

## Per-file drill-down

### 1. `tests/library/test_team.py` — score 57.0

This entry is a pre-rebuild snapshot: the 2026-08-10 hooks rebuild split this suite by capability (`test_team_core.py` plus one file per hook) and `test_team.py` is now 723 lines covering `Briefing` + `Hiring`; the line citations below refer to the pre-rebuild file. It was the offline test suite for the flat `Team` orchestrator, and its own docstring stated the design: four kinds of claim checked four different ways on purpose, asserting phase order from an interleaving record the members and lead write as they run rather than from the returned `TeamRun`, "which a badly ordered run produces just as happily" (`tests/library/test_team.py:1-6`). It carries 55 test functions and eleven purpose-built fakes — `Spy`, `FailingSpy`, `SlowSpawnSpy`, `UnspawnableSpy`, `FlakyRetireSpy` (`tests/library/test_team.py:123-203`), plus a `Counting` model that composes rather than subclasses because `ScriptedModel` is `@final` (`tests/library/test_team.py:214`).

**Recent activity:** 1 commit in the 30-day window, `→ flat`. It arrived whole in `487ea99` on 2026-08-07, "Team: the deterministic orchestrator, lifted from the war room" — a single 1839-line addition that has not been touched since.

**Owners:** Laith Al-Saadoon, 100% (1 of 1 commit).

**Findings:** 82 warn, 8 error. The error tier is 7 × `PLW0108` (unnecessary lambda, at `tests/library/test_team.py:960`, `:1247`, `:1273`, `:1292`, `:1700`, `:1729`, `:1837`) and 1 × `PLR0913` (6 arguments, `tests/library/test_team.py:289`). The warn tier is dominated by `PLR2004` magic values (25), `EM101` string-literal exception messages (10), `PT018` composite assertions (10), and `SLF001` private-member access (8) — the last being expected in a suite that verifies internal ordering. The score is driven by sheer size, not fragility: at 1839 lines this is the largest single file in the ranking and the highest-scoring by a factor of 1.7 over rank 2.

### 2. `tests/library/test_turso_memory.py` — score 33.0

The test suite for the Turso memory backend, covering storage, retrieval discrimination, and narrow gradients. Its docstring draws an explicit line between what is proved offline against a deterministic bag-of-words embedder and what needs a live Bedrock run, arguing that "the properties that would fail *silently* are exactly the ones a fake can prove, because they are properties of the plumbing rather than of Cohere's semantics" (`tests/library/test_turso_memory.py:1-8`). It names its own crux — that `search`'s `meta["results"]` survives the hop through `ParameterRecalledEvent` and `build_graph` onto `consolidate` (`tests/library/test_turso_memory.py:17-19`).

**Recent activity:** 2 commits, `→ flat`. `baae814` on 2026-07-30 ("Learn per-entry guidance and reusable code, not one prose blob") and `ca5cf34` on 2026-08-01 ("Separate the library tests from the application tests") — the second is the rename that moved it under `tests/library/`, so only one commit changed content.

**Owners:** Laith Al-Saadoon, 100% (2 of 2 commits).

**Findings:** 62 warn, 1 error. The single error is `RUF007` at `tests/library/test_turso_memory.py:998` (prefer `itertools.pairwise` over `zip` for successive pairs). The warn tier is 18 × `PLR2004`, 16 × `PLC0415` (function-local imports, e.g. `tests/library/test_turso_memory.py:340`, `:628`, `:662`), 12 × `SLF001` (private access, e.g. `_consolidate` at `tests/library/test_turso_memory.py:750`), 8 × `PT018`, and 3 × `T201` print statements inside the live tests. Four tests gate on a `_live` skipif marker (`tests/library/test_turso_memory.py:1174`, applied at `:1221`, `:1232`, `:1273`), meaning a meaningful share of this file never runs without Bedrock credentials — the real risk here is untested-by-default coverage, which the score does not capture.

### 3. `src/pneuma/casestudy/harnesslearn.py` — score 28.5

This module lets an agent propose a *harness parameter* while the detectors gate admission, on the premise that "harness code, not the model's judgment, is where the defects live, and it is the one place a bad rewrite is invisible: a broken objective does not error, it reports a confident number that gets monotonically worse while looking exactly like training" (`src/pneuma/casestudy/harnesslearn.py:1-5`). Exactly one parameter is delegated (`coverage_weight`); the threshold search window, `sweep_resolution`, and the vacuity sweep budget are held constant and the exclusion is structurally enforced — `HarnessKnobs` declares one field so `save("threshold_window", ...)` raises `KeyError` rather than being merely forbidden (`src/pneuma/casestudy/harnesslearn.py:28-31`, `:87`).

**Recent activity:** 5 commits, `↑ rising`. Spanning 2026-07-31 to 2026-08-09: `c033faa` ("Let the agent propose a harness parameter, and make the detectors admit it"), `77844d9`, `2d772d2`, `28a51b1` ("GatedProposer: the gate-as-post-condition skeleton, lifted to the library"), and `66f8044` on 2026-08-09 ("Record rehearsal path; prove suffix replay is vacuous here"). Active development through the most recent week.

**Owners:** Laith Al-Saadoon, 100% (5 of 5 commits).

**Findings:** 31 warn, 6 error. Error tier: 3 × `RUF046` redundant `int()` casts (`src/pneuma/casestudy/harnesslearn.py:210`, `:213`, `:243`) and 3 × `PLR0913` wide signatures — 9 arguments at `src/pneuma/casestudy/harnesslearn.py:470` (`admit`), 6 at `:908` (`learn`), 8 at `:940` (`train`). Warn tier: 13 × `TID252` relative parent imports (`src/pneuma/casestudy/harnesslearn.py:56-57`), 7 × `PLC0415`, 4 × `ISC004`. The combination of rising churn and wide argument lists on the three entry points (`admit`, `learn`, `train`) is the actionable signal — those signatures are where a positional-argument mistake would land silently.

### 4. `src/pneuma/detect/objective.py` — score 27.0

The largest module in the repo at 1878 lines, this probes a scoring function for degenerate optima before a training loop trusts it, detecting five failure modes: `degenerate-optimum`, `emptying-is-free`, feedback mismatch, `raises-inside-the-domain`, and `window-too-narrow` (`src/pneuma/detect/objective.py:1-8`). Its docstring flags four things that break if edited carelessly, including that `Space` must never be defaulted, because metric axes vary freely and any sane F-score is maximised at the ideal corner, making a boundary-max check fire on sound and broken objectives alike (`src/pneuma/detect/objective.py:13-17`).

**Recent activity:** 4 commits, `↑ rising`. All four fall on 2026-07-30 and 2026-07-31: `05208a2` ("Make the silent-harness-defect class mechanically detectable"), `057a6a7` ("Express both detectors as one question: does this check discriminate?"), `77844d9`, and `3ea44ff`. Note that the rising slope here reflects a burst two weeks back rather than current work — the packet's 30-day window cannot distinguish the two.

**Owners:** Laith Al-Saadoon, 100% (4 of 4 commits).

**Findings:** 16 warn, 9 error — the highest error count in the top 5. Three `C901` complexity violations: `_find_spike` at 11 (`src/pneuma/detect/objective.py:1019`), `_enumerate_degenerate` at 11 (`:1175`), `_check_emptying` at 12 (`:1318`). Five `PLR0913`, the worst being 13 arguments on `probe` itself (`src/pneuma/detect/objective.py:669`), plus `:531`, `:1497`, `:1732`. One `N818` — `ObjectiveRefused` lacks an `Error` suffix (`src/pneuma/detect/objective.py:75`) — and one `RUF046` at `:867`. This is the file where the score is most defensible as real risk: the three over-threshold functions are all internal check routines whose wrong answer would be a false negative on a broken objective, which is precisely the silent failure the module exists to catch.

### 5. `src/pneuma/team/` — score 25.5

The hooks-first team layer: a small core that spawns members, runs a lead with each member as a typed tool, and retires everybody, plus a hook library carrying every other capability (`src/pneuma/team/core.py:1-25`). Its central claim is unchanged from the flat module it replaced: the control flow is plain `asyncio` with no model anywhere in it, so members join the lead as typed tools rather than chat peers — the runtime's `send_message` only targets `STR_PROMPT` threads, so the layer mentions it nowhere.

**Recent activity:** the heaviest churn in the repo. `487ea99` on 2026-08-07 lifted the flat `team.py` out of the war room; negotiation, the worklog, and dynamic hiring landed as flags on it over 2026-08-09/10; then the hooks rebuild (`af291d8`, `c3e9e99`, `6b951d9` on 2026-08-10) split it into `core.py` + `hooks/`, deleted the flat module, and added review (`Critic`/`Council`) and learning (`Learning` + `train`). A whole architecture replaced within four days of the module existing.

**Owners:** Laith Al-Saadoon, 100%.

**Findings (pre-rebuild snapshot, kept for the trend):** 33 warn, 4 error, all four errors on the old `_hiring` function (complexity 23 against a threshold of 10, the worst in the repo). The hiring seam moved to `src/pneuma/team/hooks/hiring.py` in the rebuild; re-run the lint sweep to re-score the new layout before citing these numbers.

## See also

- [Impact analysis][impact-analysis] — 13 shared source files
- [Tech debt][tech-debt] — 9 shared source files
- [Module map][module-map] — 8 shared source files
- [Processes][processes] — 8 shared source files
- [Debugging guide][debugging-guide] — 8 shared source files

[impact-analysis]: ../insights/impact-analysis.md
[tech-debt]: ../insights/tech-debt.md
[module-map]: ../architecture/module-map.md
[processes]: ../behavior/processes.md
[debugging-guide]: ../insights/debugging-guide.md
