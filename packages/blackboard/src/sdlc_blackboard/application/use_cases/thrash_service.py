"""Per-goal coordination-thrash report (spec 001-routing-thrash, G2/T1-T5).

A read-only derived report over one goal's recorded events and command results,
certified by ``formal/Blackboard/Thrash.lean``. Each counter is a fold over that
goal's history: zero on an empty (or unknown) goal (T2), monotonically nondecreasing
as history grows (T5), and computed exclusively from the goal's own signals (T1 frame
property). The Lean model pins these semantics; this service is the Python side whose
SQL COUNT queries must satisfy them.

READ-ONLY (T3): opens its own unit of work like ``GateService.get_gate_status``, issues
only plain SELECT / COUNT queries, and takes NO row locks (no FOR UPDATE / FOR SHARE).
An unknown goal_id is indistinguishable from a quiet one at this read layer, so it
returns all-zeros rather than raising NotFound — existence checking is the caller's
concern (the operator CLI passes a goal id it already trusts).
"""

from __future__ import annotations

from uuid import UUID

from sdlc_blackboard.application.query_models import ThrashReport
from sdlc_blackboard.application.results import ErrorCode
from sdlc_blackboard.application.use_cases.wiring import ServicePorts
from sdlc_blackboard.domain.reviews import ReviewDisposition

#: The single disposition that is NOT a rejection: everything else (FINDINGS,
#: REQUEST_REVISION, ABSTAINED) counts as a review rejection for thrash purposes.
_APPROVING_DISPOSITIONS: frozenset[ReviewDisposition] = frozenset({ReviewDisposition.APPROVED})

#: The event type emitted when a review task is returned to READY for a remediation
#: revision (task_service._create_review_tasks) — a reclaim signal.
_REVIEW_REOPENED_EVENT = "review_task.reopened"


class ThrashService:
    def __init__(self, ports: ServicePorts) -> None:
        self._p = ports

    async def get_thrash_report(self, goal_id: UUID) -> ThrashReport:
        async with self._p.uow.begin() as conn:
            failure_counts = await self._p.command_failures.count_by_error_code_for_goal(
                conn, goal_id
            )
            conflicts = failure_counts.get(ErrorCode.CONFLICT.value, 0)
            stale_versions = failure_counts.get(ErrorCode.STALE_VERSION.value, 0)

            reviews = await self._p.reviews.list_for_goal(conn, goal_id)
            review_rejections = sum(
                1 for r in reviews if r.disposition not in _APPROVING_DISPOSITIONS
            )

            reopened = await self._p.events.count_by_type(conn, goal_id, _REVIEW_REOPENED_EVENT)
            tasks = await self._p.tasks.list_for_goal(conn, goal_id)
            extra_claims = sum(max(t.assignment_epoch - 1, 0) for t in tasks)
            reclaims = reopened + extra_claims

            return ThrashReport(
                goal_id=goal_id,
                conflicts=conflicts,
                stale_versions=stale_versions,
                review_rejections=review_rejections,
                reclaims=reclaims,
            )
