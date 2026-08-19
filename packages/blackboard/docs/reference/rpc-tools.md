# omnigent-blackboard-poc · RPC tools

The server is a FastMCP instance named "SDLC Blackboard" (`src/sdlc_blackboard/interfaces/mcp/server.py:59`). Every method below is registered with the `@mcp.tool` decorator and is a thin adapter that delegates to one `Services` facade call. Read tools inspect authoritative state without mutation; command tools carry a `CommandContext` (idempotency key, optimistic version, fencing epoch) and return a structured `CommandResult` so callers never infer success from prose. The `ctx: Context` parameter on every tool is FastMCP's injected request context, not a client-supplied argument. The surface is exactly 19 tools; the coordination-thrash report added in ADR-0014 is deliberately operator-only (the `blackboard thrash` CLI command, not an MCP tool) so agents cannot observe their own thrash metric.

## accept_task

```python
@mcp.tool
async def accept_task(
    command: CommandContext, request: AcceptTaskRequest, ctx: Context
) -> CommandResult[Task]:
```

Accept a SUBMITTED producer task, advancing it SUBMITTED → UNDER_REVIEW → ACCEPTED so the release gate sees an accepted binding; idempotent and resumable from a partially-advanced state.

**Input:** `command: CommandContext`, `request: AcceptTaskRequest`.
**Output:** `CommandResult[Task]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:112`

## authorize_goal_completion

```python
@mcp.tool
async def authorize_goal_completion(
    command: CommandContext, goal_id: UUID, ctx: Context
) -> CommandResult[Goal]:
```

Mark a goal SATISFIED via optimistic version. The release gate is enforced in the same transaction: the handler takes a `FOR UPDATE` lock on the goal row, re-evaluates the gate, and rejects with `precondition_failed` unless it is SATISFIED — a prior `get_gate_status` read is informational, not a substitute (ADR-0012).

**Input:** `command: CommandContext`, `goal_id: UUID`.
**Output:** `CommandResult[Goal]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:173`

## bind_runtime_session

```python
@mcp.tool
async def bind_runtime_session(
    command: CommandContext, request: BindRuntimeSessionRequest, ctx: Context
) -> CommandResult[Task]:
```

Bind an Omnigent child conversation id to the current assignment, validating the assignment epoch and returning a structured conflict if the assignment is stale.

**Input:** `command: CommandContext`, `request: BindRuntimeSessionRequest`.
**Output:** `CommandResult[Task]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:84`

## claim_task

```python
@mcp.tool
async def claim_task(
    command: CommandContext, request: ClaimTaskRequest, ctx: Context
) -> CommandResult[ClaimReceipt]:
```

Atomically assign one READY task and return the fencing epoch that later worker mutations must carry; concurrent or stale claims return a structured conflict.

**Input:** `command: CommandContext`, `request: ClaimTaskRequest`.
**Output:** `CommandResult[ClaimReceipt]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:74`

## create_goal

```python
@mcp.tool
async def create_goal(
    command: CommandContext, goal: GoalCreate, ctx: Context
) -> CommandResult[Goal]:
```

Create a goal with explicit success criteria and constraints; idempotent by `command.command_id`.

**Input:** `command: CommandContext`, `goal: GoalCreate`.
**Output:** `CommandResult[Goal]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:46`

## create_task

```python
@mcp.tool
async def create_task(
    command: CommandContext, task: TaskContractCreate, ctx: Context
) -> CommandResult[Task]:
```

Create one bounded task contract on an existing goal with goal-local dependencies; idempotent by `command.command_id`, returning a conflict when a same-key contract differs.

**Input:** `command: CommandContext`, `task: TaskContractCreate`.
**Output:** `CommandResult[Task]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:55`

## get_artifact_revision

```python
@mcp.tool
async def get_artifact_revision(revision_id: UUID, ctx: Context) -> ArtifactRevision:
```

Return one immutable artifact revision by id; read-only.

**Input:** `revision_id: UUID`.
**Output:** `ArtifactRevision`.

`src/sdlc_blackboard/interfaces/mcp/tools_read.py:43`

## get_gate_status

```python
@mcp.tool
async def get_gate_status(goal_id: UUID, ctx: Context) -> GateResult:
```

Evaluate the release gate for a goal — current implementation binding, missing or stale reviews, open blocking findings, and missing approvals; read-only.

**Input:** `goal_id: UUID`.
**Output:** `GateResult`.

`src/sdlc_blackboard/interfaces/mcp/tools_read.py:60`

## get_goal_snapshot

```python
@mcp.tool
async def get_goal_snapshot(goal_id: UUID, ctx: Context) -> GoalSnapshot:
```

Return a compact snapshot of a goal — its tasks, current artifact aliases, open findings, reviews, approvals, and ready task ids; read-only.

**Input:** `goal_id: UUID`.
**Output:** `GoalSnapshot`.

`src/sdlc_blackboard/interfaces/mcp/tools_read.py:20`

## get_task_contract

```python
@mcp.tool
async def get_task_contract(goal_id: UUID, task_id: UUID, ctx: Context) -> dict[str, object]:
```

Return the exact task contract (objective, scope, deliverables, acceptance criteria, review requirements, epoch) for one task; read-only.

**Input:** `goal_id: UUID`, `task_id: UUID`.
**Output:** `dict[str, object]` (the task's `model_dump(mode="json")`).

`src/sdlc_blackboard/interfaces/mcp/tools_read.py:30`

## open_finding

```python
@mcp.tool
async def open_finding(
    command: CommandContext, finding: FindingCreate, ctx: Context
) -> CommandResult[Finding]:
```

Open a finding against artifact revisions; a blocking finding requires a task whose contract permits it, and the assertion is immutable while its resolution state is versioned.

**Input:** `command: CommandContext`, `finding: FindingCreate`.
**Output:** `CommandResult[Finding]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:125`

## promote_artifact

```python
@mcp.tool
async def promote_artifact(
    command: CommandContext, request: PromoteArtifactRequest, ctx: Context
) -> CommandResult[ArtifactAlias]:
```

Promote the current alias for a logical artifact to a new revision via compare-and-set, staling or revoking reviews and approvals bound to the superseded revision in the same transaction.

**Input:** `command: CommandContext`, `request: PromoteArtifactRequest`.
**Output:** `CommandResult[ArtifactAlias]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:154`

## read_relevant_events

```python
@mcp.tool
async def read_relevant_events(
    goal_id: UUID, ctx: Context, limit: int = 100
) -> tuple[TeamEvent, ...]:
```

Return the goal's event log in occurrence order (keyset-paginated); read-only.

**Input:** `goal_id: UUID`, `limit: int = 100`.
**Output:** `tuple[TeamEvent, ...]`.

`src/sdlc_blackboard/interfaces/mcp/tools_read.py:52`

## record_human_approval

```python
@mcp.tool
async def record_human_approval(
    command: CommandContext, approval: ApprovalSubmission, ctx: Context
) -> CommandResult[Approval]:
```

Record an immutable, revision-bound human approval; the gate requires a non-revoked approval for the current binding.

**Input:** `command: CommandContext`, `approval: ApprovalSubmission`.
**Output:** `CommandResult[Approval]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:164`

## refresh_ready_tasks

```python
@mcp.tool
async def refresh_ready_tasks(
    command: CommandContext, request: RefreshReadyTasksRequest, ctx: Context
) -> CommandResult[TaskListReceipt]:
```

Promote draft tasks whose dependencies are all accepted to READY and return the newly-ready tasks; idempotent and safe to re-run.

**Input:** `command: CommandContext`, `request: RefreshReadyTasksRequest`.
**Output:** `CommandResult[TaskListReceipt]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:65`

## resolve_finding

```python
@mcp.tool
async def resolve_finding(
    command: CommandContext, request: ResolveFindingRequest, ctx: Context
) -> CommandResult[Finding]:
```

Transition a finding's resolution state (e.g. remediated, verified, accepted_risk) via optimistic version; idempotent by `command.command_id`.

**Input:** `command: CommandContext`, `request: ResolveFindingRequest`.
**Output:** `CommandResult[Finding]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:135`

## start_runtime_run

```python
@mcp.tool
async def start_runtime_run(
    command: CommandContext, request: StartRunRequest, ctx: Context
) -> CommandResult[RuntimeRun]:
```

Start one runtime execution attempt for an assigned task, validating epoch and actor, transitioning the task to RUNNING, and recording model provenance.

**Input:** `command: CommandContext`, `request: StartRunRequest`.
**Output:** `CommandResult[RuntimeRun]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:93`

## submit_review

```python
@mcp.tool
async def submit_review(
    command: CommandContext, review: ReviewSubmission, ctx: Context
) -> CommandResult[Review]:
```

Submit a review bound to exact artifact revisions, unique by reviewer, type, and binding fingerprint; an approved review may not carry an unresolved blocking finding it created.

**Input:** `command: CommandContext`, `review: ReviewSubmission`.
**Output:** `CommandResult[Review]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:144`

## submit_task_result

```python
@mcp.tool
async def submit_task_result(
    command: CommandContext, request: SubmitTaskResult, ctx: Context
) -> CommandResult[TaskSubmissionReceipt]:
```

Atomically submit a task result — insert immutable artifact revisions, complete the run and assignment, transition the task to SUBMITTED, and create or reopen required review tasks — validating epoch, actor, active run, and input manifest.

**Input:** `command: CommandContext`, `request: SubmitTaskResult`.
**Output:** `CommandResult[TaskSubmissionReceipt]`.

`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:102`

## See also

- [data-flow](../architecture/data-flow.md) — 3 shared source citations
- [processes](../behavior/processes.md) — 3 shared source citations
- [contract-map](../insights/contract-map.md) — 3 shared source citations
- [impact-analysis](../insights/impact-analysis.md) — 3 shared source citations
- [module-map](../architecture/module-map.md) — 2 shared source citations
