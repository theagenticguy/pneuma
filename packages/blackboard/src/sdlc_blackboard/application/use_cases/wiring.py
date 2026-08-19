"""Service wiring — the port bundle every use case is constructed from.

The composition root (``infrastructure/di.py``) builds one ``ServicePorts`` from
concrete adapters and hands it to each service. Services never import concrete
adapters; they receive ports by shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_blackboard.application.ports import (
    ApprovalRepo,
    ArtifactRepo,
    AssignmentRepo,
    Clock,
    CommandFailureRepo,
    EventRepo,
    FindingRepo,
    GoalRepo,
    OutboxRepo,
    ProcessedCommandStore,
    ReviewRepo,
    RuntimeRunRepo,
    TaskRepo,
    UnitOfWork,
)


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
