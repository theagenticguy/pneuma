"""Shared repository primitives: connection narrowing, cross-aggregate row mappers,
and the compare-and-set update helper.

hexagonal-arch-stack.md: adapters translate between domain models and rows. The
mappers here are the ones shared by more than one repository module (actor,
evidence, bindings); each aggregate keeps its own mapper next to its repository.

``S608`` (the SQL-injection heuristic) is suppressed for this package in
``pyproject.toml``: table names are trusted module constants and every VALUE is
``$N``-bound. That is the whole rationale, held in one place for the package.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from sdlc_blackboard.domain.common import (
    ActorKind,
    ActorRef,
    ArtifactBinding,
    EvidenceRef,
)

if TYPE_CHECKING:
    import asyncpg

    from sdlc_blackboard.application.ports import Conn


def conn_of(conn: Conn) -> asyncpg.Connection:
    """Narrow the opaque port handle to a concrete asyncpg connection."""
    return cast("asyncpg.Connection", conn)


async def cas_update[T](
    conn: Conn,
    mapper: Callable[[asyncpg.Record], T],
    sql: str,
    *args: object,
) -> T | None:
    """Run a version-guarded compare-and-set UPDATE ... RETURNING * and map the row.

    The shared idiom behind the version-guard CAS sites (goal/finding set_state_cas,
    task claim_cas/transition_cas): a single ``fetchrow`` whose guard clause makes the
    UPDATE a no-op — hence ``None`` — when the caller's expected version/state no longer
    holds, and otherwise returns the freshly-bumped row mapped to its domain model. The
    caller owns the SQL (so each call site stays readable and every VALUE is ``$N``-bound)
    and the aggregate mapper; this helper owns only the fetch + None-on-miss contract.

    Deliberately NOT used by ``TaskRepository.refresh_ready`` (bulk, no version guard,
    returns many rows) or ``ArtifactRepository.promote_alias_cas`` (revision-token guard
    with a two-branch shape) — those stay verbatim.
    """
    row = await conn_of(conn).fetchrow(sql, *args)
    return mapper(row) if row is not None else None


def map_actor(raw: dict[str, object]) -> ActorRef:
    return ActorRef(actor_id=str(raw["actor_id"]), kind=ActorKind(str(raw["kind"])))


def map_evidence(items: list[dict[str, object]]) -> tuple[EvidenceRef, ...]:
    return tuple(EvidenceRef.model_validate(i) for i in items)


def map_bindings(items: list[dict[str, object]]) -> tuple[ArtifactBinding, ...]:
    return tuple(ArtifactBinding.model_validate(i) for i in items)
