# pneuma · Business logic

This file indexes the domain rules the codebase enforces in application code: input validations, invariants the code refuses to break, derived calculations, and the policy gates that decide what a model or a caller is allowed to do.

**What counts as business logic here.** pneuma is a Python library for building AI agents, not a web application. There is no HTTP layer, no ORM, no migrations, and no authorization middleware, so the usual shapes are absent: nothing in `src/` matches `z.object`, `joi.`, or `marshmallow`. What takes their place is three mechanisms, and the file is organised around them:

1. **Pydantic v2 model shape plus four `@model_validator(mode="after")` hooks**, all four in `src/pneuma/process/ir.py`. The model emits a process as *data* and Pydantic is the gate before a model checker ever sees it (`src/pneuma/process/ir.py:1-22`).
2. **Explicit guard clauses** — 53 `raise ValueError` sites and 135 `raise` statements across `src/`, concentrated in the `team/` package (17), `process/ir.py` (17), `memory/turso_backend.py` (12), `casestudy/transcriptlog.py` (12), `gated.py` (11), and `recall.py` (9).
3. **23 production `assert` statements**, 18 of them in one import-time dataset self-check (`src/pneuma/demo/incident.py:1334-1397`).

Two scope decisions matter for reading this file.

**A refusal's *style* is itself a rule.** The codebase draws a deliberate line between a wiring-time refusal that raises and a model-facing refusal that returns text. The reasoning is stated in code: a tool returning `"error: ..."` reaches the model as a successful tool result whose content is that string, so the model reads the problem and fixes it, while an exception surfaces as a tool fault the model cannot act on (`src/pneuma/team/hooks/hiring.py:20-23`). Every entry below is tagged with which one it is, because a caller and a model are different audiences with different remedies.

**A third mechanism sits alongside validation and is treated as first-class: measurement verdicts.** The `detect` package's whole purpose is deciding whether a *rule itself* can catch anything, so several entries here are rules about rules — an invariant with no condition is rejected, a truncated search may not report a pass, a rule rescued only by freeing initial values loses its pass. These are captured as invariants because the code enforces them the same way it enforces anything else.

**Out of scope.** SQL schema constraints in `src/pneuma/memory/turso_backend.py:81-121` and `src/pneuma/memory/embedding.py` (`PRIMARY KEY`, `NOT NULL`) exist but are not enumerated separately; where a primary key shapes application behavior it appears in the Invariants table with `Where enforced: application + DB constraint`. Test-suite assertions are excluded. Prompt text instructing a model is excluded except where a matching code check enforces it.

Domain names are the package names from `docs/architecture/module-map.md:5-61`.

## Validations

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| A `Variable` declares exactly one domain: `low`+`high` or `values`, never both and never neither | pneuma.process | `src/pneuma/process/ir.py:79-84` | Reject — `ValueError` at model construction |
| `Variable.low` may not exceed `high` | pneuma.process | `src/pneuma/process/ir.py:85-86` | Reject — `ValueError` |
| `Variable.initial`, when given, must be inside the declared domain | pneuma.process | `src/pneuma/process/ir.py:87-88` | Reject — `ValueError` |
| Variable names match `^[a-z][a-z0-9_]*$`; state, transition, and invariant names match `^[A-Za-z][A-Za-z0-9_]*$` | pneuma.process | `src/pneuma/process/ir.py:69`, `165`, `183`, `196`, `216` | Reject — Pydantic pattern violation |
| An `Effect` assigns exactly one of `value` or `increment` | pneuma.process | `src/pneuma/process/ir.py:146-149` | Reject — `ValueError` |
| An `Invariant` with neither `forbidden_state` nor `forbidden_when` is refused: it forbids nothing | pneuma.process | `src/pneuma/process/ir.py:201-204` | Reject — `ValueError` |
| State, variable, and transition names must each be unique within a `Process` | pneuma.process | `src/pneuma/process/ir.py:232-245` | Reject — `ValueError` |
| `initial_state` must name a declared state | pneuma.process | `src/pneuma/process/ir.py:235-237` | Reject — `ValueError` |
| No transition, invariant, or variable may take a name the TLA+ renderer defines itself | pneuma.process | `src/pneuma/process/ir.py:56-58`, `247-256` | Reject — `ValueError`, in preference to a downstream TLC parse error |
| Every transition endpoint must be a declared state | pneuma.process | `src/pneuma/process/ir.py:258-261` | Reject — `ValueError` |
| Every guard and effect must name a known variable and a value inside its domain | pneuma.process | `src/pneuma/process/ir.py:262-265`, `317-322` | Reject — `ValueError` |
| An invariant's `forbidden_state` must be a declared state | pneuma.process | `src/pneuma/process/ir.py:267-269` | Reject — `ValueError` |
| A `Process` must declare at least one terminal state, or it could never complete | pneuma.process | `src/pneuma/process/ir.py:273-274` | Reject — `ValueError` |
| An ordering comparison (`lt`/`le`/`gt`/`ge`) requires integers on both sides | pneuma.process | `src/pneuma/process/ir.py:126-127` | Reject at evaluation — `TypeError` |
| An `Effect` increment requires an integer current value | pneuma.process | `src/pneuma/process/ir.py:155-156` | Reject at apply — `TypeError` |
| A state may not name the agent's own decider (`choose`) as its per-state handler | pneuma.process | `src/pneuma/process/agent.py:334-368` | Reject at `work()` entry — `RuntimeError`, before anything is compiled or spent |
| An `@ai_method` with no docstring that returns `None` has no prompt | pneuma (kernel) | `src/pneuma/method.py:124-125` | Reject — `ValueError` |
| A compilation that dropped a method's parameters is refused | pneuma (kernel) | `src/pneuma/method.py:356-360` | Reject — `RuntimeError` |
| No lifecycle operation is permitted on a retired thread | pneuma (kernel) | `src/pneuma/method.py:311-319` | Reject — `RuntimeError`; silently respawning would hand back a blank conversation |
| A hook's `on_answer` must return `Accept` or `Revise`; anything else raises naming the hook | pneuma (kernel) | `src/pneuma/team/core.py:307-311` | Reject mid-loop — `RuntimeError`; a `None` treated as accept would grade nothing while looking reviewed |
| `Negotiation(rounds=...)`, `Critic(rounds=...)`, `Council(rounds=...)`, `Revise(cap=...)`, and `Hiring(max_hires=...)` may not be negative | pneuma (kernel) | `src/pneuma/team/hooks/negotiation.py:52-57`, `src/pneuma/team/hooks/review.py:94-99`, `219-223`, `src/pneuma/team/core.py:70-75`, `src/pneuma/team/hooks/hiring.py:303-307` | Reject at construction — `ValueError`; a negative value would silently behave as 0 |
| Two cast members may not collide on the lead's wire name (dots map to underscores) | pneuma (kernel) | `src/pneuma/team/core.py:415-437` | Reject at construction — `RuntimeError`; two tools sharing a name shadow silently |
| Every member's briefing failing is not recoverable; the lead never runs | pneuma (kernel) | `src/pneuma/team/hooks/briefing.py:113-133` | Reject — `RuntimeError`; a single failure is rendered as text instead |
| A `Member`'s capability must take at least one positional parameter, or a request has nowhere to go | pneuma (kernel) | `src/pneuma/team/members.py:105-124` | Reject — `RuntimeError` |
| A member that already carries a `config_hook` may not be equipped with a second — refused only when a hook actually needs the slot | pneuma (kernel) | `src/pneuma/team/members.py:133-154`, `src/pneuma/team/core.py:388-411` | Reject pre-spawn — `RuntimeError`; the runtime calls exactly one hook per cycle |
| A `Council` panel may not be empty, and its threshold must be in (0, 1] | pneuma (kernel) | `src/pneuma/team/hooks/review.py:207-218` | Reject at construction — `ValueError`; an empty panel would accept every answer vacuously |
| A `Learning` parameter must exist, be prose (not `Procedural`), and be trainable (not `Frozen`) | pneuma (kernel) | `src/pneuma/team/hooks/learning.py:98-117` | Reject at construction — `RuntimeError`; code is not advice, and a frozen target would report rounds and learn nothing |
| A `DynamicAgent`'s instructions may not be empty | pneuma (kernel) | `src/pneuma/team/members.py:211-217` | Reject — `ValueError` (caller path) |
| A hired role must exist in the catalog | pneuma (kernel) | `src/pneuma/team/hooks/hiring.py:165-166` | Return text — `"error: no such role ..."`, so the model retries |
| A hire's name must be unused | pneuma (kernel) | `src/pneuma/team/hooks/hiring.py:167-168`, `204-205` | Return text |
| A `hire_dynamic` call must supply non-empty instructions | pneuma (kernel) | `src/pneuma/team/hooks/hiring.py:199-203` | Return text |
| `delegate` and `dismiss` must name a hire that exists | pneuma (kernel) | `src/pneuma/team/hooks/hiring.py:237-241`, `258-260` | Return text |
| A posted discovery's `kind` must be one of the four `DISCOVERY_KINDS` | pneuma (kernel) | `src/pneuma/team/hooks/worklog.py:44`, `207-208` | Return text; the model picks a real kind and posts again |
| A post-condition's first parameter may not share a name with a propose parameter | pneuma (kernel) | `src/pneuma/gated.py:285-321` | Reject at wiring — `RuntimeError` |
| `propose_k` needs at least one branch | pneuma (kernel) | `src/pneuma/gated.py:395-396` | Reject — `ValueError` |
| A gate returning an awaitable on the sync path is a wiring fault, not a verdict | pneuma (kernel) | `src/pneuma/gated.py:193-204` | Reject — `AssertionError`; every coroutine is truthy and would otherwise be admitted unjudged |
| `Recalled(k=...)` must be at least 1; `k=None` means recall whole | pneuma (kernel) | `src/pneuma/recall.py:89-100` | Reject — `ValueError`; `k=0` retrieves nothing silently |
| One annotation may carry at most one distinct `Recalled` marker | pneuma (kernel) | `src/pneuma/recall.py:127-131` | Reject — `TypeError`; which store is read must not depend on metadata order |
| A recalled method's parameters may not shadow `queries` or `overrides` | pneuma (kernel) | `src/pneuma/recall.py:184`, `219-227` | Reject at wiring — `RuntimeError` |
| A recalled parameter may not be positional-only; the binder injects by keyword | pneuma (kernel) | `src/pneuma/recall.py:232-242` | Reject at wiring — `RuntimeError` |
| Positional arguments may not land on a recalled parameter | pneuma (kernel) | `src/pneuma/recall.py:312-347` | Reject — `RuntimeError` |
| Every supplied query must be usable: the name must be marked, not explicitly supplied, and not full-recall | pneuma (kernel) | `src/pneuma/recall.py:371-388` | Reject — `RuntimeError` |
| Every search-mode recalled parameter must have a query; there is no default | pneuma (kernel) | `src/pneuma/recall.py:389-397` | Reject — `RuntimeError`; a derived query retrieves confidently-ranked garbage and the call succeeds |
| `Discrimination` counts must be non-negative | pneuma.detect | `src/pneuma/detect/discrimination.py:72-77` | Reject — `ValueError` |
| A vacuity `sweep` limit must be at least 1 | pneuma.detect | `src/pneuma/detect/vacuity.py:225-226` | Reject — `ValueError` |
| An `audit`'s relaxation list must include `"exact"` | pneuma.detect | `src/pneuma/detect/vacuity.py:631-632` | Reject — `ValueError` |
| A system that cannot be stepped, or a rule that cannot be evaluated, is a modelling bug | pneuma.detect | `src/pneuma/detect/vacuity.py:195-201`, `254-257`, `273-276` | Reject — `SweepError` naming the state; swallowing it would silently shrink the state space |
| `probe` needs at least one declared domain | pneuma.detect | `src/pneuma/detect/objective.py:717-718` | Reject — `ValueError` |
| A sweep axis needs a resolution of at least 2 | pneuma.detect | `src/pneuma/detect/objective.py:469-470` | Reject — `ValueError` |
| `probe_feedback` needs at least two probe points to see conditional reporting | pneuma.detect | `src/pneuma/detect/objective.py:1781-1782` | Reject — `ValueError` |
| An objective that raises on a declared-feasible input is a finding | pneuma.detect | `src/pneuma/detect/objective.py:882-905` | Refuse — `raises-inside-the-domain`; `raise_if_pathological` then blocks training |
| An objective scoring non-finite on a feasible input is a finding | pneuma.detect | `src/pneuma/detect/objective.py:907-925` | Refuse — `non-finite-value`; `max` over `nan` is order-dependent |
| `probe_gate_fitting`'s `edge_fraction` must be in (0, 0.5) and `budget` at least 1 | pneuma.detect | `src/pneuma/detect/gaming.py:202-205` | Reject — `ValueError` |
| `probe_duplicate_mechanisms`' `threshold` must be in (0, 1] and `budget` at least 1 | pneuma.detect | `src/pneuma/detect/gaming.py:374-377` | Reject — `ValueError` |
| `verdict_for` must name an invariant the process actually declares | pneuma.detect | `src/pneuma/detect/adapter.py:227` | Reject — `ValueError` |
| A `TursoMemoryBackend` needs either a path or a connection | pneuma.memory | `src/pneuma/memory/turso_backend.py:418` | Reject — `ValueError` |
| Entry operations are only valid on list parameters | pneuma.memory | `src/pneuma/memory/turso_backend.py:522-526` | Reject — `TypeError` |
| `numeric_value` is only valid on a numeric parameter | pneuma.memory | `src/pneuma/memory/turso_backend.py:824-827` | Reject — `TypeError` |
| `search_entries` requires `k >= 1` | pneuma.memory | `src/pneuma/memory/turso_backend.py:671-672` | Reject — `ValueError` |
| `calibrate_ceiling`'s `margin` must be in [0, 1] | pneuma.memory | `src/pneuma/memory/turso_backend.py:804-805` | Reject — `ValueError` |
| A required parameter (no schema default) cannot be deleted | pneuma.memory | `src/pneuma/memory/turso_backend.py:1116-1122` | Reject — `ValueError` |
| An embedding `input_type` must be `search_document` or `search_query` | pneuma.memory | `src/pneuma/memory/embedding.py:182-183` | Reject — `ValueError` |
| The provider must return one vector per input text | pneuma.memory | `src/pneuma/memory/embedding.py:198-202` | Reject — `RuntimeError`; a truncated batch would misalign every entry's vector |
| A returned vector's dimension must match the embedder's declared dimension | pneuma.memory | `src/pneuma/memory/embedding.py:256-261` | Reject at write time — `RuntimeError`, where the cause is still visible |
| A mined `Discovered` model's activities must all be reachable from `start_activity` | pneuma.casestudy | `src/pneuma/casestudy/aimine.py:145-161` | Refuse as post-condition — `AssertionError` re-asks the model rather than silently pruning |
| A reported `threshold_used` may not exceed the support of the weakest edge kept | pneuma.casestudy | `src/pneuma/casestudy/aimine.py:164-183` | Refuse as post-condition — `AssertionError` |
| A parsed XES log must produce at least one event | pneuma.casestudy | `src/pneuma/casestudy/eventlog.py:92-93` | Reject — `ValueError` |
| Transcript loading validates `granularity`, `min_trace_length >= 1`, `min_activity_cases >= 1`, `max_cases >= 1 or None`, and `sampling in ("longest","random")` | pneuma.casestudy | `src/pneuma/casestudy/transcriptlog.py:304-316` | Reject — `ValueError` each |
| Transcript rows must carry `session_id`, `ts`, and `tool_name`, and must be a non-empty JSON array | pneuma.casestudy | `src/pneuma/casestudy/transcriptlog.py:187-193`, `397-402` | Reject — `ValueError` / `RuntimeError` |
| At least one timestamp must parse, and filtering must not empty the log | pneuma.casestudy | `src/pneuma/casestudy/transcriptlog.py:357-361`, `450` | Reject — `ValueError` |
| A component term with no compiling model raises rather than reporting zero | pneuma.casestudy | `src/pneuma/casestudy/harnesslearn.py:246-251`, `src/pneuma/casestudy/minelearn.py:688-693` | Reject — `ValueError`; a zero would make a dead term look like it moved |
| A war-room verdict's mechanism must be in the allowed vocabulary | pneuma.demo | `src/pneuma/demo/warroom.py:135-139` | Refuse as post-condition — `AssertionError` becomes the model's next prompt |
| A verdict must cite evidence from at least 3 planes | pneuma.demo | `src/pneuma/demo/warroom.py:140-144` | Refuse as post-condition — no single plane can identify the cause |
| A verdict's `ruled_out` may not be empty | pneuma.demo | `src/pneuma/demo/warroom.py:145-149` | Refuse as post-condition |
| A verdict must match the planted ground truth on service, change id, and mechanism | pneuma.demo | `src/pneuma/demo/warroom.py:150-158`, `src/pneuma/demo/incident.py:1301-1331` | Refuse as post-condition; tolerant on formatting, strict on substance |
| A hire may not be logged before it is registered on the roster | pneuma.demo | `src/pneuma/demo/staffing.py:53-63` | Reject — `RuntimeError`; the mandate would have nowhere to land |
| The upstream `hire` tool description must still contain its anchor phrase | pneuma.demo | `src/pneuma/demo/staffing.py:127-135` | Reject — `RuntimeError`; a silent no-op would drop every role's purpose from the prompt |

## Invariants

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The agent is an untrusted oracle: only a transition in the currently enabled set is ever executed | Application code — `interpreter._elicit` filters proposals and re-asks up to `max_rejections + 1` times | `src/pneuma/process/interpreter.py:6-11`, `342-352` |
| Process invariants are re-checked after every executed step, not only by the model checker | Application code — `_assert_invariants` raises `InvariantViolated` | `src/pneuma/process/interpreter.py:295`, `355-358` |
| A non-terminal state with no enabled transition halts the run rather than stalling | Application code — `Deadlock` | `src/pneuma/process/interpreter.py:256-258` |
| A run that circles without reaching a new state halts and *names the limit it hit*, so it cannot be mistaken for an exhausted budget | Application code — `NoProgress` after `DEFAULT_MAX_REVISITS = 5` consecutive revisits | `src/pneuma/process/interpreter.py:74-81`, `107-131`, `302-303` |
| A case that completes on its final budgeted step is reported as completed, not as over budget | Application code — terminal re-check after the loop | `src/pneuma/process/interpreter.py:308-317` |
| A state with exactly one enabled transition is stepped through without a model call | Application code — `_elicit` short circuit | `src/pneuma/process/interpreter.py:338-339` |
| An invariant violation outranks dithering: the invariant check runs before the revisit halt | Application code — ordering inside `interpreter.run` | `src/pneuma/process/interpreter.py:295-302` |
| No per-state work is spent on a visit the run has just declared progress-free | Application code — `on_enter` placement after the `NoProgress` raise | `src/pneuma/process/interpreter.py:203-207`, `302-308` |
| A relaxation order in which the sound widening (`free_initial`) precedes the unsound one (`free_guards`) — reordering inverts the gate | Application code — module constant plus the diagnosis in `adapter.py` | `src/pneuma/detect/vacuity.py:19-28`, `58-61` |
| `discriminates` is three-valued and must never collapse to a boolean: a search that gave up is not evidence of safety | Application code — `Discrimination.discriminates` | `src/pneuma/detect/discrimination.py:12-20`, `79-86` |
| Zero observations with no withheld reason is a *finding*, not an abstention; a caller's own bound must be named in `withheld` | Application code | `src/pneuma/detect/discrimination.py:26-30` |
| A rule rescued only by freeing initial values loses its pass; only `exact` and `free_guards` produce witnesses | Application code — `RuleVerdict.witnesses` | `src/pneuma/detect/vacuity.py:384-399` |
| A truncated relaxed sweep is tracked separately from a truncated exact sweep, so a guarded rule is not read as decoration | Application code — `relaxation_truncated` | `src/pneuma/detect/vacuity.py:30-37`, `355-368` |
| A rule whose search was truncated at any level reports zero witnesses, withdrawing a checker's pass — deliberately stricter than `vacuous`, disagreeing in the safe direction | Application code — `Audit.witness_counts` | `src/pneuma/detect/vacuity.py:578-591` |
| Seeding the start set is itself budgeted; a cap applied only to expansion would be a cap that lies | Application code — `sweep` | `src/pneuma/detect/vacuity.py:212-217`, `234-243` |
| `contradictory` returning `None` means "not provably empty here", never "satisfiable" | Application code — documented contract | `src/pneuma/detect/vacuity.py:699-701` |
| A found exploit settles a gate-fitting verdict even under a budget, with `withheld` cleared — truncation cannot fake a positive witness | Application code — `GateFitting.discrimination` | `src/pneuma/detect/gaming.py:16-22`, `138-146` |
| Bands are fractions of observed spans, never absolute scores; a zero span is reported as withheld rather than treated as a band | Application code | `src/pneuma/detect/gaming.py:24-29`, `240-249` |
| `Space` is required and never defaulted, because a boundary check in metric space fires on sound and broken objectives alike | Application code — required keyword | `src/pneuma/detect/objective.py:13-17`, `673`, `690-691` |
| Out-of-domain inputs are neither clamped nor refused; clamping hides that the measurement was wrong | Application code — documented contract | `src/pneuma/detect/objective.py:19-25` |
| Degenerate inputs are computed from a declared `Structure`, never trusted from a hand-written list written by the same hand as the formula | Application code — `_enumerate_degenerate` plus notes when `structure` is absent | `src/pneuma/detect/objective.py:27-31`, `692-695`, `768-773` |
| A searcher's candidates are always re-scored locally; a searcher's claim about its own candidate is never the evidence | Application code — `_check_degenerate` | `src/pneuma/detect/objective.py:787-792`, `1119-1122` |
| The prober checks that feedback *states* the score, and says so — whether its advice points uphill is explicitly not decidable and is not faked | Application code — documented non-goal | `src/pneuma/detect/objective.py:1762-1771` |
| A proposer's own claim never upholds a candidate: a panel of 3 needs 2 votes, and the arithmetic is checked separately | Application code — `Judged.upheld` | `src/pneuma/detect/adversary.py:150-155`, `167-183` |
| Proposal and adjudication are separate phases; an adversary never sees another's candidates or any ballot | Application code — `_run` two-phase fan-out | `src/pneuma/detect/adversary.py:440-444` |
| A gate *fault* is never recorded or reported as a rejection; the ledger stays readable as refusals and nothing else | Application code — `admits`, `judge`, `_record`, `_fault_text` | `src/pneuma/gated.py:134-140`, `184-205`, `240-264` |
| `propose_k` takes one shot per branch and filters, so `k` is the beam width rather than a count of branches that eventually succeeded | Application code | `src/pneuma/gated.py:356-362`, `408-420` |
| Every spawned thread is retired on the unwind path, even when the gate itself raised | Application code — `finally` | `src/pneuma/gated.py:421-423` |
| Retrieval runs under `no_thread_scope()` so the recall event lands on the thread the gradient comes from — one logical recall, one event | Application code — `Recall.trace` | `src/pneuma/recall.py:292-307` |
| Retrieval errors are deliberately *not* fault-wrapped, unlike gate errors: there is no model waiting and no attempts to burn | Application code — documented asymmetry | `src/pneuma/recall.py:172-178` |
| A hire reserves its name and its headcount before it awaits anything, rolling back if the spawn raises | Application code — `commission` runs synchronously up to the first suspension | `src/pneuma/team/hooks/hiring.py:133-153` |
| A worklog post appends to the log before any delivery is awaited, so two concurrent posts cannot drop one | Application code — `Worklog.post` | `src/pneuma/team/hooks/worklog.py:113-132` |
| One failing worklog channel never stops the rest; the failure is recorded on the entry | Application code — `_deliver` | `src/pneuma/team/hooks/worklog.py:134-142` |
| A worklog poster is excluded from its own fan-out | Application code — `post` | `src/pneuma/team/hooks/worklog.py:128-131` |
| `register` replays every prior entry into a newly opened channel, so registration order does not matter | Application code | `src/pneuma/team/hooks/worklog.py:96-111` |
| The roster and the worklog's channel map live for exactly one run, keyed by workspace identity, so a second run cannot inherit the first's hires or fan into its retired threads | Application code — `Hiring.roster`, `Worklog._reset_if_new_run` | `src/pneuma/team/hooks/hiring.py:324-332`, `src/pneuma/team/hooks/worklog.py:74-83` |
| Every member, every hire, and the lead are retired even when the run raised, and a teardown hook's own raise cannot stop the unwind | Application code — `finally` in `Team.run` plus `Hiring.on_teardown` | `src/pneuma/team/core.py:263-283`, `src/pneuma/team/hooks/hiring.py:351-358` |
| `dismiss` retires before unregistering, so a failed retire leaves the thread reachable to a retry | Application code | `src/pneuma/team/hooks/hiring.py:257-273` |
| The briefing barrier waits for every member, so the lead's evidence does not depend on scheduling | Application code — `asyncio.gather(..., return_exceptions=True)` | `src/pneuma/team/hooks/briefing.py:72-96` |
| A team with no members accepts negotiation-silent rather than recording a vacuous unanimity | Application code | `src/pneuma/team/hooks/negotiation.py:107-109` |
| The core recomposes a lead's own `config_hook` and `tools=` into the one hook it installs, so a lead that carried either loses nothing | Application code — `_lead_hook` | `src/pneuma/team/core.py:326-355` |
| An errored, empty, or never-spawned reviewer never settles `Accept`; its failure counts against the answer | Application code — the review-integrity rule | `src/pneuma/team/hooks/review.py:52-67`, `138-159`, `262-264` |
| `retire` is idempotent against both the wrapper and the runtime, so an unwind loop cannot crash mid-unwind and leave the rest alive | Application code — `MethodThread.retire` suppresses `ThreadNotFoundError` | `src/pneuma/method.py:292-307` |
| Type hints are resolved with `include_extras=True`, or `Procedural` markers flatten to plain `str` and code silently becomes an ordinary prompt argument | Application code — `compile_ai_method` | `src/pneuma/method.py:141-148` |
| A compiled tool's name identifies the capability, not just the agent, so two `@ai_method`s cannot shadow each other | Application code | `src/pneuma/method.py:150-157` |
| A TLC pass must survive every gate at once: clean exit, success line, no violation, no error, and states actually explored | Application code — `CheckResult.ok` derived from `outcome` | `src/pneuma/process/tla.py:56-60`, `74-76`, `369-388` |
| A checker that broke reports neither "holds" nor "violated"; a harness failure outranks a violation so the process is not blamed | Application code — `_parse` ladder | `src/pneuma/process/tla.py:371-377` |
| Zero distinct or zero initial states is `vacuous`, not `verified`: green there means untested | Application code | `src/pneuma/process/tla.py:383-386` |
| A verified result is downgraded to `vacuous` when any invariant has zero witnesses | Application code — `with_witnesses` | `src/pneuma/process/tla.py:90-101` |
| Nondeterministic variables render as `\in domain` rather than `=`, so guarded branches are actually reached and no invariant passes vacuously | Application code — `render` | `src/pneuma/process/ir.py:91-100`, `src/pneuma/process/tla.py:154-165` |
| Liveness checking is opt-in, because a mined rework loop is legitimately non-terminating and safety-verified at once | Application code | `src/pneuma/process/tla.py:270-288` |
| A safety violation masks liveness: only a `verified` outcome carries a termination claim | Application code — documented | `src/pneuma/process/tla.py:290-295` |
| `HandlerFailed` is deliberately not a `ProcessError`, so a code bug cannot be laundered into an experimental "blocked" result | Application code | `src/pneuma/process/agent.py:71-80` |
| A handler that raises stops the run rather than producing a completed case whose work never happened | Application code — `dispatch` re-raises as `HandlerFailed` naming state, method, and part | `src/pneuma/process/agent.py:228-263`, `390-398` |
| An unrecognised `agent_method` resolves to `None` rather than raising, because every mined state carries the placeholder `handle` | Application code — `handler_for` | `src/pneuma/process/agent.py:169-173` |
| Every state walked is declared, every variable stays in its domain, and no process invariant is violated — over random Hypothesis paths | Application code — three `@invariant` methods driving the real interpreter | `src/pneuma/process/properties.py:101-117` |
| Entry ids come from a persisted monotonic counter and are never reused, so an id recorded in a forward pass still names the same entry at consolidation | Application code + DB constraint (`memory_counter` PK) | `src/pneuma/memory/turso_backend.py:35-39`, `102-107`, `552-570` |
| Never construct a bare cursor: a GC'd unfinalized SELECT silently discards pending writes in pyturso 0.7.2 and `commit()` still reports success | Application code — all reads go through `fetch_rows` / `fetch_one` | `src/pneuma/memory/turso_backend.py:12-18`, `src/pneuma/memory/embedding.py:70-108` |
| `distance_ceiling` stays off by default; an embedding backend fails soft and every smoke test passes with a constant embedder | Application code — documented default | `src/pneuma/memory/turso_backend.py:20-28` |
| An entry's digest moves with its text, self-invalidating the embedding cache; a cache keyed by entry id would serve a stale vector invisibly | Application code + DB constraint (cache keyed by digest) | `src/pneuma/memory/turso_backend.py:601-620`, `src/pneuma/memory/embedding.py:53-63` |
| Retrieval quality is measured, not assumed: `probe_retrieval` ignores `distance_ceiling` because the ceiling is derived from that measurement | Application code | `src/pneuma/memory/turso_backend.py:744-748` |
| A rounded numeric proposal is clamped *after* rounding, never before, so it cannot step back outside an exclusive bound | Application code — `_numeric_update` | `src/pneuma/memory/turso_backend.py:922-931` |
| No score channel means no write: rewriting a number from text alone would be invention a loop cannot distinguish from learning | Application code — `_consolidate` early return | `src/pneuma/memory/turso_backend.py:1054-1060` |
| A gradient can never leave unparseable code in the store; a `Procedural` rewrite passes through a post-condition that re-parses it | Application code — `_check_valid_python` | `src/pneuma/memory/turso_backend.py:130-137`, `1070-1078` |
| Consolidation re-reads entry values from the store rather than from the retrieval snapshot, because an earlier consolidation in the same round may have rewritten them | Application code | `src/pneuma/memory/turso_backend.py:1095-1107` |
| A derived rule that is declined is reported, never dropped in silence — a rule that vanishes silently is indistinguishable from one applied | Application code — `RuleNotEnforced` warning plus `Governed.skipped` | `src/pneuma/casestudy/rules.py:34-35`, `56-63`, `241-248` |
| A precedence whose prerequisite is the initial state is never enforced: the flag is still 0 on the opening move, so the invariant would fire on a correct process | Application code — `enforce` guard | `src/pneuma/casestudy/rules.py:167-172`, `195-196` |
| A duplicate compiled invariant name is refused, because the IR checks state/variable/transition uniqueness but not invariant names, and TLC would report a parse failure that reads like a counterexample | Application code | `src/pneuma/casestudy/rules.py:174-180`, `197-198` |
| The `edge_share` clamp is load-bearing: without it selectivity goes negative and the harmonic mean gains a pole where a garbage model scores 319.386 and is selected as best | Application code — clamped in both `Attempt.score` and `weighted_score` | `src/pneuma/casestudy/minelearn.py:339-346`, `352`, `src/pneuma/casestudy/harnesslearn.py:127-133`, `135` |
| Both halves of selectivity are measured on the same population, or the numerator can exceed 1 and the denominator overstates selectivity | Application code — `score_edges` | `src/pneuma/casestudy/minelearn.py:222-231` |
| An attempt with invented handoffs can never be the best round, whatever it scores | Application code — `Training.best` excludes them before ranking | `src/pneuma/casestudy/minelearn.py:374-385` |
| The mining threshold search window and sweep resolution are not schema fields, so no gradient can reach them — a coarser grid would make the gate blind | Application code — `HarnessKnobs` allowlist by omission | `src/pneuma/casestudy/harnesslearn.py:87-96`, `492-496` |
| A candidate harness weight is judged, never clamped: silent correction would hide exactly the proposal the gate exists to reject | Application code — `compose` and `admit` both leave it unvalidated | `src/pneuma/casestudy/harnesslearn.py:186-188`, `490-492` |
| Three bootstrap toolkit helpers must keep existing; a rewrite deleting one fails rehearsal and is rolled back | Application code — `missing_bootstrap` plus a probe whose first statement raises | `src/pneuma/casestudy/toolkit.py:343-357`, `385-391` |
| Rehearsal is deliberately non-defensive: wrapping each call would produce a probe that always succeeds | Application code | `src/pneuma/casestudy/toolkit.py:375-379` |
| Learned guidance is advice, never a rule: rules live in the verified IR, so no optimizer rewrite can widen what the runtime permits | Application code — `Playbook` docstring and the IR/interpreter split | `src/pneuma/casestudy/learning.py:98-103` |
| A mined process always has a terminal state; when every kept activity leads somewhere, the observed last activity becomes terminal | Application code — `mine` fallback, since the IR would otherwise reject the model | `src/pneuma/casestudy/miner.py:114-119` |
| The incident dataset's structural invariants are proven at import time: no single plane and no pair of planes resolves the mechanism, cause precedes effect, and the oracle accepts the truth and rejects six near-misses | Application code — `self_check()` called at module scope | `src/pneuma/demo/incident.py:1334-1397` |
| A bare run's `TeamRun` serialises to one key: empty `transcript` and `hooks_data` drop rather than emitting empty containers, and the demo's `investigation.json` keeps its published aliases | Application code — `model_serializer` plus serialization aliases | `src/pneuma/team/core.py:163-170`, `src/pneuma/demo/warroom.py:46-79` |
| A role factory closes over its class in its own scope, so every hire is not silently the last class the loop saw | Application code — `_factory` | `src/pneuma/demo/staffing.py:95-102` |
| The library layer never imports the application layer, even inside a function body, and never imports `polars`, `libsql`, or `pm4py` | Test-enforced boundary | `README.md:206-212` |

## Calculations

| Calculation | Inputs | Output | Citation |
| --- | --- | --- | --- |
| Initial assignments — cross product over free variables | `Process.variables` (each fixed or nondeterministic) | list of starting assignments | `src/pneuma/process/ir.py:285-296` |
| Unreachable states — topological reachability ignoring guards | `initial_state`, `transitions` | set of state names | `src/pneuma/process/ir.py:301-314` |
| Rejection count for a run | `Run.steps[].rejected` | integer sum | `src/pneuma/process/interpreter.py:171-173` |
| Rule witnesses | per-relaxation breach counts | `max(exact, free_guards)` | `src/pneuma/detect/vacuity.py:384-399` |
| Vacuity cause | violating count, both truncation flags, per-relaxation counts, antecedent count | one of six named causes, or `None` | `src/pneuma/detect/vacuity.py:458-474` |
| Retrieval separation margin | mean relevant distance, mean control distance | `control - relevant`, or `None` | `src/pneuma/memory/turso_backend.py:275-286` |
| Next numeric parameter value | current value, this round's score, observation history, schema bounds | clamped proposal | `src/pneuma/memory/turso_backend.py:867-932` |
| Schema-derived numeric search domain | `Ge`/`Gt`/`Le`/`Lt` field metadata | `(low, high)` with exclusive bounds pulled inward | `src/pneuma/memory/turso_backend.py:483-518` |
| Component discrimination over a swept grid | term values at every finite swept point, `floor` | observations, separating count, withheld reasons | `src/pneuma/detect/objective.py:1450-1493` |
| Grid axis values | `Domain.low`/`high`, resolution, `integral` | sample list, deduped in order for integer axes | `src/pneuma/detect/objective.py:468-482` |
| Gate-fitting bands | scored gate/held-out min and max, `edge_fraction` | `gate_top`, `held_floor`, then exploit/contained partition | `src/pneuma/detect/gaming.py:232-260` |
| Token-set Jaccard similarity | two texts | lowercased token-set intersection over union; two empty sets score 1.0 | `src/pneuma/detect/gaming.py:277-288` |
| Ballot rejection rate | all cast ballots | share saying "not worthless", or `None` with no ballots | `src/pneuma/detect/adversary.py:218-229` |
| Negotiation unanimity | approvals, objections | `len(approved) == len(objections)`; else `revised` or `cap_reached` | `src/pneuma/team/hooks/negotiation.py:134-149` |
| Approval detection | a member's answer text | containment of `APPROVED`, with an `error: ` prefix vetoing | `src/pneuma/team/hooks/negotiation.py:76-78`, `src/pneuma/team/hooks/review.py:247-249` |
| Council approval fraction | approvals, full panel size | `len(approved) / len(panel) >= threshold`, denominator never shrunk by errors | `src/pneuma/team/hooks/review.py:261-264` |
| Directly-follows relation | events sorted by case and position | activity pairs with event count and distinct case count | `src/pneuma/casestudy/miner.py:42-56` |
| Model conformance | events, mined process, activity→state map | share of cases replayable end to end, 4 dp | `src/pneuma/casestudy/miner.py:164-199` |
| Dropped-edge share | kept and dropped edge counts | fraction of total event volume dropped | `src/pneuma/casestudy/miner.py:36-39` |
| Edge audit shares | kept, invented, visible handoff counts | `edge_share`, `invented_share`, 4 dp | `src/pneuma/casestudy/minelearn.py:206-215` |
| Mining attempt score | coverage, `edge_share`, `invented_share` | penalised harmonic mean, 4 dp | `src/pneuma/casestudy/minelearn.py:327-357` |
| Weighted mining score | coverage, `edge_share`, `invented_share`, `weight` | weighted harmonic mean, 4 dp | `src/pneuma/casestudy/harnesslearn.py:115-139` |
| Harness admission quality | emptying margin, live-rule share | mean of the two shares, 0.0 when rejected, 4 dp | `src/pneuma/casestudy/harnesslearn.py:421-450` |
| Waiting-time statistics per activity | event timestamps within a case | median, p95, and total wait hours | `src/pneuma/casestudy/miner.py:225-237` |

Four of these deserve the formula spelled out.

**Mining attempt score** (`src/pneuma/casestudy/minelearn.py:327-357`). Clamp `edge_share` into `[0, 1]` and let selectivity be `1 - edge_share`. The honest score is the unweighted harmonic mean of coverage and selectivity, `2 * coverage * selectivity / (coverage + selectivity)`, or zero when the denominator is non-positive. Then clamp `invented_share` into `[0, 1]` and return `round(honest * (1 - invented) - invented, 4)`. The two clamps and the subtraction are all deliberate: coverage alone has a degenerate optimum (keep every handoff, replay the log perfectly, describe no process), selectivity punishes memorisation, and inventing is graded strictly below memorising, so an inventing model scores negative and can never win. The `edge_share` clamp is the one that must never be dropped in a re-derivation — see the corresponding Invariants row.

**Weighted mining score** (`src/pneuma/casestudy/harnesslearn.py:115-139`). The same shape with the component weight exposed: selectivity as above, then `coverage * selectivity / (weight * selectivity + (1 - weight) * coverage)`, with the same invention penalty applied. At `weight = 0.5` this is algebraically identical to the unweighted mean above, which is the point — the seed harness is the shipped harness, so every measured movement is against the real thing (`src/pneuma/casestudy/harnesslearn.py:118-126`).

**Next numeric parameter value** (`src/pneuma/memory/turso_backend.py:867-932`). A deterministic one-dimensional search with a shrinking trust region over the schema's own declared domain. Read `(low, high)` from the field's constraint metadata and let `span` be `high - low`, falling back to `max(abs(current), 1.0)` when unbounded. Average every recorded score per previously tried value. If some tried value averaged better than the score just measured, *exploit*: step halfway toward it, which needs no derivative and cannot overshoot. Otherwise *explore*: step `span * 0.25 * (1 - score) * 0.6 ** trials` away from the worst value tried, defaulting to upward. Each factor is load-bearing — `1 - score` makes a well-served value barely move, `0.6 ** trials` makes the sequence converge instead of oscillating, and the `0.25` trust fraction is there because omitting it was a measured bug: a `Field(20, ge=1, le=100)` scoring 0.2 proposed 99 on the first round, which is a jump to the boundary rather than a search. Round to an integer for integer fields, then clamp — in that order (`src/pneuma/memory/turso_backend.py:922-931`). A perfect score does not move the value at all; a constant score converges. Constants at `src/pneuma/memory/turso_backend.py:1222-1233`.

**Vacuity audit** (`src/pneuma/detect/vacuity.py:605-682`). A multi-step pipeline rather than a single expression. For each relaxation level in the fixed order `exact, free_initial, free_guards, free_both`, build a system at that level and breadth-first sweep it, counting per rule both the states in the rule's *scope* and the states that *break* it, up to `limit` states with a shortest witness trace recorded on the first breach. A rule that broke is resolved and drops out; a rule that did not break in a completed sweep stays pending for the next, looser level; a rule that did not break in a *truncated* relaxed sweep is marked abandoned, because it has lost every level after this one — including the `free_guards` sweep that is the only thing that could earn its pass. Once no rule is pending, remaining levels are not built at all, which keeps the common case a single sweep. Each rule's verdict then reads its counts from the `exact` sweep, carries the per-level breach counts, and takes `relaxation_truncated` from the abandoned set (`src/pneuma/detect/vacuity.py:640-682`). Witness counting and the cause ladder are the two table rows above.

## Policy and gates

There is no authentication or authorization layer in this codebase. The gates below are budget caps, structural allowlists, and detector approvals — the things that decide what a model or a caller may do.

- **Hiring headcount cap:** a lead may hold at most `max_hires` subagents at once, and the cap is checked before the recruit is built so a rejected hire spawns nothing. The `Hiring` hook defaults to 3 and the standalone `hiring_tools` seam defaults to 4, so a caller wiring the seam directly gets a looser budget than the hook grants. `src/pneuma/team/hooks/hiring.py:169-174`, `206-207`, `301`, `70`.
- **Reserve-before-spawn:** the cap and the name are both spent synchronously before any await, because the runtime's tool executor is concurrent and two `hire` calls in one assistant turn would otherwise both pass the check. `src/pneuma/team/hooks/hiring.py:133-153`.
- **Catalog as the hiring allowlist:** a lead may only hire roles present in the catalog the caller supplied; a team without the `Hiring` hook grants no hiring tools at all. `src/pneuma/team/hooks/hiring.py:296-310`.
- **Inline agent synthesis is opt-in:** `Hiring(dynamic=...)` defaults to `False`, so `hire_dynamic` is absent from the wire entirely; when enabled, synthesized instructions are recorded verbatim in the roster log as the audit trail for a prompt nobody reviewed. `src/pneuma/team/hooks/hiring.py:275-279`, `209-219`.
- **Cross-member awareness is opt-in:** the `Worklog` hook is absent by default, and when added, delivery is step-boundary by construction — `notify` appends to a thread's log without starting a cycle, so a teammate is never interrupted mid-thought. `src/pneuma/team/hooks/worklog.py:1-28`.
- **Negotiation budget:** the `Negotiation` hook is absent by default, and its `rounds` becomes the cap on every `Revise` it returns, enforced by the core's answer loop; a round landing on the cap is marked `cap_reached` rather than implying consensus. `src/pneuma/team/hooks/negotiation.py:52-58`, `139-149`.
- **Discovery vocabulary:** a member may only post one of four discovery kinds, and the poster's identity is bound by the tool rather than reported by the model, so attribution cannot be spoofed. `src/pneuma/team/hooks/worklog.py:44-51`, `164-172`, `206-208`.
- **Review is opt-in and never silent:** the team layer has no built-in grading — an answer returns exactly as the lead produced it unless a `Critic` or `Council` hook was added, and those hooks may not accept on absent evidence (the review-integrity rule). `src/pneuma/team/core.py:149-161`, `src/pneuma/team/hooks/review.py:8-15`.
- **Gate-gated proposals:** a `GatedProposer`'s answer must be admitted by its gate before it counts, and a rejection's report text plus `REASK` is what the model is asked with next. `src/pneuma/gated.py:161-205`, `240-256`, `124`.
- **Step and rejection budgets on a process walk:** `max_steps=50` caps executed transitions, `max_rejections=3` caps illegal proposals per decision, and `max_revisits=5` caps consecutive no-progress revisits. `src/pneuma/process/interpreter.py:182-183`, `75-82`.
- **Detector-approved harness parameters:** `HarnessProposer` only accepts a coverage weight both detectors approve — the objective probe must find no refusal *and* the derived compliance rules must not regress below the seed. A rejected candidate scores exactly 0.0, so the search cannot be rewarded for approaching the refusing region. `src/pneuma/casestudy/harnesslearn.py:374-450`, `470-520`.
- **Refuse-to-train gate:** `Probe.raise_if_pathological()` raises `ObjectiveRefused` rather than starting a training loop against an objective with any refusal-severity finding. `src/pneuma/detect/objective.py:75-76`, `429-436`.
- **Paranoid re-run toggle:** `trust_declared_bounds=False` strips every `bounded_by` claim so findings refuse instead of warning, because declaring a bound on every axis passes an unclamped objective with seven warnings. `src/pneuma/detect/objective.py:23-25`, `719-730`.
- **Gating versus non-gating rules:** a vacuity `Rule` carries `gates`, and only gating rules appear in the witness counts a checker's pass is withdrawn on — an unfirable wellformedness property means the model is sound, not untested. `src/pneuma/detect/vacuity.py:100-110`, `578-591`.
- **Vacuous-rule policy is caller-chosen:** `on_vacuous` selects `warn` (attach and warn, the default), `refuse` (decline it), or `ignore` (attach silently, for tests that need the unprotected artifact). `src/pneuma/casestudy/rules.py:182-187`, `227-238`.
- **Support floor separates a control from a coincidence:** a derived precedence needs `min_support=100` co-occurring cases, and at most `max_rules=3` attach because each rule doubles TLC's work. `src/pneuma/casestudy/rules.py:99-104`, `319-320`.
- **Retrieval width:** the learning loop retrieves `TOP_K = 3` entries per decision, chosen because the live measurement had a query whose correct entry ranked second, and because it bounds how much advice one decision can be blamed for. `src/pneuma/casestudy/learning.py:79-86`.
- **Distance ceiling is off unless measured:** `distance_ceiling` defaults to unset, and `calibrate_ceiling` refuses to invent one when the relevant and control distance distributions overlap. `src/pneuma/memory/turso_backend.py:20-28`, `350-357`, `804-820`.
- **Parameter allowlist by omission:** exactly one harness field is learnable; the threshold window, sweep resolution, vacuity budget, rule support floor, and every finding's severity are absent from the schema rather than defended, because a gradient cannot reach a field that does not exist. `src/pneuma/casestudy/harnesslearn.py:87-96`.
- **Memory-internal agents get no coordinator tools:** every consolidation AI function is built with `coordinator_tools_enabled=False`, so memory machinery cannot discover or message threads. `src/pneuma/memory/turso_backend.py:124-128`.
- **Live-model tests are env-gated:** six environment variables (`PNEUMA_LIVE_KERNEL`, `PNEUMA_LIVE`, `PNEUMA_LIVE_HARNESS`, `PNEUMA_LIVE_MINE`, `PNEUMA_LIVE_EMBED`, `PNEUMA_LIVE_CACHE`) each unlock one group of tests that would otherwise call a real model; the default suite runs offline against scripted models. `README.md:249-259`.
- **Prompt caching is on by default and switchable:** `opus5()` attaches `CacheConfig(strategy="auto")` so byte-identical branch prefixes are billed at cache-read rates; `cache=False` turns it off. `src/pneuma/model.py:20`, `34-46`, `53`.

## See also

- [Module map][module-map] — 29 shared source files
- [Impact analysis][impact-analysis] — 27 shared source files
- [Processes][processes] — 26 shared source files
- [Contract map][contract-map] — 25 shared source files
- [Debugging guide][debugging-guide] — 23 shared source files

[module-map]: ../architecture/module-map.md
[impact-analysis]: impact-analysis.md
[processes]: ../behavior/processes.md
[contract-map]: contract-map.md
[debugging-guide]: debugging-guide.md
