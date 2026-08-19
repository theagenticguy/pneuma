"""Output DTOs (receipts) returned inside CommandResult.value.

Never leak ORM rows or raw domain internals across the boundary; a receipt is a
purpose-built projection of what a command produced.
"""

from __future__ import annotations

from uuid import UUID

from sdlc_blackboard.domain.artifacts import ArtifactRevision
from sdlc_blackboard.domain.common import DomainModel
from sdlc_blackboard.domain.tasks import Task


class TaskSubmissionReceipt(DomainModel):
    task: Task
    artifact_revisions: tuple[ArtifactRevision, ...]
    review_task_ids: tuple[UUID, ...]


class ClaimReceipt(DomainModel):
    task: Task
    assignment_epoch: int


class TaskListReceipt(DomainModel):
    """Wraps a tuple of tasks so tuple-returning commands have a BaseModel result."""

    tasks: tuple[Task, ...]
