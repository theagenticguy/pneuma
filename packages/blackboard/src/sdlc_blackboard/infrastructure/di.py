"""Composition root — the only module that knows both ports and concrete adapters.

hexagonal-arch-stack.md §3: "Drivers build their own ports." The blackboard's
command/query services share one process-lifetime pool and are request-less (each
command opens its own unit-of-work transaction), so a single APP-scoped builder is
the right shape — no per-request scope dance is needed. This keeps the framework out
of the domain while giving the MCP lifespan and the CLI one call to build everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_blackboard.application.use_cases.services import Services
from sdlc_blackboard.application.use_cases.wiring import ServicePorts
from sdlc_blackboard.domain.settings import Settings
from sdlc_blackboard.infrastructure.clock import SystemClock
from sdlc_blackboard.infrastructure.postgres import Postgres, UnitOfWork
from sdlc_blackboard.infrastructure.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssignmentRepository,
    CommandFailureRepository,
    EventRepository,
    FindingRepository,
    GoalRepository,
    OutboxRepository,
    ProcessedCommandRepository,
    ReviewRepository,
    RuntimeRunRepository,
    TaskRepository,
)


@dataclass(frozen=True)
class Container:
    """Holds the process-lifetime pool + the built Services facade."""

    settings: Settings
    postgres: Postgres
    services: Services


def build_ports(postgres: Postgres) -> ServicePorts:
    """Wire concrete adapters into the port bundle (binds impl -> Protocol by shape)."""
    return ServicePorts(
        uow=UnitOfWork(postgres),
        clock=SystemClock(),
        goals=GoalRepository(),
        tasks=TaskRepository(),
        assignments=AssignmentRepository(),
        runs=RuntimeRunRepository(),
        artifacts=ArtifactRepository(),
        findings=FindingRepository(),
        reviews=ReviewRepository(),
        approvals=ApprovalRepository(),
        events=EventRepository(),
        outbox=OutboxRepository(),
        processed_commands=ProcessedCommandRepository(),
        command_failures=CommandFailureRepository(),
    )


async def build_container(settings: Settings | None = None) -> Container:
    """Build the pool (started) + Services. Caller is responsible for teardown via
    ``container.postgres.stop()``."""
    settings = settings or Settings()
    postgres = Postgres(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        command_timeout=settings.pool_command_timeout,
    )
    await postgres.start()
    services = Services.build(build_ports(postgres))
    return Container(settings=settings, postgres=postgres, services=services)
