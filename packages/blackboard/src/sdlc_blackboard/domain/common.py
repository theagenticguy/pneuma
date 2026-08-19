"""Pure domain value objects shared across aggregates.

hexagonal-arch-stack.md §0.2: the domain is pure — stdlib + Pydantic only.
No ORM, no HTTP, no SDK, no I/O, no unseeded randomness. Everything in
``domain/`` must run in a unit test with zero setup.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=10_000)]


class DomainModel(BaseModel):
    """Base for every domain model: frozen (immutable) and extra-forbidding."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ActorKind(StrEnum):
    """An actor's authority class — a BOUNDED CONTEXT, not a persona (handoff §29).

    A kind names the slice of organizational authority a session holds (who may
    produce artifacts, who may open blocking findings, who may review). Behavior is
    driven by the task contract, not by the kind's label. The canonical enterprise
    SDLC roster maps one specialist to each producing/reviewing kind below.
    """

    # Human + orchestration + system.
    HUMAN = "human"
    LEAD = "lead"
    SYSTEM = "system"
    # Producing contexts (author artifacts).
    ANALYST = "analyst"
    ARCHITECT = "architect"
    IMPLEMENTATION = "implementation"
    DATA = "data"
    DOCUMENTATION = "documentation"
    UX = "ux"
    # Reviewing / governing contexts (validate revisions, may open findings).
    QUALITY = "quality"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    RELEASE = "release"
    PLATFORM = "platform"
    OPERATIONS = "operations"
    FINOPS = "finops"
    SUPPORT = "support"
    VISUAL = "visual"  # reference-fidelity review of a render (palette/coverage/silhouette)


#: Contexts that produce and submit artifacts (may own an implementation/analysis task).
PRODUCER_KINDS: frozenset[ActorKind] = frozenset(
    {
        ActorKind.ANALYST,
        ActorKind.ARCHITECT,
        ActorKind.IMPLEMENTATION,
        ActorKind.DATA,
        ActorKind.DOCUMENTATION,
        ActorKind.UX,
    }
)

#: Contexts that independently review a revision and may open findings when their
#: task contract permits (blocking findings still require may_create_blocking_finding).
REVIEWER_KINDS: frozenset[ActorKind] = frozenset(
    {
        ActorKind.QUALITY,
        ActorKind.SECURITY,
        ActorKind.COMPLIANCE,
        ActorKind.RELEASE,
        ActorKind.PLATFORM,
        ActorKind.OPERATIONS,
        ActorKind.FINOPS,
        ActorKind.SUPPORT,
        ActorKind.VISUAL,  # reference-fidelity review may block on aesthetic-contract violations
        ActorKind.ARCHITECT,  # architecture review is a governing gate on design artifacts
    }
)


class ActorRef(DomainModel):
    actor_id: NonEmptyStr
    kind: ActorKind


class CommandContext(DomainModel):
    """Envelope carried by every mutating command.

    ``command_id`` drives idempotency; a client reuses it only when retrying the
    exact same mutation. ``expected_version`` and ``assignment_epoch`` carry the
    optimistic-concurrency and fencing checks.
    """

    command_id: UUID = Field(default_factory=uuid4)
    actor: ActorRef
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    assignment_epoch: int | None = Field(default=None, ge=0)
    schema_version: str = "1.0"


class ArtifactBinding(DomainModel):
    """Names an exact, immutable artifact revision by id + hash.

    Reviews and approvals bind these so authority always names a concrete revision,
    never a mutable pointer.
    """

    artifact_id: UUID
    revision_id: UUID
    logical_name: NonEmptyStr
    content_hash: NonEmptyStr


class EvidenceRef(DomainModel):
    evidence_type: NonEmptyStr
    uri: NonEmptyStr
    digest: str | None = None
    summary: str | None = None
