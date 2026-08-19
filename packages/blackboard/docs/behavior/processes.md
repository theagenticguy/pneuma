# omnigent-blackboard-poc · Processes

The kernel is a transactional organizational blackboard driven by a FastMCP tool surface, a Typer developer CLI, and operator scripts. There are no HTTP routes beyond a `/health` probe (`src/sdlc_blackboard/interfaces/mcp/server.py:71`) and no scheduled jobs. The transactional outbox is drained by an operator-invoked relay rather than an always-on consumer: `claim_unpublished`/`mark_published` ports (`src/sdlc_blackboard/infrastructure/repositories/events_outbox.py:138-168`) are driven by `OutboxService.drain_outbox` (`src/sdlc_blackboard/application/use_cases/outbox_service.py:28`) behind the `blackboard outbox-relay` CLI command — see Minor flows.

Every mutating process shares one shell: the MCP tool resolves the `Services` facade (`src/sdlc_blackboard/interfaces/mcp/server.py:53`) and delegates to one application-service method, which wraps its domain logic in `CommandService._command` — a unit-of-work transaction plus `execute_idempotently` (`src/sdlc_blackboard/application/use_cases/base.py:32`, `src/sdlc_blackboard/application/idempotency.py:42`). Each process below names that shell once, then its domain-specific steps. A raised `DomainError` propagates OUT of the `async with uow.begin()` block so the mutation and its idempotency record roll back together, and a second best-effort transaction appends the failure to a command-failure ledger without masking the original error (ADR-0014, `src/sdlc_blackboard/application/use_cases/base.py:66`).

## submit_task_result

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:101`

1. MCP tool delegates to `TaskService.submit_task_result` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:108`.
2. `_command` opens the transaction and runs the body idempotently by `command_id` `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Lock the task for update; validate assignment epoch, owning actor, and that state is RUNNING or AWAITING_INPUT `src/sdlc_blackboard/application/use_cases/task_service.py:210`.
4. Load and validate the runtime run: task/epoch match and the input manifest matches the run's recorded manifest `src/sdlc_blackboard/application/use_cases/task_service.py:217`.
5. For each submitted artifact, dedup by content hash or insert an immutable CANDIDATE revision and seed its alias `src/sdlc_blackboard/application/use_cases/task_service.py:226`.
6. Complete the run and the assignment, then CAS-transition the task to SUBMITTED `src/sdlc_blackboard/application/use_cases/task_service.py:282`.
7. Create or re-open one review task per declared review requirement `src/sdlc_blackboard/application/use_cases/task_service.py:294`.
8. Return the `TaskSubmissionReceipt` with the task, artifact revisions, and review task ids `src/sdlc_blackboard/application/use_cases/task_service.py:295`.

### Related

- `src/sdlc_blackboard/application/use_cases/task_service.py:351`
- `src/sdlc_blackboard/application/use_cases/task_service.py:49`
- `src/sdlc_blackboard/application/events.py:20`
- `src/sdlc_blackboard/domain/transitions.py`
- `src/sdlc_blackboard/application/commands.py`

## claim_task

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:73`

1. MCP tool delegates to `TaskService.claim_task` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:80`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Lock the task for update; require it to be READY `src/sdlc_blackboard/application/use_cases/task_service.py:113`.
4. Compute the next fencing epoch and open the assignment; the partial unique index is the DB-level double-claim defense `src/sdlc_blackboard/application/use_cases/task_service.py:118`.
5. CAS-claim the task on its current version, assigning the actor and epoch; a stale version raises a conflict `src/sdlc_blackboard/application/use_cases/task_service.py:123`.
6. Append the `task.assigned` event and return the `ClaimReceipt` with the fencing epoch `src/sdlc_blackboard/application/use_cases/task_service.py:128`.

### Related

- `src/sdlc_blackboard/application/use_cases/task_service.py:411`
- `src/sdlc_blackboard/application/receipts.py`
- `src/sdlc_blackboard/domain/errors.py`
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py`

## promote_artifact

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:153`

1. MCP tool delegates to `ArtifactService.promote_artifact` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:160`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Take a `FOR SHARE` lock on the goal, then load the target revision; fail if it does not exist `src/sdlc_blackboard/application/use_cases/artifact_service.py:27`.
4. Enforce compare-and-set discipline: reject promotion against an existing alias when `expected_current_revision_id` is omitted `src/sdlc_blackboard/application/use_cases/artifact_service.py:40`.
5. CAS-promote the alias to the new revision; a mismatch is a conflict `src/sdlc_blackboard/application/use_cases/artifact_service.py:46`.
6. Mark reviews stale and revoke approvals bound to superseded revisions of the artifact `src/sdlc_blackboard/application/use_cases/artifact_service.py:57`.
7. Append `artifact.promoted`, plus `review.invalidated` / `approval.invalidated` per affected record `src/sdlc_blackboard/application/use_cases/artifact_service.py:64`.

### Related

- `src/sdlc_blackboard/application/commands.py`
- `src/sdlc_blackboard/application/events.py:20`
- `src/sdlc_blackboard/domain/artifacts.py`
- `src/sdlc_blackboard/application/use_cases/gate_service.py:148`

## get_gate_status

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_read.py:59`

1. MCP read tool delegates to `GateService.get_gate_status` `src/sdlc_blackboard/interfaces/mcp/tools_read.py:63`.
2. Open a read connection and list the goal's tasks `src/sdlc_blackboard/application/use_cases/gate_service.py:66`.
3. Derive the required review types (union of blocking requirements) and the implementation artifact's logical name from the task contracts `src/sdlc_blackboard/application/use_cases/gate_service.py:67`.
4. Resolve the implementation binding from the current aliases; absent binding returns UNSATISFIED `src/sdlc_blackboard/application/use_cases/gate_service.py:71`.
5. Match reviews to the current binding fingerprint, collecting stale and satisfied-approved review types `src/sdlc_blackboard/application/use_cases/gate_service.py:92`.
6. Check for a non-revoked human release approval bound to the current binding `src/sdlc_blackboard/application/use_cases/gate_service.py:108`.
7. Fold reviews, blockers, and approval into SATISFIED / HUMAN_REQUIRED / UNSATISFIED and return the `GateResult` `src/sdlc_blackboard/application/use_cases/gate_service.py:116`.

### Related

- `src/sdlc_blackboard/application/use_cases/gate_service.py:134`
- `src/sdlc_blackboard/application/use_cases/gate_service.py:148`
- `src/sdlc_blackboard/domain/events.py`
- `src/sdlc_blackboard/domain/reviews.py`

## create_task

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:54`

1. MCP tool delegates to `TaskService.create_task` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:61`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Load the goal; fail if it does not exist `src/sdlc_blackboard/application/use_cases/task_service.py:61`.
4. Idempotency on `task_key`: return an identical existing contract, or conflict on a divergent one `src/sdlc_blackboard/application/use_cases/task_service.py:64`.
5. Validate every declared dependency belongs to the same goal `src/sdlc_blackboard/application/use_cases/task_service.py:69`.
6. Insert the task as DRAFT (if it has dependencies) or READY, and persist dependency edges `src/sdlc_blackboard/application/use_cases/task_service.py:73`.
7. Append `task.created`, plus `task.ready` when the task starts READY `src/sdlc_blackboard/application/use_cases/task_service.py:89`.

### Related

- `src/sdlc_blackboard/domain/tasks.py`
- `src/sdlc_blackboard/application/use_cases/task_service.py:426`
- `src/sdlc_blackboard/domain/errors.py`
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py`

## start_runtime_run

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:92`

1. MCP tool delegates to `TaskService.start_runtime_run` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:98`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Lock the task; validate epoch and owning actor, and require state ASSIGNED or AWAITING_INPUT `src/sdlc_blackboard/application/use_cases/task_service.py:157`.
4. Select the routing class: an explicit request value wins (invalid string becomes a validation failure), else default from the task's required actor kind via the Lean-certified routing policy `src/sdlc_blackboard/application/use_cases/task_service.py:170`.
5. Build a RUNNING `RuntimeRun` capturing input manifest, routing class, and model provenance (provider, model id, region, harness) `src/sdlc_blackboard/application/use_cases/task_service.py:180`.
6. Insert the run and CAS-transition the task to RUNNING `src/sdlc_blackboard/application/use_cases/task_service.py:193`.
7. Append `runtime.started` and return the run `src/sdlc_blackboard/application/use_cases/task_service.py:200`.

### Related

- `src/sdlc_blackboard/domain/events.py`
- `src/sdlc_blackboard/domain/routing.py:56`
- `src/sdlc_blackboard/domain/transitions.py`
- `src/sdlc_blackboard/application/commands.py`
- `src/sdlc_blackboard/application/use_cases/task_service.py:418`

## submit_review

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:143`

1. MCP tool delegates to `ReviewService.submit_review` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:150`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Take a `FOR SHARE` lock on the goal, load the review task, and require at least one artifact binding `src/sdlc_blackboard/application/use_cases/review_service.py:120`.
4. Verify every listed finding exists `src/sdlc_blackboard/application/use_cases/review_service.py:127`.
5. Block an APPROVED review that still carries an unresolved blocking finding it created `src/sdlc_blackboard/application/use_cases/review_service.py:131`.
6. Insert the review with its binding fingerprint and append `review.submitted` `src/sdlc_blackboard/application/use_cases/review_service.py:159`.

### Related

- `src/sdlc_blackboard/domain/reviews.py`
- `src/sdlc_blackboard/domain/findings.py`
- `src/sdlc_blackboard/application/events.py:20`
- `src/sdlc_blackboard/application/use_cases/gate_service.py:92`

## accept_task

Entry point: `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:111`

1. MCP tool delegates to `TaskService.accept_task` `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:121`.
2. `_command` opens the transaction and runs the body idempotently `src/sdlc_blackboard/application/use_cases/base.py:54`.
3. Lock the task; return unchanged if already ACCEPTED (idempotent) `src/sdlc_blackboard/application/use_cases/task_service.py:317`.
4. From SUBMITTED, CAS-transition to UNDER_REVIEW; from UNDER_REVIEW resume in place; otherwise fail precondition `src/sdlc_blackboard/application/use_cases/task_service.py:323`.
5. CAS-transition the intermediate state to ACCEPTED through the legal transition matrix `src/sdlc_blackboard/application/use_cases/task_service.py:339`.
6. Append `task.accepted` and return the accepted task `src/sdlc_blackboard/application/use_cases/task_service.py:345`.

### Related

- `src/sdlc_blackboard/domain/transitions.py`
- `src/sdlc_blackboard/application/use_cases/task_service.py:422`
- `src/sdlc_blackboard/domain/errors.py`
- `src/sdlc_blackboard/application/commands.py`

## Minor flows

- create_goal — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:45`. Insert an ACTIVE goal and append `goal.created` in one idempotent transaction (`src/sdlc_blackboard/application/use_cases/goal_service.py:25`).
- refresh_ready_tasks — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:64`. Promote draft tasks whose dependencies are all accepted to READY and emit `task.ready` (`src/sdlc_blackboard/application/use_cases/task_service.py:97`).
- bind_runtime_session — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:83`. Validate the assignment epoch and bind an Omnigent conversation id to the task (`src/sdlc_blackboard/application/use_cases/task_service.py:134`).
- open_finding — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:124`. Insert an immutable finding; a blocking finding requires a reviewer-kind task whose contract permits it (`src/sdlc_blackboard/application/use_cases/review_service.py:30`).
- resolve_finding — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:134`. CAS the finding's resolution state via optimistic version and append a state event (`src/sdlc_blackboard/application/use_cases/review_service.py:87`).
- record_human_approval — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:163`. Insert a revision-bound human approval and append `approval.created` (`src/sdlc_blackboard/application/use_cases/review_service.py:179`).
- authorize_goal_completion — entry at `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:172`. Take a `FOR UPDATE` lock on the goal row, re-evaluate the release gate on the same transaction, and CAS the goal to SATISFIED only if the gate is SATISFIED — enforcing, not advisory (`src/sdlc_blackboard/application/use_cases/goal_service.py:55`).
- get_goal_snapshot — entry at `src/sdlc_blackboard/interfaces/mcp/tools_read.py:19`. Read-side compose of tasks, aliases, open findings, reviews, approvals, and ready ids (`src/sdlc_blackboard/application/use_cases/query_service.py:24`).
- get_task_contract — entry at `src/sdlc_blackboard/interfaces/mcp/tools_read.py:29`. Read the goal snapshot and project one task's contract as JSON (`src/sdlc_blackboard/interfaces/mcp/tools_read.py:33`).
- get_artifact_revision — entry at `src/sdlc_blackboard/interfaces/mcp/tools_read.py:42`. Return one immutable artifact revision by id (`src/sdlc_blackboard/application/use_cases/query_service.py:46`).
- read_relevant_events — entry at `src/sdlc_blackboard/interfaces/mcp/tools_read.py:51`. Keyset-paginated read of a goal's event log in occurrence order (`src/sdlc_blackboard/application/use_cases/query_service.py:50`).
- CLI migrate — entry at `src/sdlc_blackboard/interfaces/cli.py:29`. Shell out to dbmate to apply pending SQL migrations (`src/sdlc_blackboard/infrastructure/migrations.py:35`).
- CLI list-goals / snapshot / events / gate — entry at `src/sdlc_blackboard/interfaces/cli.py:36`. Operator read commands that build a container and call the query/gate services.
- CLI thrash — entry at `src/sdlc_blackboard/interfaces/cli.py:104`. Operator-only read of a goal's coordination-thrash report (conflicts, stale versions, review rejections, reclaims) as JSON; deliberately NOT an MCP tool so agents cannot read and game their own thrash metric (`src/sdlc_blackboard/application/use_cases/thrash_service.py:39`).
- CLI outbox-relay — entry at `src/sdlc_blackboard/interfaces/cli.py:125`. Operator-invoked relay that drains the transactional outbox: `OutboxService.drain_outbox` claims unpublished rows, publishes each as a structured log line, and marks them published — the claim, publish, and `published_at` write commit atomically for at-least-once delivery (`src/sdlc_blackboard/application/use_cases/outbox_service.py:28`). Supports a one-shot drain or a bounded poll loop.
- CLI reset-demo — entry at `src/sdlc_blackboard/interfaces/cli.py:158`. Destructive truncate of all domain state including the command-failure ledger; CLI-only, never on the MCP surface (`src/sdlc_blackboard/interfaces/cli.py:166`).
- serve_blackboard — entry at `scripts/serve_blackboard.py:14`. Boot the FastMCP server over HTTP on a configurable loopback host/port.
- run_scripted_demo — entry at `scripts/run_scripted_demo.py:105`. Operator script that drives the full report-export lifecycle end to end against live Postgres, no LLMs (`scripts/run_scripted_demo.py:49`).
- bb CLI — entry at `scripts/bb.py:28`. Thin FastMCP client that forwards one tool call with a JSON argument object for specialist agents (`scripts/bb.py:25`).
- live_lead_create_goal / live_resort_create_goal — entry at `scripts/live_lead_create_goal.py`, `scripts/live_resort_create_goal.py`. Seed a goal and dependency-aware task graph through the real MCP tools.
- validate_team — entry at `scripts/validate_team.py:26`. Parse each `sdlc_team` agent config through the Omnigent parser and assert local-only sandbox backends.

## See also

- [impact-analysis](../insights/impact-analysis.md) — 17 shared source citations
- [contract-map](../insights/contract-map.md) — 15 shared source citations
- [debugging-guide](../insights/debugging-guide.md) — 11 shared source citations
- [tech-debt](../insights/tech-debt.md) — 11 shared source citations
- [business-logic](../insights/business-logic.md) — 10 shared source citations
