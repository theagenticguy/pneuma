"""Review use cases (handoff §11): open finding, submit review, resolve finding,
record human approval. All authority names exact artifact revisions.
"""

from __future__ import annotations

from sdlc_blackboard.application.commands import ResolveFindingRequest
from sdlc_blackboard.application.events import append_domain_event
from sdlc_blackboard.application.ports import Conn
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.application.use_cases.base import CommandService
from sdlc_blackboard.domain.approvals import Approval, ApprovalSubmission
from sdlc_blackboard.domain.common import REVIEWER_KINDS, CommandContext
from sdlc_blackboard.domain.errors import (
    NotFound,
    PreconditionFailed,
    StaleVersion,
    Unauthorized,
)
from sdlc_blackboard.domain.findings import Finding, FindingCreate, FindingState
from sdlc_blackboard.domain.reviews import (
    Review,
    ReviewDisposition,
    ReviewSubmission,
    binding_fingerprint,
)


class ReviewService(CommandService):
    async def open_finding(
        self, context: CommandContext, request: FindingCreate
    ) -> CommandResult[Finding]:
        async def body(conn: Conn) -> Finding:
            # FOR SHARE on the goal row serializes this gate-input write against a
            # concurrent authorize_goal_completion (which holds FOR UPDATE) — ADR-0012.
            await self._p.goals.lock_shared(conn, request.goal_id)
            task = await self._p.tasks.get(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            # A blocking finding requires a reviewing bounded context whose task
            # contract explicitly permits it (handoff §11). Any REVIEWER_KIND may govern
            # (quality, security, compliance, release, platform, ops, finops, ...), so the
            # roster is open-ended — the authority is the contract flag, not a hardcoded pair.
            if request.blocking:
                authorized = (
                    task.required_actor_kind in REVIEWER_KINDS
                    and task.contract.may_create_blocking_finding
                )
                if not authorized:
                    raise Unauthorized(
                        f"task {task.task_key} is not authorized to open a blocking finding"
                    )
            finding = Finding(
                goal_id=request.goal_id,
                task_id=request.task_id,
                category=request.category,
                severity=request.severity,
                statement=request.statement,
                affected_artifacts=request.affected_artifacts,
                evidence=request.evidence,
                blocking=request.blocking,
                resolution_criteria=request.resolution_criteria,
                state=FindingState.OPEN,
                version=0,
            )
            await self._p.findings.insert(conn, finding)
            await append_domain_event(
                self._p.events,
                conn,
                event_type="finding.created",
                aggregate_type="finding",
                aggregate_id=finding.finding_id,
                aggregate_version=finding.version,
                goal_id=finding.goal_id,
                task_id=finding.task_id,
                context=context,
                payload={
                    "category": finding.category,
                    "severity": finding.severity.value,
                    "blocking": finding.blocking,
                },
            )
            return finding

        return await self._command(context, "open_finding", request, Finding, body)

    async def resolve_finding(
        self, context: CommandContext, request: ResolveFindingRequest
    ) -> CommandResult[Finding]:
        async def body(conn: Conn) -> Finding:
            current = await self._p.findings.get(conn, request.finding_id)
            if current is None:
                raise NotFound("finding", request.finding_id)
            updated = await self._p.findings.set_state_cas(
                conn, request.finding_id, current.version, request.new_state
            )
            if updated is None:
                raise StaleVersion()
            await append_domain_event(
                self._p.events,
                conn,
                event_type=f"finding.{request.new_state.value}",
                aggregate_type="finding",
                aggregate_id=updated.finding_id,
                aggregate_version=updated.version,
                goal_id=updated.goal_id,
                task_id=updated.task_id,
                context=context,
                payload={"state": updated.state.value},
            )
            return updated

        return await self._command(context, "resolve_finding", request, Finding, body)

    async def submit_review(
        self, context: CommandContext, request: ReviewSubmission
    ) -> CommandResult[Review]:
        async def body(conn: Conn) -> Review:
            # Gate-input write: FOR SHARE on the goal serializes against authorize (ADR-0012).
            await self._p.goals.lock_shared(conn, request.goal_id)
            review_task = await self._p.tasks.get(conn, request.review_task_id)
            if review_task is None:
                raise NotFound("task", request.review_task_id)
            if not request.artifact_bindings:
                raise PreconditionFailed("a review must bind at least one artifact revision")
            # Listed findings must exist.
            for finding_id in request.finding_ids:
                if await self._p.findings.get(conn, finding_id) is None:
                    raise NotFound("finding", finding_id)
            # An approved review may not carry an unresolved blocker it created.
            if request.disposition == ReviewDisposition.APPROVED:
                for finding_id in request.finding_ids:
                    finding = await self._p.findings.get(conn, finding_id)
                    if (
                        finding is not None
                        and finding.blocking
                        and finding.state
                        in {
                            FindingState.OPEN,
                            FindingState.ACKNOWLEDGED,
                        }
                    ):
                        raise PreconditionFailed(
                            "an approved review cannot carry an unresolved blocking finding"
                        )
            review = Review(
                goal_id=request.goal_id,
                review_task_id=request.review_task_id,
                reviewer=request.reviewer,
                review_type=request.review_type,
                binding_fingerprint=binding_fingerprint(request.artifact_bindings),
                artifact_bindings=request.artifact_bindings,
                disposition=request.disposition,
                summary=request.summary,
                evidence=request.evidence,
                finding_ids=request.finding_ids,
                stale=False,
            )
            await self._p.reviews.insert(conn, review)
            await append_domain_event(
                self._p.events,
                conn,
                event_type="review.submitted",
                aggregate_type="review",
                aggregate_id=review.review_id,
                aggregate_version=0,
                goal_id=review.goal_id,
                task_id=review.review_task_id,
                context=context,
                payload={
                    "review_type": review.review_type,
                    "disposition": review.disposition.value,
                },
            )
            return review

        return await self._command(context, "submit_review", request, Review, body)

    async def record_human_approval(
        self, context: CommandContext, request: ApprovalSubmission
    ) -> CommandResult[Approval]:
        async def body(conn: Conn) -> Approval:
            # Gate-input write: FOR SHARE on the goal serializes against authorize (ADR-0012).
            await self._p.goals.lock_shared(conn, request.goal_id)
            if not request.artifact_bindings:
                raise PreconditionFailed("an approval must bind at least one artifact revision")
            approval = Approval(
                goal_id=request.goal_id,
                approval_type=request.approval_type,
                approver=request.approver,
                binding_fingerprint=binding_fingerprint(request.artifact_bindings),
                artifact_bindings=request.artifact_bindings,
                conditions=request.conditions,
                revoked=False,
            )
            await self._p.approvals.insert(conn, approval)
            await append_domain_event(
                self._p.events,
                conn,
                event_type="approval.created",
                aggregate_type="approval",
                aggregate_id=approval.approval_id,
                aggregate_version=0,
                goal_id=approval.goal_id,
                task_id=None,
                context=context,
                payload={"approval_type": approval.approval_type.value},
            )
            return approval

        return await self._command(context, "record_human_approval", request, Approval, body)
