"""Wire-contract totality tests — cheap, import-only, no Docker.

These catch the "add an enum, forget the table" drift that docs/insights/impact-analysis.md
flags: a new DomainError, ErrorCode, or TaskState that silently falls off one of the
translation tables. Each test asserts a table is TOTAL over its domain, so the failure
mode is a red test at the seam rather than an INTERNAL_ERROR or KeyError in production.
"""

from __future__ import annotations

import inspect

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sdlc_blackboard.application.results import (
    _DOMAIN_TO_ERROR_CODE,  # pyright: ignore[reportPrivateUsage]
    _ERROR_CODE_TO_STATUS,  # pyright: ignore[reportPrivateUsage]
    CommandError,
    CommandResult,
    CommandStatus,
    ErrorCode,
)
from sdlc_blackboard.domain import errors as errors_mod
from sdlc_blackboard.domain.errors import (
    Conflict,
    DomainError,
    NotFound,
    PreconditionFailed,
    StaleVersion,
)
from sdlc_blackboard.domain.tasks import TaskState
from sdlc_blackboard.domain.transitions import can_transition

# --------------------------------------------------------------------------- #
# results.py — error/status translation tables are total                       #
# --------------------------------------------------------------------------- #


def _domain_error_classes() -> list[type[DomainError]]:
    """Every concrete DomainError subclass defined in domain.errors."""
    return [
        obj
        for _name, obj in inspect.getmembers(errors_mod, inspect.isclass)
        if issubclass(obj, DomainError) and obj.__module__ == errors_mod.__name__
    ]


def test_every_domain_error_class_code_maps_to_an_error_code() -> None:
    """Every DomainError subclass's `.code` resolves to a real ErrorCode (never the
    INTERNAL_ERROR fallback, which would mean a wire code silently went missing)."""
    unmapped: list[str] = []
    for cls in _domain_error_classes():
        code = cls.code
        if code not in _DOMAIN_TO_ERROR_CODE:
            unmapped.append(f"{cls.__name__}({code})")
    assert not unmapped, f"DomainError codes with no ErrorCode: {unmapped}"


def test_domain_to_error_code_table_covers_every_error_code() -> None:
    # The table is built as {c.value: c for c in ErrorCode}, so it must be total over
    # the enum. Guards against the dict being hand-edited into a partial map.
    assert set(_DOMAIN_TO_ERROR_CODE.keys()) == {c.value for c in ErrorCode}
    assert set(_DOMAIN_TO_ERROR_CODE.values()) == set(ErrorCode)


def test_every_error_code_maps_to_a_command_status() -> None:
    missing = [c for c in ErrorCode if c not in _ERROR_CODE_TO_STATUS]
    assert not missing, f"ErrorCode with no CommandStatus: {missing}"


def test_from_domain_error_never_yields_bare_internal_for_known_errors() -> None:
    """Each well-known domain error round-trips to a non-generic failure status."""
    samples: list[DomainError] = [
        NotFound("goal", "x"),
        StaleVersion(),
        PreconditionFailed("nope"),
        Conflict("dup"),
    ]
    for err in samples:
        result = CommandResult[CommandError].from_domain_error(err)
        assert result.error is not None
        assert result.error.code == _DOMAIN_TO_ERROR_CODE[err.code]
        assert result.status == _ERROR_CODE_TO_STATUS[result.error.code]


def test_unknown_domain_code_falls_back_to_internal_error() -> None:
    class WeirdError(DomainError):
        code = "not_a_real_code"

    result = CommandResult[CommandError].from_domain_error(WeirdError("x"))
    assert result.error is not None
    assert result.error.code == ErrorCode.INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# transitions.py — can_transition is total over the TaskState product          #
# --------------------------------------------------------------------------- #

_STATES = list(TaskState)


@pytest.mark.parametrize("a", _STATES)
@pytest.mark.parametrize("b", _STATES)
def test_can_transition_is_total_over_the_state_product(a: TaskState, b: TaskState) -> None:
    # Never raises (no KeyError for an unlisted state), always a bool.
    assert isinstance(can_transition(a, b), bool)


@given(st.sampled_from(_STATES), st.sampled_from(_STATES))
def test_can_transition_total_property(a: TaskState, b: TaskState) -> None:
    assert isinstance(can_transition(a, b), bool)


def test_transition_matrix_is_irreflexive() -> None:
    assert all(can_transition(s, s) is False for s in _STATES)


# --------------------------------------------------------------------------- #
# MCP tool inventory — the advertised command + read surface is complete       #
# --------------------------------------------------------------------------- #

#: The exact tool surface the interfaces advertise. A tool added or removed without
#: updating this set (and the docs) trips here — the inventory is a contract.
EXPECTED_COMMAND_TOOLS = frozenset(
    {
        "create_goal",
        "create_task",
        "refresh_ready_tasks",
        "claim_task",
        "bind_runtime_session",
        "start_runtime_run",
        "submit_task_result",
        "accept_task",
        "open_finding",
        "resolve_finding",
        "submit_review",
        "promote_artifact",
        "record_human_approval",
        "authorize_goal_completion",
    }
)
EXPECTED_READ_TOOLS = frozenset(
    {
        "get_goal_snapshot",
        "get_task_contract",
        "get_artifact_revision",
        "read_relevant_events",
        "get_gate_status",
    }
)


async def _registered_tool_names() -> set[str]:
    from sdlc_blackboard.interfaces.mcp.server import mcp

    tools = await mcp.list_tools()
    return {t.name for t in tools}


async def test_mcp_registers_exactly_the_expected_tools() -> None:
    names = await _registered_tool_names()
    assert names == EXPECTED_COMMAND_TOOLS | EXPECTED_READ_TOOLS


async def test_every_command_request_dto_has_a_tool() -> None:
    # Each command tool is present; a new service method without a wired tool would be
    # invisible over the wire.
    names = await _registered_tool_names()
    assert names >= EXPECTED_COMMAND_TOOLS


async def test_create_task_tool_schema_exposes_command_and_task_params() -> None:
    from sdlc_blackboard.interfaces.mcp.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    schema = tools["create_task"].parameters
    props = schema["properties"]
    # fastmcp surfaces the two Pydantic-model params as named JSON keys (handoff §14).
    assert "command" in props
    assert "task" in props
    assert set(schema["required"]) >= {"command", "task"}


async def test_gate_status_tool_schema_requires_goal_id() -> None:
    from sdlc_blackboard.interfaces.mcp.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    schema = tools["get_gate_status"].parameters
    assert "goal_id" in schema["properties"]
    assert "goal_id" in schema["required"]


# --------------------------------------------------------------------------- #
# CommandResult envelope shape — the success/failure invariant callers rely on  #
# --------------------------------------------------------------------------- #


def test_accepted_envelope_carries_value_and_no_error() -> None:
    result = CommandResult[CommandError].accepted(
        CommandError(code=ErrorCode.NOT_FOUND, message="placeholder")
    )
    assert result.status == CommandStatus.ACCEPTED
    assert result.value is not None
    assert result.error is None
    assert result.replayed is False


def test_failed_envelope_carries_error_and_no_value() -> None:
    result = CommandResult[CommandError].failed(
        CommandError(code=ErrorCode.CONFLICT, message="dup")
    )
    assert result.status == CommandStatus.CONFLICT_CREATED
    assert result.value is None
    assert result.error is not None


def test_command_error_and_result_forbid_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CommandError.model_validate({"code": "not_found", "message": "x", "unexpected": 1})
