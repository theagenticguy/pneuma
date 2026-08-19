"""Structured command outcomes crossing the application boundary (handoff §7).

Concurrency conflicts are values, not exceptions: every mutating use case returns
``CommandResult[T]``. ``ErrorCode`` is the wire enum; it maps 1:1 onto the domain
error hierarchy (``domain.errors``). Adapters translate a ``Result[T, DomainError]``
into a ``CommandResult[T]`` at the edge.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sdlc_blackboard.domain.errors import DomainError


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


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE_REPLAYED = "duplicate_replayed"
    STALE_VERSION = "stale_version"
    STALE_ASSIGNMENT = "stale_assignment"
    PRECONDITION_FAILED = "precondition_failed"
    UNAUTHORIZED = "unauthorized"
    CONFLICT_CREATED = "conflict_created"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"


#: Domain error code -> wire error code. Both use the same string values, so this
#: is a total, order-free lookup that also documents the mapping explicitly.
_DOMAIN_TO_ERROR_CODE: dict[str, ErrorCode] = {c.value: c for c in ErrorCode}

#: Wire error code -> the status a failed command reports.
_ERROR_CODE_TO_STATUS: dict[ErrorCode, CommandStatus] = {
    ErrorCode.NOT_FOUND: CommandStatus.NOT_FOUND,
    ErrorCode.VALIDATION_FAILED: CommandStatus.VALIDATION_FAILED,
    ErrorCode.DUPLICATE_COMMAND_MISMATCH: CommandStatus.VALIDATION_FAILED,
    ErrorCode.STALE_VERSION: CommandStatus.STALE_VERSION,
    ErrorCode.STALE_ASSIGNMENT: CommandStatus.STALE_ASSIGNMENT,
    ErrorCode.PRECONDITION_FAILED: CommandStatus.PRECONDITION_FAILED,
    ErrorCode.UNAUTHORIZED: CommandStatus.UNAUTHORIZED,
    ErrorCode.CONFLICT: CommandStatus.CONFLICT_CREATED,
    ErrorCode.INTERNAL_ERROR: CommandStatus.VALIDATION_FAILED,
}


class CommandError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    current_version: int | None = None
    current_state: str | None = None
    details: dict[str, object] = {}

    @classmethod
    def from_domain(cls, err: DomainError) -> CommandError:
        code = _DOMAIN_TO_ERROR_CODE.get(err.code, ErrorCode.INTERNAL_ERROR)
        current_version = getattr(err, "current_version", None)
        current_state = getattr(err, "current_state", None)
        return cls(
            code=code,
            message=err.message,
            retryable=err.retryable,
            current_version=current_version,
            current_state=current_state,
        )


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

    @classmethod
    def from_domain_error(cls, err: DomainError) -> CommandResult[T]:
        return cls.failed(CommandError.from_domain(err))
