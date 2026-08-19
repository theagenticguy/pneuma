# omnigent-blackboard-poc · Business logic

This file indexes the domain rules the kernel enforces — validations, invariants, calculations, and policy/gate logic — and where each lives. It answers: *"What rules does this codebase enforce, and where?"*

**Scope.** The kernel is a hexagonal blackboard coordinating an SDLC agent team. Business logic concentrates in two layers: the pure domain (`src/sdlc_blackboard/domain/`, stdlib + Pydantic only, no I/O — `src/sdlc_blackboard/domain/common.py:1`) and the application use cases (`src/sdlc_blackboard/application/use_cases/`). Both are in scope. Database-side invariants (partial unique indexes, foreign keys, CHECK constraints) are also surfaced here — despite the application-layer focus — because they are the kernel's last line of defense (double-claim, concurrent-command dedup, revision immutability) and shape application behavior directly; they are marked `Where enforced: DB constraint`. Field-shape constraints declared on Pydantic models (`extra="forbid"`, `frozen=True`, `NonEmptyStr` min/max length) are validations and are captured at the model level rather than per field. There is no UI layer and no server-side policy table (`migrations/20260717000002_seed_poc_policies.sql:2` — authorization is contract-driven in the application layer), so no UI-form validation is in scope.

Two rules are additionally pinned by a Lean 4 model under `formal/` (spec `001-routing-thrash`): the routing policy (`src/sdlc_blackboard/domain/routing.py:8`) and the thrash-report fold semantics (`src/sdlc_blackboard/application/use_cases/thrash_service.py:1`). These are noted where they appear; the Lean side and the Python side must move in lockstep.

Domains follow the aggregate boundaries: **Goal**, **Task**, **Artifact**, **Review**, **Finding**, **Approval**, **Runtime run**, **Command** (the cross-cutting idempotency/concurrency envelope), and **Routing / Thrash** (the derived cost-and-coordination observability lens).

## Validations

| Rule | Domain | Citation | Failure mode |
|---|---|---|---|
| Every domain model is frozen (immutable) and forbids extra fields; unknown keys are rejected at construction | All | `src/sdlc_blackboard/domain/common.py:22` | Pydantic `ValidationError` at parse (rejected before use case runs) |
| All free-text fields use `NonEmptyStr`: min length 1, max length 10,000 | All | `src/sdlc_blackboard/domain/common.py:16` | Pydantic `ValidationError` (reject empty / oversized string) |
| `CommandContext.expected_version` and `assignment_epoch` must be `>= 0` when supplied | Command | `src/sdlc_blackboard/domain/common.py:104` | Pydantic `ValidationError` |
| `create_task`: referenced goal must exist | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:62` | `NotFound` -> `not_found` status |
| `create_task`: reusing a `task_key` with a *different* contract is rejected (same contract replays idempotently) | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:68` | `Conflict` -> `conflict_created` status |
| `create_task`: every `dependency_task_id` must exist and belong to the same goal | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:72` | `PreconditionFailed` -> `precondition_failed` |
| `claim_task`: task must be in `READY` state to be claimed | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:116` | `PreconditionFailed` -> `precondition_failed` |
| `start_runtime_run`: task must be `ASSIGNED` or `AWAITING_INPUT` | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:162` | `PreconditionFailed` |
| `start_runtime_run`: an explicit `routing_class` string must name one of the four Bedrock routing classes (spec R5B) | Runtime run | `src/sdlc_blackboard/application/use_cases/task_service.py:171` | `ValidationFailed` (client input error, not infra fault) |
| `submit_task_result`: task must be `RUNNING` or `AWAITING_INPUT` | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:215` | `PreconditionFailed` |
| `submit_task_result`: referenced run must exist and belong to this task+epoch | Runtime run | `src/sdlc_blackboard/application/use_cases/task_service.py:220` | `NotFound` / `StaleAssignment` |
| `submit_task_result`: submitted `input_manifest` must equal the run's recorded manifest | Runtime run | `src/sdlc_blackboard/application/use_cases/task_service.py:222` | `InputManifestMismatch` (a `PreconditionFailed`) |
| `accept_task`: task must be `SUBMITTED` or `UNDER_REVIEW` (else refuses) | Task | `src/sdlc_blackboard/application/use_cases/task_service.py:335` | `PreconditionFailed` |
| `submit_review`: must bind at least one artifact revision | Review | `src/sdlc_blackboard/application/use_cases/review_service.py:125` | `PreconditionFailed` |
| `submit_review`: every listed `finding_id` must exist | Review | `src/sdlc_blackboard/application/use_cases/review_service.py:128` | `NotFound` |
| `record_human_approval`: must bind at least one artifact revision | Approval | `src/sdlc_blackboard/application/use_cases/review_service.py:186` | `PreconditionFailed` |
| `promote_artifact`: `new_revision_id` must reference an existing revision | Artifact | `src/sdlc_blackboard/application/use_cases/artifact_service.py:30` | `NotFound` |
| `promote_artifact`: when the alias already exists, `expected_current_revision_id` is required (compare-and-set; omitting it would be a last-writer-wins bypass) | Artifact | `src/sdlc_blackboard/application/use_cases/artifact_service.py:40` | `PreconditionFailed` |
| `runtime_runs.routing_class` (when non-null) must be one of the four Bedrock routing classes | Runtime run | `migrations/20260717000001_initial.sql:78` | DB CHECK constraint violation |

## Invariants

| Invariant | Where enforced | Citation |
|---|---|---|
| Task state changes are gated by an explicit transition matrix; any edge not listed is illegal, and self-loops are always illegal | Application code (pure fn) | `src/sdlc_blackboard/domain/transitions.py:49` |
| Terminal task states (`ACCEPTED`, `FAILED`, `CANCELLED`, `SUPERSEDED`) have no outbound transitions except SUPERSEDED bookkeeping | Application code | `src/sdlc_blackboard/domain/tasks.py:35`, `src/sdlc_blackboard/domain/transitions.py:35` |
| Every use-case state change asserts `can_transition` before writing (`_require_transition`) | Application code | `src/sdlc_blackboard/application/use_cases/task_service.py:422` |
| A task is created `READY` iff it has no dependencies, else `DRAFT` (held until deps accepted) | Application code | `src/sdlc_blackboard/application/use_cases/task_service.py:79` |
| A `DRAFT` task becomes `READY` only when every dependency task is `accepted` | Application + DB (set-based UPDATE) | `src/sdlc_blackboard/infrastructure/repositories/tasks.py:124` |
| Optimistic concurrency: every mutating state change is a compare-and-set on `version`; a lost race returns `StaleVersion` | Application + DB (`where version = $expected`) | `src/sdlc_blackboard/application/use_cases/task_service.py:198`, `src/sdlc_blackboard/infrastructure/repositories/tasks.py:165` |
| Assignment fencing: a worker's `assignment_epoch` must match the task's current epoch, else the worker has lost authority | Application code (`_require_epoch`) | `src/sdlc_blackboard/application/use_cases/task_service.py:411` |
| At most one active assignment per task (the final defense against double-claim) | DB constraint (partial unique index) | `migrations/20260717000001_initial.sql:57` |
| Idempotency: a command runs at most once per `command_id`; a reused `command_id` with a different payload is rejected without executing | Application code | `src/sdlc_blackboard/application/idempotency.py:42` |
| A mutation and its idempotency record commit or roll back together: the `DomainError` catch sits OUTSIDE `uow.begin()`, so a raised domain error unwinds the context manager and rolls back (never falls through to a partial commit) | Application code | `src/sdlc_blackboard/application/use_cases/base.py:53` |
| The processed-command record is written in the same transaction as the mutation; concurrent reuse of a `command_id` that loses the dedup INSERT is a retryable `ConcurrentCommandConflict` | Application + DB (unique PK on `command_id`) | `src/sdlc_blackboard/infrastructure/repositories/idempotency.py:59` |
| The command-failure ledger is append-only: every failed attempt is one row, never deduped, and the table carries NO foreign key to goals/tasks (a failure may reference a not-yet-existing or deleted aggregate) | Application + DB constraint (no FK) | `src/sdlc_blackboard/infrastructure/repositories/failures.py:23`, `migrations/20260721000001_command_failures.sql:12` |
| A task's `(goal_id, task_key)` is unique | DB constraint | `migrations/20260717000001_initial.sql:32` |
| An artifact revision is immutable and uniquely keyed by `(artifact_id, content_hash)`; a resubmitted identical hash reuses the existing revision | Application + DB constraint (unique) | `src/sdlc_blackboard/application/use_cases/task_service.py:228`, `migrations/20260717000001_initial.sql:103` |
| The artifact alias is the only mutable pointer; it is promoted by compare-and-set and is one row per `(goal_id, logical_name)` | Application + DB (PK, CAS UPDATE) | `src/sdlc_blackboard/application/use_cases/artifact_service.py:46`, `src/sdlc_blackboard/infrastructure/repositories/artifacts.py:114`, `migrations/20260717000001_initial.sql:115` |
| On alias promotion, every review and approval bound to a *superseded* revision of that artifact is invalidated (stale/revoked) in the same transaction | Application + DB (set-based UPDATE) | `src/sdlc_blackboard/application/use_cases/artifact_service.py:57`, `src/sdlc_blackboard/infrastructure/repositories/quality.py:214` |
| A review is unique by `(review_task_id, review_type, binding_fingerprint, reviewer.actor_id)` | DB constraint (unique index) | `migrations/20260717000001_initial.sql:161` |
| A review's identity is order-independent over its bindings (sorted triples, then SHA-256) | Application code (pure fn) | `src/sdlc_blackboard/domain/reviews.py:32` |
| An approved review cannot carry an unresolved (`OPEN`/`ACKNOWLEDGED`) blocking finding it listed | Application code | `src/sdlc_blackboard/application/use_cases/review_service.py:131` |
| A finding assertion is immutable; only its resolution `state` is versioned via CAS | Application + DB (CAS UPDATE) | `src/sdlc_blackboard/domain/findings.py:1`, `src/sdlc_blackboard/infrastructure/repositories/quality.py:109` |
| The routing policy is total over `ActorKind`: every one of the 18 kinds has a default routing class, so `default_routing_class` never raises; the Python table is transcribed constructor-for-constructor from the Lean model and pinned by a contract test | Application code (pure fn) + Lean model | `src/sdlc_blackboard/domain/routing.py:32`, `tests/contract/test_routing_policy.py` |
| Each thrash counter is a fold over one goal's history: zero on an empty/unknown goal (T2), monotonically nondecreasing as history grows (T5), and computed only from that goal's own signals (T1 frame); the report takes no row locks | Application code (read-only, own UoW) + Lean model | `src/sdlc_blackboard/application/use_cases/thrash_service.py:39` |
| A task may not depend on itself | DB constraint (CHECK `no_self_dependency`) | `migrations/20260717000001_initial.sql:42` |
| All child rows cascade-delete with their goal; artifact revisions / findings / reviews reference tasks | DB constraint (FK `on delete cascade`) | `migrations/20260717000001_initial.sql:19` |

## Calculations

| Calculation | Inputs | Output | Citation |
|---|---|---|---|
| Release-gate status (derived read, never a stored boolean) | goal's tasks, artifact aliases, open blocking findings, reviews, approvals | `SATISFIED` / `HUMAN_REQUIRED` / `UNSATISFIED` `GateResult` | `src/sdlc_blackboard/application/use_cases/gate_service.py:52` |
| Required review types for a goal | the goal's task contracts' blocking `ReviewRequirement`s | ordered tuple of review-type strings (fallback `(quality, security)`) | `src/sdlc_blackboard/application/use_cases/gate_service.py:134` |
| Implementation artifact the gate governs | the goal's task contracts | logical name of the deliverable of the task with the most blocking reviews | `src/sdlc_blackboard/application/use_cases/gate_service.py:148` |
| Binding fingerprint | a set of `ArtifactBinding`s | SHA-256 hex of sorted `artifact_id:revision_id:content_hash` triples joined by `\|` | `src/sdlc_blackboard/domain/reviews.py:32` |
| Canonical request hash (idempotency key body) | a request model's JSON projection | SHA-256 hex of the sort-keyed JSON dump | `src/sdlc_blackboard/application/idempotency.py:36` |
| Deterministic artifact id | `(goal_id, logical_name)` | `uuid5` over `blackboard://{goal_id}/{logical_name}` so revisions of one logical artifact share an id | `src/sdlc_blackboard/application/use_cases/task_service.py:49` |
| Next assignment epoch on claim | task's current `assignment_epoch` | `assignment_epoch + 1` | `src/sdlc_blackboard/application/use_cases/task_service.py:118` |
| Default routing class for a run | task's `required_actor_kind` | one of four Bedrock routing classes, via the 18-row `ROUTING_POLICY` table lookup | `src/sdlc_blackboard/domain/routing.py:56` |
| Per-goal coordination-thrash report | goal's command-failure ledger, reviews, `review_task.reopened` events, tasks | `ThrashReport(conflicts, stale_versions, review_rejections, reclaims)` | `src/sdlc_blackboard/application/use_cases/thrash_service.py:39` |

**Release-gate evaluation (`get_gate_status` / `evaluate_on_conn`).** The gate is recomputed on every read, never stored. Steps (`src/sdlc_blackboard/application/use_cases/gate_service.py:52`):
1. Derive `required_types` from the union of blocking `ReviewRequirement`s across the goal's task contracts, first-seen order; fall back to `(quality, security)` if none declared (`src/sdlc_blackboard/application/use_cases/gate_service.py:134`). This is why adding a new reviewing bounded context needs no kernel change — the requirement is data on the task contract, not hardcoded.
2. Derive the implementation artifact's logical name as the deliverable of the task carrying the most blocking reviews (`src/sdlc_blackboard/application/use_cases/gate_service.py:148`), then resolve its current alias binding.
3. If no implementation binding exists, return `UNSATISFIED` with every required review and `human_release` listed missing (`src/sdlc_blackboard/application/use_cases/gate_service.py:79`).
4. Compute the current binding fingerprint. A review counts toward a required type only if it is bound to the *current* binding, is not `stale`, and its disposition is `APPROVED`; stale reviews bound to the current binding are collected separately (`src/sdlc_blackboard/application/use_cases/gate_service.py:92`).
5. Human approval is satisfied iff a non-revoked `HUMAN_RELEASE` approval binds the current binding (`src/sdlc_blackboard/application/use_cases/gate_service.py:108`).
6. `reviews_ok` = no missing required reviews and no open blocking findings. Status is `SATISFIED` when `reviews_ok and human_ok`; `HUMAN_REQUIRED` when `reviews_ok` but the human approval is the only thing missing; else `UNSATISFIED` (`src/sdlc_blackboard/application/use_cases/gate_service.py:116`).

**Coordination-thrash report (`get_thrash_report`).** A read-only derived report over one goal's history, opened on its own unit of work with no row locks (`src/sdlc_blackboard/application/use_cases/thrash_service.py:39`). Four counters:
1. `conflicts` and `stale_versions` come from the command-failure ledger, counted by error code and scoped to the goal (task-scoped failures with a null `goal_id` are joined back through the `tasks` table so a double-claim recorded with only a `task_id` still counts) (`src/sdlc_blackboard/infrastructure/repositories/failures.py:49`).
2. `review_rejections` = reviews for the goal whose disposition is anything other than `APPROVED` (findings / request-revision / abstained) (`src/sdlc_blackboard/application/use_cases/thrash_service.py:48`).
3. `reclaims` = count of `review_task.reopened` events plus the sum of `max(assignment_epoch - 1, 0)` over the goal's tasks — every claim beyond the first is re-work churn (`src/sdlc_blackboard/application/use_cases/thrash_service.py:52`).
An unknown or empty goal yields all zeros rather than an error, so the counters are total; the Lean model pins them zero-on-empty, monotone, and goal-frame.

## Policy and gates

- **Release gate:** a goal is releasable only when the implementation alias exists, every required blocking review type has a non-stale `APPROVED` review bound to the current revision, no open blocking findings remain, and a non-revoked human release approval binds that exact revision. `src/sdlc_blackboard/application/use_cases/gate_service.py:52`.
- **Contract-driven required reviews:** the gate's required review types are the union of blocking `ReviewRequirement`s declared across the goal's task contracts (fallback `quality, security`) — reviewers are task capabilities, not hardcoded personas. `src/sdlc_blackboard/application/use_cases/gate_service.py:134`.
- **Blocking-finding authority:** a blocking finding may be opened only by a task whose `required_actor_kind` is a `REVIEWER_KIND` *and* whose contract sets `may_create_blocking_finding=True`. `src/sdlc_blackboard/application/use_cases/review_service.py:44`.
- **Blocking-finding gate:** a blocking finding blocks the release gate until its state is one of `VERIFIED`, `ACCEPTED_RISK`, `REJECTED`, `SUPERSEDED` (the resolved set). `src/sdlc_blackboard/domain/findings.py:42`, enforced by the open-blocking query `src/sdlc_blackboard/infrastructure/repositories/quality.py:126`.
- **Actor ownership:** starting a run or submitting a result requires the calling actor to own the task's assignment (`assigned_actor_id`); another actor is `Unauthorized`. `src/sdlc_blackboard/application/use_cases/task_service.py:418`.
- **Assignment fencing (epoch):** a mutating call carrying an `assignment_epoch` that no longer matches the task's current epoch is rejected as `StaleAssignment` — a worker that lost its claim cannot act. `src/sdlc_blackboard/application/use_cases/task_service.py:411`.
- **Single active claim:** at most one assignment per task may be in `assigned`/`running`, enforced by a partial unique index as the final defense against double-claim beyond the CAS. `migrations/20260717000001_initial.sql:57`.
- **Idempotency policy:** a `command_id` executes at most once; reuse with a matching payload replays the stored response, reuse with a different payload is a `DUPLICATE_COMMAND_MISMATCH` validation failure. `src/sdlc_blackboard/application/idempotency.py:42`.
- **Default routing policy (cost lever):** when `start_runtime_run` carries no explicit `routing_class`, the run defaults to the class the 18-row policy table maps its actor kind to — planning/design kinds (lead/architect/analyst) get the frontier `GLOBAL_INFERENCE_PROFILE`, implementation/data/docs/ux get `GEO_INFERENCE_PROFILE`, mechanical reviewers get the cheap `IN_REGION_RUNTIME`, and human/system get `REGIONAL_MANTLE`; an explicit request value always wins (spec R1/R2). `src/sdlc_blackboard/domain/routing.py:32`.
- **Command-failure ledger records but never masks:** on a `DomainError`, a second best-effort transaction appends one row to the ledger; any failure of that write is swallowed with a `command.failure_unrecorded` warning so it can never mask the original command error. `src/sdlc_blackboard/application/use_cases/base.py:71`.
- **Thrash report is operator-only:** the per-goal coordination-thrash report is exposed only through the `blackboard thrash GOAL_ID` CLI command and is deliberately NOT an MCP tool — agents must not observe (and so cannot game) their own thrash metric. `src/sdlc_blackboard/application/query_models.py:29`, `src/sdlc_blackboard/interfaces/cli.py:105`.
- **CAS-only alias promotion:** promoting an existing alias requires the caller to supply the expected current revision; last-writer-wins is refused. `src/sdlc_blackboard/application/use_cases/artifact_service.py:40`.
- **Goal completion authorization:** `authorize_goal_completion` only flips goal state to `SATISFIED` via CAS; it re-checks the gate on the SAME transaction (taking a `FOR UPDATE` lock on the goal row so gate-input writers serialize against the evaluation window), so authorize is enforcing, not advisory. `src/sdlc_blackboard/application/use_cases/goal_service.py:55`.
- **No server-side policy table:** the PoC deliberately has no policy/permission tables; authorization is entirely contract-driven in the application layer. `migrations/20260717000002_seed_poc_policies.sql:2`.

## See also

- [impact-analysis](../insights/impact-analysis.md) — 20 shared source citations
- [contract-map](../insights/contract-map.md) — 17 shared source citations
- [debugging-guide](../insights/debugging-guide.md) — 13 shared source citations
- [processes](../behavior/processes.md) — 10 shared source citations
- [tech-debt](../insights/tech-debt.md) — 10 shared source citations
