# omnigent-blackboard-poc · State machines

## FindingState

```mermaid
stateDiagram-v2
    [*] --> OPEN : open_finding
    OPEN --> ACKNOWLEDGED : resolve_finding
    OPEN --> REMEDIATED : resolve_finding
    OPEN --> VERIFIED : resolve_finding
    OPEN --> ACCEPTED_RISK : resolve_finding
    OPEN --> REJECTED : resolve_finding
    OPEN --> SUPERSEDED : resolve_finding
```

A finding is created in `OPEN` (`src/sdlc_blackboard/application/use_cases/review_service.py:63`). `resolve_finding` writes any target `FindingState` through the unconstrained `set_state_cas` (`src/sdlc_blackboard/application/use_cases/review_service.py:94-95`; request field typed `FindingState` at `src/sdlc_blackboard/application/commands.py:63-65`), so a transition from a non-`OPEN` source is also legal; the diagram shows the `OPEN` entry hub only. `VERIFIED`, `ACCEPTED_RISK`, `REJECTED`, and `SUPERSEDED` are the gate-resolved states (`RESOLVED_FINDING_STATES`, `src/sdlc_blackboard/domain/findings.py:42-49`); source enforces no state-machine terminal, so no `--> [*]` is drawn.
Defined at: `src/sdlc_blackboard/domain/findings.py:31-38`

## GoalState

```mermaid
stateDiagram-v2
    [*] --> ACTIVE : create_goal
    ACTIVE --> SATISFIED : authorize_goal_completion
```

A goal is created directly in `ACTIVE` (`src/sdlc_blackboard/application/use_cases/goal_service.py:35`); the only kernel transition moves `ACTIVE --> SATISFIED` via `authorize_goal_completion` (`src/sdlc_blackboard/application/use_cases/goal_service.py:81-82`). The enum also declares `DRAFT`, `BLOCKED`, `FAILED`, and `CANCELLED` (`src/sdlc_blackboard/domain/goals.py:13-19`), but no kernel transition site assigns them, so no edges to those states are drawn. Source declares no explicit terminal.
Defined at: `src/sdlc_blackboard/domain/goals.py:13-19`

## RunState

```mermaid
stateDiagram-v2
    [*] --> RUNNING : start_runtime_run
    RUNNING --> SUBMITTED : submit_task_result
```

A runtime run is inserted in `RUNNING` (`src/sdlc_blackboard/application/use_cases/task_service.py:185`), then moved `RUNNING --> SUBMITTED` inside `submit_task_result` (`src/sdlc_blackboard/application/use_cases/task_service.py:282-283`). The enum also declares `CREATED`, `FAILED`, and `CANCELLED` (`src/sdlc_blackboard/domain/events.py:17-22`), but no kernel transition site assigns them, so no edges to those states are drawn. Source declares no explicit terminal.
Defined at: `src/sdlc_blackboard/domain/events.py:17-22`

## TaskState

```mermaid
stateDiagram-v2
    [*] --> DRAFT
    [*] --> READY
    DRAFT --> READY
    READY --> ASSIGNED
    ASSIGNED --> RUNNING
    RUNNING --> AWAITING_INPUT
    AWAITING_INPUT --> RUNNING
    RUNNING --> SUBMITTED
    SUBMITTED --> UNDER_REVIEW
    UNDER_REVIEW --> ACCEPTED
    UNDER_REVIEW --> REVISION_REQUIRED
    REVISION_REQUIRED --> READY
    BLOCKED --> READY
    BLOCKED --> ASSIGNED
    BLOCKED --> RUNNING
    BLOCKED --> AWAITING_INPUT
    BLOCKED --> UNDER_REVIEW
    SUBMITTED --> SUPERSEDED
    ACCEPTED --> SUPERSEDED
    DRAFT --> BLOCKED
    DRAFT --> FAILED
    DRAFT --> CANCELLED
    READY --> BLOCKED
    READY --> FAILED
    READY --> CANCELLED
    ASSIGNED --> BLOCKED
    ASSIGNED --> FAILED
    ASSIGNED --> CANCELLED
    RUNNING --> BLOCKED
    RUNNING --> FAILED
    RUNNING --> CANCELLED
    AWAITING_INPUT --> BLOCKED
    AWAITING_INPUT --> FAILED
    AWAITING_INPUT --> CANCELLED
    SUBMITTED --> BLOCKED
    SUBMITTED --> FAILED
    SUBMITTED --> CANCELLED
    UNDER_REVIEW --> BLOCKED
    UNDER_REVIEW --> FAILED
    UNDER_REVIEW --> CANCELLED
    REVISION_REQUIRED --> BLOCKED
    REVISION_REQUIRED --> FAILED
    REVISION_REQUIRED --> CANCELLED
    BLOCKED --> FAILED
    BLOCKED --> CANCELLED
    ACCEPTED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
    SUPERSEDED --> [*]
```

A task is created in `DRAFT` when it has dependencies, else `READY` (`src/sdlc_blackboard/application/use_cases/task_service.py:79`). The explicit `(from -> to)` edges come from `_EXPLICIT_EDGES` (`src/sdlc_blackboard/domain/transitions.py:15-37`); every non-terminal state additionally transitions to `BLOCKED`, `FAILED`, or `CANCELLED` via the universal rule (`src/sdlc_blackboard/domain/transitions.py:39-42,45-58`). `ACCEPTED`, `FAILED`, `CANCELLED`, and `SUPERSEDED` are terminal (`TERMINAL_TASK_STATES`, `src/sdlc_blackboard/domain/tasks.py:35-37`). Self-loops are disallowed (`src/sdlc_blackboard/domain/transitions.py:54-55`). Edges carry no labels because `transitions.py` defines them as pure state pairs; transition triggers are scattered across `transition_cas` call sites in `task_service.py` (e.g. `:195`, `:287`, `:328`, `:340`, `:370`).
Defined at: `src/sdlc_blackboard/domain/transitions.py:15-58`

## See also

- [impact-analysis](../insights/impact-analysis.md) — 9 shared source citations
- [contract-map](../insights/contract-map.md) — 8 shared source citations
- [business-logic](../insights/business-logic.md) — 6 shared source citations
- [public-api](../reference/public-api.md) — 5 shared source citations
- [processes](../behavior/processes.md) — 3 shared source citations
