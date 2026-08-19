"""Processed-command dedup store (handoff §10).

``put`` translates the ``processed_commands`` primary-key violation to a retryable
``ConcurrentCommandConflict`` at the adapter edge. ``S608`` is suppressed
package-wide (see ``_common`` docstring).
"""

from typing import TYPE_CHECKING
from uuid import UUID

from asyncpg.exceptions import UniqueViolationError

from sdlc_blackboard.domain.errors import ConcurrentCommandConflict
from sdlc_blackboard.infrastructure.repositories._common import conn_of

if TYPE_CHECKING:
    from sdlc_blackboard.application.ports import Conn


class ProcessedCommandRepository:
    async def get(self, conn: Conn, command_id: UUID) -> tuple[str, str] | None:
        row = await conn_of(conn).fetchrow(
            "select request_hash, response from processed_commands where command_id = $1",
            command_id,
        )
        if row is None:
            return None
        return (row["request_hash"], row["response"])

    async def put(
        self,
        conn: Conn,
        command_id: UUID,
        actor_id: str,
        tool_name: str,
        request_hash: str,
        response: str,
    ) -> None:
        try:
            await conn_of(conn).execute(
                """
                insert into processed_commands(
                    command_id, actor_id, tool_name, request_hash, response
                )
                values ($1, $2, $3, $4, $5)
                """,
                command_id,
                actor_id,
                tool_name,
                request_hash,
                response,
            )
        except UniqueViolationError as exc:
            # Two concurrent calls reused the same command_id: both passed the dedup
            # SELECT, both executed, and this INSERT lost the race. Translate the
            # asyncpg exception to a retryable domain error at the adapter edge
            # (hexagonal §4). The losing transaction rolls back, so no partial state
            # persists; a retry replays the winner's stored response.
            raise ConcurrentCommandConflict from exc
