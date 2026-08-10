# pneuma · Debugging guide

Something is broken. Where do you look first?

Two things about this codebase change where you look. First, there is almost no logging: one
module logger in the whole library, at `src/pneuma/detect/adversary.py:54`, and no Sentry,
Datadog, OpenTelemetry, statsd, or structlog anywhere. What replaces logs is typed return
objects that carry their own diagnostic accounting — `CheckResult`, `Discrimination`,
`interpreter.Run`, `TeamRun`, `Governed`, `Attempt` — plus exception messages written as
paragraphs, `CustomEvent` records on the coordinator's event log, and JSON files under
`artifacts/`. So the first check for most symptoms is *read a returned object's own report*,
not *grep a log*.

Second, the failures worth fearing here are not crashes. This codebase is built against
fail-soft: a run that reports success while the thing it was supposed to check never ran.
`src/pneuma/detect/discrimination.py:12-15` states the governing rule — a check that passes
without ever having been in a position to fail is the defect. Most rows below are therefore
"looks fine, is not" rather than "raised an exception".

A verdict of `False` and a verdict of `None` are different findings throughout
(`src/pneuma/detect/discrimination.py:17-20`). `None` means the measurement did not settle and
`withheld` names the bound that stopped it (`src/pneuma/detect/discrimination.py:56-59`). Never
read `None` as a pass.

## Failure-mode index

| Symptom | Likely surface | First check | Citation |
| --- | --- | --- | --- |
| Process run stops mid-case, error says "no enabled transition" | A guard the mined IR carries is unsatisfiable at the assignment the run reached — the state has outgoing edges but none is enabled | Read the `Deadlock` message: it prints the state and the full variable dict that reached it. Compare that assignment against the state's outgoing guards | `src/pneuma/process/interpreter.py:256-258` |
| Agent walks legally but the case never finishes; halt names a `max_revisits` limit | Dithering — the agent oscillates between valid states. Not a rule break; no rule was broken | Read `NoProgress.revisits`; each `Revisit` carries `state`, `step`, and `alternatives` — the moves the run could have taken instead | `src/pneuma/process/interpreter.py:301-302`, `123-131` |
| `InvariantViolated` at runtime after TLC reported `verified` | The IR and the interpreter disagree. This is a bug in `interpreter.py` or in `ir.py`, not in the data | Re-run `tla.check` on the same `Process` object the interpreter got. If TLC still verifies, the interpreter's guard/effect evaluation is wrong | `src/pneuma/process/interpreter.py:133-134`, `355-358` |
| Run halts with "no legal transition after N rejected proposals" | The model keeps naming transitions that are not on the menu — usually a prompt/menu mismatch, not a model failure | Read `Step.rejected` on the last step: it lists verbatim what the model proposed against `sorted(legal)` in the message | `src/pneuma/process/interpreter.py:348-351` |
| TLC reports success and nothing was actually checked | `outcome == "vacuous"` — an unsatisfiable `Init` or an empty state space. `distinct <= 0 or initial <= 0` | Read `CheckResult.distinct_states` and `.initial_states`. Green with zero of either means untested, not safe | `src/pneuma/process/tla.py:383-386` |
| TLC reports a violation of `"TemporalProperty"`, not the property you named | Only one `PROPERTY` is ever configured because TLC's report does not say which one failed | Confirm `liveness=True` was passed; `Termination` is the only temporal property this renderer emits | `src/pneuma/process/tla.py:250-262`, `350-351` |
| `liveness=True` returned `violated`/`vacuous`/`failed` and someone reports "termination verified" | Safety masks liveness: an invariant failure stops TLC at exit 12 before the temporal property is evaluated | Only `outcome == "verified"` carries the termination claim. Any other outcome means termination was not checked at all | `src/pneuma/process/tla.py:290-296` |
| Process step raises `RuntimeError: TLC needs java and ...` | `tools/tla2tools.jar` is gitignored and absent, or `java` is not on PATH | Call `tla.tlc_available()` — it is exactly `TLA_JAR.is_file() and shutil.which("java")` | `src/pneuma/process/tla.py:265-266`, `302-303` |
| Team run reports `correct=True` but the verdict is thin or wrong | Failed member briefings are rendered as strings, not raised. The lead reasoned from a partial or empty evidence set | Compare `len(TeamRun.briefings)` against `len(members())`, then grep the briefing values for the `"error: "` prefix | `src/pneuma/team.py:1265-1276`, `904`, `1497-1499` |
| Team run raises "every one of the N member(s) failed its briefing" | The one unrecoverable member failure — a coordinator, network, or wiring fault below the lead | The message names each member and its error. Nothing was spawned past this point, so the fault is upstream of the lead | `src/pneuma/team.py:1549-1559` |
| Lead's oracle appears to refuse every verdict for an unactionable reason | A post-condition's first parameter shares a name with a lead parameter; the runtime fills the slot twice and the `TypeError` is reported to the model as a validation failure | This is refused at wiring time now — if you see it, the check was bypassed. Compare the oracle's first parameter against `inspect.signature(lead.prompt_fn).parameters` | `src/pneuma/team.py:1561-1597` |
| Gated proposer burns every retry and never accepts anything | The gate itself raised. An exception from a gate is indistinguishable from a rejection unless it is re-dressed | Grep the model-visible text for "a fault in the gate rather than a verdict about your proposal". Faults are never appended to `self.rejected`, so an empty `rejected` with exhausted retries is the tell | `src/pneuma/gated.py:166-169`, `258-264`, `134-140` |
| Gated proposer admits everything without judging anything | An async gate reached the sync `admits` path — a coroutine is truthy, so `not verdict.ok` is always False | Refused loudly now with "the gate returned an awaitable". If it slipped past, use `judge()` (the async path) instead of `admits` | `src/pneuma/gated.py:193-204`, `207-213` |
| Hiring cap exceeded, or two subagents share one name | Two `hire` calls in one assistant turn both passed the cap check before either registered. The runtime's tool executor is concurrent | Read `Roster.log` in order: the reservation is written before the spawn `await`, so a duplicate means the reservation discipline was bypassed | `src/pneuma/team.py:378-387`, `453-483` |
| Second run on the same team handle inherits the first run's hires and log | The roster was carried across calls rather than replaced per run | `execute` replaces `self.roster` and `self.worklog` at the top of every run. Check `TeamRun.hiring_log` starts empty | `src/pneuma/team.py:1129-1138`, `1155-1156` |
| A `Team` step raised but live member threads are left on the coordinator | Two teardown paths exist for two different terminations; missing one leaks threads | `execute`'s `finally` covers the normal and faulting paths; `teardown()` covers external `terminate_now`. Both gather with `return_exceptions=True` | `src/pneuma/team.py:1189-1199`, `1469-1480` |
| Process completes and reports a finished case, but the per-state work never happened | A handler or the `on_result` hook raised and something swallowed it | `dispatch` re-raises everything as `HandlerFailed` naming state, method, and which part broke. An `async def on_result` that is never awaited is the silent variant this guards | `src/pneuma/process/agent.py:229-263`, `205-219` |
| A state spends a model call whose answer nobody reads | A state names the agent's decider (`choose`) as its `agent_method`, so the decision-maker is dispatched as work | Refused at `work()` entry. Scan `process.states` for `agent_method == "choose"` | `src/pneuma/process/agent.py:334-368` |
| Memory recall silently returns nothing, or irrelevant guidance, and the run succeeds | A search-mode `Recalled` parameter with no query, or a positional argument landing on a recalled slot | Both are wiring-time refusals now. If retrieval is running, read `Retrieved.distance` per hit — the best of a bad set looks identical to a good hit without it | `src/pneuma/recall.py:349-397`, `src/pneuma/memory/turso_backend.py:216-223` |
| Retrieval returns results but ranking is meaningless | An embedding search always returns something, so "it returned results" is not evidence | Run `probe_retrieval`. `self_retrieval_failures` non-empty means the index is broken, not weak; `separation <= 0` means unevidenced retrieval | `src/pneuma/memory/turso_backend.py:230-254`, `275-286`, `312-327` |
| Search silently omits entries that exist in the corpus | The top-k JOIN drops entries with no cached vector | Call `unranked_entries(name)` — it exists to make that countable rather than invisible. Should be empty after `embed_pending` | `src/pneuma/memory/turso_backend.py:657-668`, `697-704` |
| `calibrate_ceiling` raises `CeilingNotSeparable` | The relevant and control distance distributions overlap; no single threshold divides them | Read `Discrimination.overlaps`, `worst_relevant`, and `best_control`. A midpoint would drop real hits or admit unrelated ones, so no value is invented | `src/pneuma/memory/turso_backend.py:350-357`, `298-309` |
| Every entry's vector is misaligned after an embedding batch | The provider returned a truncated batch, or a dimension mismatch Turso would only reject at query time | Both are guarded at write time. `EmbeddingCache.calls` and `.texts_embedded` show whether the provider was reached at all | `src/pneuma/memory/embedding.py:198-202`, `257-262`, `219-222` |
| A detector reports a confident finding from a search that never finished | The sweep hit its bound. `DEFAULT_LIMIT = 200_000` states | Read `withheld` on the `Discrimination`. Non-empty makes `discriminates` `None`, which is not a pass and not a finding | `src/pneuma/detect/vacuity.py:72`, `419-429`, `src/pneuma/detect/discrimination.py:79-86` |
| A rule passes and the pass means nothing | The rule is unfirable — no reachable state can break it. The parking-garage defect | `Liveness.discrimination.idle` is "examined in full and never fired". `DOES NOT DISCRIMINATE (never in a position to fire)` is the rendered form | `src/pneuma/detect/vacuity.py:401-404`, `439-446`, `src/pneuma/detect/discrimination.py:94-96`, `117` |
| `SweepError` names a state and an assignment | A guard or rule could not be evaluated there. Raised rather than swallowed, because treating it as "not enabled" would shrink the state space | The message carries the location and the full assignment. Fix the rule; do not catch this | `src/pneuma/detect/vacuity.py:196-201`, `257`, `274-276` |
| `ObjectiveRefused` before a training loop starts | The objective probe found a pathology: garbage scoring best, feedback naming a different number than selection uses, a crash on a declared-feasible input, or a boundary maximum | Read `Probe.report()` — it lists every refusal, every warning, every sweep, and every downgraded finding with the reason it was downgraded | `src/pneuma/detect/objective.py:75-76`, `429-436`, `400-427` |
| A derived rule was attached and enforces nothing | `enforce` measured it unfirable and defaulted to `warn`, not `refuse` | Read `Governed.summary()`: it splits applied rules into `live`, `vacuous`, `unknown` and lists every decline with its reason | `src/pneuma/casestudy/rules.py:226-237`, `295-302` |
| A derived rule vanished with no error | It was declined, and the pair-unpacking `(process, applied)` compatibility shim hides declines from a caller that only unpacks | Read `Governed.skipped`. On `receipt.xes` five of the first nine candidates are declined | `src/pneuma/casestudy/rules.py:261-280`, `313-321` |
| `miner.mine` raises `ValidationError('duplicate state names')` | Two activity names collide under `_identifier`'s 40-character truncation | `LogSummary.report()` prints the colliding groups and predicts this exact failure before you run the miner | `src/pneuma/casestudy/miner.py:74-81`, `src/pneuma/casestudy/transcriptlog.py:154-163`, `src/pneuma/process/ir.py:233-234` |
| Conformance collapses after subsampling a transcript log | `sampling="longest"` is a biased sample; the longest sessions are the least typical | The report says so inline: on the full corpus it took conformance from 0.81 to 0.06. Use `sampling="random"` | `src/pneuma/casestudy/transcriptlog.py:148-153` |
| `RuntimeError: claude-sql exited N` | The external `claude-sql` CLI failed, or flags were passed before the subcommand | Call `transcriptlog.available()` first. The message carries stderr truncated to 500 chars; flags must follow the subcommand or cyclopts fails with an unrelated error | `src/pneuma/casestudy/transcriptlog.py:167-194`, `177-179` |
| Agent-written analysis code fails inside the sandbox | `io` is not an authorised import and dunder attribute access is forbidden, despite the prompt suggesting the `io.StringIO` route | `ANALYSIS_IMPORTS` is the whole allowlist. Use `polars.read_csv(log_csv.encode())`, which is what `toolkit.load_log` does | `src/pneuma/casestudy/aimine.py:46-48`, `src/pneuma/casestudy/toolkit.py:11-19` |
| A training loop reports no improvement round after round | A rewritten toolkit failed rehearsal and the last good one was silently restored | Read `Attempt.rolled_back` and `Attempt.rehearsal_error`. `unrehearsed` names helpers the rehearsal could not even call | `src/pneuma/casestudy/minelearn.py:292-301`, `284-290` |
| Training playbook grows but nothing gets better | Text was added that no decision ever retrieved | Compare `TrainingRound.entries` against `TrainingRound.retrieved_ids` — the honest denominator for "did the round learn anything" | `src/pneuma/casestudy/learning.py:235-246` |
| Adversarial search upheld every candidate it saw | The judge panel cannot reject, which is the defect class the module exists to detect | `Verdict.rejection_rate` is `None` with no ballots and `0.0` when nothing was rejected; `report()` emits an explicit warning at `0.0` | `src/pneuma/detect/adversary.py:218-229`, `268-272` |
| Adversarial search found nothing and that reads as a clean bill | A search that found nothing is a measurement about the search | Read `Verdict.errors` (per-angle failures) and `Verdict.searches` (what each angle actually searched) before concluding anything | `src/pneuma/detect/adversary.py:193-204`, `276-280` |
| A live experiment's completed/blocked split looks wrong at exactly `max_steps` | A case that lands on a terminal state with its last budgeted step is complete, not exceeded | `live.py` counts every `ProcessError` as `blocked`, so the interpreter re-checks terminality after the loop before raising | `src/pneuma/process/interpreter.py:308-317`, `src/pneuma/casestudy/live.py:168-176` |
| A test suite skip reads like a pass | Data files and live-model tests both skip silently when their prerequisite is absent | Check `tests/paths.py` markers and the `PNEUMA_LIVE*` variables. A path resolved from a caller's own depth is the failure mode named in that module's docstring | `tests/paths.py:29-32`, `1-12` |

## Log and error surfaces

| Surface | Where it emits | What to grep for | Citation |
| --- | --- | --- | --- |
| `logging` module logger — the only one in the codebase | Standard library `logging`, module `pneuma.detect.adversary`; no handler is configured, so it follows the host application's config | `adversarial search verdict:` (INFO) and `a judge failed:` (WARNING) | `src/pneuma/detect/adversary.py:45`, `54`, `425`, `571` |
| `warnings.warn` with the `RuleNotEnforced` category | Python warning stream (stderr by default), category `UserWarning` subclass | `RuleNotEnforced`, plus the four fixed decline reasons: `an activity the mined model does not contain`, `the prerequisite is the initial state`, `the rule name is already taken`, `no reachable state can violate it` | `src/pneuma/casestudy/rules.py:50-56`, `233-247` |
| Suppressed warnings — a real blind spot | `harnesslearn` installs `simplefilter("ignore", rules.RuleNotEnforced)`, so declines are invisible on that path | `catch_warnings` and `simplefilter` in both files; `rules.apply_derived_rules` re-emits captured declines deliberately | `src/pneuma/casestudy/harnesslearn.py:350-355`, `src/pneuma/casestudy/rules.py:330-344` |
| Rich console — the demo's human-facing channel | stdout via `rich.Console(record=True)`, and captured to `artifacts/console.txt` at the end of a run | `console.print` in `demo/cli.py`; verdict, causal chain, decoys, hires, token/turn stats | `src/pneuma/demo/cli.py:26-39`, `67`, `81-114` |
| Live event tape | stdout as it happens, plus `artifacts/transcript.txt`, flushed every 20 seconds so an interrupted run still leaves a usable transcript. Line format is `thread\tkind\ttext` | `failed:` for `FailedEvent`, `calls <tool>(` for tool calls, `spawned child` for new threads | `src/pneuma/demo/live.py:54-88`, `src/pneuma/demo/cli.py:71-78` |
| Coordinator `CustomEvent` log — the structured event stream | The coordinator's own event log, subscribed through `coordinator.on(...)` | Nine kinds: `team.hired`, `team.hired_dynamic`, `team.discovery`, `team.lead_running`, `team.graded`, `team.assembled`, `team.briefings_in`, `team.negotiated` | `src/pneuma/team.py:515`, `561`, `761`, `1186`, `1203`, `1237`, `1277`, `1349`, `1365` |
| `artifacts/investigation.json` | Written by `cli.py` before teardown, so a long reasoning run is not lost to a shutdown-path failure | `verdict`, `correct`, `oracle_failures`, `hiring_log`, `input_tokens`, `output_tokens`, `turns`, `wall_seconds` | `src/pneuma/demo/cli.py:52-56`, `src/pneuma/team.py:1205-1217` |
| libSQL audit database — `runs` table | The database file passed as `db_path`; one row per executed case | `outcome` holds `completed` or the exception class name (`Deadlock`, `NoProgress`, `ProcessError`, `InvariantViolated`); `detail` holds the message truncated to 200 chars | `src/pneuma/casestudy/pipeline.py:211-232`, `src/pneuma/casestudy/eventlog.py:181-190` |
| libSQL audit database — `verifications` table | Same file; one row per `(model, checker)` pair | `checker` is `tlc-structure`, `tlc-policy`, or `hypothesis`; `verified` is 0/1 and `detail` carries the summary | `src/pneuma/casestudy/pipeline.py:162-172`, `src/pneuma/casestudy/eventlog.py:172-179` |
| libSQL decision log — `llm_decisions` table | Same file; one row per model decision in the live framing experiment | `accepted` (0 means an illegal proposal) and `compliant` (0 means a control was on the menu and skipped), plus the model's own `reason` text | `src/pneuma/casestudy/live.py:50-63`, `182-200` |
| Exception messages as the primary diagnostic channel | Raised to the caller; in gated/team paths the text is fed back to the model as a validation message | `a fault in the gate rather than a verdict about your proposal` (`gated`), `rather than reporting a case whose work did not happen` (`process/agent`), `no progress:` (`interpreter`) | `src/pneuma/gated.py:258-264`, `src/pneuma/process/agent.py:390-398`, `src/pneuma/process/interpreter.py:122-128` |
| Typed report renderers — read these instead of grepping | Returned objects, printed by the caller | `Discrimination.__str__` emits `UNSETTLED` / `DOES NOT DISCRIMINATE` / `discriminates`; `turso_backend.Discrimination` emits `retrieval BROKEN` / `UNMEASURED` / `PARTIAL` | `src/pneuma/detect/discrimination.py:109-118`, `src/pneuma/memory/turso_backend.py:329-347` |
| Accounting reports that make dropped data countable | Returned strings a caller prints | `LogSummary.report()` prints the raw-vs-kept split and every knob; `Governed.summary()` prints live/vacuous/unknown; `Verdict.report()` prints per-angle failures | `src/pneuma/casestudy/transcriptlog.py:121-164`, `src/pneuma/casestudy/rules.py:294-301`, `src/pneuma/detect/adversary.py:253-281` |
| `print()` to stdout | Five sites total, all in application code, none in the library | `print(` in `demo/cli.py` (10 sites, all inside `--truth`), `demo/live.py`, `casestudy/toolkit.py`, `casestudy/live.py`, `casestudy/benchmark.py` | `src/pneuma/demo/cli.py:127-141` |

There is no observability platform to consult. Greps for `sentry`, `datadog`,
`opentelemetry`, `statsd`, `structlog`, `prometheus`, and `newrelic` across `src/`, `tests/`,
and `pyproject.toml` return zero hits, and `pyproject.toml:5-13` declares no such dependency.
Do not look for a dashboard; there is none.

## First-checks ladder

1. **Read the exception message in full before anything else.** Messages here are written as
   paragraphs that name the fix, not as labels. `NoProgress` prints the limit it hit and the
   states it circled; `Deadlock` prints the state and the whole variable dict; the team's
   wiring refusals each end with the specific rename or override that resolves them.
   `src/pneuma/process/interpreter.py:122-128`
2. **Read the returned object's own report method.** `Governed.summary()`,
   `LogSummary.report()`, `Probe.report()`, `Verdict.report()`, and both `Discrimination`
   `__str__` implementations exist so a caller does not reconstruct the accounting. Each one
   already contains the answer to "what got dropped and why". `src/pneuma/casestudy/rules.py:294-301`
3. **Check whether the verdict is `None` rather than `False`.** `None` means unsettled and
   `withheld` names the bound; treating it as a pass is the mistake the three-valued type
   exists to prevent. `src/pneuma/detect/discrimination.py:79-86`
4. **Run the test suite and compare the collected count.** `.venv/bin/python -m pytest -p
   no:randomly --collect-only -q` collects 853 tests in under a second and needs no
   credentials, no `java`, and no data files. `README.md:19` states the same count; if the two
   ever disagree, use the number you just measured as the baseline. `pyproject.toml:39-46`
5. **Confirm the external prerequisites, since their absence is a silent skip.** `java` plus
   `tools/tla2tools.jar` for TLC (`tla.tlc_available()`), `claude-sql` on PATH for the
   transcript corpus (`transcriptlog.available()`), and the data files in `data/` guarded by
   `tests/paths.py` markers. All three skip rather than fail.
   `src/pneuma/process/tla.py:265-266`
6. **For a team run, count the briefings against the cast and grep them for `"error: "`.** A
   failed briefing is a string, not an exception, so a run with half its evidence missing
   still reports `correct=True`. `len(briefings)` against `len(members())` is the only other
   signal. `src/pneuma/team.py:1265-1276`
7. **For a gated proposer that never accepts, read `self.rejected`.** An empty ledger with
   exhausted retries means the gate was faulting, not refusing — grep the model-visible text
   for "a fault in the gate". A populated ledger means the gate is working and the model is
   the problem. `src/pneuma/gated.py:134-140`
8. **For a process run, read `Run.revisits` and `Run.rejections` before re-running anything.**
   Revisits distinguish dithering from a genuine detour; rejections distinguish a model that
   cannot read the menu from one that reads it fine. Both are already on the trace.
   `src/pneuma/process/interpreter.py:164-173`
9. **Query the libSQL audit database rather than re-running the pipeline.** `runs.outcome`
   holds the exception class name per case, `verifications` holds every checker's verdict, and
   `llm_decisions` holds every model choice with `accepted` and `compliant` flags. Re-running a
   live arm costs model calls; the database is already there.
   `src/pneuma/casestudy/eventlog.py:148-190`
10. **Only then re-run with TLC.** `tla.check` shells out to a JVM with a 180-second default
    timeout (300 in the pipeline), so it is the most expensive check on this list, and the
    `failed` outcome exists precisely so a broken checker is never mistaken for a violated
    process. `src/pneuma/process/tla.py:269`, `309-327`, `371-377`

## Known incident patterns

No comments tagged `INCIDENT`, `POSTMORTEM`, `FLAKY`, `SLOW`, `KNOWN BUG`, `TODO`, `FIXME`, or
`HACK` exist anywhere in `src/` or `tests/`, and there is no `INCIDENTS.md`. This codebase
records its incident history differently: as docstrings that name a measured past failure
alongside the guard added for it. `grep -rn "Measured" src/` returns 37 such notes across ten
files. The list below is those postmortems, plus the two self-inflicted detector bugs
`README.md:228-239` describes.

- **Concurrent hire race:** two `hire` calls in one assistant turn both passed the headcount
  check before either registered, because the runtime's default tool executor is concurrent.
  Signal: headcount above `max_hires`, or two subagents sharing a name with the first's thread
  unreachable by `dismiss`, by `execute`'s `finally`, and by `teardown`. Mitigation: the name
  and the slot are reserved in the same synchronous stretch as the three refusals, before the
  spawn `await`, with a rollback if the spawn raises. `src/pneuma/team.py:378-387`, `453-483`
- **Roster carried across runs:** the same `Team` handle run twice had run 2 open with run 1's
  hiring log, find run 1's names taken, start `max_hires` short by run 1's headcount, and
  `delegate` to a hire run 1 had already retired. Signal: a non-empty `hiring_log` at the start
  of a run. Mitigation: `execute` replaces the roster and worklog — of the same class, so a
  subclass's narrowed type survives — as its first action. `src/pneuma/team.py:1129-1138`,
  `1155-1156`
- **Silently deleted post-conditions:** `AIFunction.replace` overwrites rather than appends, so
  a naive `replace(post_conditions=[self.oracle])` deleted every post-condition the subclass's
  lead already carried. Signal: none — "the checks are gone, nothing raises, and the run
  reports a gated verdict", the worst available failure mode. Mitigation: the lead's own
  conditions are read off its config and kept, with the oracle prepended.
  `src/pneuma/team.py:1402-1412`, `1424`
- **Duplicate member names dropping a briefing:** briefings are keyed by name, so a cast with
  two members called `plane` produced one entry, the earlier briefing gone with nothing raised.
  Measured on a two-member cast answering `FIRST` and `SECOND`: the report carried
  `{'plane': 'SECOND'}` and graded correct. Signal: `len(briefings)` below `len(members())` —
  the only person who could notice. Mitigation: pre-spawn refusal in
  `_check_no_duplicate_members`. `src/pneuma/team.py:1490-1520`
- **A lead ruling on no evidence at all:** with both members raising, the lead ran, its context
  mentioned no error, and the run reported `correct=True`. The lead holds no evidence of its
  own, so it reasoned from the request alone and produced a verdict shaped like a real one.
  Signal: every briefing value starting with `"error: "`. Mitigation:
  `_check_some_briefing_survived` raises before the lead is spawned — raised, not rendered,
  because there is no model here that could fix it. `src/pneuma/team.py:1522-1559`
- **Guards firing after the barrier:** with the lead composition left in its original place, a
  colliding oracle was refused only after both members had been spawned, briefed with a real
  model call, and retired. Signal: a wiring error arriving from the middle of a run rather than
  at construction. Mitigation: `members()`, the duplicate check, and `_gated_lead()` all run
  before `assemble`. `src/pneuma/team.py:1158-1169`
- **Two teardown paths, both needed:** the worker awaits `teardown` on termination and does
  *not* await it when `execute` raises. Signal: live member threads left on the coordinator
  after a run ended. Mitigation: both `execute`'s `finally` and `teardown` retire everything,
  and `retire` is idempotent so overlapping is harmless. `src/pneuma/team.py:1469-1480`
- **A gate fault masquerading as a rejection:** an exception from the gate is indistinguishable
  from a refusal and burns every retry on a bug. Signal: retries exhausted with an empty
  `self.rejected` ledger. Mitigation: every gate, extractor, and verdict call is wrapped and
  re-raised through `_fault_text`, whose wording says the failure is internal; faults are never
  appended to `rejected`. `src/pneuma/gated.py:161-192`, `240-264`
- **An async gate admitting everything:** a coroutine is truthy, so `not verdict.ok` on one
  admits without ever judging. Signal: a gate that never rejects anything, plus a
  `RuntimeWarning` about a never-awaited coroutine at collection time. Mitigation: the
  awaitable is closed and an `AssertionError` naming the wiring fault is raised; async gates
  go through `judge()`. `src/pneuma/gated.py:193-204`
- **A never-awaited `on_result` hook:** an `async def on_result` would have been a silent no-op
  — the paperwork never written, the run still reporting a completed case, the only trace a
  garbage-collection `RuntimeWarning`. Signal: a completed `Run` with no recorded per-state
  output. Mitigation: one `inspect.isawaitable` check makes it unrepresentable, at the cost of
  one line. `src/pneuma/process/agent.py:205-219`, `255-262`
- **The decider dispatched as work:** a state naming `choose` as its `agent_method` either dies
  on the signature bind with a `TypeError` about `choose` from a state that never mentioned
  choosing, or — worse — spends a real model call returning a transition name the interpreter
  never sees, at a state that then also gets a genuine decision. Signal: in the bad case, none;
  the run completes and the wasted turn is invisible. Mitigation: `_check_no_decider_handler`
  scans declared `agent_method`s at `work()` entry. `src/pneuma/process/agent.py:334-368`
- **A complete run reported as budget-exhausted:** a case whose final budgeted step landed on a
  terminal state raised `exceeded N steps` because the loop's terminal check sits at the top.
  Signal: `live.py` counts `ProcessError` as `blocked`, so the completed/blocked split was
  corrupted at exactly the budget the experiment used. Mitigation: terminality is re-checked
  after the loop before the raise. `src/pneuma/process/interpreter.py:308-317`
- **Dithering, which no rule check can catch:** in 6 of 10 live cases the agent broke no rule
  and instead moved back and forth between valid states until the step budget stopped it. The
  prompt genuinely contained no evidence a state was a repeat. Signal: high `Run.revisits` with
  zero `InvariantViolated`. Mitigation: the `[REVISIT]` marker in `offer`, the same fact
  recorded as data on `Run.revisits`, and the `NoProgress` halt at
  `DEFAULT_MAX_REVISITS = 5`. `src/pneuma/process/interpreter.py:360-401`, `75-82`, `278-291`
- **A truncated sweep reported as a confident finding:** a detector hit its search limit
  partway through and still reported the partial result as a finding — the exact mistake these
  tools exist to catch, happening inside a tool. Signal: a `False` verdict from a search whose
  state count sits at the limit. Mitigation: the three-valued `discriminates` with `None` for
  unsettled, plus `withheld` naming which bound was hit. `src/pneuma/detect/discrimination.py:12-24`,
  `src/pneuma/detect/vacuity.py:419-429`
- **The objective prober's shared blind spot:** it asked the caller to supply examples of bad
  answers and checked whether they scored poorly — but the same person writes the bad-answer
  list and the scoring formula, so both shared one blind spot and a genuinely broken formula
  passed with zero findings. Signal: a probe reporting no findings at all. Mitigation: callers
  now describe the shape of the answer space through `structure`, and the prober enumerates the
  degenerate inputs from it — "a declared list of bad answers is written by the same hand as the
  scoring formula and is wrong in the same direction". `README.md:233-238`,
  `src/pneuma/detect/objective.py:692-696`
- **An unevaluable guard silently shrinking the state space:** treating a guard that cannot be
  evaluated as "not enabled" would make the reachable space smaller and the sweep's verdict
  meaningless — the same defect the module detects. Signal: an unexpectedly small
  `reachable_states`. Mitigation: `SweepError` is raised with the location and the full
  assignment that broke it. `src/pneuma/detect/vacuity.py:196-201`, `254-276`
- **A judge panel that cannot reject:** a panel upholding everything it ever saw is a check
  with no teeth, which is the defect class the adversarial module was written against. Signal:
  `rejection_rate == 0.0` with a non-empty ballot list. Mitigation: the rate is reported rather
  than assumed, and `report()` emits an explicit "treat its verdicts as unmeasured" warning.
  `src/pneuma/detect/adversary.py:218-229`, `268-272`
- **A silently reverted learnable parameter:** a loop that reverted the toolkit it was supposed
  to be learning looks, from the outside, exactly like a loop that learned nothing. Signal:
  flat scores across rounds. Mitigation: `Attempt.rolled_back` and `Attempt.rehearsal_error`
  are fields on the record, and `unrehearsed` names helpers the rehearsal could not construct
  arguments for. `src/pneuma/casestudy/minelearn.py:292-301`, `284-290`
- **A prompt heading followed by nothing:** an empty advice string left "Guidance learned from
  previous runs:" followed by blank space, which a model reads as a section to fill in rather
  than an absence — and left an operator unable to tell "the playbook is empty" from
  "retrieval found nothing relevant", the second of which happens on purpose under a
  calibrated `distance_ceiling`. Signal: invented guidance in a transcript. Mitigation:
  `render_advice` emits an explicit "(no relevant guidance retrieved for this decision)".
  `src/pneuma/casestudy/learning.py:258-270`
- **The `io.StringIO` route the prompt itself recommended:** `io` is not in `SAFE_BUILTINS` and
  `ANALYSIS_IMPORTS` does not add it, so `polars.read_csv(io.StringIO(log_csv))` raises
  `InterpreterError: Import of io is not allowed`. `polars.__version__` also raises, because
  the sandbox forbids dunder attribute access — and that one fails at call time rather than
  load time. Signal: `InterpreterError` from agent-written analysis. Mitigation: `load_log`
  exists so the agent cannot get it wrong, plus `minelearn`'s rehearsal step.
  `src/pneuma/casestudy/toolkit.py:11-19`
- **Advertised prose the agent never saw:** `procedural_signatures` walks top-level `def`s and
  emits signatures plus docstrings only — module docstrings, comments, and module-level
  constants are silently dropped. Measured, not inferred. Signal: an agent ignoring guidance
  that is visibly present in the source. Mitigation: anything the agent must read is a
  docstring on a function it can call, and the prose parameter is a separate gradient target.
  `src/pneuma/casestudy/toolkit.py:21-25`
- **A biased subsample presented as a subsample:** `sampling="longest"` took conformance from
  0.81 to 0.06 on the full corpus, because the longest sessions are the least typical. Signal:
  an implausible conformance drop after sampling. Mitigation: the number and the cause are
  printed inline by `LogSummary.report()`, with `sampling="random"` named as the fix.
  `src/pneuma/casestudy/transcriptlog.py:148-153`
- **A stale test count in the README:** an earlier README stated 738 passed and 10 skipped for
  748 collected while a `--collect-only` run collected 853; the README was rewritten on
  2026-08-10 with the measured count (`README.md:19`). Signal: a collection delta read as a
  regression. Mitigation: measure the baseline yourself rather than trusting the prose. The
  same rewrite dropped a stale `-p no:randomly` instruction — pytest-randomly is not installed
  and is not declared in the dev dependency group. `pyproject.toml:24-31`

## See also

- [Module map][module-map] — 25 shared source files
- [Processes][processes] — 25 shared source files
- [Impact analysis][impact-analysis] — 24 shared source files
- [Business logic][business-logic] — 23 shared source files
- [Contract map][contract-map] — 21 shared source files

[module-map]: ../architecture/module-map.md
[processes]: ../behavior/processes.md
[impact-analysis]: impact-analysis.md
[business-logic]: business-logic.md
[contract-map]: contract-map.md
