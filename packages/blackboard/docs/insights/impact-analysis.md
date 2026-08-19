# omnigent-blackboard-poc · Impact analysis

This file answers one question: *if I change surface X, what else must I touch or validate?*

**High-impact surface** here means a **module ranked by inbound internal-import count** — the number of distinct files across `src/`, `tests/`, and `scripts/` that import a name from it. The ranking was built by grepping every `from sdlc_blackboard.<module> import` statement and counting distinct importing files (`demo_app/` excluded). This is a hexagonal (ports-and-adapters) kernel: the `src/sdlc_blackboard/domain/` layer is pure Pydantic value objects, `src/sdlc_blackboard/application/` orchestrates use cases against `Protocol` ports, `infrastructure/` adapts to Postgres, and `interfaces/` exposes MCP tools and a CLI. Inbound-import count tracks blast radius well here because dependencies flow inward toward `src/sdlc_blackboard/domain/`, so the most-imported domain modules are the ones whose change ripples furthest.

The top 8 surfaces by this criterion, in order: `domain.common` (43 importers), `domain.tasks` (24), `application.ports` (20), `domain.goals` (19), `domain.artifacts` (18), `domain.reviews` (17), `application.results` (17), `domain.events` (16). Lower-ranked but structurally load-bearing surfaces (`application.commands`, `infrastructure.di`, `domain.transitions`, `domain.routing`, `application.use_cases.thrash_service`) are in `## Other notable surfaces`.

Table conventions: `Type` is one of `direct import` / `indirect` / `runtime dispatch` / `test` / `config`. `Touch on change` is `yes` (must edit), `likely` (review even without a signature change), `no` (only a behavioral change reaches it).

## domain.common

Defined at: `src/sdlc_blackboard/domain/common.py:1`

The shared value-object base of the whole kernel: `DomainModel` (frozen, `extra="forbid"`) `src/sdlc_blackboard/domain/common.py:19`, `ActorKind` `:25`, the `PRODUCER_KINDS`/`REVIEWER_KINDS` frozensets `:58`/`:71`, `ActorRef` `:87`, `CommandContext` `:92`, `ArtifactBinding` `:109`, `EvidenceRef` `:122`, and the `NonEmptyStr` alias `:16`. 43 files import from it.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/domain/tasks.py` | direct import | likely | `src/sdlc_blackboard/domain/tasks.py:10` |
| `src/sdlc_blackboard/domain/goals.py` | direct import | likely | `src/sdlc_blackboard/domain/goals.py:10` |
| `src/sdlc_blackboard/domain/artifacts.py` | direct import | likely | `src/sdlc_blackboard/domain/artifacts.py:14` |
| `src/sdlc_blackboard/domain/reviews.py` | direct import | likely | `src/sdlc_blackboard/domain/reviews.py:16` |
| `src/sdlc_blackboard/domain/events.py` | direct import | likely | `src/sdlc_blackboard/domain/events.py:14` |
| `src/sdlc_blackboard/domain/routing.py` (`ActorKind` keys the routing table) | direct import | yes | `src/sdlc_blackboard/domain/routing.py:19` |
| `src/sdlc_blackboard/domain/approvals.py`, `src/sdlc_blackboard/domain/findings.py` | direct import | likely | `src/sdlc_blackboard/domain/approvals.py:15`, `src/sdlc_blackboard/domain/findings.py:15` |
| `src/sdlc_blackboard/application/commands.py` | direct import | likely | `src/sdlc_blackboard/application/commands.py:16` |
| `src/sdlc_blackboard/application/events.py`, `src/sdlc_blackboard/application/ports.py`, `src/sdlc_blackboard/application/query_models.py`, `src/sdlc_blackboard/application/receipts.py` | direct import | likely | `src/sdlc_blackboard/application/ports.py:27` |
| `src/sdlc_blackboard/application/use_cases/*` (task, goal, review, artifact, gate services + base) | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:31`, `src/sdlc_blackboard/application/use_cases/review_service.py:13` |
| `infrastructure/repositories/` (package, split by aggregate) | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/_common.py:16` |
| `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:36` |
| `tests/*` (unit, property, contract, integration, e2e — 17 files) | test | likely | `tests/unit/test_domain_models.py:14`, `tests/contract/test_routing_policy.py`, `tests/e2e/test_scripted_flow.py:29` |

Blast-radius notes:
- `DomainModel` sets `frozen=True, extra="forbid"` `src/sdlc_blackboard/domain/common.py:22`, so every downstream aggregate is immutable and rejects unknown fields. Loosening this contract silently changes construction/mutation semantics for all ~15 domain models that subclass it.
- `ActorKind` membership is partitioned into `PRODUCER_KINDS` and `REVIEWER_KINDS` `src/sdlc_blackboard/domain/common.py:58`,`:71`; adding a new kind requires deciding which set it joins, and it now also requires a new entry in `ROUTING_POLICY` `src/sdlc_blackboard/domain/routing.py:32` and its Lean mirror, or the routing contract test fails (see `## Other notable surfaces` · `domain.routing`).
- `CommandContext` carries the idempotency (`command_id`), optimistic-concurrency (`expected_version`), and fencing (`assignment_epoch`) fields `src/sdlc_blackboard/domain/common.py:100`,`:104`,`:105`; every mutating use case threads it, so a field change touches all `src/sdlc_blackboard/application/use_cases/*` services.

## domain.tasks

Defined at: `src/sdlc_blackboard/domain/tasks.py:1`

The task aggregate and its state machine's alphabet: `TaskState` `src/sdlc_blackboard/domain/tasks.py:18`, `TERMINAL_TASK_STATES` `:35`, `DeliverableSpec` `:40`, `ReviewRequirement` `:46`, `TaskContractCreate` `:52`, `Task` `:69`. 24 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/domain/transitions.py` | direct import | yes | `src/sdlc_blackboard/domain/transitions.py:10` |
| `src/sdlc_blackboard/application/ports.py` (`TaskRepo` signatures) | direct import | yes | `src/sdlc_blackboard/application/ports.py:32` |
| `src/sdlc_blackboard/application/use_cases/task_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:45` |
| `src/sdlc_blackboard/application/use_cases/gate_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/gate_service.py:31` |
| `src/sdlc_blackboard/application/use_cases/query_service.py`, `src/sdlc_blackboard/application/query_models.py`, `src/sdlc_blackboard/application/receipts.py` | direct import | likely | `src/sdlc_blackboard/application/use_cases/query_service.py:17` |
| `infrastructure/repositories/tasks.py` (Task serialization) | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/tasks.py:16` |
| `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:41` |
| `tests/unit/test_transitions.py`, `test_gate_derivation.py` | test | yes | `tests/unit/test_transitions.py:5` |
| `tests/property/test_domain_invariants.py`, `tests/integration/*`, `tests/e2e/*` | test | likely | `tests/property/test_domain_invariants.py:14`, `tests/integration/test_accept_task.py:28` |

Blast-radius notes:
- `TaskState` is the alphabet of `src/sdlc_blackboard/domain/transitions.py`; adding or renaming a state forces edits to `_EXPLICIT_EDGES` `src/sdlc_blackboard/domain/transitions.py:15` and to `TERMINAL_TASK_STATES` `src/sdlc_blackboard/domain/tasks.py:35`, or the state becomes unreachable/non-terminal by default.
- `TERMINAL_TASK_STATES` is consumed by `can_transition` via `_nonterminal_states()` `src/sdlc_blackboard/domain/transitions.py:45`; a task in a terminal state has no outbound edges except the explicit `SUPERSEDED` bookkeeping edges `:34`,`:35`, so moving a state in/out of this set silently opens or closes transitions.
- `TaskContractCreate` is stored inline on `Task.contract` `src/sdlc_blackboard/domain/tasks.py:81` and persisted by `src/sdlc_blackboard/infrastructure/repositories/tasks.py:16`; changing its shape requires a matching persistence/migration change. `Task.required_actor_kind` is now also read by `start_runtime_run` to default the routing class `src/sdlc_blackboard/application/use_cases/task_service.py:179`.

## application.ports

Defined at: `src/sdlc_blackboard/application/ports.py:1`

The hexagonal seam: 15 `Protocol` definitions the application layer depends on and the infrastructure layer implements — `Clock` `src/sdlc_blackboard/application/ports.py:40`, `UnitOfWork` `:47`, `AbstractAsyncTxn` `:53`, plus the repos `GoalRepo` `:66`, `TaskRepo` `:78`, `AssignmentRepo` `:106`, `RuntimeRunRepo` `:114`, `ArtifactRepo` `:127`, `FindingRepo` `:151`, `ReviewRepo` `:162`, `ApprovalRepo` `:171`, `EventRepo` `:180`, `OutboxRepo` `:211`, `ProcessedCommandStore` `:217`, and the new `CommandFailureRepo` `:233`. 20 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/application/use_cases/wiring.py` (`ServicePorts` bundles the protocols) | direct import | yes | `src/sdlc_blackboard/application/use_cases/wiring.py:12` |
| `src/sdlc_blackboard/application/use_cases/base.py` (`_record_command_failure` calls `command_failures.record`) | direct import | yes | `src/sdlc_blackboard/application/use_cases/base.py:23` |
| `src/sdlc_blackboard/application/use_cases/task_service.py`, `goal_service.py`, `review_service.py`, `artifact_service.py`, `gate_service.py`, `outbox_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:18` |
| `src/sdlc_blackboard/application/events.py`, `src/sdlc_blackboard/application/idempotency.py` | direct import | likely | `src/sdlc_blackboard/application/events.py:17` |
| `infrastructure/repositories/` (each module structurally implements a port — `tasks.py`, `goals.py`, `artifacts.py`, `quality.py`, `events_outbox.py`, `idempotency.py`, `failures.py`, `_common.py`) | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/tasks.py:26`, `src/sdlc_blackboard/infrastructure/repositories/failures.py:19` |
| `tests/unit/fakes.py` (in-memory fakes implement every port) | test | yes | `tests/unit/fakes.py:25` |
| `tests/unit/test_logging.py` | test | likely | `tests/unit/test_logging.py` |

Blast-radius notes:
- Ports are `Protocol`s (structural typing), not ABCs — implementers do not inherit, so nothing errors at import time when a method is added. A new port method requires a matching addition in the Postgres adapter under `infrastructure/repositories/` *and* in `tests/unit/fakes.py`, or a use case that calls it fails only at runtime / under the fakes. The recent `EventRepo.count_by_type` `src/sdlc_blackboard/application/ports.py:192` and the whole `CommandFailureRepo` protocol `:233` are examples: both landed with paired real + fake implementations.
- `ServicePorts` in `src/sdlc_blackboard/application/use_cases/wiring.py:31` is the single struct that carries every port to the services; a new port field (e.g. `command_failures` `src/sdlc_blackboard/application/use_cases/wiring.py:47`) must also be wired in `src/sdlc_blackboard/infrastructure/di.py:44` (`build_ports`) or container assembly fails.

## domain.goals

Defined at: `src/sdlc_blackboard/domain/goals.py:1`

The top-level objective aggregate: `GoalState` `src/sdlc_blackboard/domain/goals.py:13`, `GoalCreate` `:22`, `Goal` `:30`. 19 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/application/ports.py` (`GoalRepo`) | direct import | yes | `src/sdlc_blackboard/application/ports.py:30` |
| `src/sdlc_blackboard/application/use_cases/goal_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/goal_service.py:15` |
| `src/sdlc_blackboard/application/use_cases/query_service.py`, `src/sdlc_blackboard/application/query_models.py` | direct import | likely | `src/sdlc_blackboard/application/use_cases/query_service.py:16` |
| `infrastructure/repositories/goals.py` | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/goals.py:10` |
| `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:39` |
| `tests/unit/test_domain_models.py`, `tests/property/test_domain_invariants.py` | test | likely | `tests/unit/test_domain_models.py:16` |
| `tests/integration/*`, `tests/e2e/test_scripted_flow.py`, `scripts/run_scripted_demo.py` | test | likely | `tests/integration/test_reliability_invariants.py:25`, `scripts/run_scripted_demo.py:26` |

Blast-radius notes:
- `GoalCreate` is the input DTO and `Goal` the persisted aggregate; they diverge (`Goal` adds `goal_id`, `state`, `version`) `src/sdlc_blackboard/domain/goals.py:30`. A field added to one usually must be mirrored in the other and in the `GoalRepo` port + Postgres mapping (`src/sdlc_blackboard/application/ports.py:30`, `src/sdlc_blackboard/infrastructure/repositories/goals.py:10`).
- `Goal.version` `src/sdlc_blackboard/domain/goals.py:38` is the optimistic-concurrency token checked against `CommandContext.expected_version`; removing or reusing it breaks conflict detection in `goal_service.py`.

## domain.artifacts

Defined at: `src/sdlc_blackboard/domain/artifacts.py:1`

Immutable artifact revisions and the mutable alias pointer: `ArtifactStatus` `src/sdlc_blackboard/domain/artifacts.py:17`, `ArtifactSubmission` `:23`, `ArtifactRevision` `:33`, `ArtifactAlias` `:48`. 18 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/application/commands.py` (`SubmitTaskResult`) | direct import | yes | `src/sdlc_blackboard/application/commands.py:15` |
| `src/sdlc_blackboard/application/ports.py` (`ArtifactRepo`) | direct import | yes | `src/sdlc_blackboard/application/ports.py:26` |
| `src/sdlc_blackboard/application/use_cases/task_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:26` |
| `src/sdlc_blackboard/application/use_cases/artifact_service.py` (alias CAS) | direct import | yes | `src/sdlc_blackboard/application/use_cases/artifact_service.py:15` |
| `src/sdlc_blackboard/application/use_cases/query_service.py`, `src/sdlc_blackboard/application/receipts.py` | direct import | likely | `src/sdlc_blackboard/application/use_cases/query_service.py:13` |
| `infrastructure/repositories/artifacts.py` | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/artifacts.py:11` |
| `interfaces/mcp/tools_read.py`, `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_read.py:14`, `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:35` |
| `tests/integration/test_multi_context_gate.py`, `test_accept_task.py`, `test_reliability_invariants.py`, `tests/e2e/test_scripted_flow.py` | test | likely | `tests/integration/test_multi_context_gate.py:22`, `tests/e2e/test_scripted_flow.py:28` |

Blast-radius notes:
- A revision is uniquely keyed by `(artifact_id, content_hash)` and is immutable; only `ArtifactAlias` is mutable, promoted by compare-and-set `src/sdlc_blackboard/domain/artifacts.py:4`,`:48`. Any consumer that treats a revision as mutable, or that promotes an alias without CAS, breaks this invariant — `src/sdlc_blackboard/application/use_cases/artifact_service.py:15` and `src/sdlc_blackboard/infrastructure/repositories/artifacts.py:11` are the enforcement points.
- `ArtifactBinding` (in `domain.common`) names a revision by id + hash, and `ArtifactSubmission`/`ArtifactRevision` carry `content_hash` `src/sdlc_blackboard/domain/artifacts.py:27`,`:39`; reviews bind concrete hashes, so a change to how the hash is produced invalidates existing bindings and the review fingerprint in `src/sdlc_blackboard/domain/reviews.py:32`.

## domain.reviews

Defined at: `src/sdlc_blackboard/domain/reviews.py:1`

Review aggregate and its identity function: `ReviewDisposition` `src/sdlc_blackboard/domain/reviews.py:25`, `binding_fingerprint` `:32`, `single_binding_fingerprint` `:42`, `ReviewSubmission` `:53`, `Review` `:65`. 17 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/application/ports.py` (`ReviewRepo`) | direct import | yes | `src/sdlc_blackboard/application/ports.py:31` |
| `src/sdlc_blackboard/application/use_cases/review_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/review_service.py:21` |
| `src/sdlc_blackboard/application/use_cases/gate_service.py` (reads `ReviewDisposition`, `single_binding_fingerprint`) | direct import | yes | `src/sdlc_blackboard/application/use_cases/gate_service.py:30` |
| `src/sdlc_blackboard/application/query_models.py` | direct import | likely | `src/sdlc_blackboard/application/query_models.py:15` |
| `infrastructure/repositories/quality.py` | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/quality.py:18` |
| `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:40` |
| `tests/unit/test_domain_models.py` (fingerprint) | test | yes | `tests/unit/test_domain_models.py:17` |
| `tests/integration/test_multi_context_gate.py`, `tests/e2e/test_scripted_flow.py` | test | likely | `tests/integration/test_multi_context_gate.py:31` |

Blast-radius notes:
- `binding_fingerprint` is order-independent: it sorts the `(artifact_id, revision_id, content_hash)` triples before hashing `src/sdlc_blackboard/domain/reviews.py:38`, so a review's identity is stable regardless of binding order. Changing the hash inputs or the sort invalidates every stored `Review.binding_fingerprint` `:71` and breaks stale-review detection. `single_binding_fingerprint` `:42` is defined as exactly the one-element case (`binding_fingerprint((binding,))`); the two must stay consistent or gate derivation mismatches.
- `ReviewDisposition` drives the gate derivation in `src/sdlc_blackboard/application/use_cases/gate_service.py:30`; adding a disposition value forces a decision about how the gate treats it, otherwise the gate silently ignores the new outcome.

## application.results

Defined at: `src/sdlc_blackboard/application/results.py:1`

The command-outcome envelope crossing the application boundary: `ErrorCode` `src/sdlc_blackboard/application/results.py:18`, `CommandStatus` `:30`, `CommandError` `:60`, and the generic `CommandResult[T]` `:84`, plus the two mapping tables `_DOMAIN_TO_ERROR_CODE` `:44` and `_ERROR_CODE_TO_STATUS` `:47`. 17 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/application/use_cases/base.py` (base returns `CommandResult`) | direct import | yes | `src/sdlc_blackboard/application/use_cases/base.py:19` |
| `src/sdlc_blackboard/application/use_cases/task_service.py`, `goal_service.py`, `review_service.py`, `artifact_service.py` | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:24` |
| `src/sdlc_blackboard/application/idempotency.py` | direct import | yes | `src/sdlc_blackboard/application/idempotency.py:24` (idempotency replays a stored `CommandResult`) |
| `interfaces/mcp/tools_commands.py` (serializes result to MCP wire) | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:33` |
| `tests/unit/test_domain_models.py` | test | yes | `tests/unit/test_domain_models.py:8` |
| `tests/e2e/test_scripted_flow.py`, `tests/integration/test_accept_task.py`, `test_reliability_invariants.py` | test | likely | `tests/e2e/test_scripted_flow.py:26`, `tests/integration/test_accept_task.py:23` |

Blast-radius notes:
- Concurrency conflicts are returned as `CommandResult` values, never raised `src/sdlc_blackboard/application/results.py:3`; every mutating use case is typed to return `CommandResult[T]`, so a consumer that expects exceptions on conflict is wrong. `DomainError`, by contrast, *is* raised and is caught in `base.py` OUTSIDE the `uow.begin()` block `src/sdlc_blackboard/application/use_cases/base.py:66` (explicit rollback), then recorded to the command-failure ledger in a second best-effort transaction `:71`,`:92` — so a new `DomainError` path must still resolve to a known `ErrorCode` for that ledger row to carry a meaningful code.
- `ErrorCode` maps 1:1 onto `domain.errors` by shared string value via `_DOMAIN_TO_ERROR_CODE` `src/sdlc_blackboard/application/results.py:44`; adding a `DomainError` code without a matching `ErrorCode` member silently falls through to `INTERNAL_ERROR` in `CommandError.from_domain` `:72`.
- `_ERROR_CODE_TO_STATUS` `src/sdlc_blackboard/application/results.py:47` must stay total over `ErrorCode`; a new error code without a status entry falls back to `VALIDATION_FAILED` in `CommandResult.failed` `:99`, which the MCP adapter then reports to clients.

## domain.events

Defined at: `src/sdlc_blackboard/domain/events.py:1`

Runtime-run and event vocabulary plus gate results: `RunState` `src/sdlc_blackboard/domain/events.py:17`, `RoutingClass` `:25`, `RuntimeRun` `:34`, `TeamEvent` `:50`, `GateStatus` `:65`, `GateResult` `:71`. 16 importing files.

| Downstream | Type | Touch on change | Citation |
|---|---|---|---|
| `src/sdlc_blackboard/domain/routing.py` (`RoutingClass` is the codomain of the routing table) | direct import | yes | `src/sdlc_blackboard/domain/routing.py:20` |
| `src/sdlc_blackboard/application/events.py` (`TeamEvent` publishing) | direct import | yes | `src/sdlc_blackboard/application/events.py:14` |
| `src/sdlc_blackboard/application/ports.py` (`RuntimeRunRepo`, `EventRepo`) | direct import | yes | `src/sdlc_blackboard/application/ports.py:28` |
| `src/sdlc_blackboard/application/use_cases/task_service.py` (`RunState`, `RuntimeRun`, `RoutingClass`) | direct import | yes | `src/sdlc_blackboard/application/use_cases/task_service.py:43` |
| `src/sdlc_blackboard/application/use_cases/gate_service.py` (`GateResult`, `GateStatus`) | direct import | yes | `src/sdlc_blackboard/application/use_cases/gate_service.py:29` |
| `src/sdlc_blackboard/application/use_cases/query_service.py` | direct import | likely | `src/sdlc_blackboard/application/use_cases/query_service.py:14` |
| `infrastructure/repositories/` (`tasks.py` runs, `events_outbox.py` events) | direct import | yes | `src/sdlc_blackboard/infrastructure/repositories/events_outbox.py:13`, `src/sdlc_blackboard/infrastructure/repositories/tasks.py:15` |
| `interfaces/mcp/tools_read.py`, `interfaces/mcp/tools_commands.py` | direct import | yes | `src/sdlc_blackboard/interfaces/mcp/tools_read.py:15`, `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:37` |
| `tests/e2e/test_scripted_flow.py`, `tests/integration/test_multi_context_gate.py` | test | likely | `tests/e2e/test_scripted_flow.py:35` |

Blast-radius notes:
- `RoutingClass` `src/sdlc_blackboard/domain/events.py:25` is the codomain of `ROUTING_POLICY` in `domain/routing.py` and is mirrored in `formal/Blackboard/Routing.lean`; renaming or reordering its members forces a paired edit to the routing table, the Lean spec, and the contract test (see `## Other notable surfaces` · `domain.routing`).
- `GateStatus`/`GateResult` are produced by `gate_service.py` and read back through `src/sdlc_blackboard/interfaces/mcp/tools_read.py:15`; changing gate-result shape touches both the deriving service and the MCP read adapter that surfaces it to clients.
- `RunState` gates the run lifecycle in `src/sdlc_blackboard/application/use_cases/task_service.py:43`; `TeamEvent` is the append-only event record persisted by `src/sdlc_blackboard/infrastructure/repositories/events_outbox.py:13` and published via the outbox in `src/sdlc_blackboard/application/events.py:14`, so a schema change requires a coordinated persistence + publishing edit.

## Other notable surfaces

- `application.commands` — `src/sdlc_blackboard/application/commands.py:26`. The mutating-command request DTOs (`ClaimTaskRequest` `:26`, `StartRunRequest` `:36`, `SubmitTaskResult` `:48`, `PromoteArtifactRequest` `:69`, `AcceptTaskRequest` `:80`, etc.). 12 importers. These are the boundary contract between the MCP adapter and the use cases: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:18` constructs them from tool arguments and each service consumes exactly one (`src/sdlc_blackboard/application/use_cases/task_service.py:9`, `src/sdlc_blackboard/application/use_cases/artifact_service.py:10`, `src/sdlc_blackboard/application/use_cases/review_service.py:7`). Adding a required field forces a matching change in both the adapter and the service, or command construction fails validation (`extra="forbid"` via `DomainModel`). The `CommandContext` envelope (idempotency, version, epoch) is passed alongside, not inside these DTOs.
- `domain.routing` — `src/sdlc_blackboard/domain/routing.py:56` (`default_routing_class`) + `ROUTING_POLICY` table `:32`. NEW at commit 19fd0da. Only 2 importers (`src/sdlc_blackboard/application/use_cases/task_service.py:44`, `tests/contract/test_routing_policy.py`), but a **lockstep constraint** makes it high-impact: the Python table (18 `ActorKind` → 4 `RoutingClass`) is mirrored in `formal/Blackboard/Routing.lean`, and `tests/contract/test_routing_policy.py` asserts the two tables are identical over all 18 kinds plus tier monotonicity. Touch on change: `yes` — editing `ROUTING_POLICY` *or* `Routing.lean` without the other breaks the contract test; adding an `ActorKind` (in `domain.common`) requires a new row in both. `start_runtime_run` calls `default_routing_class(task.required_actor_kind)` only when the request carries no explicit `routing_class` `src/sdlc_blackboard/application/use_cases/task_service.py:179` (R1: an explicit request value still wins).
- `application.use_cases.thrash_service` — `src/sdlc_blackboard/application/use_cases/thrash_service.py:35`. NEW at 19fd0da. `ThrashService.get_thrash_report(goal_id)` `:39` is a read-only aggregator (own UoW, no locks) returning the frozen `ThrashReport` `src/sdlc_blackboard/application/query_models.py:29` (`conflicts`, `stale_versions`, `review_rejections`, `reclaims` `:44`,`:47`,`:50`,`:54`). Wired onto the services facade `src/sdlc_blackboard/application/use_cases/services.py:32`,`:44` and reached only via the operator-only `blackboard thrash GOAL_ID` CLI command `src/sdlc_blackboard/interfaces/cli.py:105` — deliberately NOT an MCP tool, so agents cannot observe their own thrash metric. The fold semantics (zero-on-empty, monotone, goal-frame) are pinned by `formal/Blackboard/Thrash.lean`; a change to the counting logic should be checked against that spec and `tests/unit/test_thrash_service.py`.
- `infrastructure.di` — `src/sdlc_blackboard/infrastructure/di.py:36`. `Container` `:36` and `build_ports` `:44` / `build_container` `:64` wire the concrete Postgres adapters (including the new `CommandFailureRepository()` `:60` and `ThrashService`) into `ServicePorts`. 12 importers, reached by runtime dispatch from every entry point: `interfaces/cli.py`, `interfaces/mcp/server.py`, `scripts/run_scripted_demo.py`, and the integration/e2e fixtures (`tests/e2e/conftest.py`, `tests/integration/test_mcp_tools.py`, `test_thrash_and_routing.py`). Touch on change: `likely` — assembly wiring, not called by symbol name.
- `domain.transitions` — `src/sdlc_blackboard/domain/transitions.py:49`. `can_transition` is the pure state-transition guard, imported only by `src/sdlc_blackboard/application/use_cases/task_service.py:46` (called at `:423`) and the unit + property suites (`tests/unit/test_transitions.py`, `tests/property/test_domain_invariants.py`). Low import count but the canonical guard for every task state change; editing the edge set is a behavioral change the property tests exist to catch.
- `application.use_cases.wiring` — `src/sdlc_blackboard/application/use_cases/wiring.py:31`. `ServicePorts` bundles all repo protocols (now including `command_failures` `:47`); imported by `infrastructure/di.py` and the use-case constructors. A field added here propagates to `build_ports` and every service that unpacks it.
- `interfaces.mcp.server` — `src/sdlc_blackboard/interfaces/mcp/server.py:1`. The MCP framework-dispatch entry point; `tools_commands.py` and `tools_read.py` register 19 tools against it, and `scripts/serve_blackboard.py` + `tests/integration/test_mcp_tools.py` launch it. Tools are reached by string-keyed framework dispatch, so signature changes are verified through the integration test rather than a direct import graph. The `thrash` operation is intentionally absent from this surface (CLI-only).

## See also

- [contract-map](../insights/contract-map.md) — 33 shared source citations
- [business-logic](../insights/business-logic.md) — 20 shared source citations
- [processes](../behavior/processes.md) — 17 shared source citations
- [module-map](../architecture/module-map.md) — 15 shared source citations
- [tech-debt](../insights/tech-debt.md) — 14 shared source citations
