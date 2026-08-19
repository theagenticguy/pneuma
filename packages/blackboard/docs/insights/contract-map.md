# omnigent-blackboard-poc · Contract map

This is a typed Python codebase organized in clean/hexagonal layers under
`src/sdlc_blackboard/`: `src/sdlc_blackboard/domain/` (pure Pydantic value objects and state machines),
`src/sdlc_blackboard/application/` (use-case services, ports, DTOs), `interfaces/` (the FastMCP driving
adapter), and `infrastructure/` (asyncpg adapters). For this file a **contract** is a
type declared in one module and depended on across a boundary by another module — a
frozen Pydantic `DomainModel`, a `BaseModel` DTO, a `StrEnum`, or a
`typing.Protocol`. Because the code is fully typed, each Shape block is quoted
verbatim from the producing declaration. Two kinds of boundary carry the most weight:
the **domain value objects** that every layer shares (chiefly `ArtifactBinding`), and
the **port `Protocol` seam** (`src/sdlc_blackboard/application/ports.py`) that the infrastructure adapters
satisfy structurally without importing the application layer. A third kind of contract
is now formal: two pure domain tables (`ROUTING_POLICY` and `ThrashReport`'s fold
semantics) are transcribed constructor-for-constructor from a Lean 4 model under
`formal/` and pinned in lockstep by `tests/contract/` — a change to one side without the
other breaks the build. Contracts are ranked by number of consuming modules; the top 12
are detailed, the remainder listed under `## Other contracts`.

Every consuming site is verified from a grep across `src tests scripts` plus reads of
the cited files. `demo_app/` is excluded by orchestrator directive.

## ArtifactBinding

The single most-shared value object: an immutable pointer that names an exact
artifact revision by id + hash + logical name. Reviews, approvals, findings, events,
task contracts, the runtime manifest, the gate, and the query snapshot all bind it.

**Producer:** `src/sdlc_blackboard/domain/common.py:109-119`

**Consumer(s):**
- `src/sdlc_blackboard/domain/tasks.py:60` — `TaskContractCreate.inputs` manifest.
- `src/sdlc_blackboard/domain/events.py:61` — `TeamEvent.artifact_bindings` and `RuntimeRun.input_manifest` (`src/sdlc_blackboard/domain/events.py:41`).
- `src/sdlc_blackboard/domain/findings.py:58` — `FindingCreate.affected_artifacts` / `Finding.affected_artifacts` (`src/sdlc_blackboard/domain/findings.py:71`).
- `src/sdlc_blackboard/domain/reviews.py:58` — `ReviewSubmission.artifact_bindings`; fingerprinted at `src/sdlc_blackboard/domain/reviews.py:32-39`.
- `src/sdlc_blackboard/domain/approvals.py:32` — `ApprovalSubmission.artifact_bindings`.
- `src/sdlc_blackboard/application/commands.py:39,52` — `StartRunRequest.input_manifest`, `SubmitTaskResult.input_manifest`.
- `src/sdlc_blackboard/application/query_models.py:22` — `GoalSnapshot.artifact_aliases`.
- `src/sdlc_blackboard/application/ports.py:147` — `ArtifactRepo.list_aliases` return type.
- `src/sdlc_blackboard/application/use_cases/gate_service.py:89-111` — gate binding match via the shared `single_binding_fingerprint`.
- `src/sdlc_blackboard/infrastructure/repositories/artifacts.py:169` — constructed from a joined alias+revision row.

**Shape:**
```python
class ArtifactBinding(DomainModel):
    """Names an exact, immutable artifact revision by id + hash.

    Reviews and approvals bind these so authority always names a concrete revision,
    never a mutable pointer.
    """

    artifact_id: UUID
    revision_id: UUID
    logical_name: NonEmptyStr
    content_hash: NonEmptyStr
```

**Assumptions consumers make:**
- The binding names a *concrete* revision, never a mutable alias — the gate compares against `impl_binding` produced from the current alias and treats any non-matching review as not-bound-current (`src/sdlc_blackboard/application/use_cases/gate_service.py:71,95-99`).
- All four fields participate in identity: the review fingerprint sorts `artifact_id:revision_id:content_hash` triples but omits `logical_name` (`src/sdlc_blackboard/domain/reviews.py:38`), and the gate now matches through the *same* function on a single binding (`src/sdlc_blackboard/domain/reviews.py:42`) — logical_name is display-only for identity purposes.
- `logical_name` and `content_hash` are `NonEmptyStr`; the repository trusts the DB row is non-empty when constructing bindings (`src/sdlc_blackboard/infrastructure/repositories/artifacts.py:169`).

**Drift risk (RESOLVED, `9d47451`):** The dual-fingerprint hazard is fixed. There was previously a second, independent fingerprint (`gate_service._binding_fp`, a raw string) alongside `reviews.binding_fingerprint` (sorted, hashed), so adding or reordering an identity field in one without the other silently desynchronized review uniqueness from gate matching. `_binding_fp` is deleted; the domain now exposes `single_binding_fingerprint` (`src/sdlc_blackboard/domain/reviews.py:42-50`) defined as exactly `binding_fingerprint((binding,))`, and the gate calls it (`src/sdlc_blackboard/application/use_cases/gate_service.py:89-111`). One definition, two call sites — the drift point is gone.

## Port Protocols + ServicePorts

The load-bearing seam of the codebase: a bundle of `@runtime_checkable Protocol`s
declared next to their consumers (application), implemented by asyncpg adapters in
infrastructure by shape only. `ServicePorts` is the frozen dataclass every service is
constructed from.

**Producer:** `src/sdlc_blackboard/application/ports.py:39-256` (the Protocols) and `src/sdlc_blackboard/application/use_cases/wiring.py:30-47` (`ServicePorts`).

**Consumer(s):**
- `src/sdlc_blackboard/application/use_cases/base.py:29,54` — `CommandService` holds `ServicePorts`, opens `self._p.uow.begin()`.
- `src/sdlc_blackboard/application/use_cases/base.py:71-102` — `_record_command_failure` writes `self._p.command_failures` in a second transaction after a caught `DomainError`.
- `src/sdlc_blackboard/application/use_cases/task_service.py:60-303` — calls `goals`, `tasks`, `assignments`, `runs`, `artifacts`, `events` ports.
- `src/sdlc_blackboard/application/use_cases/gate_service.py:38,66-77` — reads `tasks`, `artifacts`, `findings`, `reviews`, `approvals` ports.
- `src/sdlc_blackboard/application/use_cases/query_service.py:20-44` — reads via the same ports.
- `src/sdlc_blackboard/application/use_cases/thrash_service.py:39-53` — reads `command_failures`, `reviews`, `events`, `tasks` ports (read-only, own UoW).
- `src/sdlc_blackboard/infrastructure/di.py:44-61` — `build_ports` binds concrete `*Repository` adapters into the bundle.
- `src/sdlc_blackboard/infrastructure/repositories/__init__.py:38-51` — the twelve `*Repository` classes (one module per aggregate) are re-exported here and implement the port method signatures.

**Shape:**
```python
@dataclass(frozen=True)
class ServicePorts:
    """Immutable bundle of every port a command/query service needs."""

    uow: UnitOfWork
    clock: Clock
    goals: GoalRepo
    tasks: TaskRepo
    assignments: AssignmentRepo
    runs: RuntimeRunRepo
    artifacts: ArtifactRepo
    findings: FindingRepo
    reviews: ReviewRepo
    approvals: ApprovalRepo
    events: EventRepo
    outbox: OutboxRepo
    processed_commands: ProcessedCommandStore
    command_failures: CommandFailureRepo
```

Representative port (all take an opaque `Conn` and speak domain types):
```python
@runtime_checkable
class TaskRepo(Protocol):
    async def insert(self, conn: Conn, task: Task) -> None: ...
    async def get(self, conn: Conn, task_id: UUID) -> Task | None: ...
    async def get_for_update(self, conn: Conn, task_id: UUID) -> Task | None: ...
    async def get_by_key(self, conn: Conn, goal_id: UUID, task_key: str) -> Task | None: ...
    async def list_for_goal(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]: ...
    async def add_dependencies(
        self, conn: Conn, task_id: UUID, depends_on: tuple[UUID, ...]
    ) -> None: ...
    async def refresh_ready(self, conn: Conn, goal_id: UUID) -> tuple[Task, ...]: ...
    async def claim_cas(
        self, conn: Conn, task_id: UUID, expected_version: int, actor_id: str, next_epoch: int
    ) -> Task | None: ...
    async def transition_cas(
        self,
        conn: Conn,
        task_id: UUID,
        expected_version: int,
        expected_state: TaskState,
        new_state: TaskState,
        assigned_actor_id: str | None = None,
    ) -> Task | None: ...
    async def bind_conversation(
        self, conn: Conn, task_id: UUID, epoch: int, conversation_id: str
    ) -> Task | None: ...
```

**Assumptions consumers make:**
- CAS methods return `None` to signal a lost optimistic-version race, not raise; services translate `None` into `StaleVersion()` (`src/sdlc_blackboard/application/use_cases/task_service.py:126-127,198-199,290-291`).
- `Conn` is opaque (`type Conn = object`, `src/sdlc_blackboard/application/ports.py:36`); services only thread it into repo calls and never call methods on it — the adapter narrows it to `asyncpg.Connection` (`repositories/_common.py` `conn_of`).
- Ports return *domain* types (`Task`, `Goal`), never asyncpg `Record`s — the docstring makes this a non-negotiable (`src/sdlc_blackboard/application/ports.py:8-9`).
- Every command runs inside one `uow.begin()` transaction spanning domain mutation + event + outbox + processed-command write (`src/sdlc_blackboard/application/use_cases/base.py:54-63`, `src/sdlc_blackboard/application/ports.py:11-14`).

**Drift risk:** Because adapters implement by shape (not inheritance), a signature change to a port (rename a param, change a return type) is caught only by the type checker / `runtime_checkable` at wiring — a repository whose method drifts still satisfies `isinstance` for method *presence*. Mitigation: keep `mypy`/`ty` green in CI and add an integration test that exercises each adapter through its port.

## CommandResult + ErrorCode / CommandStatus envelope

Every mutating use case returns `CommandResult[T]`; conflicts are values, not
exceptions. `ErrorCode` is the wire enum mapping 1:1 onto domain error codes.

**Producer:** `src/sdlc_blackboard/application/results.py:18-104`

**Consumer(s):**
- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:45-178` — every `@mcp.tool` return type is `CommandResult[...]`.
- `src/sdlc_blackboard/application/use_cases/base.py:39,69` — `_command` returns it; `from_domain_error` on caught `DomainError`.
- `src/sdlc_blackboard/application/idempotency.py:51,68-82` — `execute_idempotently` builds `accepted`/`failed`, replays stored results.
- `src/sdlc_blackboard/application/use_cases/task_service.py:57,94` (and goal/review/artifact services) — declared return types.
- `tests/e2e/test_scripted_flow.py`, `tests/integration/test_reliability_invariants.py` — assert on `status`/`error`.

**Shape:**
```python
class CommandResult[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CommandStatus
    value: T | None = None
    error: CommandError | None = None
    replayed: bool = False

    @classmethod
    def accepted(cls, value: T, *, replayed: bool = False) -> CommandResult[T]:
        status = CommandStatus.DUPLICATE_REPLAYED if replayed else CommandStatus.ACCEPTED
        return cls(status=status, value=value, replayed=replayed)

    @classmethod
    def failed(cls, error: CommandError) -> CommandResult[T]:
        status = _ERROR_CODE_TO_STATUS.get(error.code, CommandStatus.VALIDATION_FAILED)
        return cls(status=status, error=error)
```

**Assumptions consumers make:**
- Success is `status == ACCEPTED` or `DUPLICATE_REPLAYED`; callers never infer success from prose — the server instructions state the `CommandResult` is authoritative (`src/sdlc_blackboard/interfaces/mcp/server.py:61-65`).
- On success `value` is populated and `error` is `None`; on failure the inverse — `accepted`/`failed` enforce this but nothing type-level prevents a caller reading `value` on a failure.
- A raised `DomainError` is caught OUTSIDE the unit of work so the transaction rolls back naturally, then a best-effort *second* transaction appends a command-failure ledger row before returning `from_domain_error` (`src/sdlc_blackboard/application/use_cases/base.py:47-102`); the ledger write never masks the original failure (logs `command.failure_unrecorded` on its own failure).
- `ErrorCode` values equal domain `DomainError.code` strings, so `_DOMAIN_TO_ERROR_CODE` is total (`src/sdlc_blackboard/application/results.py:44`, keyed by iterating `ErrorCode`); an unknown domain code falls back to `INTERNAL_ERROR` (`src/sdlc_blackboard/application/results.py:72`).
- `extra="forbid"` means the replay path deserializing a stored result rejects any added field (`src/sdlc_blackboard/application/idempotency.py:76`).

**Drift risk:** `_ERROR_CODE_TO_STATUS` (`src/sdlc_blackboard/application/results.py:47-57`) is a hand-maintained dict; adding a new `ErrorCode` variant without a status entry silently degrades it to `VALIDATION_FAILED` via `.get` default. Mitigation: make the map total (assert every `ErrorCode` is a key at import time).

## CommandContext

The envelope carried by every mutating command: idempotency key, actor identity,
optimistic-version and fencing-epoch checks.

**Producer:** `src/sdlc_blackboard/domain/common.py:92-106`

**Consumer(s):**
- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:45-178` — first parameter of every command tool.
- `src/sdlc_blackboard/application/use_cases/base.py:34,58` — threaded into `execute_idempotently` (and into `_record_command_failure` on failure).
- `src/sdlc_blackboard/application/idempotency.py:46,63,87` — reads `command_id`, `actor.actor_id`.
- `src/sdlc_blackboard/application/events.py:30,41-43` — reads `actor`, `correlation_id`, `causation_id`.
- `src/sdlc_blackboard/application/use_cases/task_service.py:411-420` — `_require_epoch` / `_require_actor` compare `assignment_epoch` and `actor.actor_id`.

**Shape:**
```python
class CommandContext(DomainModel):
    """Envelope carried by every mutating command.

    ``command_id`` drives idempotency; a client reuses it only when retrying the
    exact same mutation. ``expected_version`` and ``assignment_epoch`` carry the
    optimistic-concurrency and fencing checks.
    """

    command_id: UUID = Field(default_factory=uuid4)
    actor: ActorRef
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    assignment_epoch: int | None = Field(default=None, ge=0)
    schema_version: str = "1.0"
```

**Assumptions consumers make:**
- `command_id` defaults to a fresh uuid4; a client must *reuse* it to get idempotent replay — reusing it with a different payload yields `DUPLICATE_COMMAND_MISMATCH` (`src/sdlc_blackboard/application/idempotency.py:64-73`).
- `assignment_epoch` is optional; `_require_epoch` only enforces it when the caller supplies one (`src/sdlc_blackboard/application/use_cases/task_service.py:411-416`) — omitting it skips the fencing check.
- `actor.actor_id` is the ownership key for `_require_actor` (`src/sdlc_blackboard/application/use_cases/task_service.py:418-420`) and the stored idempotency record's actor (`src/sdlc_blackboard/application/idempotency.py:87`).

**Drift risk:** `schema_version` defaults to `"1.0"` but no consumer branches on it; a future breaking change to the envelope has no dispatch point. Mitigation: gate parsing on `schema_version` before it diverges, or drop the field to avoid implying versioned handling that does not exist.

## Task

The task-contract aggregate + its `TaskState` machine — the central mutable entity
the whole workflow advances.

**Producer:** `src/sdlc_blackboard/domain/tasks.py:69-81` (`Task`), `src/sdlc_blackboard/domain/tasks.py:18-31` (`TaskState`).

**Consumer(s):**
- `src/sdlc_blackboard/application/ports.py:77-102` — `TaskRepo` returns `Task`, threads `TaskState`.
- `src/sdlc_blackboard/application/use_cases/task_service.py:73-348` — constructs and transitions tasks.
- `src/sdlc_blackboard/application/use_cases/gate_service.py:134-163` — reads `task.contract.review_requirements` / `deliverables`.
- `src/sdlc_blackboard/application/use_cases/query_service.py:29,35` — `list_for_goal`, `READY` filter.
- `src/sdlc_blackboard/application/receipts.py:16,22,27` — `TaskSubmissionReceipt`, `ClaimReceipt`, `TaskListReceipt` wrap `Task`.
- `src/sdlc_blackboard/domain/transitions.py:49-58` — `can_transition` over `TaskState`.
- `src/sdlc_blackboard/interfaces/mcp/tools_read.py:36-38` — `get_task_contract` iterates `snapshot.tasks`.

**Shape:**
```python
class Task(DomainModel):
    task_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    task_key: NonEmptyStr
    title: NonEmptyStr
    objective: NonEmptyStr
    required_actor_kind: ActorKind
    state: TaskState
    version: int
    assignment_epoch: int
    assigned_actor_id: str | None = None
    omnigent_conversation_id: str | None = None
    contract: TaskContractCreate
```

**Assumptions consumers make:**
- State changes go only through `can_transition` — services call `_require_transition` before every `transition_cas` (`src/sdlc_blackboard/application/use_cases/task_service.py:422-424`, `src/sdlc_blackboard/domain/transitions.py:49-58`); the DB CAS is the final defense.
- `version` is the optimistic-concurrency token passed to CAS; a mismatch returns `None` (`src/sdlc_blackboard/application/use_cases/task_service.py:195-199`).
- `assigned_actor_id is None` means unclaimed — `_require_actor` skips the ownership check when it is `None` (`src/sdlc_blackboard/application/use_cases/task_service.py:418-419`).
- A `Task` with no dependency ids is created directly in `READY`, otherwise `DRAFT` (`src/sdlc_blackboard/application/use_cases/task_service.py:79`).

**Drift risk:** Adding a `TaskState` variant without adding its edges to `_EXPLICIT_EDGES` leaves it unreachable/terminal-by-omission (`src/sdlc_blackboard/domain/transitions.py:15-37`); adding a terminal state also requires updating `TERMINAL_TASK_STATES` (`src/sdlc_blackboard/domain/tasks.py:35-37`). Mitigation: the Hypothesis property suite over `can_transition` should assert every state is reachable and every terminal state has no outbound edge.

## TaskContractCreate

The declarative task contract — scope, deliverables, acceptance criteria, and review
requirements. It is both the create-command input and the embedded `Task.contract`,
and the gate derives required reviews from it.

**Producer:** `src/sdlc_blackboard/domain/tasks.py:52-66`

**Consumer(s):**
- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:54-61` — `create_task` accepts it.
- `src/sdlc_blackboard/application/use_cases/task_service.py:57-94,382-393` — create + synthesized review-task contract.
- `src/sdlc_blackboard/domain/tasks.py:81` — embedded as `Task.contract`.
- `src/sdlc_blackboard/application/use_cases/gate_service.py:142,159` — `contract.review_requirements`, `contract.deliverables`.

**Shape:**
```python
class TaskContractCreate(DomainModel):
    goal_id: UUID
    task_key: NonEmptyStr
    title: NonEmptyStr
    objective: NonEmptyStr
    required_actor_kind: ActorKind
    scope: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...] = ()
    inputs: tuple[ArtifactBinding, ...] = ()
    deliverables: tuple[DeliverableSpec, ...]
    acceptance_criteria: tuple[NonEmptyStr, ...]
    dependency_task_ids: tuple[UUID, ...] = ()
    review_requirements: tuple[ReviewRequirement, ...] = ()
    may_create_blocking_finding: bool = False
    may_modify_repository: bool = False
```

**Assumptions consumers make:**
- `create_task` treats an existing same-key task as idempotent only when `existing.contract == request` (full frozen-model equality), else a conflict (`src/sdlc_blackboard/application/use_cases/task_service.py:63-66`) — so every field participates in contract identity.
- The gate unions `blocking` `ReviewRequirement`s across tasks to derive required review types (`src/sdlc_blackboard/application/use_cases/gate_service.py:134-145`); a contract with no blocking review contributes nothing and the gate falls back to `("quality","security")` (`src/sdlc_blackboard/application/use_cases/gate_service.py:34`).
- `may_create_blocking_finding` on a review task is set only when `req.reviewer_kind in REVIEWER_KINDS and req.blocking` (`src/sdlc_blackboard/application/use_cases/task_service.py:392`).
- `dependency_task_ids` must belong to the same goal or create raises `PreconditionFailed` (`src/sdlc_blackboard/application/use_cases/task_service.py:69-72`).

**Drift risk:** Because contract equality is exact, adding a defaulted field changes the equality of two otherwise-identical contracts only if clients send differing values — a silently divergent default could flip a re-create from idempotent to conflict. Mitigation: keep new fields defaulted and covered by the create-idempotency test.

## TeamEvent

The immutable append-only event record written (with an outbox row) in the same
transaction as every mutation.

**Producer:** `src/sdlc_blackboard/domain/events.py:50-62`

**Consumer(s):**
- `src/sdlc_blackboard/application/events.py:34-47` — `append_domain_event` constructs it.
- `src/sdlc_blackboard/application/ports.py:183,184` — `EventRepo.append` / `read_relevant` signatures (and `count_by_type` at `src/sdlc_blackboard/application/ports.py:192`).
- `src/sdlc_blackboard/application/use_cases/query_service.py:50-60` — `read_relevant_events` returns `tuple[TeamEvent, ...]`.
- `src/sdlc_blackboard/interfaces/mcp/tools_read.py:52-56` — `read_relevant_events` tool return type.
- `src/sdlc_blackboard/infrastructure/repositories/events_outbox.py:45` — `EventRepository` persists and reads it.

**Shape:**
```python
class TeamEvent(DomainModel):
    event_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    task_id: UUID | None
    aggregate_type: NonEmptyStr
    aggregate_id: UUID
    aggregate_version: int
    event_type: NonEmptyStr
    actor: ActorRef
    correlation_id: UUID
    causation_id: UUID | None
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    payload: dict[str, object] = Field(default_factory=dict)
```

**Assumptions consumers make:**
- `event_type` and `aggregate_type` are free-form `NonEmptyStr` strings, not enums — producers pass literals like `"task.created"`, `"artifact.created"` (`src/sdlc_blackboard/application/use_cases/task_service.py:89,261`); consumers reading the log must string-match. The thrash report counts one such literal, `"review_task.reopened"`, via `EventRepo.count_by_type` (`src/sdlc_blackboard/application/use_cases/thrash_service.py:32,52`).
- `append` returns the assigned outbox/event id (`src/sdlc_blackboard/application/ports.py:183`), and the same call must write the outbox row atomically (`src/sdlc_blackboard/application/ports.py:181`).
- `payload` is an untyped `dict[str, object]`; readers infer structure per `event_type` with no schema (`src/sdlc_blackboard/application/use_cases/task_service.py:268,439`).

**Drift risk:** Free-form `event_type` strings and untyped `payload` mean a renamed event type or reshaped payload breaks consumers silently (no compiler signal) — and now the thrash report's `reclaims` counter depends on the exact string `"review_task.reopened"`. Mitigation: promote `event_type` to a `StrEnum` and type payloads per event, or add a consumer-side registry test.

## GateResult

The derived release-gate read — never a stored boolean. Reports the current
implementation binding and every unmet gate condition.

**Producer:** `src/sdlc_blackboard/domain/events.py:71-77` (`GateResult`), `src/sdlc_blackboard/domain/events.py:65-69` (`GateStatus`).

**Consumer(s):**
- `src/sdlc_blackboard/application/use_cases/gate_service.py:41-131` — the sole producer of instances.
- `src/sdlc_blackboard/interfaces/mcp/tools_read.py:59-63` — `get_gate_status` tool return type.
- `tests/integration/test_multi_context_gate.py` — asserts on gate status transitions.

**Shape:**
```python
class GateResult(DomainModel):
    status: GateStatus
    implementation_binding: ArtifactBinding | None = None
    missing_reviews: tuple[str, ...] = ()
    open_blocking_finding_ids: tuple[UUID, ...] = ()
    stale_review_ids: tuple[UUID, ...] = ()
    missing_approvals: tuple[str, ...] = ()
```

**Assumptions consumers make:**
- `status == SATISFIED` requires all four sub-conditions clear; `HUMAN_REQUIRED` is returned when only the human approval is missing (`src/sdlc_blackboard/application/use_cases/gate_service.py:116-122`). Callers must distinguish `HUMAN_REQUIRED` from `UNSATISFIED`.
- `implementation_binding is None` means no impl artifact alias yet — the gate short-circuits to `UNSATISFIED` with all reviews listed missing (`src/sdlc_blackboard/application/use_cases/gate_service.py:79-87`).
- `missing_reviews` / `missing_approvals` are string logical/type names, not ids; the human approval is the literal `"human_release"` (`src/sdlc_blackboard/application/use_cases/gate_service.py:86,114`).
- `authorize_goal_completion` re-evaluates the gate in the same transaction it flips the goal, rejecting with `PreconditionFailed` unless the gate is `SATISFIED` (`src/sdlc_blackboard/application/use_cases/goal_service.py:74-85`); it is enforcing, not advisory.

**Drift risk (RESOLVED, `f461390`, ADR-0012):** The gate/completion split was previously advisory — nothing forced `authorize_goal_completion` to observe a `SATISFIED` `GateResult`, so adding a gate condition updated the read but not the mutation. Now the command takes a `FOR UPDATE` lock on the goal row, calls `GateService.evaluate_on_conn` on that same unit of work, and CASes the state only if satisfied (`src/sdlc_blackboard/application/use_cases/goal_service.py:55-106`). Gate-input writers take `FOR SHARE` on the goal row (`GoalRepo.lock_shared`, `src/sdlc_blackboard/application/ports.py:70`), so concurrent finding/review commits serialize against the evaluation window and no TOCTOU gap remains.

## GoalSnapshot

The compact read-model projection agents fetch instead of full history.

**Producer:** `src/sdlc_blackboard/application/query_models.py:19-27`

**Consumer(s):**
- `src/sdlc_blackboard/application/use_cases/query_service.py:24-44` — the sole producer.
- `src/sdlc_blackboard/interfaces/mcp/tools_read.py:20-39` — `get_goal_snapshot` returns it; `get_task_contract` iterates `snapshot.tasks`.

**Shape:**
```python
class GoalSnapshot(DomainModel):
    goal: Goal
    tasks: tuple[Task, ...]
    artifact_aliases: tuple[ArtifactBinding, ...]
    open_findings: tuple[Finding, ...]
    reviews: tuple[Review, ...]
    approvals: tuple[Approval, ...]
    ready_task_ids: tuple[UUID, ...]
```

**Assumptions consumers make:**
- `open_findings` excludes findings in `RESOLVED_FINDING_STATES` — the field name promises "open" but the filter lives in the query service, not the type (`src/sdlc_blackboard/application/use_cases/query_service.py:34`).
- `ready_task_ids` is derived from `tasks` where `state == READY` (`src/sdlc_blackboard/application/use_cases/query_service.py:35`) — a redundant projection consumers may trust instead of re-scanning `tasks`.
- `reviews` includes stale reviews (no filter at `src/sdlc_blackboard/application/use_cases/query_service.py:32`); consumers wanting only current reviews must check `review.stale` themselves.

**Drift risk:** No current drift risk — the snapshot is a straightforward aggregation with no cross-field invariant beyond the documented filters.

## RuntimeRun

An execution attempt against a claimed task, carrying model provenance for
cost/repro/failure analysis.

**Producer:** `src/sdlc_blackboard/domain/events.py:34-47` (`RuntimeRun`), `src/sdlc_blackboard/domain/events.py:25-31` (`RoutingClass`), `src/sdlc_blackboard/domain/events.py:17-23` (`RunState`).

**Consumer(s):**
- `src/sdlc_blackboard/application/use_cases/task_service.py:157-201,217-223` — constructs a run, validates `task_id`/`assignment_epoch`/`input_manifest` and selects the routing class.
- `src/sdlc_blackboard/application/ports.py:114-123` — `RuntimeRunRepo` signatures.
- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:92-98` — `start_runtime_run` returns `CommandResult[RuntimeRun]`.
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py:246` — `RuntimeRunRepository` persists it (including the `result_manifest` on submit).

**Shape:**
```python
class RuntimeRun(DomainModel):
    run_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    assignment_epoch: int
    actor_id: NonEmptyStr
    omnigent_conversation_id: str | None = None
    state: RunState
    input_manifest: tuple[ArtifactBinding, ...]
    # Model provenance (handoff §15A.8) — required for cost/repro/failure analysis.
    provider: str | None = None
    model_id: str | None = None
    aws_region: str | None = None
    routing_class: RoutingClass | None = None
    harness: str | None = None
```

**Assumptions consumers make:**
- Submit re-reads the run and requires `run.task_id == task.task_id and run.assignment_epoch == task.assignment_epoch`, else `StaleAssignment` (`src/sdlc_blackboard/application/use_cases/task_service.py:220`).
- `run.input_manifest` must equal the submitted manifest exactly or `InputManifestMismatch` (`src/sdlc_blackboard/application/use_cases/task_service.py:222-223`) — tuple equality including order.
- Provenance fields are all optional; `routing_class` follows spec R1/R2/R5B — an explicit `request.routing_class` wins (parsed into the enum; an unknown string becomes `ValidationFailed`), otherwise it defaults from `default_routing_class(task.required_actor_kind)` via the Lean-certified routing policy (`src/sdlc_blackboard/application/use_cases/task_service.py:164-179`).

**Drift risk (RESOLVED, `9d47451` then `19fd0da`):** `routing_class` was previously hardcoded to `None` at start, so a consumer reading it for cost analysis always saw `None`. It now flows through from `StartRunRequest.routing_class` (`src/sdlc_blackboard/application/commands.py:44`) when the request carries one, and otherwise defaults from the actor-kind routing policy (`src/sdlc_blackboard/domain/routing.py`, mirrored in `formal/Blackboard/Routing.lean`); either way it is persisted on the run.

## SubmitTaskResult + submission receipts

The producer's submission payload (`SubmitTaskResult`) and the receipts returned from
claim/submit/refresh commands.

**Producer:** `src/sdlc_blackboard/application/commands.py:48-60` (`SubmitTaskResult`), `src/sdlc_blackboard/application/receipts.py:16-30` (`TaskSubmissionReceipt`, `ClaimReceipt`, `TaskListReceipt`).

**Consumer(s):**
- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py:101-108,73-80,64-70` — `submit_task_result`, `claim_task`, `refresh_ready_tasks` tool payloads/returns.
- `src/sdlc_blackboard/application/use_cases/task_service.py:206-299` — reads `request.run_id`, `request.artifacts`, `request.input_manifest`; persists `disposition`/`summary`/`finding_ids`/`assumptions`/`unresolved_questions`/`residual_risks` into `runtime_runs.result_manifest`; builds `TaskSubmissionReceipt`.
- `src/sdlc_blackboard/application/use_cases/task_service.py:104,129` — `TaskListReceipt`, `ClaimReceipt` construction.

**Shape:**
```python
class SubmitTaskResult(DomainModel):
    task_id: UUID
    run_id: UUID
    disposition: NonEmptyStr
    input_manifest: Annotated[tuple[ArtifactBinding, ...], Field(max_length=_MAX_COLLECTION)]
    artifacts: Annotated[tuple[ArtifactSubmission, ...], Field(max_length=_MAX_COLLECTION)]
    finding_ids: Annotated[tuple[UUID, ...], Field(max_length=_MAX_COLLECTION)] = ()
    # Model-supplied free text (prompt-injectable) — cap element size (NonEmptyStr, 10k)
    # AND collection count so an unbounded jsonb write cannot amplify storage (SEC-01).
    assumptions: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    unresolved_questions: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    residual_risks: Annotated[tuple[NonEmptyStr, ...], Field(max_length=_MAX_COLLECTION)] = ()
    summary: NonEmptyStr
```
```python
class TaskSubmissionReceipt(DomainModel):
    task: Task
    artifact_revisions: tuple[ArtifactRevision, ...]
    review_task_ids: tuple[UUID, ...]
```

**Assumptions consumers make:**
- `run_id` must reference an active run whose epoch matches the task (`src/sdlc_blackboard/application/use_cases/task_service.py:217-221`); `input_manifest` must match the run's exactly (`src/sdlc_blackboard/application/use_cases/task_service.py:222-223`).
- Each `artifacts` entry is deduped by `(artifact_id, content_hash)` — an existing revision with the same hash is reused rather than re-inserted (`src/sdlc_blackboard/application/use_cases/task_service.py:226-233`), so `artifact_revisions` in the receipt may contain pre-existing revisions.
- `disposition`, `summary`, `finding_ids`, `assumptions`, `unresolved_questions`, `residual_risks` are persisted: the handler writes them into the `runtime_runs.result_manifest` jsonb column rather than dropping them (`src/sdlc_blackboard/application/use_cases/task_service.py:274-281`). The free-text list fields are capped in element size and collection count at the wire boundary (`src/sdlc_blackboard/application/commands.py:54-59`) so a prompt-injected payload cannot amplify storage.

**Drift risk (RESOLVED, `9d47451`):** These fields were previously accepted but unused by the handler, so clients could reasonably expect a persistence or action that never happened. They are now persisted to `runtime_runs.result_manifest`, and the free-text lists are bounded (`_MAX_COLLECTION`) to close the unbounded-jsonb-write hazard (SEC-01).

## ActorRef + ActorKind

The actor identity and authority-class enum threaded through commands, events,
reviews, approvals, and goals.

**Producer:** `src/sdlc_blackboard/domain/common.py:87-89` (`ActorRef`), `src/sdlc_blackboard/domain/common.py:25-54` (`ActorKind`), plus `PRODUCER_KINDS`/`REVIEWER_KINDS` frozensets (`src/sdlc_blackboard/domain/common.py:58-84`).

**Consumer(s):**
- `src/sdlc_blackboard/domain/common.py:101` — `CommandContext.actor`.
- `src/sdlc_blackboard/domain/events.py:58` — `TeamEvent.actor`; `RuntimeRun` uses `actor_id` string.
- `src/sdlc_blackboard/domain/reviews.py:56,69` and `approvals.py:30,39` — `reviewer` / `approver`.
- `src/sdlc_blackboard/domain/goals.py:27,36` — `GoalCreate.owner` / `Goal.owner`.
- `src/sdlc_blackboard/domain/tasks.py:57,75` — `required_actor_kind: ActorKind`.
- `src/sdlc_blackboard/domain/routing.py:32-52` — `ROUTING_POLICY` keys every `ActorKind` to a `RoutingClass`.
- `src/sdlc_blackboard/application/use_cases/task_service.py:31,392` — `REVIEWER_KINDS` gate on `may_create_blocking_finding`.

**Shape:**
```python
class ActorRef(DomainModel):
    actor_id: NonEmptyStr
    kind: ActorKind
```
```python
class ActorKind(StrEnum):
    HUMAN = "human"
    LEAD = "lead"
    SYSTEM = "system"
    ANALYST = "analyst"
    ARCHITECT = "architect"
    IMPLEMENTATION = "implementation"
    DATA = "data"
    DOCUMENTATION = "documentation"
    UX = "ux"
    QUALITY = "quality"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    RELEASE = "release"
    PLATFORM = "platform"
    OPERATIONS = "operations"
    FINOPS = "finops"
    SUPPORT = "support"
    VISUAL = "visual"
```

**Assumptions consumers make:**
- `kind` names an authority class (bounded context), not a persona; behavior is driven by the task contract, not the label (`src/sdlc_blackboard/domain/common.py:25-32`).
- A kind's authority is derived through `PRODUCER_KINDS` / `REVIEWER_KINDS` membership — e.g., `may_create_blocking_finding` requires the reviewer kind be in `REVIEWER_KINDS` (`src/sdlc_blackboard/application/use_cases/task_service.py:392`). Note `ARCHITECT` appears in both producer and reviewer sets (`common.py:66,82`).
- All eighteen kinds must have a `ROUTING_POLICY` entry — `default_routing_class` indexes the map directly and would `KeyError` on a missing kind; totality is asserted by the contract test (`src/sdlc_blackboard/domain/routing.py:56-62`).
- `_require_actor` matches on `actor.actor_id` only, ignoring `kind` (`src/sdlc_blackboard/application/use_cases/task_service.py:418-420`) — ownership is by id, authority is by kind.

**Drift risk:** Adding an `ActorKind` variant requires deciding its membership in `PRODUCER_KINDS`/`REVIEWER_KINDS` AND adding a `ROUTING_POLICY` row (and its Lean mirror); a variant absent from the sets silently has neither produce nor review authority, and one absent from `ROUTING_POLICY` makes `start_runtime_run` raise. Mitigation: `tests/contract/test_routing_policy.py` already pins routing totality over all kinds; add a companion test asserting every non-`{HUMAN,LEAD,SYSTEM}` kind is in exactly one authority set (or intentionally both).

## Other contracts

- `ROUTING_POLICY` / `default_routing_class` (`src/sdlc_blackboard/domain/routing.py:32-62`) — pure, total `ActorKind -> RoutingClass` map (18 kinds -> 4 classes); consumed by `start_runtime_run` as the default when the request carries no `routing_class` (`src/sdlc_blackboard/application/use_cases/task_service.py:179`). Transcribed constructor-for-constructor from `formal/Blackboard/Routing.lean` `routingPolicy` and pinned in lockstep by `tests/contract/test_routing_policy.py` (Python table == Lean table over all 18 kinds + tier monotonicity). A change on one side without the other breaks the contract test.
- `CommandFailureRepo` (`src/sdlc_blackboard/application/ports.py:233-256`) — append-only command-failure ledger port implemented by `CommandFailureRepository` (`src/sdlc_blackboard/infrastructure/repositories/failures.py:22-64`), wired at `src/sdlc_blackboard/infrastructure/di.py:60`; consumed by `CommandService._record_command_failure` on a raised `DomainError` (`src/sdlc_blackboard/application/use_cases/base.py:71-102`) and by `ThrashService` for conflict/stale counts. `count_by_error_code_for_goal` resolves task-scoped rows (goal_id NULL) back to their goal via the `tasks` table, so a double-claim conflict recorded with only a `task_id` still counts (migration deliberately has NO FK to goals).
- `ThrashReport` / `ThrashService` (`src/sdlc_blackboard/application/query_models.py:29-54`, `src/sdlc_blackboard/application/use_cases/thrash_service.py:35-63`) — read-only per-goal thrash counters (`conflicts`, `stale_versions`, `review_rejections`, `reclaims`); each counter's fold semantics (zero-on-empty T2, monotone T5, goal-frame T1) mirror `formal/Blackboard/Thrash.lean`. CLI-only via `blackboard thrash GOAL_ID` (`src/sdlc_blackboard/interfaces/cli.py:104-122`) — deliberately absent from the MCP surface so agents cannot read/game their own thrash metric.
- `Goal` / `GoalState` / `GoalCreate` (`src/sdlc_blackboard/domain/goals.py:13-38`) — goal aggregate; consumed by `GoalRepo` (`src/sdlc_blackboard/application/ports.py:66-74`), `goal_service`, `GoalSnapshot`, and `create_goal` / `authorize_goal_completion` tools.
- `ArtifactRevision` / `ArtifactAlias` / `ArtifactStatus` / `ArtifactSubmission` (`src/sdlc_blackboard/domain/artifacts.py:17-54`) — immutable revision + CAS-promoted alias; consumed by `ArtifactRepo` (`src/sdlc_blackboard/application/ports.py:127-147`), `task_service` submit path, `get_artifact_revision` tool.
- `Finding` / `FindingCreate` / `FindingState` / `FindingSeverity` / `RESOLVED_FINDING_STATES` (`src/sdlc_blackboard/domain/findings.py:23-76`) — consumed by `FindingRepo`, `review_service`, `query_service` open-finding filter, `open_finding`/`resolve_finding` tools.
- `Review` / `ReviewSubmission` / `ReviewDisposition` / `binding_fingerprint` (`src/sdlc_blackboard/domain/reviews.py:25-77`) — consumed by `ReviewRepo`, `gate_service` (disposition match), `submit_review` tool, and `ThrashService.review_rejections` (any disposition other than APPROVED).
- `Approval` / `ApprovalSubmission` / `ApprovalType` / `Decision` (`src/sdlc_blackboard/domain/approvals.py:24-56`) — consumed by `ApprovalRepo`, `gate_service` human-release check, `record_human_approval` tool.
- `EvidenceRef` (`src/sdlc_blackboard/domain/common.py:122-126`) — shared evidence pointer embedded in `ArtifactSubmission`, `Finding`, `Review`, `Approval`, `Decision`.
- `DeliverableSpec` / `ReviewRequirement` (`src/sdlc_blackboard/domain/tasks.py:40-49`) — embedded in `TaskContractCreate`; `gate_service` derives required reviews and impl artifact from them.
- `DomainError` hierarchy + codes (`src/sdlc_blackboard/domain/errors.py:14-94`) — mapped 1:1 onto `ErrorCode` (`src/sdlc_blackboard/application/results.py:44`, `CommandError.from_domain` at `src/sdlc_blackboard/application/results.py:70-81`).
- `can_transition` (`src/sdlc_blackboard/domain/transitions.py:49-58`) — pure transition oracle consumed by every task state change (`task_service._require_transition`).
- `Conn` type alias (`src/sdlc_blackboard/application/ports.py:36`) — opaque transaction handle threaded through every repo method; narrowed to `asyncpg.Connection` by `repositories/_common.py` `conn_of`.
- `OutboxRepo` + `OutboxEntry` (`src/sdlc_blackboard/application/ports.py:210-213`, `src/sdlc_blackboard/application/ports.py:195-207`) — `claim_unpublished` returns a typed `tuple[OutboxEntry, ...]` (a Pydantic projection of the claimed row), not a raw `dict`; the publish loop indexes typed fields. `OutboxService` (`src/sdlc_blackboard/application/use_cases/outbox_service.py:24`) drains it, exposed via the `blackboard outbox-relay` CLI command.
- `ProcessedCommandStore` (`src/sdlc_blackboard/application/ports.py:216-229`) — idempotency dedup table port; `get` returns a `tuple[str, str]` of `(request_hash, response)` consumed by `execute_idempotently` (`src/sdlc_blackboard/application/idempotency.py:63-73`).
- `Services` facade (`src/sdlc_blackboard/application/use_cases/services.py:23-45`) — bundles the eight services (goals, tasks, artifacts, reviews, gate, query, outbox, thrash); consumed by the MCP `services_from` helper (`src/sdlc_blackboard/interfaces/mcp/server.py:53-56`) and the CLI.

## See also

- [impact-analysis](../insights/impact-analysis.md) — 33 shared source citations
- [business-logic](../insights/business-logic.md) — 17 shared source citations
- [processes](../behavior/processes.md) — 15 shared source citations
- [module-map](../architecture/module-map.md) — 13 shared source citations
- [debugging-guide](../insights/debugging-guide.md) — 13 shared source citations
