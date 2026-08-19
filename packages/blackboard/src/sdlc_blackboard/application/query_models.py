"""Read-model DTOs for the query side (handoff §13).

Agents retrieve compact state, not full history. These are output projections and
never leak persistence types.
"""

from __future__ import annotations

from uuid import UUID

from sdlc_blackboard.domain.approvals import Approval
from sdlc_blackboard.domain.common import ArtifactBinding, DomainModel
from sdlc_blackboard.domain.findings import Finding
from sdlc_blackboard.domain.goals import Goal
from sdlc_blackboard.domain.reviews import Review
from sdlc_blackboard.domain.tasks import Task


class GoalSnapshot(DomainModel):
    goal: Goal
    tasks: tuple[Task, ...]
    artifact_aliases: tuple[ArtifactBinding, ...]
    open_findings: tuple[Finding, ...]
    reviews: tuple[Review, ...]
    approvals: tuple[Approval, ...]
    ready_task_ids: tuple[UUID, ...]


class ThrashReport(DomainModel):
    """Per-goal coordination-thrash counters (spec G2/T1-T5).

    A derived read over one goal's recorded events and command results — mirrors
    ``formal/Blackboard/Thrash.lean`` ``ThrashReport``. Each counter is a
    monotonically nondecreasing function of the goal's history (T5) and is computed
    exclusively from that goal's signals (T1 frame property); an empty or unknown goal
    yields all zeros rather than an error (T2). Operators read this to detect swarm
    thrash before gate time; it is CLI-only, never on the MCP surface (agents gaming
    their own thrash metric is the failure mode).
    """

    goal_id: UUID
    #: Command failures recorded with error_code = 'conflict' for the goal (double-claim,
    #: duplicate review, CAS races).
    conflicts: int = 0
    #: Command failures recorded with error_code = 'stale_version' for the goal
    #: (optimistic-concurrency misses).
    stale_versions: int = 0
    #: Reviews for the goal whose disposition is anything other than APPROVED
    #: (findings / request_revision / abstained) — a reviewer declined to approve.
    review_rejections: int = 0
    #: Reclaim churn: count of 'review_task.reopened' events for the goal PLUS the sum of
    #: greatest(assignment_epoch - 1, 0) over the goal's tasks (every claim beyond the
    #: first is a re-claim). Both components are re-work signals the swarm generated.
    reclaims: int = 0
