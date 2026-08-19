# Omnigent + Blackboard Kernel + Thin MCP Interface
## End-to-End Proof-of-Concept Implementation Handoff

**Status:** Implementation-ready POC specification  
**Date:** July 17, 2026  
**Primary language:** Python 3.14  
**Audience:** An autonomous coding agent or engineer implementing the POC  
**Expected outcome:** A locally runnable cross-functional agent team in Omnigent, coordinated through a transactional PostgreSQL blackboard exposed through a thin FastMCP interface

---

## 0. Executive Summary

Build a small but complete **Agentic SDLC Team Runtime** with three hard boundaries:

```text
Omnigent
  = agent sessions
  + heterogeneous inner harnesses
  + parent/child supervision
  + human-visible collaboration
  + sandboxing and contextual policies

Blackboard kernel
  = authoritative organizational state
  + goals, tasks, assignments, artifacts, findings, reviews, decisions
  + lifecycle invariants
  + optimistic concurrency
  + idempotency
  + assignment fencing
  + release-gate evaluation

Thin MCP server
  = schema-safe agent-facing adapter
  + coarse-grained domain tools
  + no orchestration logic
  + no direct table CRUD
```

The POC must demonstrate:

```text
Human gives goal to Omnigent lead
  ↓
Lead creates goal and task contracts in the blackboard
  ↓
Lead delegates analysis to an analyst child session
  ↓
Analyst publishes an accepted requirements artifact
  ↓
Lead delegates implementation to Claude Code or Codex
  ↓
Implementer publishes a code artifact and verification evidence
  ↓
Lead delegates QA and security reviews in parallel
  ↓
A blocking finding causes a remediation task
  ↓
Implementer publishes a new artifact revision
  ↓
Previous review becomes stale
  ↓
QA and security review the new revision
  ↓
Blackboard gate evaluates all required evidence
  ↓
Human approves the final POC release
  ↓
Goal is marked satisfied
```

The architectural rule is:

> **Omnigent sessions execute work. The blackboard kernel owns truth. MCP exposes the kernel but does not define its semantics.**

### POC non-goals

Do not add these to the initial POC:

- a durable workflow engine such as Restate or Temporal;
- Kafka or another external event bus;
- CRDT-backed documents;
- a custom Omnigent UI;
- webhook-based wake-up from the blackboard into Omnigent;
- production deployment credentials;
- multi-tenant authorization;
- distributed locks;
- autonomous production release.

---

## 1. Current Source Baseline

As of July 17, 2026:

- Omnigent is an open-source meta-harness over Claude Code, Codex, Cursor, Pi, custom agents, and other harnesses.
- Its agent YAML supports MCP tools, Python functions, sub-agents, OS access, policies, asynchronous work, cancellation, and timers.
- The Polly example demonstrates a supervisor that delegates autonomous child sessions and is awakened through an inbox when children finish.
- The latest visible released Omnigent version is `v0.5.1`, dated July 10, 2026. Repository `main` is moving toward `0.6.0.dev0`, so pin a release for the POC.
- FastMCP 3 is stable; the latest visible release is `3.4.4`.
- The current final MCP specification is `2025-11-25`. A breaking `2026-07-28` release candidate exists, but the POC must not depend on release-candidate features.

Primary references:

1. Omnigent: https://github.com/omnigent-ai/omnigent
2. Agent YAML specification: https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md
3. Polly documentation: https://omnigent.ai/docs/use/builtin-agents/polly
4. Polly configuration: https://raw.githubusercontent.com/omnigent-ai/omnigent/main/examples/polly/config.yaml
5. Omnigent policies: https://omnigent.ai/docs/policies/custom
6. FastMCP: https://gofastmcp.com/
7. FastMCP HTTP server: https://gofastmcp.com/deployment/running-server
8. MCP 2025-11-25: https://modelcontextprotocol.io/specification/2025-11-25
9. MCP release candidate: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/

### Recommended versions

```toml
requires-python = ">=3.14,<3.15"

dependencies = [
  "fastmcp>=3.4.4,<4",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "asyncpg>=0.30,<1",
  "structlog>=25,<26",
  "tenacity>=9,<10",
  "orjson>=3.10,<4",
  "typer>=0.16,<1",
  "uvicorn>=0.35,<1",
  "opentelemetry-api>=1.35,<2",
  "opentelemetry-sdk>=1.35,<2",
]
```

Install Omnigent separately:

```bash
uv tool install "omnigent==0.5.1"
```

Do not silently use repository `main`. When a required fix exists only there, pin an exact Git commit and document it.

---

## 2. Definition of Done

### Functional

- [ ] Human starts the Omnigent lead from the repository.
- [ ] Lead creates a goal through MCP.
- [ ] Lead creates a dependency-aware task graph.
- [ ] Lead launches at least three specialist child sessions.
- [ ] Specialists retrieve task contracts and artifacts through MCP.
- [ ] Specialists publish authoritative outputs through MCP.
- [ ] Implementation runs inside Claude Code or Codex.
- [ ] QA and security review an exact implementation revision.
- [ ] A blocking finding prevents completion.
- [ ] Remediation produces a new implementation revision.
- [ ] Reviews against the old revision become stale.
- [ ] The gate requires current QA, current security, and human approval.
- [ ] Final snapshot shows accepted artifacts, completed tasks, resolved findings, and approvals.

### Reliability

- [ ] Duplicate mutating calls are idempotent.
- [ ] Two concurrent claims cannot both own one task.
- [ ] Stale task versions cannot overwrite current state.
- [ ] Stale assignment epochs cannot submit authoritative results.
- [ ] Artifact revisions are immutable.
- [ ] Alias promotion uses compare-and-set.
- [ ] Reviews bind exact revisions and hashes.
- [ ] Domain state and outbox events commit atomically.
- [ ] Event handlers can safely reprocess events.

### Engineering

- [ ] Domain logic is independent from FastMCP.
- [ ] MCP tools are thin application-service calls.
- [ ] Persistence details live in PostgreSQL repositories.
- [ ] No raw database CRUD is exposed to agents.
- [ ] `pytest`, `ruff`, and `pyright` pass.
- [ ] README contains exact setup and demo commands.
- [ ] Important logs include goal, task, command, epoch, and run identifiers.
- [ ] Entire POC runs locally.

---

## 3. Architecture

```mermaid
flowchart TB
    H[Human operator]

    subgraph O[Omnigent]
        L[Team lead]
        A[Analyst]
        I[Implementation\nClaude Code or Codex]
        Q[Quality]
        S[Security]
    end

    subgraph M[Thin FastMCP adapter]
        MR[Read tools]
        MC[Atomic command tools]
    end

    subgraph K[Blackboard application kernel]
        KG[Goal service]
        KT[Task service]
        KA[Artifact service]
        KR[Review service]
        KX[Gate service]
        KE[Event service]
    end

    subgraph P[PostgreSQL]
        PT[(Domain tables)]
        PO[(Transactional outbox)]
        PC[(Processed commands)]
    end

    H --> L
    L --> A
    L --> I
    L --> Q
    L --> S

    L --> M
    A --> M
    I --> M
    Q --> M
    S --> M

    M --> K
    K --> PT
    K --> PO
    K --> PC
```

### Trust boundaries

```text
Human ↔ Omnigent
  Human observes, steers, cancels, and approves.

Lead ↔ specialist sessions
  Lead delegates bounded task contracts.
  Specialists own their internal execution loops.

Agents ↔ MCP
  Tool inputs are untrusted and schema validated.

MCP ↔ kernel
  MCP cannot bypass domain commands.

Kernel ↔ PostgreSQL
  Repositories and transactions implement persistence.

Prose ↔ authoritative state
  Chat is informative. Blackboard records are authoritative.
```

### Why Streamable HTTP MCP

Use one server at:

```text
http://127.0.0.1:8000/mcp
```

This permits all Omnigent child sessions to share one service and one PostgreSQL pool. Bind it only to loopback because the POC has no protocol authentication.

---

## 4. Repository Layout

```text
omnigent-blackboard-poc/
├── README.md
├── HANDOFF.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example
├── compose.yaml
├── Makefile
│
├── migrations/
│   ├── 001_initial.sql
│   └── 002_seed_poc_policies.sql
│
├── src/sdlc_blackboard/
│   ├── __init__.py
│   ├── config.py
│   ├── errors.py
│   ├── logging.py
│   ├── domain/
│   │   ├── common.py
│   │   ├── goals.py
│   │   ├── tasks.py
│   │   ├── artifacts.py
│   │   ├── findings.py
│   │   ├── reviews.py
│   │   ├── approvals.py
│   │   └── events.py
│   ├── application/
│   │   ├── unit_of_work.py
│   │   ├── goal_service.py
│   │   ├── task_service.py
│   │   ├── artifact_service.py
│   │   ├── review_service.py
│   │   ├── gate_service.py
│   │   └── query_service.py
│   ├── infrastructure/
│   │   ├── postgres.py
│   │   ├── repositories.py
│   │   ├── migrations.py
│   │   └── outbox.py
│   ├── mcp/
│   │   ├── server.py
│   │   ├── tools_read.py
│   │   ├── tools_commands.py
│   │   └── mapping.py
│   ├── cli.py
│   └── demo.py
│
├── omnigent/sdlc_team/
│   ├── config.yaml
│   ├── LEAD.md
│   ├── agents/
│   │   ├── analyst/config.yaml
│   │   ├── implementation_claude_opus/config.yaml
│   │   ├── implementation_claude_fable/config.yaml
│   │   ├── implementation_codex_sol/config.yaml
│   │   ├── quality/config.yaml
│   │   └── security/config.yaml
│   └── skills/
│       ├── initialize-goal/SKILL.md
│       ├── dispatch-task/SKILL.md
│       ├── process-result/SKILL.md
│       └── evaluate-gate/SKILL.md
│
├── demo_app/
├── scripts/
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

---

## 5. Local Environment

### `.python-version`

```text
3.14
```

### `pyproject.toml`

```toml
[project]
name = "omnigent-blackboard-poc"
version = "0.1.0"
description = "Transactional SDLC blackboard with a thin MCP adapter"
requires-python = ">=3.14,<3.15"
dependencies = [
  "fastmcp>=3.4.4,<4",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "asyncpg>=0.30,<1",
  "structlog>=25,<26",
  "tenacity>=9,<10",
  "orjson>=3.10,<4",
  "typer>=0.16,<1",
  "uvicorn>=0.35,<1",
  "opentelemetry-api>=1.35,<2",
  "opentelemetry-sdk>=1.35,<2",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4,<9",
  "pytest-asyncio>=1.1,<2",
  "pytest-xdist>=3.8,<4",
  "pytest-cov>=6.2,<7",
  "ruff>=0.12,<1",
  "pyright>=1.1.403,<2",
]

[project.scripts]
blackboard = "sdlc_blackboard.cli:app"

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sdlc_blackboard"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q --strict-markers"

[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ASYNC", "RUF"]

[tool.pyright]
pythonVersion = "3.14"
typeCheckingMode = "strict"
include = ["src", "tests"]
```

### `.env.example`

```dotenv
BLACKBOARD_DATABASE_URL=postgresql://blackboard:blackboard@127.0.0.1:5432/blackboard
BLACKBOARD_HOST=127.0.0.1
BLACKBOARD_PORT=8000
BLACKBOARD_LOG_LEVEL=INFO
BLACKBOARD_ENV=local

ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

### `compose.yaml`

```yaml
services:
  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: blackboard
      POSTGRES_PASSWORD: blackboard
      POSTGRES_DB: blackboard
    ports:
      - "127.0.0.1:5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U blackboard -d blackboard"]
      interval: 2s
      timeout: 3s
      retries: 30
    volumes:
      - blackboard_pg:/var/lib/postgresql/data

volumes:
  blackboard_pg:
```

### `Makefile`

```makefile
.PHONY: install db-up db-down migrate mcp test lint omnigent demo-reset

install:
	uv sync --extra dev
	uv tool install "omnigent==0.5.1"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	uv run blackboard migrate

mcp:
	uv run fastmcp run src/sdlc_blackboard/mcp/server.py:mcp \
		--transport http --host 127.0.0.1 --port 8000

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

omnigent:
	omnigent run omnigent/sdlc_team/

demo-reset:
	uv run blackboard reset-demo
```

---

## 6. Domain Model

Use Pydantic v2 models. Do not use database or ORM models as the domain model.

### Common values

```python
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=10_000)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActorKind(StrEnum):
    HUMAN = "human"
    LEAD = "lead"
    ANALYST = "analyst"
    IMPLEMENTATION = "implementation"
    QUALITY = "quality"
    SECURITY = "security"
    SYSTEM = "system"


class ActorRef(DomainModel):
    actor_id: NonEmptyStr
    kind: ActorKind


class CommandContext(DomainModel):
    command_id: UUID = Field(default_factory=uuid4)
    actor: ActorRef
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    assignment_epoch: int | None = Field(default=None, ge=0)
    schema_version: str = "1.0"


class ArtifactBinding(DomainModel):
    artifact_id: UUID
    revision_id: UUID
    logical_name: NonEmptyStr
    content_hash: NonEmptyStr


class EvidenceRef(DomainModel):
    evidence_type: NonEmptyStr
    uri: NonEmptyStr
    digest: str | None = None
    summary: str | None = None
```

### Goal

```python
class GoalState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    BLOCKED = "blocked"
    SATISFIED = "satisfied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalCreate(DomainModel):
    title: NonEmptyStr
    objective: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...] = ()
    owner: ActorRef


class Goal(DomainModel):
    goal_id: UUID = Field(default_factory=uuid4)
    title: NonEmptyStr
    objective: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...]
    constraints: tuple[NonEmptyStr, ...]
    owner: ActorRef
    state: GoalState
    version: int
```

### Task contract

```python
class TaskState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    REVISION_REQUIRED = "revision_required"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class DeliverableSpec(DomainModel):
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    required: bool = True


class ReviewRequirement(DomainModel):
    reviewer_kind: ActorKind
    review_type: NonEmptyStr
    blocking: bool = True


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

### Artifact

```python
class ArtifactStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class ArtifactSubmission(DomainModel):
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    content_uri: NonEmptyStr
    content_hash: NonEmptyStr
    summary: NonEmptyStr
    evidence: tuple[EvidenceRef, ...] = ()
    parent_revision_ids: tuple[UUID, ...] = ()


class ArtifactRevision(DomainModel):
    artifact_id: UUID
    revision_id: UUID = Field(default_factory=uuid4)
    artifact_type: NonEmptyStr
    logical_name: NonEmptyStr
    content_uri: NonEmptyStr
    content_hash: NonEmptyStr
    summary: NonEmptyStr
    produced_by_task_id: UUID
    produced_by_run_id: UUID
    parent_revision_ids: tuple[UUID, ...]
    status: ArtifactStatus
```

### Finding and review

```python
class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    REMEDIATED = "remediated"
    VERIFIED = "verified"
    ACCEPTED_RISK = "accepted_risk"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class FindingCreate(DomainModel):
    goal_id: UUID
    task_id: UUID
    category: NonEmptyStr
    severity: FindingSeverity
    statement: NonEmptyStr
    affected_artifacts: tuple[ArtifactBinding, ...]
    evidence: tuple[EvidenceRef, ...]
    blocking: bool
    resolution_criteria: tuple[NonEmptyStr, ...]


class ReviewDisposition(StrEnum):
    APPROVED = "approved"
    FINDINGS = "findings"
    REQUEST_REVISION = "request_revision"
    ABSTAINED = "abstained"


class ReviewSubmission(DomainModel):
    goal_id: UUID
    review_task_id: UUID
    reviewer: ActorRef
    review_type: NonEmptyStr
    artifact_bindings: tuple[ArtifactBinding, ...]
    disposition: ReviewDisposition
    summary: NonEmptyStr
    evidence: tuple[EvidenceRef, ...] = ()
    finding_ids: tuple[UUID, ...] = ()
```

---

## 7. Structured Command Outcomes

Concurrency conflicts must return structured results.

```python
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    DUPLICATE_COMMAND_MISMATCH = "duplicate_command_mismatch"
    STALE_VERSION = "stale_version"
    STALE_ASSIGNMENT = "stale_assignment"
    PRECONDITION_FAILED = "precondition_failed"
    UNAUTHORIZED = "unauthorized"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"


class CommandError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    current_version: int | None = None
    current_state: str | None = None
    details: dict[str, object] = {}


class CommandResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    status: str
    value: T | None = None
    error: CommandError | None = None
    replayed: bool = False
```

Recommended statuses:

```text
accepted
duplicate_replayed
stale_version
stale_assignment
precondition_failed
unauthorized
conflict_created
validation_failed
```

---

## 8. PostgreSQL Schema

Create `migrations/001_initial.sql`.

```sql
create extension if not exists pgcrypto;

create table goals (
    goal_id uuid primary key,
    title text not null,
    objective text not null,
    success_criteria jsonb not null,
    constraints jsonb not null,
    owner jsonb not null,
    state text not null,
    version bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table tasks (
    task_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_key text not null,
    title text not null,
    objective text not null,
    required_actor_kind text not null,
    contract jsonb not null,
    state text not null,
    version bigint not null default 0,
    assignment_epoch bigint not null default 0,
    assigned_actor_id text,
    omnigent_conversation_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(goal_id, task_key)
);

create index tasks_goal_state_idx on tasks(goal_id, state);

create table task_dependencies (
    task_id uuid not null references tasks(task_id) on delete cascade,
    depends_on_task_id uuid not null references tasks(task_id) on delete cascade,
    dependency_type text not null default 'completion',
    primary key (task_id, depends_on_task_id, dependency_type),
    constraint no_self_dependency check (task_id <> depends_on_task_id)
);

create table task_assignments (
    assignment_id uuid primary key,
    task_id uuid not null references tasks(task_id) on delete cascade,
    assignment_epoch bigint not null,
    actor_id text not null,
    omnigent_conversation_id text,
    state text not null,
    created_at timestamptz not null default now(),
    ended_at timestamptz
);

create unique index one_active_assignment_per_task
    on task_assignments(task_id)
    where state in ('assigned', 'running');

create table runtime_runs (
    run_id uuid primary key,
    task_id uuid not null references tasks(task_id) on delete cascade,
    assignment_epoch bigint not null,
    actor_id text not null,
    omnigent_conversation_id text,
    state text not null,
    input_manifest jsonb not null,
    result_manifest jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table artifact_revisions (
    revision_id uuid primary key,
    artifact_id uuid not null,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    produced_by_task_id uuid not null references tasks(task_id),
    produced_by_run_id uuid not null references runtime_runs(run_id),
    artifact_type text not null,
    logical_name text not null,
    content_uri text not null,
    content_hash text not null,
    summary text not null,
    parent_revision_ids uuid[] not null default '{}',
    evidence jsonb not null default '[]'::jsonb,
    status text not null,
    created_at timestamptz not null default now(),
    unique (artifact_id, content_hash)
);

create index artifact_logical_name_idx
    on artifact_revisions(goal_id, logical_name, created_at desc);

create table artifact_aliases (
    goal_id uuid not null references goals(goal_id) on delete cascade,
    logical_name text not null,
    current_revision_id uuid not null references artifact_revisions(revision_id),
    version bigint not null default 0,
    updated_at timestamptz not null default now(),
    primary key (goal_id, logical_name)
);

create table findings (
    finding_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_id uuid not null references tasks(task_id),
    category text not null,
    severity text not null,
    statement text not null,
    affected_artifacts jsonb not null,
    evidence jsonb not null,
    blocking boolean not null,
    resolution_criteria jsonb not null,
    state text not null,
    version bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index findings_goal_open_idx
    on findings(goal_id, blocking, state);

create table reviews (
    review_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    review_task_id uuid not null references tasks(task_id),
    reviewer jsonb not null,
    review_type text not null,
    binding_fingerprint text not null,
    disposition text not null,
    summary text not null,
    evidence jsonb not null,
    finding_ids uuid[] not null default '{}',
    stale boolean not null default false,
    created_at timestamptz not null default now()
);

create table review_artifact_bindings (
    review_id uuid not null references reviews(review_id) on delete cascade,
    artifact_id uuid not null,
    revision_id uuid not null,
    content_hash text not null,
    primary key (review_id, artifact_id, revision_id)
);

create unique index one_review_per_actor_type_binding
    on reviews(
      review_task_id,
      review_type,
      binding_fingerprint,
      ((reviewer->>'actor_id'))
    );

create table approvals (
    approval_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    approval_type text not null,
    approver jsonb not null,
    binding_fingerprint text not null,
    conditions jsonb not null,
    revoked boolean not null default false,
    created_at timestamptz not null default now()
);

create table approval_artifact_bindings (
    approval_id uuid not null references approvals(approval_id) on delete cascade,
    artifact_id uuid not null,
    revision_id uuid not null,
    content_hash text not null,
    primary key (approval_id, artifact_id, revision_id)
);

create table decisions (
    decision_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    question text not null,
    selected_option text not null,
    rationale text not null,
    evidence jsonb not null,
    decided_by jsonb not null,
    affected_artifacts jsonb not null,
    supersedes uuid[],
    created_at timestamptz not null default now()
);

create table team_events (
    event_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_id uuid references tasks(task_id),
    aggregate_type text not null,
    aggregate_id uuid not null,
    aggregate_version bigint not null,
    event_type text not null,
    actor jsonb not null,
    correlation_id uuid not null,
    causation_id uuid,
    artifact_bindings jsonb not null default '[]'::jsonb,
    payload jsonb not null,
    evidence jsonb not null default '[]'::jsonb,
    occurred_at timestamptz not null default now()
);

create index team_events_goal_cursor_idx
    on team_events(goal_id, occurred_at, event_id);

create table processed_commands (
    command_id uuid primary key,
    actor_id text not null,
    tool_name text not null,
    request_hash text not null,
    response jsonb not null,
    created_at timestamptz not null default now()
);

create table outbox (
    outbox_id bigserial primary key,
    event_id uuid not null unique,
    event_type text not null,
    aggregate_type text not null,
    aggregate_id uuid not null,
    payload jsonb not null,
    published_at timestamptz,
    attempts integer not null default 0,
    created_at timestamptz not null default now()
);

create index outbox_unpublished_idx
    on outbox(outbox_id)
    where published_at is null;

create table human_requests (
    request_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    request_type text not null,
    question text not null,
    options jsonb not null,
    state text not null,
    response jsonb,
    created_at timestamptz not null default now(),
    responded_at timestamptz
);
```

The POC stores rich contracts and evidence as JSONB, while lifecycle, version, ownership, revision binding, and foreign-key fields remain relational.


---

## 9. PostgreSQL Infrastructure

### Connection pool

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from asyncpg import Pool


class Postgres:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=2,
            max_size=20,
            command_timeout=30,
        )

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> Pool:
        if self._pool is None:
            raise RuntimeError("Postgres pool has not started")
        return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                yield connection
```

### Unit of work

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


class UnitOfWork:
    def __init__(self, postgres: Postgres) -> None:
        self._postgres = postgres

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._postgres.transaction() as connection:
            yield connection
```

Use explicit SQL because the critical operations are compare-and-set transitions, partial unique constraints, `FOR UPDATE`, and `SKIP LOCKED`. An ORM can be introduced later for read models, but it should not hide concurrency-sensitive SQL.

---

## 10. Idempotent Commands

Every mutating tool carries:

```python
class CommandContext(DomainModel):
    command_id: UUID
    actor: ActorRef
    correlation_id: UUID
    causation_id: UUID | None
    expected_version: int | None
    assignment_epoch: int | None
    schema_version: str
```

The client must reuse the same `command_id` only when retrying the exact same mutation.

```python
import hashlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

import asyncpg
import orjson
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def canonical_hash(value: BaseModel) -> str:
    raw = orjson.dumps(
        value.model_dump(mode="json"),
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(raw).hexdigest()


async def execute_idempotently(
    *,
    connection: asyncpg.Connection,
    context: CommandContext,
    tool_name: str,
    request: BaseModel,
    execute: Callable[[], Awaitable[T]],
) -> CommandResult[T]:
    request_hash = canonical_hash(request)

    prior = await connection.fetchrow(
        """
        select request_hash, response
          from processed_commands
         where command_id = $1
        """,
        context.command_id,
    )

    if prior is not None:
        if prior["request_hash"] != request_hash:
            return CommandResult[T](
                status="validation_failed",
                error=CommandError(
                    code=ErrorCode.DUPLICATE_COMMAND_MISMATCH,
                    message="Command ID was reused with different arguments.",
                ),
            )

        stored = orjson.loads(prior["response"])
        return CommandResult[T].model_validate(
            {**stored, "replayed": True}
        )

    value = await execute()
    result = CommandResult[T](
        status="accepted",
        value=value,
        replayed=False,
    )

    await connection.execute(
        """
        insert into processed_commands(
            command_id,
            actor_id,
            tool_name,
            request_hash,
            response
        )
        values ($1, $2, $3, $4, $5::jsonb)
        """,
        context.command_id,
        context.actor.actor_id,
        tool_name,
        request_hash,
        result.model_dump_json(),
    )

    return result
```

The command record must be written in the same transaction as the domain mutation.

---

## 11. Core Application Commands

### Create goal

```python
class GoalService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create_goal(
        self,
        context: CommandContext,
        request: GoalCreate,
    ) -> CommandResult[Goal]:
        async with self._uow.begin() as conn:
            async def execute() -> Goal:
                goal = Goal(
                    title=request.title,
                    objective=request.objective,
                    success_criteria=request.success_criteria,
                    constraints=request.constraints,
                    owner=request.owner,
                    state=GoalState.ACTIVE,
                    version=0,
                )

                await conn.execute(
                    """
                    insert into goals(
                        goal_id, title, objective,
                        success_criteria, constraints,
                        owner, state, version
                    )
                    values ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
                    """,
                    goal.goal_id,
                    goal.title,
                    goal.objective,
                    orjson.dumps(goal.success_criteria).decode(),
                    orjson.dumps(goal.constraints).decode(),
                    goal.owner.model_dump_json(),
                    goal.state.value,
                    goal.version,
                )

                await append_domain_event(
                    conn,
                    event_type="goal.created",
                    aggregate_type="goal",
                    aggregate_id=goal.goal_id,
                    aggregate_version=goal.version,
                    goal_id=goal.goal_id,
                    task_id=None,
                    context=context,
                    payload=goal.model_dump(mode="json"),
                )
                return goal

            return await execute_idempotently(
                connection=conn,
                context=context,
                tool_name="create_goal",
                request=request,
                execute=execute,
            )
```

### Create task

Preconditions:

- goal exists and is active;
- dependencies belong to the same goal;
- `task_key` is unique within the goal;
- task contract is internally valid;
- the actor is authorized to create work for the goal.

If the same semantic task is retried with a new command ID, the `unique(goal_id, task_key)` constraint prevents duplication. Return the existing task only when the contract hashes match; otherwise return a conflict.

### Refresh ready tasks

A draft task becomes ready when all dependencies are accepted.

```sql
update tasks t
   set state = 'ready',
       version = version + 1,
       updated_at = now()
 where t.goal_id = $1
   and t.state = 'draft'
   and not exists (
       select 1
         from task_dependencies d
         join tasks dependency
           on dependency.task_id = d.depends_on_task_id
        where d.task_id = t.task_id
          and dependency.state <> 'accepted'
   )
returning *;
```

### Claim task with fencing

```python
async def claim_task(
    *,
    connection: asyncpg.Connection,
    context: CommandContext,
    task_id: UUID,
    actor_id: str,
) -> Task:
    row = await connection.fetchrow(
        """
        select *
          from tasks
         where task_id = $1
         for update
        """,
        task_id,
    )
    if row is None:
        raise NotFound("task", task_id)

    if row["state"] != "ready":
        raise PreconditionFailed(
            f"Task is {row['state']}, not ready."
        )

    next_epoch = int(row["assignment_epoch"]) + 1
    assignment_id = uuid4()

    await connection.execute(
        """
        insert into task_assignments(
            assignment_id,
            task_id,
            assignment_epoch,
            actor_id,
            state
        )
        values ($1, $2, $3, $4, 'assigned')
        """,
        assignment_id,
        task_id,
        next_epoch,
        actor_id,
    )

    updated = await connection.fetchrow(
        """
        update tasks
           set state = 'assigned',
               assigned_actor_id = $2,
               assignment_epoch = $3,
               version = version + 1,
               updated_at = now()
         where task_id = $1
           and state = 'ready'
           and version = $4
        returning *
        """,
        task_id,
        actor_id,
        next_epoch,
        row["version"],
    )

    if updated is None:
        raise StaleVersion()

    return map_task(updated)
```

The database partial unique index is the final defense against double assignment.

### Bind Omnigent conversation

The lead usually claims before it has the child `conversation_id`.

Sequence:

```text
claim_task
  ↓
sys_session_send
  ↓
bind_runtime_session
```

`bind_runtime_session` validates:

- current assignment epoch;
- current actor;
- expected task version;
- conversation ID is not already bound to another active assignment.

If dispatch fails, release or fail the assignment.

### Start runtime run

A runtime run is one execution attempt. It is not the organizational task.

```python
class StartRunRequest(DomainModel):
    task_id: UUID
    omnigent_conversation_id: NonEmptyStr
    input_manifest: tuple[ArtifactBinding, ...]
```

Validate:

```text
context.assignment_epoch == task.assignment_epoch
context.actor.actor_id == task.assigned_actor_id
task.state in {assigned, awaiting_input}
```

Transition:

```text
task: assigned → running
assignment: assigned → running
runtime run: created → running
```

### Submit task result atomically

```python
class SubmitTaskResult(DomainModel):
    task_id: UUID
    run_id: UUID
    disposition: NonEmptyStr
    input_manifest: tuple[ArtifactBinding, ...]
    artifacts: tuple[ArtifactSubmission, ...]
    finding_ids: tuple[UUID, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    residual_risks: tuple[str, ...] = ()
    summary: NonEmptyStr
```

Transaction steps:

1. Lock task.
2. Check assignment epoch.
3. Check actor.
4. Check active run.
5. Verify input manifest.
6. Insert immutable artifacts.
7. Mark run submitted.
8. Mark assignment completed.
9. Transition task to `submitted`.
10. Create required review tasks.
11. Append events and outbox rows.
12. Store idempotent response.
13. Commit.

```python
async def submit_task_result(
    context: CommandContext,
    request: SubmitTaskResult,
) -> CommandResult[TaskSubmissionReceipt]:
    async with uow.begin() as conn:
        async def execute() -> TaskSubmissionReceipt:
            task = await lock_task(conn, request.task_id)

            require_assignment_epoch(task, context.assignment_epoch)
            require_actor(task, context.actor.actor_id)
            require_state(
                task,
                {TaskState.RUNNING, TaskState.AWAITING_INPUT},
            )

            run = await lock_runtime_run(conn, request.run_id)
            require_run_matches_task_and_epoch(run, task)
            validate_input_manifest(
                submitted=request.input_manifest,
                run_manifest=run.input_manifest,
            )

            revisions = []
            for artifact in request.artifacts:
                revision = await insert_artifact_revision(
                    conn=conn,
                    goal_id=task.goal_id,
                    task_id=task.task_id,
                    run_id=request.run_id,
                    submission=artifact,
                )
                revisions.append(revision)

            await complete_run(conn, request.run_id, request)
            await complete_assignment(
                conn,
                task.task_id,
                task.assignment_epoch,
            )

            updated_task = await transition_task_cas(
                conn=conn,
                task_id=task.task_id,
                expected_version=task.version,
                expected_state=task.state,
                new_state=TaskState.SUBMITTED,
            )

            review_task_ids = await create_required_review_tasks(
                conn=conn,
                producer_task=updated_task,
                revisions=revisions,
            )

            await append_domain_event(...)
            return TaskSubmissionReceipt(
                task=updated_task,
                artifact_revisions=tuple(revisions),
                review_task_ids=tuple(review_task_ids),
            )

        return await execute_idempotently(...)
```

### Open finding

A task may create a blocking finding only when its contract permits it.

POC authorization:

```text
task.required_actor_kind in {quality, security}
and task.contract.may_create_blocking_finding == true
```

A finding assertion is immutable. Its resolution state is versioned.

### Submit review

Validate:

- reviewer owns the review task;
- artifact bindings exactly match the review contract;
- review is unique by reviewer, type, and binding;
- listed findings exist;
- an approved review cannot simultaneously contain an unresolved blocker created by that review.

Binding fingerprint:

```python
def binding_fingerprint(
    bindings: tuple[ArtifactBinding, ...],
) -> str:
    values = sorted(
        f"{b.artifact_id}:{b.revision_id}:{b.content_hash}"
        for b in bindings
    )
    return hashlib.sha256("|".join(values).encode()).hexdigest()
```

### Invalidate stale reviews

When current artifact alias changes:

```sql
update reviews r
   set stale = true
  from review_artifact_bindings b
 where b.review_id = r.review_id
   and b.artifact_id = $1
   and b.revision_id <> $2
   and r.stale = false;
```

Do the same for approvals when their binding becomes obsolete.

### Promote artifact alias

Concurrent candidate revisions may coexist. Alias promotion is exclusive.

```sql
update artifact_aliases
   set current_revision_id = $4,
       version = version + 1,
       updated_at = now()
 where goal_id = $1
   and logical_name = $2
   and current_revision_id = $3
returning *;
```

### Gate evaluation

The POC release gate requires:

- current implementation artifact alias;
- non-stale QA approval for the exact binding;
- non-stale security approval for the exact binding;
- no open blocking findings;
- human release approval for the exact binding.

```python
class GateStatus(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    HUMAN_REQUIRED = "human_required"


class GateResult(DomainModel):
    status: GateStatus
    implementation_binding: ArtifactBinding | None
    missing_reviews: tuple[str, ...]
    open_blocking_finding_ids: tuple[UUID, ...]
    stale_review_ids: tuple[UUID, ...]
    missing_approvals: tuple[str, ...]
```

Do not store a generic `security_approved = true` flag.

---

## 12. Events and Transactional Outbox

Domain mutation and event creation commit together.

```python
async def append_domain_event(
    conn: asyncpg.Connection,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    goal_id: UUID,
    task_id: UUID | None,
    context: CommandContext,
    payload: dict[str, object],
) -> UUID:
    event_id = uuid4()

    await conn.execute(
        """
        insert into team_events(
            event_id, goal_id, task_id,
            aggregate_type, aggregate_id, aggregate_version,
            event_type, actor, correlation_id, causation_id, payload
        )
        values (
            $1, $2, $3, $4, $5, $6, $7,
            $8::jsonb, $9, $10, $11::jsonb
        )
        """,
        event_id,
        goal_id,
        task_id,
        aggregate_type,
        aggregate_id,
        aggregate_version,
        event_type,
        context.actor.model_dump_json(),
        context.correlation_id,
        context.causation_id,
        orjson.dumps(payload).decode(),
    )

    await conn.execute(
        """
        insert into outbox(
            event_id, event_type, aggregate_type, aggregate_id, payload
        )
        values ($1, $2, $3, $4, $5::jsonb)
        """,
        event_id,
        event_type,
        aggregate_type,
        aggregate_id,
        orjson.dumps(payload).decode(),
    )

    return event_id
```

POC outbox worker:

```sql
select *
  from outbox
 where published_at is null
 order by outbox_id
 for update skip locked
 limit 100;
```

For the POC, publishing may mean structured logging, updating a simple projection, and marking `published_at`. No Kafka is needed.

---

## 13. Query Model

Agents retrieve compact state rather than full history.

```python
class GoalSnapshot(DomainModel):
    goal: Goal
    tasks: tuple[Task, ...]
    artifact_aliases: tuple[ArtifactBinding, ...]
    open_findings: tuple[Finding, ...]
    reviews: tuple[ReviewRecord, ...]
    approvals: tuple[Approval, ...]
    ready_task_ids: tuple[UUID, ...]
    human_requests: tuple[dict[str, object], ...]
```

Relevant event pagination uses a keyset cursor:

```sql
where goal_id = $1
  and (occurred_at, event_id) > ($2, $3)
order by occurred_at, event_id
limit $4;
```

---

## 14. Thin FastMCP Server

### Server

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass
class AppContext:
    postgres: Postgres
    services: Services


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
    settings = Settings()
    postgres = Postgres(settings.database_url)
    await postgres.start()
    services = Services.build(postgres)

    try:
        yield AppContext(postgres=postgres, services=services)
    finally:
        await postgres.stop()


mcp = FastMCP(
    name="SDLC Blackboard",
    instructions=(
        "Transactional organizational blackboard. "
        "Use read tools to inspect state and atomic commands to mutate it. "
        "Never infer success from prose."
    ),
    lifespan=lifespan,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
```

Verify the exact FastMCP 3.4 lifespan-context accessor in an integration test.

### Tool surface

Read tools:

```text
get_goal_snapshot
get_task_contract
get_artifact_revision
read_relevant_events
get_gate_status
```

Atomic commands:

```text
create_goal
create_task
refresh_ready_tasks
claim_task
bind_runtime_session
start_runtime_run
submit_task_result
open_finding
resolve_finding
submit_review
promote_artifact
record_human_approval
authorize_goal_completion
```

### Example tools

```python
from fastmcp import Context


@mcp.tool
async def create_task(
    command: CommandContext,
    task: TaskContractCreate,
    ctx: Context,
) -> CommandResult:
    """
    Create one bounded task contract.

    The goal must exist and dependencies must belong to it.
    The command is idempotent by command.command_id.
    """
    app = ctx.lifespan_context
    return await app.services.tasks.create_task(command, task)


@mcp.tool
async def claim_task(
    command: CommandContext,
    task_id: UUID,
    actor_id: str,
    ctx: Context,
) -> CommandResult:
    """
    Atomically assign one READY task.

    Returns the fencing epoch. Later worker mutations must carry that epoch.
    Concurrent or stale claims return a structured conflict.
    """
    app = ctx.lifespan_context
    return await app.services.tasks.claim_task(
        command,
        task_id=task_id,
        actor_id=actor_id,
    )
```

Every description should state purpose, preconditions, idempotency, concurrency behavior, outputs, and retry safety.

### MCP prohibitions

The MCP layer must not:

- contain state-transition rules;
- issue SQL directly from individual tools;
- keep authoritative in-memory state;
- own child-session lifecycle;
- silently resolve stale versions;
- merge competing revisions;
- infer authority from natural language;
- expose generic CRUD or SQL tools.


---

## 15A. Amazon Bedrock Model and Harness Configuration

The POC must support three implementation execution profiles:

| Profile | Harness | Bedrock model ID | Routing |
|---|---|---|---|
| Claude Opus 4.8 | Claude Code through Omnigent `claude-sdk` | `global.anthropic.claude-opus-4-8` | Global cross-Region inference profile |
| Claude Fable 5 | Claude Code through Omnigent `claude-sdk` | `global.anthropic.claude-fable-5` | Global cross-Region inference profile |
| GPT-5.6 Sol | Codex through Omnigent `codex` | `openai.gpt-5.6-sol` | Regional Bedrock Mantle endpoint |

### Critical endpoint distinction

For Claude, “global” means a **global inference profile ID**, not a global DNS hostname. Claude Code still sends the request to a regional `bedrock-runtime` endpoint such as:

```text
https://bedrock-runtime.us-east-1.amazonaws.com
```

The model ID instructs Bedrock to route globally:

```text
global.anthropic.claude-opus-4-8
global.anthropic.claude-fable-5
```

For GPT-5.6 Sol, Bedrock currently exposes:

```text
model ID: openai.gpt-5.6-sol
endpoint: https://bedrock-mantle.<region>.api.aws/openai/v1
```

GPT-5.6 Sol currently has **no Geo or Global inference ID**. Do not invent a value such as:

```text
global.openai.gpt-5.6-sol
```

It will not work. Use `us-east-1` or another region explicitly supported by the current AWS model card. The POC standardizes all clients on `us-east-1` to simplify shared AWS credentials.

### Selected POC region

```dotenv
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
AWS_PROFILE=agentic-poc
```

The Claude model may execute in another commercial region because its model ID is global. The Codex/GPT-5.6 Sol request remains served through the regional Mantle deployment.

### Model characteristics that affect the POC

Claude Opus 4.8:

```text
context window: 1M
maximum output: 128K
reasoning: supported
Bedrock global ID: global.anthropic.claude-opus-4-8
endpoint family: bedrock-runtime
```

Claude Fable 5:

```text
context window: 1M
maximum output: 128K
adaptive reasoning: always enabled
temperature: 1.0 or unset
top_p: at least 0.99 and below 1.0, or unset
top_k: unsupported
Bedrock global ID: global.anthropic.claude-fable-5
endpoint family: bedrock-runtime
```

Do not configure Fable with sampling values inherited from a generic model profile.

GPT-5.6 Sol:

```text
context window on Bedrock: 272K
API: Responses API
Bedrock model ID: openai.gpt-5.6-sol
endpoint family: bedrock-mantle
global inference: not supported
```

Let Codex's built-in `amazon-bedrock` provider derive the endpoint rather than manually replacing Codex's OpenAI URL.

### 15A.1 AWS account preparation

#### Anthropic models

In the Amazon Bedrock model catalog:

1. Select Claude Opus 4.8.
2. Submit the Anthropic use-case form if the account has not previously done so.
3. Verify access to Claude Fable 5.
4. Confirm that the global system-defined inference profiles are visible.

Verification:

```bash
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --type-equals SYSTEM_DEFINED \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'claude-opus-4-8') || contains(inferenceProfileId, 'claude-fable-5')].[inferenceProfileId,status]" \
  --output table
```

Expected IDs:

```text
global.anthropic.claude-opus-4-8
global.anthropic.claude-fable-5
```

Inspect the unfiltered JSON if the CLI query fields differ in the installed AWS CLI.

#### OpenAI model

In the Bedrock model catalog:

1. Verify access to GPT-5.6 Sol.
2. Confirm it is available in `us-east-1`.
3. Confirm the account can use Bedrock Mantle and the Responses API.
4. Review the separate Bedrock Mantle quotas.

Direct SDK smoke test:

```python
from openai import BedrockOpenAI

client = BedrockOpenAI(aws_region="us-east-1")
response = client.responses.create(
    model="openai.gpt-5.6-sol",
    input="Reply with exactly: bedrock-sol-ok",
)
print(response.output_text)
```

Install the Bedrock-enabled SDK dependencies when running this outside Codex:

```bash
uv pip install "openai[bedrock]"
```

### 15A.2 Authentication

Use one AWS SSO or IAM profile for both Claude Code and Codex:

```bash
aws sso login --profile agentic-poc

export AWS_PROFILE=agentic-poc
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
```

Validate:

```bash
aws sts get-caller-identity
```

The POC should inherit these credentials through the Omnigent caller process. Do not place temporary access keys in committed YAML.

For a first local experiment, the AWS-managed `AmazonBedrockLimitedAccess` policy is acceptable. Post-POC, replace it with a least-privilege policy scoped to the approved inference profiles, backing models, GPT-5.6 Sol, and required discovery operations.

Claude Code's documented baseline requires:

```text
bedrock:InvokeModel
bedrock:InvokeModelWithResponseStream
bedrock:ListInferenceProfiles
bedrock:GetInferenceProfile
```

Global inference can route to multiple destination regions. Avoid restricting foundation-model resources to only `us-east-1` unless the final policy has been tested against every destination selected by the global profile.

#### Bedrock API-key alternative

Both clients can use:

```bash
export AWS_BEARER_TOKEN_BEDROCK="<bedrock-api-key>"
export AWS_REGION=us-east-1
```

Prefer AWS SSO or refreshable short-term credentials for the POC. Do not commit a long-term Bedrock API key.

### 15A.3 Claude Code global-profile configuration

Claude Code uses the AWS credential chain when:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_PROFILE=agentic-poc
export AWS_REGION=us-east-1
```

Do **not** set `ANTHROPIC_BEDROCK_BASE_URL` for normal AWS operation. The regional runtime endpoint plus a global model ID is the intended configuration.

#### Opus 4.8 smoke test

```bash
CLAUDE_CODE_USE_BEDROCK=1 \
AWS_PROFILE=agentic-poc \
AWS_REGION=us-east-1 \
claude \
  --model "global.anthropic.claude-opus-4-8" \
  -p "Reply with exactly: opus-bedrock-global-ok"
```

Run `/status` in an interactive session and verify:

```text
provider: Amazon Bedrock
region: us-east-1
model: global.anthropic.claude-opus-4-8
```

Claude Code may expose a `[1m]` selector for manually pinned Opus models:

```text
global.anthropic.claude-opus-4-8[1m]
```

Use it only after verifying that the pinned Claude Code version recognizes the variant. The base Bedrock model has a 1M-token context window.

#### Fable 5 smoke test

```bash
CLAUDE_CODE_USE_BEDROCK=1 \
AWS_PROFILE=agentic-poc \
AWS_REGION=us-east-1 \
claude \
  --model "global.anthropic.claude-fable-5" \
  -p "Reply with exactly: fable-bedrock-global-ok"
```

Verify `/status` shows the exact global Fable model ID. Do not set generic temperature, `top_p`, or `top_k` overrides.

#### Optional Claude settings file

`~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_PROFILE": "agentic-poc",
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-4-8"
  }
}
```

Do not set a global `ANTHROPIC_MODEL` when the Omnigent team must switch between Opus and Fable. Each specialist sets `executor.model` explicitly.

Claude Code background tasks normally follow the explicitly selected primary model. Record this in cost measurements.

### 15A.4 Codex with GPT-5.6 Sol on Bedrock

Codex has a first-party Amazon Bedrock provider. Configure:

`~/.codex/config.toml`

```toml
model_provider = "amazon-bedrock"
model = "openai.gpt-5.6-sol"
```

For CLI runs:

```bash
export AWS_PROFILE=agentic-poc
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
```

For a desktop or IDE process that may not inherit shell variables, add:

`~/.codex/.env`

```dotenv
AWS_PROFILE=agentic-poc
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
```

Do not set an OpenAI-hosted API key. Codex uses AWS-native authentication for the `amazon-bedrock` provider.

Verify interactively:

```bash
codex
```

Then run:

```text
/status
```

Expected:

```text
model provider: amazon-bedrock
model: openai.gpt-5.6-sol
region: us-east-1
```

Scriptable smoke test:

```bash
AWS_PROFILE=agentic-poc \
AWS_REGION=us-east-1 \
codex exec \
  --model "openai.gpt-5.6-sol" \
  "Reply with exactly: codex-sol-bedrock-ok"
```

If the installed Codex release does not accept `--model` on `codex exec`, use the model in `~/.codex/config.toml` and rerun without the flag.

#### No global Sol endpoint

The actual endpoint is regional:

```text
https://bedrock-mantle.us-east-1.api.aws/openai/v1
```

The model card states that Geo and Global inference are unsupported. The POC therefore cannot make Codex global in the same way as Claude.

#### Optional regional retry

Use `us-east-1` as primary and `us-east-2` as secondary. A failed Codex execution becomes a new `runtime_run`. Do not switch regions inside one active Codex session and pretend it is the same attempt.

```python
regions = ("us-east-1", "us-east-2")

for region in regions:
    run = await blackboard.start_runtime_run(
        task_id=task_id,
        provider="amazon-bedrock",
        model="openai.gpt-5.6-sol",
        region=region,
    )
    result = await launch_codex(run, region=region)

    if result.succeeded:
        break

    await blackboard.fail_runtime_run(
        run_id=run.run_id,
        retryable=result.retryable,
    )
```

This is client-side regional failover, not Bedrock global inference.

### 15A.5 Omnigent implementation profiles

Replace the single implementation agent with:

```text
agents/
├── implementation_claude_opus/config.yaml
├── implementation_claude_fable/config.yaml
└── implementation_codex_sol/config.yaml
```

The organizational task still requires `implementation`. The lead selects an execution profile for the experiment.

#### Claude Opus 4.8

```yaml
spec_version: 1

name: implementation_claude_opus

description: >-
  Executes a bounded implementation task using Claude Code with Claude Opus 4.8
  through the Amazon Bedrock global inference profile.

executor:
  harness: claude-sdk
  model: global.anthropic.claude-opus-4-8

prompt: |
  Execute exactly one implementation TaskContract.
  Retrieve and verify the task, assignment epoch, artifact bindings, scope, and
  acceptance criteria. Start a runtime run before substantive work. Publish the
  immutable source revision and evidence through submit_task_result.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths: [.]
    allow_network: true
```

#### Claude Fable 5

```yaml
spec_version: 1

name: implementation_claude_fable

description: >-
  Executes a bounded implementation task using Claude Code with Claude Fable 5
  through the Amazon Bedrock global inference profile.

executor:
  harness: claude-sdk
  model: global.anthropic.claude-fable-5

prompt: |
  Execute exactly one implementation TaskContract.
  Treat adaptive reasoning as provider-managed. Do not set temperature, top_p,
  or top_k. Publish immutable artifacts and evidence through the blackboard.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths: [.]
    allow_network: true
```

#### Codex GPT-5.6 Sol

```yaml
spec_version: 1

name: implementation_codex_sol

description: >-
  Executes a bounded implementation task using Codex with GPT-5.6 Sol through
  Amazon Bedrock Mantle in the configured region.

executor:
  harness: codex
  model: openai.gpt-5.6-sol

prompt: |
  Execute exactly one implementation TaskContract.
  Verify the task and assignment epoch, then start a runtime run. Use local
  Codex tools and publish the immutable source revision and evidence through
  submit_task_result.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths: [.]
    allow_network: true
```

Validate each profile:

```bash
omnigent run omnigent/sdlc_team/agents/implementation_claude_opus/config.yaml \
  -p "Report your configured provider and model without changing files."

omnigent run omnigent/sdlc_team/agents/implementation_claude_fable/config.yaml \
  -p "Report your configured provider and model without changing files."

omnigent run omnigent/sdlc_team/agents/implementation_codex_sol/config.yaml \
  -p "Report your configured provider and model without changing files."
```

Current Omnigent documentation names the harnesses `claude-sdk` and `codex`. If the pinned release uses another alias, use the alias reported by `omnigent run --help` and record the deviation.

### 15A.6 Lead selection policy

Add all profiles to the lead:

```yaml
tools:
  agents:
    - analyst
    - implementation_claude_opus
    - implementation_claude_fable
    - implementation_codex_sol
    - quality
    - security
```

For deterministic comparisons, the human selects:

```text
implementation_profile:
  claude_opus_4_8
  claude_fable_5
  gpt_5_6_sol
```

Do not let the lead silently switch models after failure. A model change creates a new runtime attempt and records provider, model, region, routing class, harness, conversation ID, timestamps, and failure reason.

```yaml
routing:
  default: claude_opus_4_8

  use_fable_when:
    - long_horizon_task
    - multi_stage_delegation
    - explicit_fable_experiment

  use_codex_sol_when:
    - explicit_codex_experiment
    - independent_implementation_attempt
    - cross_model_replication

  fallback:
    automatic_model_switching: false
    require_new_runtime_run: true
```

### 15A.7 Startup environment

Extend `.env.example`:

```dotenv
AWS_PROFILE=agentic-poc
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1

CLAUDE_CODE_USE_BEDROCK=1
ANTHROPIC_DEFAULT_OPUS_MODEL=global.anthropic.claude-opus-4-8

# Optional alternative to the AWS credential chain:
# AWS_BEARER_TOKEN_BEDROCK=

# Codex reads ~/.codex/config.toml:
# model_provider = "amazon-bedrock"
# model = "openai.gpt-5.6-sol"
```

Launch:

```bash
set -a
source .env
set +a

aws sso login --profile "$AWS_PROFILE"
omnigent run omnigent/sdlc_team/
```

Because `os_env.type: caller_process` is used, native child harnesses inherit AWS credentials. The sandbox must allow outbound HTTPS to:

```text
bedrock-runtime.us-east-1.amazonaws.com
bedrock-mantle.us-east-1.api.aws
AWS STS and SSO endpoints required by the credential chain
```

### 15A.8 Model provenance in the blackboard

Extend `runtime_runs`:

```sql
alter table runtime_runs
    add column provider text,
    add column model_id text,
    add column aws_region text,
    add column routing_class text,
    add column harness text;

alter table runtime_runs
    add constraint runtime_runs_routing_class_check
    check (
      routing_class is null or routing_class in (
        'global_inference_profile',
        'geo_inference_profile',
        'in_region_runtime',
        'regional_mantle'
      )
    );
```

Example provenance:

```json
{
  "provider": "amazon-bedrock",
  "model_id": "global.anthropic.claude-opus-4-8",
  "aws_region": "us-east-1",
  "routing_class": "global_inference_profile",
  "harness": "claude-sdk"
}
```

```json
{
  "provider": "amazon-bedrock",
  "model_id": "global.anthropic.claude-fable-5",
  "aws_region": "us-east-1",
  "routing_class": "global_inference_profile",
  "harness": "claude-sdk"
}
```

```json
{
  "provider": "amazon-bedrock",
  "model_id": "openai.gpt-5.6-sol",
  "aws_region": "us-east-1",
  "routing_class": "regional_mantle",
  "harness": "codex"
}
```

This is required for cost comparison, reproducibility, debugging, and model-specific failure analysis.

### 15A.9 Bedrock acceptance tests

Add:

```text
tests/acceptance/test_claude_opus_bedrock.py
tests/acceptance/test_claude_fable_bedrock.py
tests/acceptance/test_codex_sol_bedrock.py
```

Run them only when explicitly enabled:

```bash
RUN_BEDROCK_ACCEPTANCE=1 uv run pytest tests/acceptance -v
```

Assert:

1. the provider is Bedrock;
2. the exact model ID is used;
3. Claude uses a `global.` inference profile ID;
4. Codex uses `amazon-bedrock`;
5. GPT Sol uses `regional_mantle`;
6. streaming and one tool call work;
7. provenance is recorded;
8. no first-party Anthropic or OpenAI API key is required;
9. permission errors are explicit.

Negative test:

```python
def test_sol_is_not_declared_global() -> None:
    profile = load_execution_profile("implementation_codex_sol")
    assert profile.model_id == "openai.gpt-5.6-sol"
    assert not profile.model_id.startswith("global.")
    assert profile.routing_class == "regional_mantle"
```

### 15A.10 Troubleshooting

#### Claude `AccessDeniedException`

Check identity, model access, inference-profile discovery, destination-model permissions, and whether `AWS_PROFILE` reaches the child process.

#### Claude silently uses another model

Pin `executor.model` to the exact global ID and inspect `/status`. Do not use aliases such as `opus`.

#### Fable rejects parameters

Remove temperature, `top_p`, and `top_k` overrides.

#### Codex uses OpenAI-hosted inference

Check `~/.codex/config.toml` and `/status`. The provider must be `amazon-bedrock`.

#### Codex cannot find Sol

Check the exact model ID, region, Codex version, model access, Mantle quotas, and credentials.

#### A stakeholder requests a global Codex endpoint

Document the current Bedrock limitation. Use a new runtime attempt in a secondary supported region when resilience is required.


---

## 15. Omnigent Team Configuration

Follow Omnigent's bundled Polly directory pattern:

```text
omnigent/sdlc_team/
  config.yaml
  LEAD.md
  agents/<name>/config.yaml
```

### Lead `config.yaml`

```yaml
spec_version: 1

name: sdlc_team_lead

description: >-
  Cross-functional SDLC team lead. Converts a human objective into explicit
  blackboard task contracts, delegates autonomous specialist work, integrates
  artifacts and evidence, coordinates review and remediation, and requests
  human approval at the final gate.

spawn: true

executor:
  type: omnigent
  context_window: 1000000
  config:
    harness: claude-sdk

instructions: LEAD.md

async: true
cancellable: true
timers: true

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - create_goal
      - create_task
      - refresh_ready_tasks
      - get_goal_snapshot
      - get_task_contract
      - claim_task
      - bind_runtime_session
      - read_relevant_events
      - get_gate_status
      - record_human_approval
      - authorize_goal_completion

  agents:
    - analyst
    - implementation_claude_opus
    - implementation_claude_fable
    - implementation_codex_sol
    - quality
    - security

guardrails:
  ask_timeout: 86400

  policies:
    spawn_bounds:
      type: function
      function:
        path: omnigent.inner.nessie.policies.spawn_bounds
      arguments:
        max_dispatches_per_turn: 6
        dispatch_tools:
          - sys_session_send
          - sys_session_create
```

The `spawn_bounds` path is demonstrated by Polly but is an Omnigent internal module. Verify it against `0.5.1`. If it is unavailable, omit it for the first run and replace it with a custom public policy.

### Lead instructions

`omnigent/sdlc_team/LEAD.md`

```markdown
# SDLC Team Lead

You coordinate organizational work. You do not perform specialist analysis,
implementation, QA, or security review when a matching specialist is available.

## Authoritative state

The blackboard is the source of truth for:

- goals;
- task contracts and dependencies;
- assignments and epochs;
- runtime attempts;
- artifact revisions;
- findings;
- reviews;
- approvals;
- completion gates.

Your chat history and child inbox messages are not authoritative state.

## Required lifecycle

1. Create a goal with explicit success criteria and constraints.
2. Create bounded task contracts.
3. Refresh readiness.
4. Claim a task before dispatching a specialist.
5. Dispatch the matching Omnigent child session.
6. Bind the returned conversation ID to the assignment.
7. The specialist starts a runtime run and publishes through MCP.
8. When the child completes, read the goal snapshot.
9. Create review or remediation work from authoritative state.
10. Complete only after get_gate_status and authorize_goal_completion succeed.

## Dispatch contract

Every dispatch includes:

- goal ID;
- task ID;
- assignment epoch;
- actor ID;
- exact objective;
- scope and constraints;
- authoritative artifact bindings;
- deliverables;
- acceptance criteria;
- permitted actions;
- required MCP reporting behavior.

Do not pass the entire parent conversation by default.

Use sys_session_send for declared specialists. Store its conversation_id with
bind_runtime_session. Do not busy-poll; Omnigent wakes you through its inbox.

## Concurrency

- STALE_VERSION means reread and replan.
- STALE_ASSIGNMENT means the worker no longer has authority.
- Reuse command_id only for the exact same retry.
- Do not generate a new command_id to bypass a failed precondition.
- Cancellation is not proof that an old worker cannot write; the blackboard
  assignment epoch is the authority fence.
- Never reuse a review or approval for a different artifact revision.

## Completion

The goal is not complete because all children returned.

It is complete only when:

- required tasks are accepted;
- current artifact revisions are promoted;
- required QA and security reviews are current and approved;
- no blocking finding remains open;
- human approval exists for the current binding;
- authorize_goal_completion succeeds.
```

### Specialist assignment message

```text
TASK CONTRACT

goal_id: <uuid>
task_id: <uuid>
assignment_epoch: <integer>
actor_id: <actor>

objective:
  ...

scope:
  - ...

constraints:
  - ...

authoritative_inputs:
  - logical_name: ...
    artifact_id: ...
    revision_id: ...
    content_hash: ...

deliverables:
  - ...

acceptance_criteria:
  - ...

authority:
  may_modify_repository: true|false
  may_create_blocking_finding: true|false

REPORTING PROTOCOL

1. Call get_task_contract and verify task ID and epoch.
2. Call start_runtime_run before substantive work.
3. Perform the task using your native harness.
4. Publish consequential outputs through submit_task_result, open_finding, or
   submit_review.
5. The final chat message is a concise human summary. The blackboard result is
   authoritative.
```

---

## 16. Specialist Configurations

### Analyst

```yaml
spec_version: 1

name: analyst

description: >-
  Executes bounded requirements and business-analysis tasks and produces
  explicit requirements, assumptions, acceptance criteria, and open decisions.

executor:
  type: omnigent
  config:
    harness: pi

prompt: |
  Execute exactly one supplied task contract.

  Do not role-play a generic business analyst. Work toward the explicit goal,
  deliverables, acceptance criteria, and evidence requirements.

  You may not modify product source code. Publish authoritative outputs through
  the blackboard MCP tools.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - submit_task_result

os_env:
  type: caller_process
  cwd: .
  sandbox:
    write_paths:
      - ./artifacts/analyst
    allow_network: false
```

### Generic implementation profile — superseded by section 15A

Retain this only as a non-Bedrock fallback. The primary profiles are `implementation_claude_opus`, `implementation_claude_fable`, and `implementation_codex_sol`.

```yaml
spec_version: 1

name: implementation

description: >-
  Executes one bounded implementation task with a native coding harness,
  modifies the repository within scope, runs verification, and publishes an
  immutable revision manifest.

executor:
  type: omnigent
  config:
    harness: claude-native
    permission_mode: auto

prompt: |
  Execute exactly one implementation TaskContract.

  Retrieve and verify the contract, assignment epoch, input bindings, scope, and
  acceptance criteria before coding. Start a runtime run.

  Use your native coding workflow. Stay within scope. Run relevant tests, lint,
  and type checks. Publish the final commit or workspace revision through
  submit_task_result with exact validation commands and evidence.

  Your final prose response is informational only.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths:
      - .
    allow_network: true

guardrails:
  ask_timeout: 86400
```

Codex alternative:

```yaml
executor:
  type: omnigent
  config:
    harness: codex-native
```

### Quality

```yaml
spec_version: 1

name: quality

description: >-
  Independently validates an exact artifact revision against acceptance
  criteria and publishes revision-bound evidence and findings.

executor:
  type: omnigent
  config:
    harness: pi

prompt: |
  Execute exactly one QA review task.

  Review only the exact artifact bindings in the contract. Do not treat a branch
  name, mutable workspace, or later commit as equivalent.

  Run or create tests only as permitted. Publish findings with evidence and
  submit a review bound to the exact revision and content hash.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - open_finding
      - submit_review
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths:
      - ./qa-output
    allow_network: false
```

### Security

```yaml
spec_version: 1

name: security

description: >-
  Independently reviews an exact implementation revision for security risk,
  publishes evidence-bearing findings, and submits a revision-bound review.

executor:
  type: omnigent
  config:
    harness: pi

prompt: |
  Execute exactly one security review task.

  Inspect the exact artifact revision in the contract. Distinguish observed
  defects, inferred risks, and hypotheses. Every blocking finding must contain
  reproducible evidence and explicit resolution criteria.

  You may create blocking findings. You may not approve production release or
  modify implementation source unless a separate remediation task permits it.

async: true
cancellable: true

tools:
  blackboard:
    type: mcp
    url: http://127.0.0.1:8000/mcp
    tools:
      - get_task_contract
      - get_artifact_revision
      - read_relevant_events
      - start_runtime_run
      - open_finding
      - submit_review
      - submit_task_result

os_env:
  type: caller_process
  cwd: ./demo_app
  sandbox:
    write_paths:
      - ./security-output
    allow_network: false
```

---

## 17. Lead Skills

### `initialize-goal/SKILL.md`

```markdown
# Initialize Goal

1. Convert the human objective into measurable success criteria and constraints.
2. Call create_goal.
3. Create an analysis task unless requirements are already explicit.
4. Do not create implementation work until required analysis artifacts exist.
5. Return the goal ID and work graph.
```

### `dispatch-task/SKILL.md`

```markdown
# Dispatch Task

1. Read the goal snapshot.
2. Select one READY task.
3. Match required_actor_kind to a specialist.
4. Call claim_task with a stable command ID.
5. Dispatch through sys_session_send.
6. Include task ID and assignment epoch.
7. Bind the returned conversation ID.
8. If dispatch fails, release or fail the assignment.
9. Do not claim multiple tasks unintentionally.
```

### `process-result/SKILL.md`

```markdown
# Process Result

1. Read the child inbox message.
2. Read the authoritative goal snapshot.
3. Confirm the expected task state changed.
4. Ignore claims not persisted in the blackboard.
5. If SUBMITTED, dispatch required reviews.
6. If AWAITING_INPUT, answer from accepted artifacts or ask a human.
7. If FAILED, retry through a new run or replan.
8. If STALE_ASSIGNMENT, reject the result.
```

### `evaluate-gate/SKILL.md`

```markdown
# Evaluate Gate

1. Call get_gate_status.
2. Dispatch missing reviews.
3. Replace stale reviews.
4. Create remediation for blockers.
5. Ask the human when approval is missing.
6. Record the human approval.
7. Call authorize_goal_completion only when the gate is satisfied.
```

---

## 18. Demonstration Scenario

Use a small FastAPI application in `demo_app`.

### Human goal

```text
Add an authenticated CSV report export endpoint.

Requirements:
- endpoint: POST /api/v1/reports/export
- caller must have reports:export permission
- at most 10,000 records per export
- response must avoid spreadsheet formula injection
- every export creates an audit event
- include unit and integration tests
- release requires QA, security, and human approval
```

This exercises requirements, implementation, QA, security, remediation, revision-bound review, and release gates.

### Expected graph

```mermaid
graph TD
    A[Analyze requirements] --> I[Implement export]
    I --> Q[QA review]
    I --> S[Security review]
    S --> R[Remediate security finding]
    Q --> G[Release gate]
    R --> Q2[QA review revision 2]
    R --> S2[Security review revision 2]
    Q2 --> G
    S2 --> G
    G --> H[Human approval]
    H --> C[Complete goal]
```

The first implementation should intentionally or naturally expose a CSV formula-injection risk by writing values beginning with `=`, `+`, `-`, or `@` without protection. Security opens a blocking finding. Remediation produces revision 2 and invalidates revision 1 reviews.

### Artifact names

```text
requirements/report-export
source/report-export
test-report/report-export
security-review/report-export
release-evidence/report-export
```

Source artifact:

```text
content_uri: git://demo_app/<commit_sha>
content_hash: <commit_sha>
```

Analysis artifact:

```text
content_uri: file://artifacts/analyst/report-export-requirements.md
```

### Expected event trace

```text
goal.created
task.created: requirements
task.ready
task.assigned: analyst, epoch 1
runtime.started
artifact.created: requirements revision 1
task.submitted
task.accepted
task.ready: implementation
task.assigned: implementation, epoch 1
runtime.started
artifact.created: source revision 1
task.submitted
review_task.created: quality
review_task.created: security
review.submitted: quality approved revision 1
finding.created: formula injection, blocking
review.submitted: security request_revision revision 1
task.created: remediation
task.assigned: implementation, epoch 1
artifact.created: source revision 2
artifact.promoted: revision 1 → revision 2
review.invalidated: quality revision 1
review.invalidated: security revision 1
review.submitted: quality approved revision 2
finding.remediated
review.submitted: security approved revision 2
gate.human_required
approval.created: human release revision 2
goal.satisfied
```

---

## 19. Startup Runbook

### Prerequisites

- Linux or macOS preferred.
- Python 3.14 through `uv`.
- Docker.
- Git.
- Model credentials.
- Claude Code or Codex CLI for native implementation.
- `bubblewrap` on Linux where Omnigent requires it.
- `tmux` for native terminal wrappers where required.

### Install

```bash
git clone <repository>
cd omnigent-blackboard-poc
cp .env.example .env

uv sync --extra dev
uv tool install "omnigent==0.5.1"
omnigent setup
```

### PostgreSQL

```bash
docker compose up -d postgres
docker compose ps
uv run blackboard migrate
```

### Validation

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

### MCP

```bash
uv run fastmcp run src/sdlc_blackboard/mcp/server.py:mcp \
  --transport http \
  --host 127.0.0.1 \
  --port 8000
```

Check:

```bash
curl http://127.0.0.1:8000/health
uv run fastmcp list http://127.0.0.1:8000/mcp
```

### Omnigent

```bash
omnigent run omnigent/sdlc_team/
```

### Demo prompt

```text
Initialize and execute the full SDLC POC for the report-export feature in
HANDOFF.md section 18. Use the blackboard as authoritative state. Delegate
analysis, implementation, QA, and security. Stop at the human release gate and
ask me for approval.
```

### Inspect state

```bash
uv run blackboard list-goals
uv run blackboard snapshot <goal-id>
uv run blackboard events <goal-id>
uv run blackboard gate <goal-id>
```

---

## 20. CLI

Use Typer for developer operations, not agent access.

```python
import asyncio
from pathlib import Path
from uuid import UUID

import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def migrate() -> None:
    asyncio.run(run_migrations(Path("migrations")))


@app.command("list-goals")
def list_goals() -> None:
    asyncio.run(print_goals())


@app.command()
def snapshot(goal_id: UUID) -> None:
    asyncio.run(print_snapshot(goal_id))


@app.command()
def events(goal_id: UUID) -> None:
    asyncio.run(print_events(goal_id))


@app.command()
def gate(goal_id: UUID) -> None:
    asyncio.run(print_gate(goal_id))


@app.command("reset-demo")
def reset_demo() -> None:
    asyncio.run(reset_demo_state())
```

Do not expose destructive maintenance commands through MCP.

---

## 21. Test Plan

### State transitions

```python
@pytest.mark.parametrize(
    ("from_state", "to_state", "allowed"),
    [
        (TaskState.DRAFT, TaskState.READY, True),
        (TaskState.READY, TaskState.ASSIGNED, True),
        (TaskState.RUNNING, TaskState.SUBMITTED, True),
        (TaskState.ACCEPTED, TaskState.RUNNING, False),
        (TaskState.CANCELLED, TaskState.SUBMITTED, False),
    ],
)
def test_transition_matrix(from_state, to_state, allowed):
    assert can_transition(from_state, to_state) is allowed
```

### Idempotency

```python
async def test_duplicate_command_replays_original(services):
    command_id = uuid4()
    context = make_context(command_id=command_id)
    request = make_goal_create()

    first = await services.goals.create_goal(context, request)
    second = await services.goals.create_goal(context, request)

    assert first.value.goal_id == second.value.goal_id
    assert second.replayed is True
```

### Command mismatch

```python
async def test_command_id_with_different_payload_is_rejected(services):
    command_id = uuid4()
    context = make_context(command_id=command_id)

    await services.goals.create_goal(context, make_goal_create(title="A"))
    result = await services.goals.create_goal(
        context,
        make_goal_create(title="B"),
    )

    assert result.error.code == ErrorCode.DUPLICATE_COMMAND_MISMATCH
```

### Concurrent claim

```python
async def test_only_one_claim_wins(services, ready_task):
    async def claim(actor_id: str):
        return await services.tasks.claim_task(
            make_context(actor_id=actor_id),
            task_id=ready_task.task_id,
            actor_id=actor_id,
        )

    a, b = await asyncio.gather(
        claim("implementation-a"),
        claim("implementation-b"),
    )

    accepted = [x for x in (a, b) if x.status == "accepted"]
    rejected = [x for x in (a, b) if x.status != "accepted"]

    assert len(accepted) == 1
    assert len(rejected) == 1
```

Run repeatedly.

### Fencing

```python
async def test_stale_assignment_cannot_submit(services, reassigned_task):
    result = await services.tasks.submit_task_result(
        context=make_context(
            actor_id="old-worker",
            assignment_epoch=reassigned_task.old_epoch,
        ),
        request=make_submission(reassigned_task.task_id),
    )

    assert result.error.code == ErrorCode.STALE_ASSIGNMENT
```

### Review binding

```python
async def test_revision_one_review_does_not_approve_revision_two(...):
    review = await approve(revision_one)
    await promote(revision_two)

    gate = await get_gate_status(goal_id)

    assert review.review_id in gate.stale_review_ids
    assert "quality" in gate.missing_reviews
```

### Blocking finding

```python
async def test_open_blocker_prevents_gate(...):
    await open_blocking_security_finding(...)
    gate = await get_gate_status(goal_id)

    assert gate.status == GateStatus.UNSATISFIED
    assert gate.open_blocking_finding_ids
```

### MCP integration

Use a FastMCP in-memory or HTTP test client:

```python
async def test_mcp_create_and_read_goal(mcp_client):
    created = await mcp_client.call_tool(
        "create_goal",
        {
            "command": make_context_dict(),
            "goal": make_goal_create_dict(),
        },
    )
    goal_id = created.structured_content["value"]["goal_id"]

    snapshot = await mcp_client.call_tool(
        "get_goal_snapshot",
        {"goal_id": goal_id},
    )
    assert snapshot.structured_content["goal"]["goal_id"] == goal_id
```

### Scripted E2E without LLMs

Automate:

1. create goal;
2. create tasks;
3. submit analysis;
4. submit implementation revision 1;
5. submit QA approval;
6. open security blocker;
7. create remediation;
8. submit implementation revision 2;
9. verify stale reviews;
10. submit current reviews;
11. record human approval;
12. authorize completion.

This proves the kernel independently of Omnigent and model behavior.

---

## 22. Observability

Every log and span should include where applicable:

```text
goal_id
task_id
command_id
correlation_id
causation_id
assignment_epoch
runtime_run_id
omnigent_conversation_id
artifact_revision_id
actor_id
tool_name
```

Example:

```python
logger.info(
    "task_result_submitted",
    goal_id=str(task.goal_id),
    task_id=str(task.task_id),
    run_id=str(request.run_id),
    assignment_epoch=task.assignment_epoch,
    actor_id=context.actor.actor_id,
    artifact_revision_ids=[str(r.revision_id) for r in revisions],
)
```

POC counters:

- commands accepted;
- idempotent replays;
- stale versions;
- stale assignments;
- claim conflicts;
- findings opened;
- stale reviews;
- gate failures by reason;
- specialist runs;
- runtime failures;
- task cycle time;
- goal cycle time.

---

## 23. Failure Recovery

### Child fails before runtime start

- mark assignment failed;
- increment epoch on reassignment;
- return task to ready;
- record failure event.

### Child starts but never submits

- cancel the Omnigent child;
- expire assignment;
- reassign with a new epoch.

Post-POC, add leases, heartbeats, and workflow timeouts.

### Child submits but inbox result is lost

Read the blackboard snapshot. It is authoritative.

### MCP response is lost after commit

Retry with the same `command_id`. The original response is replayed.

### Old worker returns after reassignment

Its epoch is stale. Reject authoritative mutation. Optionally preserve the output as an unattached candidate for debugging.

### Omnigent restarts

Start a new lead session and supply the existing goal ID. The new lead reads the snapshot and resumes.

### MCP restarts

All authoritative state is in PostgreSQL. Clients reconnect and retry safely.

### PostgreSQL outage

Return a retryable structured error. Do not generate a new command ID for the same intended mutation.

---

## 24. Security Posture

### POC

- bind MCP to `127.0.0.1`;
- no production credentials;
- no production deployment tool;
- separate filesystem scopes;
- use Omnigent sandbox defaults where practical;
- retain human observation and cancellation;
- log every authoritative command.

### Known weaknesses

- actor IDs can be spoofed in payloads;
- no service authentication;
- no task-scoped credentials;
- no cryptographic approval;
- no tenant isolation;
- local artifact URIs may be mutable unless hashes are rechecked.

### Post-POC hardening

1. Add FastMCP token verification or OAuth.
2. Issue task-scoped JWTs containing actor, task, epoch, permissions, and expiry.
3. Derive actor identity from verified claims.
4. Enforce object-level authorization server-side.
5. Store artifacts in immutable object storage.
6. Sign approvals and decisions.
7. Add TLS and reverse proxy controls.
8. Add rate limits and command budgets.
9. Pin and attest MCP servers.
10. Add an external audit sink.
11. Apply Omnigent contextual policies as defense in depth.

---

## 25. Concurrency Summary

| Object | Write model | Control |
|---|---|---|
| Team event | Immutable append | Unique event and command IDs |
| Goal | Versioned state machine | Compare-and-set |
| Task | Versioned state machine | Compare-and-set |
| Assignment | One active owner | Partial unique index |
| Worker authority | Fenced | Assignment epoch |
| Runtime attempt | Append attempt | Unique run ID |
| Artifact | Immutable revision | Hash and unique revision |
| Artifact alias | Mutable pointer | Compare-and-set |
| Finding | Assertion plus versioned disposition | Expected version |
| Review | Immutable and revision-bound | Fingerprint and unique index |
| Approval | Immutable and revision-bound | Exact binding fingerprint |
| Gate | Derived query | Transactional authorization |
| Command | Idempotent | Command ID and request hash |
| Event delivery | At least once | Outbox and deduplication |
| Snapshot | Read projection | Rebuildable |

---

## 26. Implementation Order

### Milestone 1 — Kernel

- package layout;
- settings and pool;
- migrations;
- domain models;
- goal and task repositories;
- idempotency;
- unit tests.

**Exit:** scripted goal and task creation works.

### Milestone 2 — Concurrency

- task CAS;
- active-assignment constraint;
- epochs;
- runtime runs;
- concurrent-claim and zombie-worker tests.

**Exit:** concurrency tests pass repeatedly.

### Milestone 3 — Artifacts and review

- artifact revisions and aliases;
- findings;
- reviews and fingerprints;
- stale invalidation;
- gate query.

**Exit:** scripted flow reaches human-required gate.

### Milestone 4 — MCP

- FastMCP server;
- read tools;
- atomic commands;
- health route;
- MCP integration tests.

**Exit:** scripted flow works exclusively through MCP.

### Milestone 5 — Omnigent

- pinned install;
- lead and skills;
- four specialist configs;
- validate each agent independently;
- run analyst;
- run native implementation.

**Exit:** children persist authoritative results through MCP.

### Milestone 6 — Full demo

- run report-export scenario;
- produce security finding;
- remediate;
- repeat reviews;
- collect human approval;
- authorize completion;
- save final snapshot.

---

## 27. Agent Handoff Prompt

```text
Implement the repository described in
omnigent_blackboard_mcp_poc_handoff.md.

Verify the exact public APIs of the pinned versions before coding, especially
FastMCP lifespan context access and Omnigent YAML syntax.

Rules:

1. Python 3.14, Pydantic v2, asyncpg, FastMCP 3.
2. Domain and application logic must not depend on MCP.
3. Use explicit SQL migrations.
4. Every mutation is idempotent by command_id and request hash.
5. Mutable aggregates use optimistic versions.
6. Assignments use a unique active owner and fencing epochs.
7. Artifacts and reviews are immutable and revision-bound.
8. MCP exposes coarse-grained domain intentions, never table CRUD.
9. Complete deterministic kernel tests before Omnigent integration.
10. Do not add Kafka, Temporal, Restate, Redis, Yjs, or a custom UI.
11. Bind the unauthenticated MCP service to loopback only.
12. Pin Omnigent to a release or exact commit.
13. Run ruff, pyright, and pytest before completion.
14. Provide exact setup and demo commands.
15. Record deviations as ADRs in README.

Build in the milestone order in section 26. Preserve the architectural boundary
when adapting to changed public APIs.
```

---

## 28. Post-POC Roadmap

### Durable outer workflow

Add Restate or Temporal when goals span days, human decisions are long-lived, external events must wake the loop, and timeout/retry policy becomes important. Keep specialist inner loops inside one runtime activity or external run.

### Authentication and capabilities

Add task-scoped signed credentials. Remove actor identity from agent-controlled mutation inputs.

### Event bus

Publish the outbox to Kafka or another durable event system. PostgreSQL remains transactional truth.

### Team UI

Create:

```text
Goal
Work graph
Activity
Artifacts
Findings
Reviews
Decisions
Human requests
```

### CRDT collaboration

Use Yjs only for draft requirements, annotations, co-authoring, comments, and presence. Keep ownership, promotion, findings, approvals, and gates transactional.

### Persistent functions

Add durable function identities, project memory, subscriptions, capability scores, trust calibration, and dynamic runtime assignment.

### Additional functions

Add UX, architecture, deployment, reliability, support, documentation, data, and compliance as task capabilities rather than theatrical personas.

---

## 29. Final Invariants

```text
The human goal becomes explicit task contracts.

A function name is an organizational convenience.
The task contract drives behavior.

An Omnigent child session is an execution attempt.
It is not the authoritative task.

The blackboard does not depend on the inner harness.

MCP is an adapter.
It is not the event bus, transaction manager, or workflow engine.

Every mutation is idempotent.

Every mutable aggregate is versioned.

Every task assignment is fenced.

Every artifact revision is immutable.

Every review and approval names exact artifact revisions.

Chat is a projection of activity.
It is not organizational truth.

A release is authorized by deterministic gates.
It is not authorized because the lead says it looks ready.
```

---

## Appendix A. Tool Matrix

| Tool | Lead | Analyst | Implementation | QA | Security |
|---|---:|---:|---:|---:|---:|
| `create_goal` | ✓ |  |  |  |  |
| `create_task` | ✓ |  |  |  |  |
| `refresh_ready_tasks` | ✓ |  |  |  |  |
| `get_goal_snapshot` | ✓ |  |  |  |  |
| `get_task_contract` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `get_artifact_revision` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `read_relevant_events` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `claim_task` | ✓ |  |  |  |  |
| `bind_runtime_session` | ✓ |  |  |  |  |
| `start_runtime_run` |  | ✓ | ✓ | ✓ | ✓ |
| `submit_task_result` |  | ✓ | ✓ | ✓ | ✓ |
| `open_finding` |  |  |  | ✓ | ✓ |
| `resolve_finding` | ✓ |  | ✓* | ✓ | ✓ |
| `submit_review` |  |  |  | ✓ | ✓ |
| `promote_artifact` | ✓ |  |  |  |  |
| `get_gate_status` | ✓ |  |  |  |  |
| `record_human_approval` | ✓ |  |  |  |  |
| `authorize_goal_completion` | ✓ |  |  |  |  |

`*` Implementation may mark a finding remediated through a remediation task. The reviewing function verifies closure.

---

## Appendix B. Task Transition Matrix

| From | To | Trigger |
|---|---|---|
| `draft` | `ready` | dependencies accepted |
| `ready` | `assigned` | atomic claim |
| `assigned` | `running` | runtime starts |
| `running` | `awaiting_input` | specialist requests input |
| `awaiting_input` | `running` | input supplied |
| `running` | `submitted` | result submission |
| `submitted` | `under_review` | review tasks created |
| `under_review` | `accepted` | reviews and criteria pass |
| `under_review` | `revision_required` | remediable blocker |
| `revision_required` | `ready` | revised work prepared |
| nonterminal | `blocked` | external blocker |
| `blocked` | prior state | blocker resolved |
| nonterminal | `failed` | unrecoverable failure |
| nonterminal | `cancelled` | authorized cancellation |
| submitted/accepted | `superseded` | replacement |

Do not accept arbitrary transitions from the model.

---

## Appendix C. Example Gate Output

```json
{
  "status": "human_required",
  "implementation_binding": {
    "artifact_id": "8f469e90-b70c-4eeb-9b27-6ee86f71d7ee",
    "revision_id": "f27e73d6-2dd4-432f-a319-eb3eb303f338",
    "logical_name": "source/report-export",
    "content_hash": "a81237b..."
  },
  "missing_reviews": [],
  "open_blocking_finding_ids": [],
  "historical_stale_review_ids": [
    "a4fc2469-2e6e-43d0-a40a-780bfe194459"
  ],
  "missing_approvals": [
    "human_release"
  ]
}
```

---

## Appendix D. Review Checklist

### Architecture

- [ ] Omnigent conversation IDs are not task primary keys.
- [ ] MCP tools contain no SQL.
- [ ] Kernel services import no FastMCP.
- [ ] Domain models import no asyncpg.
- [ ] Chat messages do not authorize transitions.

### Concurrency

- [ ] CAS checks expected version and state.
- [ ] Active assignment is database-enforced.
- [ ] Epoch is checked for worker mutations.
- [ ] Duplicate command IDs replay responses.
- [ ] Reused ID with changed payload is rejected.
- [ ] Alias updates require expected revision.

### Reviews

- [ ] Bindings include artifact ID, revision ID, and hash.
- [ ] Review uniqueness is database-enforced.
- [ ] New revisions invalidate old reviews.
- [ ] Open blockers prevent completion.
- [ ] Human approval binds current revision.

### Operations

- [ ] MCP binds loopback.
- [ ] Health endpoint works.
- [ ] PostgreSQL restart preserves state.
- [ ] MCP restart preserves state.
- [ ] New lead session resumes an existing goal.
- [ ] Logs contain goal, task, command, epoch, and run IDs.

### Demo

- [ ] Revision 1 contains a detectable CSV injection risk.
- [ ] Security opens a blocker.
- [ ] Remediation creates revision 2.
- [ ] Revision 1 reviews become stale.
- [ ] Revision 2 passes QA and security.
- [ ] Human approval is required.
- [ ] Completion succeeds only after approval.

---

## Appendix E. References

- Omnigent repository: https://github.com/omnigent-ai/omnigent
- Omnigent Agent YAML: https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md
- Omnigent Polly: https://github.com/omnigent-ai/omnigent/tree/main/examples/polly
- Polly configuration: https://raw.githubusercontent.com/omnigent-ai/omnigent/main/examples/polly/config.yaml
- Omnigent custom policies: https://omnigent.ai/docs/policies/custom
- Contextual policies: https://www.databricks.com/blog/contextual-policies-omnigent-using-session-state-better-govern-ai-agents
- FastMCP: https://gofastmcp.com/
- FastMCP tools: https://gofastmcp.com/servers/tools
- FastMCP HTTP: https://gofastmcp.com/deployment/running-server
- FastMCP authentication: https://gofastmcp.com/servers/auth/authentication
- FastMCP authorization: https://gofastmcp.com/servers/authorization
- MCP specification: https://modelcontextprotocol.io/specification/2025-11-25
- MCP release candidate: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- PostgreSQL isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL locking: https://www.postgresql.org/docs/current/explicit-locking.html
- Claude Opus 4.8 model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-8.html
- Claude Fable 5 model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-fable-5.html
- GPT-5.6 Sol model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-sol.html
- Claude Code on Amazon Bedrock: https://code.claude.com/docs/en/amazon-bedrock
- Codex on Amazon Bedrock: https://learn.chatgpt.com/docs/amazon-bedrock
- OpenAI models on Amazon Bedrock: https://developers.openai.com/api/docs/guides/amazon-bedrock
- Bedrock global cross-Region inference: https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html
- Bedrock endpoint availability: https://docs.aws.amazon.com/bedrock/latest/userguide/models-endpoint-availability.html
