# pneuma · Module map

The repo is one distribution, `pneuma`, built from `src/pneuma` (`pyproject.toml:2`, `pyproject.toml:38`), so every module below is a subpackage of that one import root. The layer split is declared, not inferred: `LIBRARY = {"detect", "gated", "memory", "method", "model", "process", "recall", "team"}` and `APPLICATION = {"casestudy", "demo"}` (`tests/library/test_boundary.py:46`). Modules are ordered library-before-application, matching the flowchart in `docs/architecture/system-overview.md`; within the library layer the kernel root comes first and the packages follow by import fan-in, with `src/pneuma/process/ir.py` the single most imported file in the repo at 102 inbound imports.

## pneuma

The flat root of the package is the kernel: five classes that every other module subclasses or calls. `method.py` defines the foundation — `ai_method` turns a decorated method into a typed AI function (`src/pneuma/method.py:79`), `MethodThread` keeps one ability running as a live conversation (`src/pneuma/method.py:163`), and `MethodAgent` publishes a subclass's decorated methods as tools another agent can call (`src/pneuma/method.py:326`). The other three add one capability each: `GatedProposer` checks an answer before it counts (`src/pneuma/gated.py:101`), the `Recalled` marker and `Recall` binder fill a parameter from memory on every call (`src/pneuma/recall.py:66`, `src/pneuma/recall.py:158`), and `Team` assembles members, enforces a hiring budget through `hiring_tools`, and grades the lead against an oracle (`src/pneuma/team.py:780`, `src/pneuma/team.py:362`). `model.py` is the smallest file in the module and holds only the Bedrock configuration for `global.anthropic.claude-opus-5` (`src/pneuma/model.py:9`).

- `src/pneuma/team.py` (1655 LOC)
- `src/pneuma/method.py` (428 LOC)
- `src/pneuma/gated.py` (423 LOC)
- `src/pneuma/recall.py` (409 LOC)
- `src/pneuma/model.py` (61 LOC)
- `src/pneuma/__init__.py` (0 LOC)

## pneuma.process

`process` ties an agent to a flowchart that a model checker has already verified, and it does it in 7 files and 1739 LOC — the smallest of the two multi-file library packages that other modules depend on. One Pydantic data structure, `Process`, is the single source three consumers read (`src/pneuma/process/ir.py:213`): `tla.render` translates it to TLA+ and `tla.check` shells out to `tools/tla2tools.jar` to model-check it (`src/pneuma/process/tla.py:119`, `src/pneuma/process/tla.py:269`, `src/pneuma/process/tla.py:28`), `interpreter.run` walks it step by step and refuses any transition the checked process does not permit (`src/pneuma/process/interpreter.py:176`), and `properties.machine_for` builds a Hypothesis state machine that drives the same interpreter down random paths (`src/pneuma/process/properties.py:72`). `ProcessAgent` both proposes the next step and does the work inside each state it enters (`src/pneuma/process/agent.py:83`), with `Navigator` as the minimal subclass the case study drives (`src/pneuma/process/agent_driver.py:31`). `interpreter.py` also defines the failure taxonomy — `Deadlock`, `NoProgress`, `InvariantViolated` (`src/pneuma/process/interpreter.py:102`, `src/pneuma/process/interpreter.py:106`, `src/pneuma/process/interpreter.py:133`).

- `src/pneuma/process/interpreter.py` (407 LOC)
- `src/pneuma/process/agent.py` (401 LOC)
- `src/pneuma/process/tla.py` (400 LOC)
- `src/pneuma/process/ir.py` (326 LOC)
- `src/pneuma/process/properties.py` (157 LOC)
- `src/pneuma/process/agent_driver.py` (47 LOC)
- `src/pneuma/process/__init__.py` (1 LOC)

## pneuma.detect

`detect` is the largest library package at 7 files and 4176 LOC, and it answers one question: can this rule or scoring formula actually catch anything? All of its probes return the same three-valued `Discrimination` verdict, whose `withheld` field names the specific bound a search hit so an unfinished search is never reported as a confident pass (`src/pneuma/detect/discrimination.py:45`). `objective.py` is the single biggest source file in the repo and probes a scoring function for four failure modes via `probe` (`src/pneuma/detect/objective.py:669`, `src/pneuma/detect/objective.py:365`); `vacuity.py` explores reachable states with `sweep` and then loosens the system step by step in `audit` to find where a rule first becomes breakable (`src/pneuma/detect/vacuity.py:204`, `src/pneuma/detect/vacuity.py:605`). `adversary.py` hires language models to cheat an objective on purpose through `adversarial_search` (`src/pneuma/detect/adversary.py:384`), and `gaming.py` probes for gate-fitting and duplicate mechanisms (`src/pneuma/detect/gaming.py:177`, `src/pneuma/detect/gaming.py:348`). `adapter.py` is the one place `detect` reaches into `process`, mapping a `Process` IR onto the generic `System` protocol via `system_for` (`src/pneuma/detect/adapter.py:86`), and `__init__.py` re-exports it lazily through a module-level `__getattr__` (`src/pneuma/detect/__init__.py:120`).

- `src/pneuma/detect/objective.py` (1878 LOC)
- `src/pneuma/detect/vacuity.py` (741 LOC)
- `src/pneuma/detect/adversary.py` (588 LOC)
- `src/pneuma/detect/gaming.py` (425 LOC)
- `src/pneuma/detect/adapter.py` (228 LOC)
- `src/pneuma/detect/__init__.py` (198 LOC)
- `src/pneuma/detect/discrimination.py` (118 LOC)

## pneuma.memory

`memory` supplies the vector-retrieval memory backend, three files and 1703 LOC that together implement one `MemoryBackend`. `TursoMemoryBackend` is the whole surface — a libSQL-backed store that ranks entries by meaning rather than word overlap, can learn numeric settings from scored feedback within schema-declared bounds, and raises `CeilingNotSeparable` when a learned ceiling cannot be distinguished (`src/pneuma/memory/turso_backend.py:360`, `src/pneuma/memory/turso_backend.py:350`). `embedding.py` supplies the vectors, calling `global.cohere.embed-v4:0` at 1536 dimensions through `BedrockCohereEmbedder` and caching the results in the same database file via `EmbeddingCache` (`src/pneuma/memory/embedding.py:45`, `src/pneuma/memory/embedding.py:47`, `src/pneuma/memory/embedding.py:145`, `src/pneuma/memory/embedding.py:206`). The package `__init__.py` is a pure re-export list of 16 names in `__all__` and holds no logic (`src/pneuma/memory/__init__.py:24`).

- `src/pneuma/memory/turso_backend.py` (1376 LOC)
- `src/pneuma/memory/embedding.py` (286 LOC)
- `src/pneuma/memory/__init__.py` (41 LOC)

## pneuma.casestudy

`casestudy` is the widest module in the repo — 15 files, 6011 LOC — and it is the application layer that produces every measurement the README cites. `pipeline.py` is the module's orchestrator rather than a dependency — it imports `process` and its own siblings but almost nothing imports it (`src/pneuma/casestudy/pipeline.py:27`), and its `run` drives the six-step study end to end (`src/pneuma/casestudy/pipeline.py:120`). The data path starts at `eventlog.parse_xes`, which loads XES logs into Polars tables (`src/pneuma/casestudy/eventlog.py:43`), and `transcriptlog.to_events` loads the second, agent-transcript data set for shape contrast (`src/pneuma/casestudy/transcriptlog.py:258`); `miner.mine` discovers a process model from either (`src/pneuma/casestudy/miner.py:84`) and `rules.enforce` attaches derived precedence rules to it (`src/pneuma/casestudy/rules.py:151`). Three modules make parts of the system learnable — `learning.train` trains the dithering out of the navigator (`src/pneuma/casestudy/learning.py:404`), `minelearn.LearningMiner` makes both the miner's guidance and its tools learnable (`src/pneuma/casestudy/minelearn.py:136`), and `harnesslearn.HarnessProposer` only accepts a parameter the detectors approve (`src/pneuma/casestudy/harnesslearn.py:572`) — while `handlers.Caseworker` is the `ProcessAgent` whose typed methods do the work inside each state (`src/pneuma/casestudy/handlers.py:88`).

- `src/pneuma/casestudy/minelearn.py` (1180 LOC)
- `src/pneuma/casestudy/harnesslearn.py` (1053 LOC)
- `src/pneuma/casestudy/transcriptlog.py` (506 LOC)
- `src/pneuma/casestudy/learning.py` (496 LOC)
- `src/pneuma/casestudy/aimine.py` (457 LOC)
- `src/pneuma/casestudy/rules.py` (355 LOC)
- `src/pneuma/casestudy/handlers.py` (334 LOC)
- `src/pneuma/casestudy/pipeline.py` (267 LOC)

## pneuma.demo

`demo` is the incident war-room and ships the project's only console script, `pneuma = "pneuma.demo.cli:main"` (`pyproject.toml:22`). `cli.main` runs one investigation, writes `artifacts/`, and exits non-zero when the oracle rejects the verdict (`src/pneuma/demo/cli.py:117`). `WarRoom` is the room itself, a `Team` subclass returning an `Investigation` verdict (`src/pneuma/demo/warroom.py:83`, `src/pneuma/demo/warroom.py:41`), staffed by `Staff` and `staffing_tools`, the demo's binding of the library's hiring tools (`src/pneuma/demo/staffing.py:32`, `src/pneuma/demo/staffing.py:66`). The module carries the same cast twice on purpose — `cast.py` builds it on the string-prompt `Agent` class with a message bus (`src/pneuma/demo/cast.py:69`, `src/pneuma/demo/agent.py:38`) and `typed_cast.py` rebuilds it on `MethodAgent` with typed methods and no bus (`src/pneuma/demo/typed_cast.py:54`) — while `incident.py`, the largest file, generates a synthetic incident and machine-checks that no single clue gives the `GroundTruth` away (`src/pneuma/demo/incident.py:1120`, `src/pneuma/demo/incident.py:1174`).

- `src/pneuma/demo/incident.py` (1398 LOC)
- `src/pneuma/demo/cast.py` (249 LOC)
- `src/pneuma/demo/warroom.py` (204 LOC)
- `src/pneuma/demo/agent.py` (171 LOC)
- `src/pneuma/demo/typed_cast.py` (156 LOC)
- `src/pneuma/demo/cli.py` (149 LOC)
- `src/pneuma/demo/staffing.py` (135 LOC)
- `src/pneuma/demo/live.py` (98 LOC)

## tests/library

`tests/library` is the larger half of the suite at 19 files and 12795 LOC, and its separation from `tests/app` is itself enforced architecture rather than filing convention. `test_boundary.py` is the enforcing file: it declares the two sides of the layer split, rejects any library import of an application package including imports hidden inside function bodies, and blocks `polars`, `libsql`, and `pm4py` from the library side (`tests/library/test_boundary.py:46`, `tests/library/test_boundary.py:50`, `tests/library/test_boundary.py:134`, `tests/library/test_boundary.py:141`). It goes further than static reading — `blocking_import` re-imports each library module in a fresh Python process with the data engines blocked, and a self-check asserts the blocker really blocks (`tests/library/test_boundary.py:161`, `tests/library/test_boundary.py:208`). The largest files track the largest kernel surfaces — `test_team.py` at 1839 LOC against `team.py`, `test_turso_memory.py` at 1349 LOC against the memory backend — and every test here runs offline against scripted models (`README.md:12-13`).

- `tests/library/test_team.py` (1839 LOC)
- `tests/library/test_turso_memory.py` (1349 LOC)
- `tests/library/test_process_agent.py` (1090 LOC)
- `tests/library/test_recall.py` (1003 LOC)
- `tests/library/test_process.py` (953 LOC)
- `tests/library/test_team_dynamic.py` (788 LOC)
- `tests/library/test_objective.py` (739 LOC)
- `tests/library/test_gated.py` (693 LOC)

## tests/app

`tests/app` holds 19 files and 8112 LOC covering the application layer, and it is the half that must not collect when the data engines are blocked — `test_an_application_test_module_does_not_collect_under_the_blocker` asserts exactly that, making the directory split load-bearing in both directions (`tests/library/test_boundary.py:259`). Several files exist specifically to run the detectors against the real logs rather than fixtures: `test_objective_on_the_live_score.py`, `test_vacuity_on_real_logs.py`, and `test_discrimination_on_real_logs.py` are the tests that surfaced two bugs inside the detectors themselves (`README.md:228-239`). `test_kernel_live.py` is the live-Bedrock gate, run only when `PNEUMA_LIVE_KERNEL` is set (`README.md:253`, `README.md:268`). Both halves import fixture paths from a shared `tests/paths.py`, which resolves every path once from the repo root so that moving a test file down a directory cannot silently turn a real failure into a skip (`tests/paths.py:20`, `tests/paths.py:29`).

- `tests/app/test_minelearn.py` (1207 LOC)
- `tests/app/test_harnesslearn.py` (1017 LOC)
- `tests/app/test_fixture_two.py` (664 LOC)
- `tests/app/test_objective_on_the_live_score.py` (635 LOC)
- `tests/app/test_casestudy.py` (570 LOC)
- `tests/app/test_counterfactual_replay.py` (525 LOC)
- `tests/app/test_aimine.py` (487 LOC)
- `tests/app/test_transcriptlog.py` (463 LOC)

## Supporting code

- `tests/paths.py` (32 LOC)
- `docs/build_pdf.py` (153 LOC)
- `src/pneuma/casestudy/toolkit.py` (413 LOC)
- `src/pneuma/casestudy/miner.py` (272 LOC)
- `src/pneuma/casestudy/live.py` (256 LOC)
- `src/pneuma/casestudy/eventlog.py` (253 LOC)
- `src/pneuma/casestudy/benchmark.py` (122 LOC)
- `src/pneuma/casestudy/ir_petri.py` (47 LOC)
- `src/pneuma/casestudy/__init__.py` (0 LOC)
- `src/pneuma/demo/__init__.py` (7 LOC)
- `tests/library/test_vacuity.py` (617 LOC)
- `tests/library/test_team_worklog.py` (603 LOC)
- `tests/library/test_discrimination.py` (479 LOC)
- `tests/library/test_method.py` (471 LOC)
- `tests/library/test_team_negotiation.py` (465 LOC)
- `tests/library/test_adversary.py` (357 LOC)
- `tests/library/test_boundary.py` (313 LOC)
- `tests/library/test_gaming.py` (311 LOC)
- `tests/library/test_liftability.py` (275 LOC)
- `tests/library/test_interpreter_no_progress.py` (248 LOC)
- `tests/library/test_model_cache.py` (202 LOC)
- `tests/app/test_kernel_live.py` (456 LOC)
- `tests/app/test_rules.py` (387 LOC)
- `tests/app/test_demo.py` (361 LOC)
- `tests/app/test_vacuity_on_real_logs.py` (314 LOC)
- `tests/app/test_learning_memory.py` (229 LOC)
- `tests/app/test_learning_no_progress.py` (206 LOC)
- `tests/app/test_discrimination_on_real_logs.py` (150 LOC)
- `tests/app/test_typed_cast_method.py` (139 LOC)
- `tests/app/test_portability.py` (139 LOC)
- `tests/app/test_adversary_live.py` (100 LOC)
- `tests/app/test_handlers_gate.py` (63 LOC)

## See also

- [Impact analysis][impact-analysis] — 35 shared source files
- [Processes][processes] — 31 shared source files
- [Contract map][contract-map] — 31 shared source files
- [Business logic][business-logic] — 29 shared source files
- [Debugging guide][debugging-guide] — 25 shared source files

[impact-analysis]: ../insights/impact-analysis.md
[processes]: ../behavior/processes.md
[contract-map]: ../insights/contract-map.md
[business-logic]: ../insights/business-logic.md
[debugging-guide]: ../insights/debugging-guide.md
