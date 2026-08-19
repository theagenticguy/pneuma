"""asyncpg repository adapters implementing the application ports.

hexagonal-arch-stack.md: adapters implement ports by shape; they translate between
domain models and rows. No ORM — explicit SQL, because the load-bearing operations
are compare-and-set transitions, partial unique constraints, ``FOR UPDATE``, and
``SKIP LOCKED`` (handoff §9).

This package splits the former ~1000-LOC flat module by aggregate for cohesion
(one module per bounded slice) while re-exporting every public repository so
``from sdlc_blackboard.infrastructure.repositories import X`` keeps working. The
jsonb codec (``infrastructure.postgres``) means we pass and receive plain
``dict``/``list`` for jsonb columns — no per-call ``orjson`` dance. ``S608`` is
suppressed for the whole package in ``pyproject.toml`` with that reason; the
per-module docstrings restate the trusted-table-constant stance.
"""

from sdlc_blackboard.infrastructure.repositories.artifacts import ArtifactRepository
from sdlc_blackboard.infrastructure.repositories.events_outbox import (
    EventRepository,
    OutboxRepository,
)
from sdlc_blackboard.infrastructure.repositories.failures import CommandFailureRepository
from sdlc_blackboard.infrastructure.repositories.goals import GoalRepository
from sdlc_blackboard.infrastructure.repositories.idempotency import (
    ProcessedCommandRepository,
)
from sdlc_blackboard.infrastructure.repositories.quality import (
    ApprovalRepository,
    FindingRepository,
    ReviewRepository,
)
from sdlc_blackboard.infrastructure.repositories.tasks import (
    AssignmentRepository,
    RuntimeRunRepository,
    TaskRepository,
)

__all__ = [
    "ApprovalRepository",
    "ArtifactRepository",
    "AssignmentRepository",
    "CommandFailureRepository",
    "EventRepository",
    "FindingRepository",
    "GoalRepository",
    "OutboxRepository",
    "ProcessedCommandRepository",
    "ReviewRepository",
    "RuntimeRunRepository",
    "TaskRepository",
]
