# omnigent-blackboard-poc · Public API

### TaskState

```py
class TaskState(StrEnum):
```

Closed enum of the task lifecycle states, from `DRAFT`/`READY` through `SUBMITTED`, `UNDER_REVIEW`, and the terminal `ACCEPTED`/`FAILED`/`CANCELLED`/`SUPERSEDED`.
`src/sdlc_blackboard/domain/tasks.py:18`

### ActorKind

```py
class ActorKind(StrEnum):
```

An actor's authority class — a bounded context, not a persona — naming the slice of organizational authority a session holds.
`src/sdlc_blackboard/domain/common.py:25`

### Task

```py
class Task(DomainModel):
```

The task aggregate: an assignable, versioned unit of work carrying its immutable contract, current state, and assignment fencing fields.
`src/sdlc_blackboard/domain/tasks.py:69`

### CommandResult

```py
class CommandResult[T](BaseModel):
```

Generic structured outcome returned by every mutating use case, wrapping a status, an optional value, and an optional error so conflicts are values rather than exceptions.
`src/sdlc_blackboard/application/results.py:84`

### CommandContext

```py
class CommandContext(DomainModel):
```

Envelope carried by every mutating command, bundling the actor, command id for idempotency, correlation/causation ids, and optimistic-concurrency checks.
`src/sdlc_blackboard/domain/common.py:92`

### ActorRef

```py
class ActorRef(DomainModel):
```

References an actor by its stable id and its `ActorKind` authority class.
`src/sdlc_blackboard/domain/common.py:87`

### ArtifactBinding

```py
class ArtifactBinding(DomainModel):
```

Names an exact, immutable artifact revision by id plus hash so authority always binds a concrete revision, never a mutable pointer.
`src/sdlc_blackboard/domain/common.py:109`

### DomainModel

```py
class DomainModel(BaseModel):
```

Base for every domain model: frozen (immutable) and extra-forbidding.
`src/sdlc_blackboard/domain/common.py:19`

### Goal

```py
class Goal(DomainModel):
```

The goal aggregate — the top-level organizational objective — carrying its criteria, owner, current state, and version.
`src/sdlc_blackboard/domain/goals.py:30`

### CommandStatus

```py
class CommandStatus(StrEnum):
```

Wire enum of command outcomes a caller observes, from `ACCEPTED` and `DUPLICATE_REPLAYED` through the concurrency and precondition failure statuses.
`src/sdlc_blackboard/application/results.py:30`

### Container

```py
class Container:
```

Holds the process-lifetime pool + the built Services facade.
`src/sdlc_blackboard/infrastructure/di.py:36`

### Finding

```py
class Finding(DomainModel):
```

The finding aggregate: an immutable assertion whose versioned resolution state can block the release gate until remediated or accepted-as-risk.
`src/sdlc_blackboard/domain/findings.py:64`

### GoalCreate

```py
class GoalCreate(DomainModel):
```

Input DTO to create a goal, carrying its title, objective, success criteria, constraints, and owner.
`src/sdlc_blackboard/domain/goals.py:22`

### create_goal

```py
    async def create_goal(
        self, context: CommandContext, request: GoalCreate
    ) -> CommandResult[Goal]:
```

`GoalService` command that inserts a new active goal and appends a `goal.created` domain event.
`src/sdlc_blackboard/application/use_cases/goal_service.py:25`

### ArtifactRevision

```py
class ArtifactRevision(DomainModel):
```

An immutable artifact revision keyed by artifact id plus content hash, recording its content pointer, provenance, parents, and status.
`src/sdlc_blackboard/domain/artifacts.py:33`

### TaskContractCreate

```py
class TaskContractCreate(DomainModel):
```

Input DTO defining a task's immutable contract: objective, required actor kind, scope, deliverables, acceptance criteria, dependencies, and review requirements.
`src/sdlc_blackboard/domain/tasks.py:52`

### ErrorCode

```py
class ErrorCode(StrEnum):
```

Wire error enum that maps one-to-one onto the domain error hierarchy in `domain.errors`.
`src/sdlc_blackboard/application/results.py:18`

### build_container

```py
async def build_container(settings: Settings | None = None) -> Container:
```

Builds the started asyncpg pool and Services facade into a `Container`; the caller is responsible for teardown via `container.postgres.stop()`.
`src/sdlc_blackboard/infrastructure/di.py:64`

### Review

```py
class Review(DomainModel):
```

The review aggregate: an immutable, binding-fingerprinted disposition over exact artifact revisions that goes stale when the bound revision is superseded.
`src/sdlc_blackboard/domain/reviews.py:65`

### FindingState

```py
class FindingState(StrEnum):
```

Closed enum of a finding's resolution states, from `OPEN`/`ACKNOWLEDGED` through the resolved `VERIFIED`/`ACCEPTED_RISK`/`REJECTED`/`SUPERSEDED`.
`src/sdlc_blackboard/domain/findings.py:31`

### DeliverableSpec

```py
class DeliverableSpec(DomainModel):
```

Specifies a single deliverable a task must produce, by artifact type and logical name, with a required flag.
`src/sdlc_blackboard/domain/tasks.py:40`

### binding_fingerprint

```py
def binding_fingerprint(bindings: tuple[ArtifactBinding, ...]) -> str:
```

Order-independent SHA-256 fingerprint of a set of artifact bindings, giving a review a stable identity regardless of binding order.
`src/sdlc_blackboard/domain/reviews.py:32`

### ReviewRequirement

```py
class ReviewRequirement(DomainModel):
```

Declares a review a task requires, by reviewer kind and review type, with a blocking flag.
`src/sdlc_blackboard/domain/tasks.py:46`

### Postgres

```py
class Postgres:
```

Owns the asyncpg pool lifecycle (APP scope in the DI container).
`src/sdlc_blackboard/infrastructure/postgres.py:44`

### Approval

```py
class Approval(DomainModel):
```

The approval aggregate: an immutable, binding-fingerprinted human sign-off required by the release gate against the current implementation revision.
`src/sdlc_blackboard/domain/approvals.py:36`

### ReviewDisposition

```py
class ReviewDisposition(StrEnum):
```

Closed enum of the outcomes a review can record: `APPROVED`, `FINDINGS`, `REQUEST_REVISION`, or `ABSTAINED`.
`src/sdlc_blackboard/domain/reviews.py:25`

### ClaimTaskRequest

```py
class ClaimTaskRequest(DomainModel):
```

Input DTO to claim a ready task, carrying the task id and the claiming actor id.
`src/sdlc_blackboard/application/commands.py:26`

### StartRunRequest

```py
class StartRunRequest(DomainModel):
```

Input DTO to start a runtime run for a task, carrying the conversation id, input manifest, and Bedrock model-provenance fields.
`src/sdlc_blackboard/application/commands.py:36`

### NotFound

```py
class NotFound(DomainError):
```

Domain error raised when a named entity and id cannot be found, mapping to the `not_found` wire code.
`src/sdlc_blackboard/domain/errors.py:25`

### append_domain_event

```py
async def append_domain_event(
```

Appends one immutable record to the team event log within the current unit-of-work transaction.
`src/sdlc_blackboard/application/events.py:20`

## See also

- [contract-map](../insights/contract-map.md) — 13 shared source citations
- [impact-analysis](../insights/impact-analysis.md) — 12 shared source citations
- [module-map](../architecture/module-map.md) — 6 shared source citations
- [state-machines](../behavior/state-machines.md) — 5 shared source citations
- [business-logic](../insights/business-logic.md) — 5 shared source citations
