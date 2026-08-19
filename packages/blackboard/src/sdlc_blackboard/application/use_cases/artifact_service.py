"""Artifact use cases (handoff §11): promote alias (CAS) + stale invalidation.

Promotion is exclusive (compare-and-set on the alias). When the current alias moves
to a new revision, every review and approval bound to the superseded revision of
that artifact is invalidated in the same transaction.
"""

from __future__ import annotations

from sdlc_blackboard.application.commands import PromoteArtifactRequest
from sdlc_blackboard.application.events import append_domain_event
from sdlc_blackboard.application.ports import Conn
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.application.use_cases.base import CommandService
from sdlc_blackboard.domain.artifacts import ArtifactAlias
from sdlc_blackboard.domain.common import CommandContext
from sdlc_blackboard.domain.errors import Conflict, NotFound, PreconditionFailed


class ArtifactService(CommandService):
    async def promote_artifact(
        self, context: CommandContext, request: PromoteArtifactRequest
    ) -> CommandResult[ArtifactAlias]:
        async def body(conn: Conn) -> ArtifactAlias:
            # Promotion stales reviews/approvals — a gate input. FOR SHARE on the goal
            # serializes it against a concurrent authorize (FOR UPDATE), ADR-0012.
            await self._p.goals.lock_shared(conn, request.goal_id)
            new_revision = await self._p.artifacts.get_revision(conn, request.new_revision_id)
            if new_revision is None:
                raise NotFound("artifact_revision", request.new_revision_id)

            # Promotion is a compare-and-set (handoff §11): when an alias already exists,
            # the caller MUST supply the expected current revision. Omitting it against an
            # existing alias would be a last-writer-wins bypass, so reject that here rather
            # than in the repository (which keeps a null-expected path only for a genuine
            # first set, where no alias row yet exists).
            existing = await self._p.artifacts.get_alias(
                conn, request.goal_id, request.logical_name
            )
            if existing is not None and request.expected_current_revision_id is None:
                raise PreconditionFailed(
                    "promote_artifact requires expected_current_revision_id when the alias "
                    "already exists (compare-and-set); read the current revision first"
                )

            promoted = await self._p.artifacts.promote_alias_cas(
                conn,
                request.goal_id,
                request.logical_name,
                request.expected_current_revision_id,
                request.new_revision_id,
            )
            if promoted is None:
                raise Conflict("alias promotion failed: expected revision did not match current")

            # Invalidate reviews/approvals bound to superseded revisions of this artifact.
            stale_reviews = await self._p.reviews.mark_stale_for_artifact(
                conn, new_revision.artifact_id, request.new_revision_id
            )
            revoked_approvals = await self._p.approvals.mark_revoked_for_artifact(
                conn, new_revision.artifact_id, request.new_revision_id
            )

            await append_domain_event(
                self._p.events,
                conn,
                event_type="artifact.promoted",
                aggregate_type="artifact_alias",
                aggregate_id=new_revision.artifact_id,
                aggregate_version=promoted.version,
                goal_id=request.goal_id,
                task_id=None,
                context=context,
                payload={
                    "logical_name": request.logical_name,
                    "new_revision_id": str(request.new_revision_id),
                },
            )
            for review_id in stale_reviews:
                await append_domain_event(
                    self._p.events,
                    conn,
                    event_type="review.invalidated",
                    aggregate_type="review",
                    aggregate_id=review_id,
                    aggregate_version=0,
                    goal_id=request.goal_id,
                    task_id=None,
                    context=context,
                    payload={"reason": "artifact promoted"},
                )
            for approval_id in revoked_approvals:
                await append_domain_event(
                    self._p.events,
                    conn,
                    event_type="approval.invalidated",
                    aggregate_type="approval",
                    aggregate_id=approval_id,
                    aggregate_version=0,
                    goal_id=request.goal_id,
                    task_id=None,
                    context=context,
                    payload={"reason": "artifact promoted"},
                )
            return promoted

        return await self._command(context, "promote_artifact", request, ArtifactAlias, body)
