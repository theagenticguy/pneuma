"""Release-gate evaluation (handoff §11).

The gate is a derived read, never a stored boolean. It is satisfied only when:
- the implementation artifact alias exists (the current binding under review);
- every REQUIRED review type has a non-stale APPROVED review bound to that exact
  current binding;
- no open blocking findings remain;
- a non-revoked human release approval exists for the exact current binding.

The required review types and the implementation artifact are DERIVED from the goal's
own task contracts, not hardcoded: the gate unions the blocking ``ReviewRequirement``s
declared across the goal's tasks. Adding a compliance / finops / release reviewer to the
roster and giving the implementation task a blocking review requirement for it therefore
makes that review a gate condition automatically — the kernel needs no code change per
bounded context (handoff §28: functions are task capabilities, not hardcoded personas).

``get_gate_status`` returns HUMAN_REQUIRED when everything else is satisfied but the
human approval is the only thing missing.
"""

from __future__ import annotations

from uuid import UUID

from sdlc_blackboard.application.ports import Conn
from sdlc_blackboard.application.use_cases.wiring import ServicePorts
from sdlc_blackboard.domain.approvals import ApprovalType
from sdlc_blackboard.domain.common import ArtifactBinding
from sdlc_blackboard.domain.events import GateResult, GateStatus
from sdlc_blackboard.domain.reviews import ReviewDisposition, single_binding_fingerprint
from sdlc_blackboard.domain.tasks import DeliverableSpec, Task

#: Fallback when a goal declares no blocking review requirements at all.
DEFAULT_REQUIRED_REVIEW_TYPES: tuple[str, ...] = ("quality", "security")


class GateService:
    def __init__(self, ports: ServicePorts) -> None:
        self._p = ports

    async def get_gate_status(
        self,
        goal_id: UUID,
        *,
        implementation_artifact: str | None = None,
    ) -> GateResult:
        async with self._p.uow.begin() as conn:
            return await self.evaluate_on_conn(
                conn, goal_id, implementation_artifact=implementation_artifact
            )

    async def evaluate_on_conn(
        self,
        conn: Conn,
        goal_id: UUID,
        *,
        implementation_artifact: str | None = None,
    ) -> GateResult:
        """Evaluate the gate on an already-open transaction connection.

        ``get_gate_status`` is the read-only public entrypoint (opens its own UoW).
        ``authorize_goal_completion`` calls this on the SAME transaction it flips the
        goal in, so the gate re-check and the state change share one unit of work and
        no TOCTOU window opens between them (handoff §11).
        """
        tasks = await self._p.tasks.list_for_goal(conn, goal_id)
        required_types = required_review_types(tasks)
        impl_name = implementation_artifact or implementation_logical_name(tasks)

        aliases = await self._p.artifacts.list_aliases(conn, goal_id)
        impl_binding = _find_binding(aliases, impl_name) if impl_name else None

        open_blockers = await self._p.findings.list_open_blocking(conn, goal_id)
        open_blocking_ids = tuple(f.finding_id for f in open_blockers)

        reviews = await self._p.reviews.list_for_goal(conn, goal_id)
        approvals = await self._p.approvals.list_for_goal(conn, goal_id)

        if impl_binding is None:
            return GateResult(
                status=GateStatus.UNSATISFIED,
                implementation_binding=None,
                missing_reviews=required_types,
                open_blocking_finding_ids=open_blocking_ids,
                stale_review_ids=(),
                missing_approvals=("human_release",),
            )

        current_fp = single_binding_fingerprint(impl_binding)
        stale_ids: list[UUID] = []
        satisfied_types: set[str] = set()
        for review in reviews:
            if review.review_type not in required_types:
                continue
            bound_current = any(
                single_binding_fingerprint(b) == current_fp for b in review.artifact_bindings
            )
            if not bound_current:
                continue
            if review.stale:
                stale_ids.append(review.review_id)
                continue
            if review.disposition == ReviewDisposition.APPROVED:
                satisfied_types.add(review.review_type)

        missing_reviews = tuple(t for t in required_types if t not in satisfied_types)

        human_ok = any(
            a.approval_type == ApprovalType.HUMAN_RELEASE
            and not a.revoked
            and any(single_binding_fingerprint(b) == current_fp for b in a.artifact_bindings)
            for a in approvals
        )
        missing_approvals = () if human_ok else ("human_release",)

        reviews_ok = not missing_reviews and not open_blocking_ids
        if reviews_ok and human_ok:
            status = GateStatus.SATISFIED
        elif reviews_ok and not human_ok:
            status = GateStatus.HUMAN_REQUIRED
        else:
            status = GateStatus.UNSATISFIED

        return GateResult(
            status=status,
            implementation_binding=impl_binding,
            missing_reviews=missing_reviews,
            open_blocking_finding_ids=open_blocking_ids,
            stale_review_ids=tuple(stale_ids),
            missing_approvals=missing_approvals,
        )


def required_review_types(tasks: tuple[Task, ...]) -> tuple[str, ...]:
    """Union of blocking review types declared across the goal's task contracts.

    Falls back to (quality, security) when no task declares a blocking review.
    Order-stable: first-seen order across tasks, then their requirement order.
    """
    seen: list[str] = []
    for task in tasks:
        for req in task.contract.review_requirements:
            if req.blocking and req.review_type not in seen:
                seen.append(req.review_type)
    return tuple(seen) if seen else DEFAULT_REQUIRED_REVIEW_TYPES


def implementation_logical_name(tasks: tuple[Task, ...]) -> str | None:
    """The logical name of the artifact the gate governs.

    Chosen as the deliverable of the task carrying the most blocking review
    requirements (the one the reviewers converge on); ties break on first-seen.
    """
    best: tuple[int, DeliverableSpec] | None = None
    for task in tasks:
        blocking = sum(1 for r in task.contract.review_requirements if r.blocking)
        if blocking == 0:
            continue
        for deliverable in task.contract.deliverables:
            if best is None or blocking > best[0]:
                best = (blocking, deliverable)
                break
    return best[1].logical_name if best is not None else None


def _find_binding(
    bindings: tuple[ArtifactBinding, ...], logical_name: str
) -> ArtifactBinding | None:
    for b in bindings:
        if b.logical_name == logical_name:
            return b
    return None
