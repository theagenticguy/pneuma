"""Idempotent command execution (handoff §10).

Every mutating command is idempotent by ``command_id`` + a canonical hash of its
request. The processed-command record is written in the SAME transaction as the
domain mutation (the caller passes the transaction ``conn``), so a duplicate call
replays the original response and a reused command_id with a different payload is
rejected.

This module holds no persistence details: it reads and writes the dedup record
through the ``ProcessedCommandStore`` port, keeping the application layer free of
asyncpg (a hexagonal non-negotiable).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import orjson
import structlog
from pydantic import BaseModel

from sdlc_blackboard.application.results import (
    CommandError,
    CommandResult,
    CommandStatus,
    ErrorCode,
)

if TYPE_CHECKING:
    from sdlc_blackboard.application.ports import Conn, ProcessedCommandStore
    from sdlc_blackboard.domain.common import CommandContext


def canonical_hash(value: BaseModel) -> str:
    """Order-independent SHA-256 of a request model's JSON projection (handoff §10)."""
    raw = orjson.dumps(value.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()


async def execute_idempotently[T: BaseModel](
    *,
    store: ProcessedCommandStore,
    conn: Conn,
    context: CommandContext,
    tool_name: str,
    request: BaseModel,
    execute: Callable[[], Awaitable[T]],
    result_type: type[T],
) -> CommandResult[T]:
    """Run ``execute`` at most once per ``command_id``, replaying on duplicates.

    - First call: runs ``execute()``, stores ``(command_id, request_hash, response)``
      through the store within the current transaction, returns accepted.
    - Duplicate call, same payload: returns the stored response with ``replayed=True``.
    - Duplicate command_id, different payload: returns a DUPLICATE_COMMAND_MISMATCH
      validation failure without running ``execute``.
    """
    log = structlog.get_logger()
    request_hash = canonical_hash(request)

    prior = await store.get(conn, context.command_id)
    if prior is not None:
        prior_hash, prior_response = prior
        if prior_hash != request_hash:
            log.warning("command.mismatch", tool=tool_name)
            return CommandResult[result_type].failed(
                CommandError(
                    code=ErrorCode.DUPLICATE_COMMAND_MISMATCH,
                    message="Command ID was reused with different arguments.",
                )
            )
        log.info("command.replayed", tool=tool_name)
        stored = orjson.loads(prior_response)
        replayed = CommandResult[result_type].model_validate(stored)
        return replayed.model_copy(
            update={"replayed": True, "status": CommandStatus.DUPLICATE_REPLAYED}
        )

    value = await execute()
    result = CommandResult[result_type].accepted(value)

    await store.put(
        conn,
        context.command_id,
        context.actor.actor_id,
        tool_name,
        request_hash,
        result.model_dump_json(),
    )
    return result
