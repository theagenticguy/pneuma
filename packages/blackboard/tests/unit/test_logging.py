"""structlog wiring unit tests (T-B).

Covers (1) configure_logging is callable + idempotent, (2) the _command envelope
emits with command_id bound via contextvars, and (3) the idempotency dedup-replay
branch logs. The command-service tests drive CommandService._command with in-memory
fakes for the uow + processed-command store — no Postgres needed.
"""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace, TracebackType
from typing import cast

import structlog
from pydantic import BaseModel
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from sdlc_blackboard.application.idempotency import canonical_hash
from sdlc_blackboard.application.ports import Conn, ProcessedCommandStore
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.application.use_cases.base import CommandService
from sdlc_blackboard.application.use_cases.wiring import ServicePorts
from sdlc_blackboard.domain.common import ActorKind, ActorRef, CommandContext
from sdlc_blackboard.infrastructure.logging import configure_logging


class _Payload(BaseModel):
    value: int


class _ExposedService(CommandService):
    """Public wrapper over the protected _command so tests can drive it."""

    async def run(
        self,
        context: CommandContext,
        tool_name: str,
        request: _Payload,
        body: Callable[[Conn], Awaitable[_Payload]],
    ) -> CommandResult[_Payload]:
        return await self._command(context, tool_name, request, _Payload, body)


class _FakeTxn:
    async def __aenter__(self) -> Conn:
        return object()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return None


class _FakeUoW:
    def begin(self) -> _FakeTxn:
        return _FakeTxn()


class _FakeStore:
    """In-memory ProcessedCommandStore fake."""

    def __init__(self, prior: tuple[str, str] | None = None) -> None:
        self._prior = prior
        self.puts: list[tuple[str, str]] = []

    async def get(self, conn: Conn, command_id: object) -> tuple[str, str] | None:
        return self._prior

    async def put(
        self,
        conn: Conn,
        command_id: object,
        actor_id: str,
        tool_name: str,
        request_hash: str,
        response: str,
    ) -> None:
        self.puts.append((request_hash, response))


def _ports(store: ProcessedCommandStore) -> ServicePorts:
    return cast(ServicePorts, SimpleNamespace(uow=_FakeUoW(), processed_commands=store))


def _context() -> CommandContext:
    return CommandContext(actor=ActorRef(actor_id="tester", kind=ActorKind.HUMAN))


def test_configure_logging_is_idempotent() -> None:
    configure_logging("DEBUG", "console")
    configure_logging("INFO", "json")  # second call must not raise
    with capture_logs() as cap:
        structlog.get_logger().info("hello", k="v")
    assert cap == [{"event": "hello", "k": "v", "log_level": "info"}]


def test_command_emits_envelope_with_bound_command_id() -> None:
    store = _FakeStore()
    service = _ExposedService(_ports(store))
    context = _context()

    async def _body(conn: Conn) -> _Payload:
        return _Payload(value=7)

    with capture_logs(processors=[merge_contextvars]) as cap:
        result = asyncio.run(service.run(context, "create_goal", _Payload(value=7), _body))

    assert result.value == _Payload(value=7)
    envelopes = [e for e in cap if e["event"] == "command.executed"]
    assert len(envelopes) == 1
    assert envelopes[0]["command_id"] == str(context.command_id)
    assert envelopes[0]["actor"] == "tester"
    assert envelopes[0]["tool"] == "create_goal"
    assert envelopes[0]["status"] == "accepted"


def test_dedup_replay_path_logs() -> None:
    request = _Payload(value=3)
    stored = CommandResult[_Payload].accepted(_Payload(value=3)).model_dump_json()
    store = _FakeStore(prior=(canonical_hash(request), stored))
    service = _ExposedService(_ports(store))

    async def _body(conn: Conn) -> _Payload:  # must NOT run on a replay
        raise AssertionError("body ran on duplicate replay")

    with capture_logs() as cap:
        result = asyncio.run(service.run(_context(), "create_goal", request, _body))

    assert result.replayed is True
    assert any(e["event"] == "command.replayed" for e in cap)
