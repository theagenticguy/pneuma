# pneuma · Impact analysis

If you touch one of the eight surfaces below, this file tells you what else you have to think about.

**What counts as a "high-impact surface" here.** The ranking is *distinct inbound source files* — the number of separate `.py` files under `src/`, `tests/`, and `tools/` that import the surface. The count comes from an `ast` walk over every file in the repo that resolves relative imports (`from ..process.ir import Process`) to their absolute module, not from a grep over names. The AST pass matters because several consumers use the parenthesised multi-line form (`src/pneuma/detect/__init__.py:47`) that a line-oriented grep truncates, and one large consumer imports twenty names in a single statement.

The rule to interpret the tables: a surface earns its place by *breadth of dependency*, not by size or by how much logic it contains. `src/pneuma/process/ir.py` is 326 lines and 28 files import it; `src/pneuma/detect/objective.py` is 1878 lines and 13 files import it. Breadth is what predicts blast radius.

`Touch on change` is judged per consumer, not per surface:

- `yes` — a signature or shape change to the surface forces an edit in this consumer.
- `likely` — the consumer needs review even when nothing in the signature moved, usually because it depends on a behavioural promise the type does not express.
- `no` — only a behavioural change reaches it; a mechanical rename would be caught by the type checker or the import.

## `Process` — the typed IR

Defined at: `src/pneuma/process/ir.py:213`

The Pydantic model every other layer reads. Three consumers walk the same validated object: `tla.py` renders it to TLA+, `interpreter.py` executes it, `properties.py` renders it to a Hypothesis state machine (`src/pneuma/process/ir.py:5-10`).

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `process/tla.py` — imports `Process` and the private `_render_tla` | direct import | yes | `src/pneuma/process/tla.py:26` |
| `process/interpreter.py` — reads `state_map`, `outgoing`, `initial_assignments` | direct import | yes | `src/pneuma/process/interpreter.py:24`, `:234-236` |
| `process/properties.py` — builds Hypothesis rules from `transitions` and `invariants` | direct import | yes | `src/pneuma/process/properties.py:33` |
| `process/agent.py` — `Process`, `State`, `Transition` | direct import | yes | `src/pneuma/process/agent.py:59` |
| `process/agent_driver.py` | direct import | likely | `src/pneuma/process/agent_driver.py:26` |
| `detect/adapter.py` — the one file in `detect/` that binds `vacuity` to the IR | direct import | yes | `src/pneuma/detect/adapter.py:26` |
| `casestudy/miner.py` — constructs `Process`, `State`, `Transition`, plus `Guard`/`Invariant`/`Variable` | direct import | yes | `src/pneuma/casestudy/miner.py:24`, `:141-147` |
| `casestudy/rules.py` — attaches invariants to a mined process | direct import | yes | `src/pneuma/casestudy/rules.py:47` |
| `casestudy/pipeline.py` — six IR names | direct import | yes | `src/pneuma/casestudy/pipeline.py:28` |
| `casestudy/aimine.py`, `handlers.py`, `learning.py`, `live.py`, `ir_petri.py` | direct import | likely | `src/pneuma/casestudy/aimine.py:43`, `src/pneuma/casestudy/handlers.py:33`, `src/pneuma/casestudy/learning.py:60`, `src/pneuma/casestudy/live.py:31`, `src/pneuma/casestudy/ir_petri.py:17` |
| `casestudy/harnesslearn.py` — `TYPE_CHECKING`-only import | indirect | no | `src/pneuma/casestudy/harnesslearn.py:74` |
| `tests/library/test_process.py`, `test_process_agent.py`, `test_vacuity.py` — all seven IR names each | test | yes | `tests/library/test_process.py:18`, `tests/library/test_process_agent.py:40`, `tests/library/test_vacuity.py:26` |
| `tests/library/test_discrimination.py`, `test_interpreter_no_progress.py` | test | yes | `tests/library/test_discrimination.py:59`, `tests/library/test_interpreter_no_progress.py:20` |
| `tests/app/test_fixture_two.py`, `test_rules.py` — six IR names each | test | yes | `tests/app/test_fixture_two.py:61`, `tests/app/test_rules.py:23` |
| `tests/app/test_handlers_gate.py`, `test_kernel_live.py`, `test_learning_memory.py`, `test_learning_no_progress.py`, `test_casestudy.py`, `test_vacuity_on_real_logs.py` | test | likely | `tests/app/test_handlers_gate.py:53`, `tests/app/test_kernel_live.py:210`, `tests/app/test_learning_memory.py:173`, `tests/app/test_learning_no_progress.py:25`, `tests/app/test_casestudy.py:284`, `tests/app/test_vacuity_on_real_logs.py:29` |

### Blast-radius notes

- **`_render_tla` is private and imported across a module boundary.** `src/pneuma/process/tla.py:26` does `from .ir import Process, _render_tla`, and it is also called from inside the IR itself at `src/pneuma/process/ir.py:118`. Renaming it breaks the renderer with no type error at the IR's own boundary.
- **The validator rejects at construction, so a stricter rule breaks every fixture at once.** `_referentially_sound` (`src/pneuma/process/ir.py:224`) enforces unique names, a declared `initial_state`, no `_TLA_RESERVED` collision (`:56`), and at least one terminal state (`:273`). Tightening it turns previously valid hand-built test processes into `ValidationError`s, in every file listed above that constructs a `Process` inline.
- **`Guard` and `Effect` are deliberately not expressions, and that is load-bearing for TLC.** A guard compares one named variable against a literal (`src/pneuma/process/ir.py:17-21`). Widening either to free-form expressions requires an expression compiler in `tla.py` and blows the reachable state space TLC exhausts.

## `ai_method` / `MethodAgent` — the decorator paradigm

Defined at: `src/pneuma/method.py:79` (`ai_method`), `src/pneuma/method.py:326` (`MethodAgent`)

The compilation seam between a Python method and an `AIFunction`. Every agent in the project subclasses `MethodAgent` and decorates its capabilities.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `team.py` — `MethodAgent`, `ai_method`; the whole `Team`/`Member` layer | direct import | yes | `src/pneuma/team.py:55` |
| `gated.py` — `MethodAgent`, `MethodThread`; `GatedProposer` subclasses it | direct import | yes | `src/pneuma/gated.py:51`, `:101` |
| `recall.py` — `MethodAgent` and the private `_owner_name` | direct import | yes | `src/pneuma/recall.py:60`, `:406` |
| `process/agent.py` — `MethodAgent`, `_owner_name`, `ai_method` | direct import | yes | `src/pneuma/process/agent.py:57` |
| `casestudy/minelearn.py` — `MethodAgent`, `MethodThread`, `ai_method` | direct import | yes | `src/pneuma/casestudy/minelearn.py:71` |
| `casestudy/aimine.py`, `learning.py` | direct import | yes | `src/pneuma/casestudy/aimine.py:42`, `src/pneuma/casestudy/learning.py:58` |
| `casestudy/handlers.py`, `harnesslearn.py` — `ai_method` only | direct import | likely | `src/pneuma/casestudy/handlers.py:31`, `src/pneuma/casestudy/harnesslearn.py:69` |
| `demo/typed_cast.py` | direct import | likely | `src/pneuma/demo/typed_cast.py:30` |
| `MethodAgent.compiled(name)` reached by string method name | runtime dispatch | likely | `src/pneuma/process/agent.py:149`, `:251`; `src/pneuma/casestudy/live.py:119`; `src/pneuma/recall.py:308`; `src/pneuma/gated.py:283` |
| `tests/library/test_method.py` — the surface's own suite | test | yes | `tests/library/test_method.py:26` |
| `tests/library/test_team.py`, `test_team_dynamic.py`, `test_team_negotiation.py`, `test_team_worklog.py`, `test_recall.py` | test | yes | `tests/library/test_team.py:46`, `tests/library/test_team_dynamic.py:37`, `tests/library/test_team_negotiation.py:34`, `tests/library/test_team_worklog.py:33`, `tests/library/test_recall.py:44` |
| `tests/library/test_gated.py`, `test_model_cache.py`, `test_process_agent.py` | test | likely | `tests/library/test_gated.py:31`, `tests/library/test_model_cache.py:30`, `tests/library/test_process_agent.py:36` |
| `tests/app/test_kernel_live.py`, `test_counterfactual_replay.py` | test | likely | `tests/app/test_kernel_live.py:35`, `tests/app/test_counterfactual_replay.py:58` |

### Blast-radius notes

- **Tests script the model by replacing `compiled` on the instance, so every dispatch path must route through it.** `spawn` compiles through `self.compiled(name, **overrides)` rather than calling `compile_ai_method` directly, precisely so an instance-level binding is honoured (`src/pneuma/method.py:407-411`). A new dispatch path that calls the module function bypasses every offline binding and reaches a real model.
- **`_owner_name` is one definition with two consumers that must agree.** It produces the compiled tool name `{owner}.{method}` and the lifecycle's error messages (`src/pneuma/method.py:61-68`). `src/pneuma/recall.py:402-406` imports it rather than copying it for that reason. Change its output and error messages start naming threads a caller cannot find in the tool schema it was reading.
- **Type hints are resolved with `include_extras=True`, and dropping that silently degrades behaviour rather than failing.** `Annotated[str, ProceduralMarker()]` flattens to plain `str` without extras, and reusable code becomes an ordinary prompt argument with no error at all (`src/pneuma/method.py:141-148`).

## `miner.mine` — process discovery

Defined at: `src/pneuma/casestudy/miner.py:84` (`mine`), `src/pneuma/casestudy/miner.py:28` (`Discovery`)

The front half of the pipeline: reads an event frame, emits the `Process` IR that `tla.py` checks and `interpreter.py` runs (`src/pneuma/casestudy/miner.py:3-5`).

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `casestudy/pipeline.py` — imports the module, calls `_identifier` | direct import | yes | `src/pneuma/casestudy/pipeline.py:29`, `:69-70` |
| `casestudy/aimine.py` — `_identifier`, `conformance`, `directly_follows`, `mine` | direct import | yes | `src/pneuma/casestudy/aimine.py:44` |
| `casestudy/rules.py` — `_identifier` for invariant and flag naming | direct import | yes | `src/pneuma/casestudy/rules.py:48`, `:77-81`, `:189-190` |
| `casestudy/transcriptlog.py` — `_identifier`, plus a collision check built on its 40-char truncation | direct import | yes | `src/pneuma/casestudy/transcriptlog.py:53`, `:245-254` |
| `casestudy/minelearn.py` — `directly_follows`, `start_and_end_activities` | direct import | yes | `src/pneuma/casestudy/minelearn.py:73` |
| `casestudy/benchmark.py` — `mine` | direct import | yes | `src/pneuma/casestudy/benchmark.py:25` |
| `casestudy/harnesslearn.py`, `casestudy/live.py` — module-level, lazy in `live` | direct import | likely | `src/pneuma/casestudy/harnesslearn.py:70`, `src/pneuma/casestudy/live.py:245`, `:253` |
| `tests/app/test_portability.py` — the "a second log mines with no code changes" claim | test | yes | `tests/app/test_portability.py:15`, `:36-40` |
| `tests/app/test_minelearn.py` — `directly_follows`, then `conformance` and `mine` | test | yes | `tests/app/test_minelearn.py:77`, `:522` |
| `tests/app/test_rules.py`, `test_transcriptlog.py`, `test_casestudy.py` — all call `miner._identifier` directly | test | yes | `tests/app/test_rules.py:236-237`, `tests/app/test_transcriptlog.py:149`, `tests/app/test_casestudy.py:135` |
| `tests/app/test_aimine.py`, `test_fixture_two.py`, `test_vacuity_on_real_logs.py`, `test_harnesslearn.py`, `test_discrimination_on_real_logs.py`, `test_objective_on_the_live_score.py` | test | likely | `tests/app/test_aimine.py:24`, `tests/app/test_fixture_two.py:57`, `tests/app/test_vacuity_on_real_logs.py:27`, `tests/app/test_harnesslearn.py:907`, `tests/app/test_discrimination_on_real_logs.py:136`, `tests/app/test_objective_on_the_live_score.py:50` |

### Blast-radius notes

- **`_identifier` is private and has six cross-module consumers, including three tests that assert on its exact output.** It truncates to 40 characters (`src/pneuma/casestudy/miner.py:81`), and `transcriptlog` builds a *deliberate* collision detector on that truncation (`src/pneuma/casestudy/transcriptlog.py:245`, asserted at `tests/app/test_transcriptlog.py:149`). Changing the slug rule changes every mined state name, every derived invariant name in `src/pneuma/casestudy/rules.py:81`, and the collision set `transcriptlog` reports.
- **Every mined state gets `agent_method="handle"`, a placeholder no agent implements.** Written at `src/pneuma/casestudy/miner.py:125`. `src/pneuma/process/agent.py:169-173` returns `None` for an unrecognised name *specifically* so mined processes stay runnable; making that raise would make every mined process unrunnable by the class built to run it.
- **`mine` guarantees at least one terminal state by falling back to the observed last activity.** `src/pneuma/casestudy/miner.py:113-119`. Remove the fallback and the IR validator rejects the mined process (`src/pneuma/process/ir.py:273`), which is a construction-time failure, not a mining warning.

## `eventlog.parse_xes` — the event frame shape

Defined at: `src/pneuma/casestudy/eventlog.py:43`

Ground truth for everything downstream (`src/pneuma/casestudy/eventlog.py:3-5`). Emits a nine-column Polars frame: `case_id`, `activity`, `timestamp`, `resource`, `group`, `channel`, `department`, `ts`, `position`.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `casestudy/pipeline.py` | direct import | yes | `src/pneuma/casestudy/pipeline.py:29` |
| `casestudy/live.py` — also uses `connect` / `init_schema` for the decision log | direct import | yes | `src/pneuma/casestudy/live.py:32`, `:111-112` |
| `casestudy/benchmark.py` — lazy import inside the pm4py branch | direct import | likely | `src/pneuma/casestudy/benchmark.py:53` |
| `casestudy/miner.py` — consumes the frame's `case_id` / `activity` / `position` columns without importing the module | indirect | yes | `src/pneuma/casestudy/miner.py:49-53`, `:94` |
| `casestudy/transcriptlog.py` — a second producer that must emit the same nine columns | indirect | yes | `src/pneuma/casestudy/transcriptlog.py:427`, `:460-470` |
| `TursoMemoryBackend` sharing the audit database connection | runtime dispatch | likely | `src/pneuma/memory/turso_backend.py:400-403` |
| `tests/app/test_casestudy.py`, `test_portability.py`, `test_rules.py`, `test_transcriptlog.py`, `test_fixture_two.py`, `test_vacuity_on_real_logs.py` | test | yes | `tests/app/test_casestudy.py:17`, `tests/app/test_portability.py:15`, `tests/app/test_rules.py:21`, `tests/app/test_transcriptlog.py:25`, `tests/app/test_fixture_two.py:57`, `tests/app/test_vacuity_on_real_logs.py:27` |
| `tests/app/test_aimine.py`, `test_minelearn.py`, `test_harnesslearn.py`, `test_objective_on_the_live_score.py`, `test_learning_memory.py`, `test_adversary_live.py`, `test_discrimination_on_real_logs.py` | test | likely | `tests/app/test_aimine.py:24`, `tests/app/test_minelearn.py:64`, `tests/app/test_harnesslearn.py:80`, `tests/app/test_objective_on_the_live_score.py:47`, `tests/app/test_learning_memory.py:89`, `tests/app/test_adversary_live.py:77`, `tests/app/test_discrimination_on_real_logs.py:85` |
| `tests/library/test_boundary.py` — pins `libsql`/`polars` to the application side and proves `eventlog` fails under the blocker | config | likely | `tests/library/test_boundary.py:50`, `:208-214` |

### Blast-radius notes

- **The nine-column frame shape is a contract stated in a docstring, not in a type.** `transcriptlog._finalise` promises "`parse_xes`'s exact nine columns" (`src/pneuma/casestudy/transcriptlog.py:427`) and reproduces them in a `.select(...)` at `:460-470`. Adding or renaming a column in `parse_xes` silently desynchronises the second producer; nothing type-checks the pair.
- **`position` is the ordering key `miner` and every conformance calculation depend on.** `parse_xes` assigns it as a per-case ordinal rank over `ts` (`src/pneuma/casestudy/eventlog.py:109`); `transcriptlog` assigns it with an extra `tool_use_id` tiebreak for determinism across same-millisecond calls (`src/pneuma/casestudy/transcriptlog.py:456-459`). `directly_follows` sorts by `["case_id", "position"]` (`src/pneuma/casestudy/miner.py:49`), so a change to how `position` is derived changes every discovered edge.
- **Timestamps are normalised to naive UTC on purpose, because the permit log crosses a DST boundary.** The offset itself changes mid-log, so the parse is explicit and then converted (`src/pneuma/casestudy/eventlog.py:95-105`). Relaxing that turns cross-boundary durations into wall-clock arithmetic, which silently corrupts `case_durations` and `stats` rather than raising.

## `detect.objective.probe` — the pre-flight objective prober

Defined at: `src/pneuma/detect/objective.py:669`

Sweeps a scoring function over its declared domain and just outside it, before a training loop runs against it. Returns a `Probe` (`src/pneuma/detect/objective.py:365`).

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `detect/__init__.py` — re-exports 20 names from this module into the flat surface | direct import | yes | `src/pneuma/detect/__init__.py:47-68` |
| `detect/adversary.py` — `Brief`, `Degenerate`, `Sample`; supplies the `search=` callable | direct import | yes | `src/pneuma/detect/adversary.py:52`, `:395` |
| `casestudy/minelearn.py` — eight names; two `probe` calls plus the pre-flight refusal | direct import | yes | `src/pneuma/casestudy/minelearn.py:60`, `:767`, `:792`, `:1108-1110` |
| `casestudy/harnesslearn.py` — eight names, two `probe` calls | direct import | yes | `src/pneuma/casestudy/harnesslearn.py:57`, `:515`, `:536` |
| `search=` callable invoked with a `Brief` and its candidates re-scored | runtime dispatch | likely | `src/pneuma/detect/objective.py:775-792` |
| `tests/library/test_objective.py` — nine names, the surface's own suite | test | yes | `tests/library/test_objective.py:35`, `:432` |
| `tests/library/test_adversary.py` | test | yes | `tests/library/test_adversary.py:51` |
| `tests/app/test_objective_on_the_live_score.py` — eight names, five `raise_if_pathological` assertions | test | yes | `tests/app/test_objective_on_the_live_score.py:51`, `:196-201`, `:629-635` |
| `tests/app/test_fixture_two.py`, `test_harnesslearn.py`, `test_adversary_live.py` | test | likely | `tests/app/test_fixture_two.py:60`, `tests/app/test_harnesslearn.py:101`, `tests/app/test_adversary_live.py:25` |
| `tests/library/test_discrimination.py`, `tests/app/test_discrimination_on_real_logs.py` — `Severity` only | test | no | `tests/library/test_discrimination.py:58`, `tests/app/test_discrimination_on_real_logs.py:21` |
| `tests/library/test_liftability.py` — asserts this module imports only the standard library | config | likely | `tests/library/test_liftability.py:78`, `:83-90` |

### Blast-radius notes

- **`space` is required with no default, and defaulting it would make the boundary check meaningless.** In `Space.METRIC` the ideal corner is supposed to win, so `_check_boundary` is skipped and a note says so (`src/pneuma/detect/objective.py:821-836`). Any new caller must state which space it is in, or it gets a probe that cannot separate a sound objective from a broken one.
- **This module must import only the standard library, and a test measures it rather than trusting the docstring.** `tests/library/test_liftability.py:83-90` fails on any non-stdlib import here; `tests/library/test_liftability.py:78` pins the liftable set to exactly `{discrimination, gaming, objective, vacuity}.py`. Adding a pydantic or polars import to `objective.py` breaks a test that is about architecture, not behaviour.
- **A silent check is reported through `notes`, never through silence.** `probe` appends a note when `components` is empty (`src/pneuma/detect/objective.py:750-756`) and when `structure` is absent (`:768-773`). Removing a note is a behavioural regression of the same class the module exists to detect: a report silent about what it skipped.

## `interpreter.run` — the fixed interpreter

Defined at: `src/pneuma/process/interpreter.py:176`

Hand-written, reviewed once, reused for every process (`src/pneuma/process/interpreter.py:3-5`). Treats the agent as an untrusted oracle: it proposes, the interpreter decides legality.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `process/agent.py` — builds a `Decide` closure and calls `interpreter.offer` | direct import | yes | `src/pneuma/process/agent.py:58`, `:149-157` |
| `process/properties.py` — drives `run` from a Hypothesis state machine | direct import | yes | `src/pneuma/process/properties.py:32`, `:7-11` |
| `casestudy/live.py` — `run(..., max_steps=12)`; counts `ProcessError` as `blocked` | direct import | yes | `src/pneuma/casestudy/live.py:29`, `:169-175` |
| `casestudy/pipeline.py` | direct import | yes | `src/pneuma/casestudy/pipeline.py:27` |
| `casestudy/learning.py` | direct import | likely | `src/pneuma/casestudy/learning.py:59` |
| `_HISTORY` / `_REVISITS` ContextVars read by `history()` / `revisits()` from inside a decider | runtime dispatch | likely | `src/pneuma/process/interpreter.py:65`, `:72`, `:84-95` |
| `on_enter` hook — installed by a caller, called once per state visit | runtime dispatch | likely | `src/pneuma/process/interpreter.py:57`, `:248-249`, `:306-307` |
| `tests/library/test_process.py`, `test_process_agent.py`, `test_interpreter_no_progress.py` | test | yes | `tests/library/test_process.py:17`, `tests/library/test_process_agent.py:37`, `tests/library/test_interpreter_no_progress.py:19` |
| `tests/app/test_casestudy.py`, `test_portability.py`, `test_learning_no_progress.py` | test | likely | `tests/app/test_casestudy.py:18`, `tests/app/test_portability.py:16`, `tests/app/test_learning_no_progress.py:24` |

### Blast-radius notes

- **`NoProgress` is a `ProcessError` subclass on purpose, and the exception hierarchy is load-bearing for downstream accounting.** `src/pneuma/process/interpreter.py:106-130` says so explicitly and names `src/pneuma/casestudy/live.py:170-175` as the consumer whose completed/blocked split depends on it. Reparenting `NoProgress` outside `ProcessError` changes the arm results the live experiment reports without failing any test in `process/`.
- **A run whose final budgeted step lands on a terminal state has completed, and the re-check at the loop exit is what makes that true.** `src/pneuma/process/interpreter.py:308-317`. Without it a case completing in exactly `max_steps` transitions is counted as blocked — at precisely the budget the experiment uses.
- **A single enabled transition is stepped through without consulting the decider, so a decider-maintained history is missing exactly those steps.** `src/pneuma/process/interpreter.py:338-339`, and the ContextVar comment at `:59-65` records why `run` owns the history instead. Any caller tracking its own visit list will silently disagree with `offer`'s `[REVISIT]` marker.

## `TursoMemoryBackend` — learned parameters over libSQL

Defined at: `src/pneuma/memory/turso_backend.py:360`

Addressable entries, vector retrieval, and numeric parameters learned from `GradFeedback.score` (`src/pneuma/memory/turso_backend.py:361-388`).

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `memory/__init__.py` — re-exports it plus five siblings | direct import | yes | `src/pneuma/memory/__init__.py:15-22`; retrieval `Discrimination` built at `src/pneuma/memory/turso_backend.py:776` |
| `casestudy/learning.py` — `TursoMemoryBackend(Playbook, actor_id="navigator", ...)` | direct import | yes | `src/pneuma/casestudy/learning.py:57`, `:437` |
| `casestudy/minelearn.py` — `TursoMemoryBackend(Guidance, actor_id="miner", ...)` | direct import | yes | `src/pneuma/casestudy/minelearn.py:70`, `:1113` |
| `casestudy/harnesslearn.py` — `TursoMemoryBackend(HarnessKnobs, actor_id="harness", ...)` | direct import | yes | `src/pneuma/casestudy/harnesslearn.py:68`, `:991` |
| `recall.py` — consumes the `MemoryBackend` protocol this class implements | indirect | likely | `src/pneuma/recall.py:57`, `:409` |
| `EntryToolProvider` — tools built per parameter name and handed to a model | runtime dispatch | likely | `src/pneuma/memory/turso_backend.py:1243`, `:1134` (`tool_provider(*names)`) |
| `tests/library/test_turso_memory.py` — five names, then `connect`, then `BedrockCohereEmbedder` four times | test | yes | `tests/library/test_turso_memory.py:56`, `:340`, `:1224-1297` |
| `tests/app/test_learning_memory.py`, `test_learning_no_progress.py`, `test_minelearn.py`, `test_counterfactual_replay.py`, `test_harnesslearn.py` | test | yes | `tests/app/test_learning_memory.py:24`, `tests/app/test_learning_no_progress.py:22`, `tests/app/test_minelearn.py:85`, `tests/app/test_counterfactual_replay.py:57`, `tests/app/test_harnesslearn.py:102` |
| `tests/app/test_harnesslearn.py:502` — imports the private `_EXPLORE_DECAY` and `_TRUST_FRACTION` to recompute a horizon | test | yes | `tests/app/test_harnesslearn.py:502-505`; constants at `src/pneuma/memory/turso_backend.py:1222`, `:1233` |
| `tests/library/test_discrimination.py:461` — imports the *retrieval* `Discrimination`, not the `detect` one | test | no | `tests/library/test_discrimination.py:461` |

### Blast-radius notes

- **The `_*` hooks are the override points; overriding `recall`/`query`/`search`/`consolidate`/`save`/`fetch`/`delete` skips `ParameterRecalledEvent` emission and parameters vanish from the optimizer graph with no error.** Stated at `src/pneuma/memory/turso_backend.py:384-388`. This is the failure mode a new subclass hits first, and it is silent.
- **A shared connection is not closed by `close()`, because the owner is still using it.** `src/pneuma/memory/turso_backend.py:460-469`, and the constructor tracks `_owns_connection` at `:421`. The colocation this enables — parameters and evidence in the audit database from `casestudy.eventlog` (`:400-403`) — breaks if `close` starts closing unconditionally.
- **Two unrelated types are both named `Discrimination` and the difference is deliberate.** The retrieval one (`src/pneuma/memory/turso_backend.py:231`) reports a *margin* between two distance distributions; the `detect` primitive reports counts. `src/pneuma/detect/discrimination.py:32-36` records why they were not unified. A refactor that merges them either loses the margin or makes `separating` a number with no meaning.

## `Discrimination` — the shared three-valued verdict

Defined at: `src/pneuma/detect/discrimination.py:45`

Twenty lines of state and one verdict, shared by both detectors in `detect/`: can this check tell its two cases apart, or does it pass because it was never in a position to fail (`src/pneuma/detect/discrimination.py:1-8`)?

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `detect/vacuity.py` — builds one per rule; `vacuous` derives from `.idle` | direct import | yes | `src/pneuma/detect/vacuity.py:50`, `:430-437`, `:456` |
| `detect/objective.py` — builds one per declared component | direct import | yes | `src/pneuma/detect/objective.py:51`, `:1467` |
| `detect/gaming.py` — three construction sites | direct import | yes | `src/pneuma/detect/gaming.py:41`, `:141`, `:149`, `:329` |
| `detect/__init__.py` — flat re-export | direct import | yes | `src/pneuma/detect/__init__.py:46` |
| `casestudy/harnesslearn.py` — four construction sites | direct import | yes | `src/pneuma/casestudy/harnesslearn.py:56`, `:288`, `:304`, `:311`, `:364` |
| `Probe.discrimination` / `Probe.idle_components` — reached through the returned report, not by import | indirect | likely | `src/pneuma/detect/objective.py:377`, `:386-389` |
| `tests/app/test_counterfactual_replay.py`, `tests/app/test_harnesslearn.py` | test | yes | `tests/app/test_counterfactual_replay.py:56`, `tests/app/test_harnesslearn.py:806` |
| `tests/library/test_liftability.py` — pins it into the stdlib-only liftable set | config | likely | `tests/library/test_liftability.py:78`, `:83-90` |

### Blast-radius notes

- **`discriminates` is three-valued and must never become a bare boolean.** Under a boolean, "the search found no witness" and "the search gave up before it could find one" collapse into the same `False`, reporting a truncated sweep as a confident finding (`src/pneuma/detect/discrimination.py:12-20`). Every consumer above branches on `None`.
- **`observations == 0` with no `withheld` reason is a finding, not an abstention.** `src/pneuma/detect/discrimination.py:26-30`. Only the caller knows whether an empty observation set belongs to the subject or to its own harness, so a caller-side bound *must* be named in `withheld` — an unnamed bound is a silent cap.
- **`observations` is a reference scale, not a strict denominator, and is deliberately not validated against `separating`.** `vacuity` gathers `separating` at a wider level than it counted `observations` at when a relaxed sweep reaches more states than the exact one (`src/pneuma/detect/discrimination.py:50-54`). Adding a `separating <= observations` check in `__post_init__` breaks that caller.

## Other notable surfaces

- `src/pneuma/process/tla.py:269` (`check`) — 9 inbound files. `TLA_JAR` resolves `tools/tla2tools.jar` via `Path(__file__).resolve().parents[3]` (`src/pneuma/process/tla.py:28`), so moving the module up or down a directory silently breaks model checking rather than raising an import error.
- `src/pneuma/team.py:780` (`Team`), `:108` (`Member`), `:362` (`hiring_tools`) — 7 inbound files, all four `tests/library/test_team*.py` suites plus `src/pneuma/demo/warroom.py:28` and `src/pneuma/demo/staffing.py:27`.
- `src/pneuma/casestudy/rules.py:46` — 7 inbound files. The only consumer of `detect`'s flat `DEFAULT_LIMIT` / `RuleVerdict` / `audit_process` one-liner from application code.
- `src/pneuma/detect/vacuity.py:50` — 6 inbound files. Re-exported from `src/pneuma/detect/__init__.py:85` as `ReachabilitySweep` because `vacuity.Sweep` and `objective.Sweep` are unrelated types that collided under a flat re-export (`src/pneuma/detect/__init__.py:141-145`).
- `casestudy/minelearn.py` — 8 inbound files but 22 total references, the highest refs-per-file ratio in the repo; `Attempt`, `threshold_objective`, and `probe_objective` are each imported from four or more test files.
- `src/pneuma/model.py:15` (`opus5`) — 6 inbound files. `cache=True` is the default because Bedrock's Converse API does not cache Anthropic prompts without an explicit `cachePoint` block (`src/pneuma/model.py:34-47`); flipping it makes every request pay full input price with no error.
- `src/pneuma/gated.py:101` (`GatedProposer`) — 4 inbound files. Every hook it calls runs inside a post-condition validator, where the runtime turns any exception into `[VALIDATION ERROR]` feedback the next attempt reads (`src/pneuma/process/agent.py:237-243`), so a bug there burns every retry masquerading as a refusal.
- `src/pneuma/demo/cli.py:117` (`main`) — one inbound file, but it is the declared console script `pneuma` in `pyproject.toml:22`. Renaming it breaks the installed entry point with no test coverage.
- `tests/library/test_boundary.py:46-50` — not a code surface, but a config gate every new top-level module hits: an undeclared module fails `test_every_module_is_declared_on_one_side_of_the_boundary` (`tests/library/test_boundary.py:126`), and a library module that reaches `polars`, `libsql`, or `pm4py` fails `:141`.

## See also

- [Contract map][contract-map] — 37 shared source files
- [Module map][module-map] — 35 shared source files
- [Processes][processes] — 32 shared source files
- [Tech debt][tech-debt] — 30 shared source files
- [Business logic][business-logic] — 27 shared source files

[contract-map]: contract-map.md
[module-map]: ../architecture/module-map.md
[processes]: ../behavior/processes.md
[tech-debt]: tech-debt.md
[business-logic]: business-logic.md
