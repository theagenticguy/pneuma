# pneuma · Processes

`pneuma` exposes one console-script initiator — `pneuma = "pneuma.demo.cli:main"` (`pyproject.toml:22-23`) — and no HTTP routes, RPC tool registrations, cron declarations, or queue consumers. Only three modules carry an `if __name__ == "__main__"` block: `src/pneuma/demo/cli.py:148`, `src/pneuma/casestudy/live.py:242`, and `src/pneuma/casestudy/benchmark.py:119`. The remaining processes below are therefore the library's top-level orchestration entry points and the case study's measurement drivers, per this packet's library fallback: each is the first function executed when its initiator fires, whether that initiator is the CLI, a test, or a manual driver.

## CLI war-room investigation

Entry point: `src/pneuma/demo/cli.py:115`

1. `main` parses `--max-hires`, `--out`, `--quiet`, `--truth` (`src/pneuma/demo/cli.py:116-121`).
2. With `--truth`, print `incident.GROUND_TRUTH` plus `single_plane_ambiguity()` as JSON and exit 0 without spending a model call (`src/pneuma/demo/cli.py:123-140`).
3. Otherwise `asyncio.run(investigate(...))` (`src/pneuma/demo/cli.py:142`); the exit code is 0 only when the demo's own check graded the verdict correct (`src/pneuma/demo/cli.py:143`).
4. `investigate` builds a recording `Console` and a `Tape`, then creates the output directory (`src/pneuma/demo/cli.py:26-40`).
5. Stand up an `InMemoryCoordinator`, register a `LocalWorker`, and subscribe the tape to the coordinator's event stream (`src/pneuma/demo/cli.py:41-43`).
6. Construct `WarRoom(question=QUESTION, max_hires=max_hires)` (`src/pneuma/demo/cli.py:45`).
7. Start the periodic tape flusher so an interrupted run still leaves a transcript (`src/pneuma/demo/cli.py:49`, `src/pneuma/demo/cli.py:73-76`).
8. `await room.investigate(coordinator)` — a `Team` run with a `Briefing` hook, the demo's staffing tools on the lead's own `config_hook`, and its standard on the lead's own `post_conditions` — and persist `investigation.json` before teardown; the `finally` cancels the flusher, unsubscribes, writes the tape, and closes the worker (`src/pneuma/demo/cli.py:50-60`), then `_report` renders verdict, causal chain, dismissed decoys, hires, and usage (`src/pneuma/demo/cli.py:63`, `src/pneuma/demo/cli.py:79-112`).

### Related

- `src/pneuma/demo/warroom.py:100`
- `src/pneuma/demo/live.py:44`
- `src/pneuma/demo/incident.py:1120`
- `src/pneuma/demo/cast.py:204`
- `src/pneuma/demo/staffing.py:66`

## Team run pipeline

Entry point: `src/pneuma/team/core.py:202`

1. With no coordinator supplied, stand up a private `InMemoryCoordinator` + `LocalWorker` pair for the run and close it in a `finally` — the convenience path for scripts (`src/pneuma/team/core.py:222-231`).
2. Compose the lead's one `config_hook` — its own hook and `tools=` recomposed first, then the members-as-tools, then every hook's `tools_for_lead` — and spawn the lead's thread, registered but not running, so the whole run is one subtree (`src/pneuma/team/core.py:236-244`, `326-355`).
3. Fold every hook's `tools_for_member` into one equipped hook per member, then spawn each member as a child of the lead's thread (`src/pneuma/team/core.py:246-248`, `388-411`).
4. Call every hook's `on_assemble` in order — members are live, the lead has not cycled; briefing-style hooks hold their barrier here (`src/pneuma/team/core.py:250-253`, `src/pneuma/team/hooks/briefing.py:72-96`).
5. Fold the request left through every hook's `on_request` — the seam that delivers briefings and learned guidance into the lead's own prompt (`src/pneuma/team/core.py:255-258`).
6. Run the lead once, then drive the answer loop: each hook with `on_answer` reviews in order, `Revise(feedback)` re-runs the lead bounded by the verdict's cap, and cap exhaustion passes the last answer on with a `revise_cap` transcript entry (`src/pneuma/team/core.py:260-261`, `287-322`).
7. Return `TeamRun(answer, transcript, hooks_data)` — the answer exactly as the lead produced it, the wire-recorded member calls and revise rounds, and whatever hooks left behind (`src/pneuma/team/core.py:262`, `149-170`).
8. Unconditional `finally`: run every hook's `on_teardown` (each guarded, first error resurfacing only when nothing else propagates), then retire every member and the lead with `return_exceptions=True` (`src/pneuma/team/core.py:263-283`).

### Related

- `src/pneuma/team/members.py:62`
- `src/pneuma/team/hooks/briefing.py:45`
- `src/pneuma/team/hooks/hiring.py:282`
- `src/pneuma/team/hooks/learning.py:244`
- `src/pneuma/method.py:367`
- `src/pneuma/demo/warroom.py:168`

## Verified-process walk

Entry point: `src/pneuma/process/interpreter.py:176`

1. Seed variables from `start` or the IR's first allowed assignment, resolve `state_map`, and open a fresh `Run` trace at the initial state (`src/pneuma/process/interpreter.py:234-239`).
2. Assert every invariant against the initial state before anything is decided, so a process that starts illegal never runs (`src/pneuma/process/interpreter.py:241`, `src/pneuma/process/interpreter.py:354-357`).
3. Set the history and revisit `ContextVar`s, then call `on_enter` for the initial state inside that scope so a hook sees the same `history()` a decider would (`src/pneuma/process/interpreter.py:243-249`).
4. Per step: return the trace if the current state is terminal, else collect the outgoing transitions whose guards are enabled, raising `Deadlock` when none are (`src/pneuma/process/interpreter.py:252-258`).
5. `_elicit` takes a single enabled transition without a model call; otherwise it asks `decide` up to `max_rejections + 1` times, collecting illegal proposals and raising `ProcessError` when none is legal (`src/pneuma/process/interpreter.py:325-351`).
6. Apply the chosen transition's effects, move to its target, extend the history, and append a `Step` recording the rejected proposals (`src/pneuma/process/interpreter.py:262-276`).
7. Record a `Revisit` when the new state was already seen, else clear the consecutive counter; re-assert invariants, then raise `NoProgress` when `max_revisits` consecutive revisits accumulate — the dithering halt, checked after the invariant so a violation outranks it (`src/pneuma/process/interpreter.py:281-302`).
8. Call `on_enter` once per visit rather than per state name, and re-check terminality after the budgeted loop so a run landing on a terminal state at exactly `max_steps` reports completion instead of `ProcessError` (`src/pneuma/process/interpreter.py:306-322`).

### Related

- `src/pneuma/process/interpreter.py:360`
- `src/pneuma/process/interpreter.py:98`
- `src/pneuma/process/ir.py:213`
- `src/pneuma/process/ir.py:298`
- `src/pneuma/process/agent.py:137`

## ProcessAgent work walk

Entry point: `src/pneuma/process/agent.py:267`

1. `_check_no_decider_handler` refuses at wiring time a process whose state names `choose` as its handler, because that collision is silent rather than loud (`src/pneuma/process/agent.py:314`, `src/pneuma/process/agent.py:334`).
2. Resolve `state_map` once, since the property rebuilds the dict on every access and the hook runs at every step (`src/pneuma/process/agent.py:317`).
3. Build the `on_enter` closure that dispatches the entered state's handler with the run's `ThreadConfig` overrides (`src/pneuma/process/agent.py:319-320`).
4. Compile `choose` once per decider through `self.compiled`, so a test's model binding is honoured, and adapt it into the `Decide` callable via `interpreter.offer` (`src/pneuma/process/agent.py:137-157`).
5. Call `interpreter.run` with the process, that decider, the step and revisit budgets, and the hook (`src/pneuma/process/agent.py:322-330`).
6. Per entered state, `dispatch` resolves the handler through `handler_for` plus `arguments_for`, returning `None` for the pure control points that are the common case in a mined process (`src/pneuma/process/agent.py:245-247`, `src/pneuma/process/agent.py:161-203`).
7. Await the compiled handler and hand its result to `on_result`, awaiting the hook when it is awaitable so an `async def` override is not a silent no-op (`src/pneuma/process/agent.py:251-263`).
8. Re-raise any fault from either hook as `HandlerFailed` naming the state, the method, and which part broke, rather than letting a run report a completed case whose work never happened (`src/pneuma/process/agent.py:252-262`, `src/pneuma/process/agent.py:390`).

### Related

- `src/pneuma/casestudy/handlers.py:88`
- `src/pneuma/casestudy/handlers.py:225`
- `src/pneuma/casestudy/handlers.py:282`
- `src/pneuma/process/agent_driver.py:31`
- `src/pneuma/process/interpreter.py:176`

## Gated proposal cycle

Entry point: `src/pneuma/gated.py:271`

1. `gated` pops any caller-supplied `post_conditions`, runs the collision guard over each, and compiles the propose method with `admits` prepended so a subclass cannot accidentally replace the gate (`src/pneuma/gated.py:278-283`).
2. `_check_no_collision` refuses at wiring time a post-condition whose first parameter shares a propose parameter's name, which the runtime would turn into a `TypeError` swallowed as a validation failure (`src/pneuma/gated.py:285`, `src/pneuma/gated.py:324`).
3. On each model attempt the runtime calls `admits`, which extracts the candidate through `candidate_of` and re-raises an extractor bug as a fault rather than a verdict (`src/pneuma/gated.py:184-188`, `src/pneuma/gated.py:144-157`).
4. Call the gate, re-raising any exception as `_fault_text` so a bug in the gate cannot masquerade as a rejection and burn every retry (`src/pneuma/gated.py:189-192`, `src/pneuma/gated.py:258`).
5. Refuse an awaitable verdict on the sync path — closing the coroutine first — because every coroutine is truthy and would be admitted unjudged (`src/pneuma/gated.py:193-204`).
6. `_record` reads `ok` and renders `report_text` under the same fault discipline, appends a rejection to the `rejected` ledger, and raises the report plus `REASK` so the runtime feeds it back as the next prompt (`src/pneuma/gated.py:240-256`, `src/pneuma/gated.py:124`).
7. `judge` is the async path: it awaits an async gate, renders the report eagerly so a broken verdict detonates at the gate rather than in a later summary, and returns the rejection instead of raising it (`src/pneuma/gated.py:207-238`).
8. `propose_k` widens this into a beam: spawn one thread, run each seed cycle, fork `k - 1` byte-identical branches, run one cycle per branch, judge each directly rather than as a post-condition so `k` counts branches and not retries, and retire every thread in a `finally` (`src/pneuma/gated.py:340`, `src/pneuma/gated.py:395-423`).

### Related

- `src/pneuma/gated.py:57`
- `src/pneuma/gated.py:83`
- `src/pneuma/method.py:163`
- `src/pneuma/casestudy/harnesslearn.py:572`
- `src/pneuma/team/core.py:326`

## Case-study pipeline

Entry point: `src/pneuma/casestudy/pipeline.py:120`

1. Parse the XES log into a Polars frame (`src/pneuma/casestudy/pipeline.py:129`, `src/pneuma/casestudy/eventlog.py:43`).
2. Open the libSQL database, initialise the schema, and persist every event (`src/pneuma/casestudy/pipeline.py:131-133`, `src/pneuma/casestudy/eventlog.py:194`).
3. Mine a process model at `min_edge_cases` and record it against the log path (`src/pneuma/casestudy/pipeline.py:135-141`, `src/pneuma/casestudy/miner.py:84`).
4. `measure_control_skip` groups each case's activity path and counts, per channel, the cases that never perform the mandatory check (`src/pneuma/casestudy/pipeline.py:143`, `src/pneuma/casestudy/pipeline.py:102-117`).
5. Assemble `Findings` from log stats, state and edge counts, coverage, dropped share, the skip measurement, bottlenecks, and rework rate (`src/pneuma/casestudy/pipeline.py:144-155`).
6. `governed` attaches the derived precedence: a `checked` variable set by every transition into the check state, plus the `NoDetermineWithoutCheck` invariant (`src/pneuma/casestudy/pipeline.py:157`, `src/pneuma/casestudy/pipeline.py:55-101`).
7. When TLC is on the path, model-check both the structural and the governed process and record each verdict (`src/pneuma/casestudy/pipeline.py:159-163`, `src/pneuma/process/tla.py:269`).
8. Run the Hypothesis state machine over the governed process, record whether a violation was found, commit, and close (`src/pneuma/casestudy/pipeline.py:165-176`, `src/pneuma/casestudy/pipeline.py:179-197`, `src/pneuma/process/properties.py:72`).

### Related

- `src/pneuma/casestudy/eventlog.py:207`
- `src/pneuma/casestudy/miner.py:167`
- `src/pneuma/casestudy/pipeline.py:200`
- `src/pneuma/casestudy/rules.py:92`
- `src/pneuma/process/tla.py:265`

## Objective probe

Entry point: `src/pneuma/detect/objective.py:669`

1. Refuse a probe with no domains, and when `trust_declared_bounds` is false rebuild every `Domain` with its `bounded_by` claim stripped so the paranoid view refuses where the trusting one warns (`src/pneuma/detect/objective.py:717-730`).
2. Sweep the declared feasible box at `resolution` samples per axis, then note every axis whose grid skipped feasible values — which makes the reported ceiling a lower bound (`src/pneuma/detect/objective.py:734-735`, `src/pneuma/detect/objective.py:846`).
3. Run the arithmetic checks: inputs the objective raised on, non-finite results, poles chased by bisection, and unbounded growth (`src/pneuma/detect/objective.py:737-744`).
4. Measure each declared `Component` for whether it varies across the swept space at all, and note the absence of components as the reason a downstream finding names a symptom without its cause (`src/pneuma/detect/objective.py:748-756`, `src/pneuma/detect/objective.py:1420`).
5. Enumerate degenerate candidates from the declared `Structure` and check whether emptying the answer is free — preferred over a hand-written `degenerate` list, which the same hand that wrote the formula would get wrong in the same direction (`src/pneuma/detect/objective.py:758-773`, `src/pneuma/detect/objective.py:1318`).
6. When a `search` is supplied, build a `Brief` carrying the sweep's ceiling, best point, structure, samples, and source, then re-score every proposed candidate here so a searcher's own claim is never the evidence (`src/pneuma/detect/objective.py:775-792`).
7. Check every candidate — declared, enumerated, and searched — against the objective's maximum (`src/pneuma/detect/objective.py:794`, `src/pneuma/detect/objective.py:1107`).
8. Sweep outside the declared bounds by `reach` spans, then apply the boundary-max check only in `Space.DECISION`, because in metric space the ideal corner is supposed to win and the check cannot discriminate; return the `Probe` (`src/pneuma/detect/objective.py:807-843`, `src/pneuma/detect/objective.py:1607`).

### Related

- `src/pneuma/detect/objective.py:365`
- `src/pneuma/detect/objective.py:429`
- `src/pneuma/detect/objective.py:147`
- `src/pneuma/detect/objective.py:199`
- `src/pneuma/detect/adversary.py:384`
- `src/pneuma/casestudy/harnesslearn.py:168`

## Vacuity audit

Entry point: `src/pneuma/detect/vacuity.py:605`

1. Refuse a relaxation ladder that omits `"exact"`, since the exact level is what every verdict is reported against (`src/pneuma/detect/vacuity.py:631-632`).
2. Initialise the per-rule relaxation record, the abandoned set, and `pending` as the full rule list — sweeping the whole set is what makes this a detector rather than a query (`src/pneuma/detect/vacuity.py:634-638`).
3. Per relaxation level, build a `System` for that level and sweep only the still-unresolved rules, so the common case is a single sweep and the looser levels are never built (`src/pneuma/detect/vacuity.py:640-643`).
4. `sweep` seeds every start state at depth 0 under the same budget as expansion, then breadth-first enumerates reachable states, wrapping a stepping failure as `SweepError` naming the state that caused it (`src/pneuma/detect/vacuity.py:228-257`).
5. Per visited state, count each rule's scope and breach, recording a shortest trace the first time a rule breaks; a rule that cannot be evaluated is a `SweepError`, not a verdict (`src/pneuma/detect/vacuity.py:265-276`).
6. Enqueue unseen successors until the state limit, flagging `truncated` rather than silently stopping, and return per-rule `Count`s (`src/pneuma/detect/vacuity.py:278-304`).
7. Back in `audit`, a rule that broke at a relaxed level keeps that level's trace; a rule still unbroken and untruncated stays pending; a rule unresolved at a truncated relaxed level is marked abandoned because it loses every level that could still earn its pass (`src/pneuma/detect/vacuity.py:645-660`).
8. Build one `RuleVerdict` per rule from the exact sweep's counts plus the relaxation record, the traces, any `contradictory` note, and the truncation flags, and return the `Audit` (`src/pneuma/detect/vacuity.py:662-682`, `src/pneuma/detect/vacuity.py:328`).

### Related

- `src/pneuma/detect/vacuity.py:407`
- `src/pneuma/detect/vacuity.py:690`
- `src/pneuma/detect/adapter.py:178`
- `src/pneuma/detect/adapter.py:86`
- `src/pneuma/detect/discrimination.py:45`

## Minor flows

- Live framing experiment — entry at `src/pneuma/casestudy/live.py:206`. Runs the neutral and pressured arms sequentially through `run_arm` (`src/pneuma/casestudy/live.py:101`), logging every model decision to `llm_decisions` with its legality and compliance flags.
- Navigator training loop — entry at `src/pneuma/casestudy/learning.py:404`. Per round, runs a batch of cases whose decisions recall playbook advice through `Recall.trace`, phrases feedback from the round's dithering rate, and lets `TextGradOptimizer` edit the entries that round actually read.
- Harness proposal training — entry at `src/pneuma/casestudy/harnesslearn.py:940`. Recalls the coverage weight per round, has `HarnessProposer` propose one behind the `admit` gate (`src/pneuma/casestudy/harnesslearn.py:470`), and substitutes the measured quality for the model's own score before consolidating (`src/pneuma/casestudy/harnesslearn.py:908`).
- Miner toolkit training — entry at `src/pneuma/casestudy/minelearn.py:1069`. Refuses to start against a pathological objective, then recalls both `toolkit` and `advice` per round as call arguments so both are gradient targets.
- Agent-written miner grading — entry was `discover_and_grade`, deleted 2026-08-10; `Miner` (`src/pneuma/casestudy/aimine.py:186`) and `grade` (`src/pneuma/casestudy/aimine.py:399`) remain, but nothing composes them. Had the `Miner` agent discover a model from a CSV sample, then grades it against the fixed miner at both a default and a log-derived threshold (`src/pneuma/casestudy/aimine.py:399`).
- Team hiring tools — entry at `src/pneuma/team/hooks/hiring.py:66` (`hiring_tools`), wrapped as the `Hiring` hook at `:282`. Rebuilds `hire`/`delegate`/`dismiss` per cycle against that cycle's context; `commission` (`src/pneuma/team/hooks/hiring.py:133`) reserves the name and headcount before the spawn await so two concurrent hires in one turn cannot both pass the cap.
- Adversarial objective search — entry at `src/pneuma/detect/adversary.py:384`. Bridges into `_run` (`src/pneuma/detect/adversary.py:431`), which fans out one adversary per angle in parallel, then judges each candidate with an independent panel that never sees another ballot.
- Vector memory retrieval — entry at `src/pneuma/memory/turso_backend.py:657`. Embeds pending entries, then ranks by `vector_distance_cos` inside the database and drops hits beyond `distance_ceiling`, so an empty result is honest rather than the best of a bad set.
- Retrieval discrimination probe — entry at `src/pneuma/memory/turso_backend.py:718`. Measures relevant probes, self-retrieval, and control queries with the ceiling suspended, since the ceiling is derived from this measurement (`src/pneuma/memory/turso_backend.py:784`).
- Recalled parameter binding — entry at `src/pneuma/recall.py:247`. Validates every query first, fetches each marked parameter fresh under `no_thread_scope`, injects it as a keyword argument, and traces the method so the optimizer sees parameter nodes.
- TLC model check — entry at `src/pneuma/process/tla.py:269`. Renders the IR to TLA+ with a config and runs TLC for safety by default; `liveness=True` adds `PROPERTY Termination`, which a legitimately cyclic mined process will refute.
- Hypothesis property machine — entry at `src/pneuma/process/properties.py:72`. Generates one rule per transition so Hypothesis searches step sequences, with `choose_start` sampling the initial assignment to avoid the same vacuity trap the detectors exist to catch.
- Miner benchmark — entry at `src/pneuma/casestudy/benchmark.py:45`. Scores the mined model against four pm4py baselines on one log, importing pm4py lazily because it is an AGPL dev-only dependency.
- Transcript log loading — entry at `src/pneuma/casestudy/transcriptlog.py:474`. Fetches live Claude Code tool-use records and shapes them into events; `load_sample` (`src/pneuma/casestudy/transcriptlog.py:502`) does the same from a committed JSON file so the logic is testable offline.
- Incident dataset self-check — entry at `src/pneuma/demo/incident.py:1334`. Machine-checks that every plane is ambiguous, that all four planes intersect to exactly the planted mechanism, and that no pair of planes already resolves it.

## See also

- [Impact analysis][impact-analysis] — 32 shared source files
- [Module map][module-map] — 31 shared source files
- [Contract map][contract-map] — 28 shared source files
- [Business logic][business-logic] — 26 shared source files
- [Debugging guide][debugging-guide] — 25 shared source files

[impact-analysis]: ../insights/impact-analysis.md
[module-map]: ../architecture/module-map.md
[contract-map]: ../insights/contract-map.md
[business-logic]: ../insights/business-logic.md
[debugging-guide]: ../insights/debugging-guide.md
