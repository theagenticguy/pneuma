"""Shared command-service base: transaction + idempotency + error translation.

Each command method supplies a ``body(conn)`` coroutine; the base owns opening the
unit-of-work transaction, running it idempotently, and translating a raised
``DomainError`` into a failed ``CommandResult``. This keeps every service method to
just its domain logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from pydantic import BaseModel

from sdlc_blackboard.application.idempotency import execute_idempotently
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.domain.errors import DomainError

if TYPE_CHECKING:
    from sdlc_blackboard.application.ports import Conn
    from sdlc_blackboard.application.use_cases.wiring import ServicePorts
    from sdlc_blackboard.domain.common import CommandContext


class CommandService:
    def __init__(self, ports: ServicePorts) -> None:
        self._p = ports

    async def _command[R: BaseModel](
        self,
        context: CommandContext,
        tool_name: str,
        request: BaseModel,
        result_type: type[R],
        body: Callable[[Conn], Awaitable[R]],
    ) -> CommandResult[R]:
        log = structlog.get_logger()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            command_id=str(context.command_id),
            actor=context.actor.actor_id,
            tool=tool_name,
        )
        # The DomainError catch lives OUTSIDE `async with uow.begin()` so a raised domain
        # error propagates out of the context manager and the unit of work rolls back
        # naturally (ADR-0014). Catching inside the block let control fall through to a
        # clean __aexit__, which COMMITTED the empty/partial transaction — a latent
        # partial-commit trap. Now the mutation and its idempotency record either both
        # commit (clean exit) or both roll back (exception unwinds the context).
        try:
            async with self._p.uow.begin() as conn:
                result = await execute_idempotently(
                    store=self._p.processed_commands,
                    conn=conn,
                    context=context,
                    tool_name=tool_name,
                    request=request,
                    execute=lambda: body(conn),
                    result_type=result_type,
                )
                log.info("command.executed", status=result.status.value)
                return result
        except DomainError as err:
            log.warning("command.failed", error_code=err.code, error=err.message)
            await self._record_command_failure(context, tool_name, request, err.code)
            return CommandResult[result_type].from_domain_error(err)

    async def _record_command_failure(
        self,
        context: CommandContext,
        tool_name: str,
        request: BaseModel,
        error_code: str,
    ) -> None:
        """Append one row to the command-failure ledger in a SECOND, short transaction.

        Best-effort by design (spec T1): a ledger write is pure observability and must
        never mask the original command failure, so any exception here is swallowed with
        a ``command.failure_unrecorded`` warning rather than propagated. Runs in its own
        unit of work because the command's transaction has already rolled back. The
        goal_id / task_id are pulled off the request when present (many commands carry
        only one), so task-scoped failures resolve to their goal at read time.
        """
        log = structlog.get_logger()
        try:
            goal_id = getattr(request, "goal_id", None)
            task_id = getattr(request, "task_id", None)
            async with self._p.uow.begin() as conn:
                await self._p.command_failures.record(
                    conn,
                    command_id=context.command_id,
                    tool_name=tool_name,
                    actor_id=context.actor.actor_id,
                    goal_id=goal_id if isinstance(goal_id, UUID) else None,
                    task_id=task_id if isinstance(task_id, UUID) else None,
                    error_code=error_code,
                )
        except Exception:
            log.warning("command.failure_unrecorded", tool=tool_name, error_code=error_code)
