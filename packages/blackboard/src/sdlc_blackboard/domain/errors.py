"""Closed domain error hierarchy (hexagonal-arch-stack.md §4: errors are values).

Use cases return ``Result[T, DomainError]`` across the application boundary.
These typed errors map 1:1 onto the wire ``ErrorCode`` in ``application.results``.
Exceptions are reserved for genuinely exceptional infrastructure failure and are
translated to these typed errors at the adapter edge.
"""

from __future__ import annotations

from uuid import UUID


class DomainError(Exception):
    """Base of the closed hierarchy. Carries a stable machine code + human message."""

    code: str = "internal_error"
    retryable: bool = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    code = "not_found"

    def __init__(self, entity: str, entity_id: object) -> None:
        super().__init__(f"{entity} {entity_id} not found")
        self.entity = entity
        self.entity_id = entity_id


class ValidationFailed(DomainError):
    code = "validation_failed"


class DuplicateCommandMismatch(DomainError):
    code = "duplicate_command_mismatch"


class StaleVersion(DomainError):
    code = "stale_version"

    def __init__(self, message: str = "Optimistic version check failed.") -> None:
        super().__init__(message)
        self.current_version: int | None = None


class StaleAssignment(DomainError):
    code = "stale_assignment"

    def __init__(self, message: str = "Assignment epoch is stale; worker lost authority.") -> None:
        super().__init__(message)


class PreconditionFailed(DomainError):
    code = "precondition_failed"


class Unauthorized(DomainError):
    code = "unauthorized"


class Conflict(DomainError):
    code = "conflict"


class ConcurrentCommandConflict(Conflict):
    """The same command_id was processed concurrently (dedup INSERT lost the race).

    Retryable: the losing transaction rolls back cleanly, and a retry with the same
    command_id replays the winner's stored response.
    """

    retryable = True

    def __init__(self) -> None:
        super().__init__("Command is being processed concurrently; retry to replay the result.")


class InvalidTransition(PreconditionFailed):
    """A task/goal state transition not permitted by the transition matrix."""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(f"Illegal transition {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


class InputManifestMismatch(PreconditionFailed):
    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"Submitted input manifest does not match the runtime run for {task_id}")
        self.task_id = task_id
