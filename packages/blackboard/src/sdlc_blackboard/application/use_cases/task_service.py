"""Task use cases (handoff §11): create, refresh-ready, claim-with-fencing, bind,
start-run, submit-result. The reliability invariants live here.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from sdlc_blackboard.application.commands import (
    AcceptTaskRequest,
    BindRuntimeSessionRequest,
    ClaimTaskRequest,
    RefreshReadyTasksRequest,
    StartRunRequest,
    SubmitTaskResult,
)
from sdlc_blackboard.application.events import append_domain_event
from sdlc_blackboard.application.ports import Conn
from sdlc_blackboard.application.receipts import (
    ClaimReceipt,
    TaskListReceipt,
    TaskSubmissionReceipt,
)
from sdlc_blackboard.application.results import CommandResult
from sdlc_blackboard.application.use_cases.base import CommandService
from sdlc_blackboard.domain.artifacts import (
    ArtifactAlias,
    ArtifactRevision,
    ArtifactStatus,
)
from sdlc_blackboard.domain.common import REVIEWER_KINDS, CommandContext
from sdlc_blackboard.domain.errors import (
    Conflict,
    InputManifestMismatch,
    InvalidTransition,
    NotFound,
    PreconditionFailed,
    StaleAssignment,
    StaleVersion,
    Unauthorized,
    ValidationFailed,
)
from sdlc_blackboard.domain.events import RoutingClass, RunState, RuntimeRun
from sdlc_blackboard.domain.routing import default_routing_class
from sdlc_blackboard.domain.tasks import Task, TaskContractCreate, TaskState
from sdlc_blackboard.domain.transitions import can_transition


def _artifact_id_for(goal_id: UUID, logical_name: str) -> UUID:
    """Deterministic artifact_id per (goal, logical_name), so revisions of the same
    logical artifact share an artifact_id (uuid5 over a stable namespace)."""
    return uuid5(NAMESPACE_URL, f"blackboard://{goal_id}/{logical_name}")


class TaskService(CommandService):
    # ------------------------------------------------------------------ create
    async def create_task(
        self, context: CommandContext, request: TaskContractCreate
    ) -> CommandResult[Task]:
        async def body(conn: Conn) -> Task:
            goal = await self._p.goals.get(conn, request.goal_id)
            if goal is None:
                raise NotFound("goal", request.goal_id)
            existing = await self._p.tasks.get_by_key(conn, request.goal_id, request.task_key)
            if existing is not None:
                if existing.contract == request:
                    return existing
                raise Conflict(f"task_key {request.task_key!r} exists with a different contract")
            for dep_id in request.dependency_task_ids:
                dep = await self._p.tasks.get(conn, dep_id)
                if dep is None or dep.goal_id != request.goal_id:
                    raise PreconditionFailed(f"dependency {dep_id} not in goal {request.goal_id}")
            task = Task(
                goal_id=request.goal_id,
                task_key=request.task_key,
                title=request.title,
                objective=request.objective,
                required_actor_kind=request.required_actor_kind,
                state=TaskState.DRAFT if request.dependency_task_ids else TaskState.READY,
                version=0,
                assignment_epoch=0,
                contract=request,
            )
            await self._p.tasks.insert(conn, task)
            if request.dependency_task_ids:
                await self._p.tasks.add_dependencies(
                    conn, task.task_id, request.dependency_task_ids
                )
            await self._task_event(conn, context, "task.created", task)
            if task.state == TaskState.READY:
                await self._task_event(conn, context, "task.ready", task)
            return task

        return await self._command(context, "create_task", request, Task, body)

    # ------------------------------------------------------------- refresh ready
    async def refresh_ready_tasks(
        self, context: CommandContext, request: RefreshReadyTasksRequest
    ) -> CommandResult[TaskListReceipt]:
        async def body(conn: Conn) -> TaskListReceipt:
            newly_ready = await self._p.tasks.refresh_ready(conn, request.goal_id)
            for task in newly_ready:
                await self._task_event(conn, context, "task.ready", task)
            return TaskListReceipt(tasks=newly_ready)

        return await self._command(context, "refresh_ready_tasks", request, TaskListReceipt, body)

    # -------------------------------------------------------------------- claim
    async def claim_task(
        self, context: CommandContext, request: ClaimTaskRequest
    ) -> CommandResult[ClaimReceipt]:
        async def body(conn: Conn) -> ClaimReceipt:
            task = await self._p.tasks.get_for_update(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            if task.state != TaskState.READY:
                raise PreconditionFailed(f"Task is {task.state}, not ready.")
            next_epoch = task.assignment_epoch + 1
            # The partial unique index is the DB-level defense against double-claim.
            await self._p.assignments.open_assignment(
                conn, task.task_id, next_epoch, request.actor_id
            )
            claimed = await self._p.tasks.claim_cas(
                conn, task.task_id, task.version, request.actor_id, next_epoch
            )
            if claimed is None:
                raise StaleVersion()
            await self._task_event(conn, context, "task.assigned", claimed)
            return ClaimReceipt(task=claimed, assignment_epoch=next_epoch)

        return await self._command(context, "claim_task", request, ClaimReceipt, body)

    # --------------------------------------------------------------------- bind
    async def bind_runtime_session(
        self, context: CommandContext, request: BindRuntimeSessionRequest
    ) -> CommandResult[Task]:
        async def body(conn: Conn) -> Task:
            task = await self._p.tasks.get_for_update(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            self._require_epoch(task, context)
            bound = await self._p.tasks.bind_conversation(
                conn, task.task_id, task.assignment_epoch, request.omnigent_conversation_id
            )
            if bound is None:
                raise StaleAssignment()
            await self._task_event(conn, context, "runtime.bound", bound)
            return bound

        return await self._command(context, "bind_runtime_session", request, Task, body)

    # ---------------------------------------------------------------- start run
    async def start_runtime_run(
        self, context: CommandContext, request: StartRunRequest
    ) -> CommandResult[RuntimeRun]:
        async def body(conn: Conn) -> RuntimeRun:
            task = await self._p.tasks.get_for_update(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            self._require_epoch(task, context)
            self._require_actor(task, context)
            if task.state not in {TaskState.ASSIGNED, TaskState.AWAITING_INPUT}:
                raise PreconditionFailed(f"Task is {task.state}, cannot start a run.")
            # Routing class selection (spec R1/R2/R5B):
            #   R1 — an explicit request.routing_class wins, persisted unchanged.
            #   R5B — an invalid string is a client input error, not an infrastructure
            #         fault, so translate the enum's ValueError into a ValidationFailed.
            #   R2 — when None, derive the default from the task's required actor kind via
            #        the Lean-certified routing policy (domain/routing.py) and persist it.
            routing_class: RoutingClass
            if request.routing_class is not None:
                try:
                    routing_class = RoutingClass(request.routing_class)
                except ValueError as exc:
                    raise ValidationFailed(
                        f"unknown routing_class {request.routing_class!r}"
                    ) from exc
            else:
                routing_class = default_routing_class(task.required_actor_kind)
            run = RuntimeRun(
                task_id=task.task_id,
                assignment_epoch=task.assignment_epoch,
                actor_id=task.assigned_actor_id or context.actor.actor_id,
                omnigent_conversation_id=request.omnigent_conversation_id,
                state=RunState.RUNNING,
                input_manifest=request.input_manifest,
                provider=request.provider,
                model_id=request.model_id,
                aws_region=request.aws_region,
                routing_class=routing_class,
                harness=request.harness,
            )
            await self._p.runs.insert(conn, run)
            self._require_transition(task, TaskState.RUNNING)
            updated = await self._p.tasks.transition_cas(
                conn, task.task_id, task.version, task.state, TaskState.RUNNING
            )
            if updated is None:
                raise StaleVersion()
            await self._task_event(conn, context, "runtime.started", updated)
            return run

        return await self._command(context, "start_runtime_run", request, RuntimeRun, body)

    # ------------------------------------------------------------- submit result
    async def submit_task_result(
        self, context: CommandContext, request: SubmitTaskResult
    ) -> CommandResult[TaskSubmissionReceipt]:
        async def body(conn: Conn) -> TaskSubmissionReceipt:
            task = await self._p.tasks.get_for_update(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            self._require_epoch(task, context)
            self._require_actor(task, context)
            if task.state not in {TaskState.RUNNING, TaskState.AWAITING_INPUT}:
                raise PreconditionFailed(f"Task is {task.state}, cannot submit.")
            run = await self._p.runs.get_for_update(conn, request.run_id)
            if run is None:
                raise NotFound("runtime_run", request.run_id)
            if run.task_id != task.task_id or run.assignment_epoch != task.assignment_epoch:
                raise StaleAssignment()
            if run.input_manifest != request.input_manifest:
                raise InputManifestMismatch(task.task_id)

            revisions: list[ArtifactRevision] = []
            for submission in request.artifacts:
                artifact_id = _artifact_id_for(task.goal_id, submission.logical_name)
                existing = await self._p.artifacts.get_revision_by_hash(
                    conn, artifact_id, submission.content_hash
                )
                if existing is not None:
                    revisions.append(existing)
                    continue
                revision = ArtifactRevision(
                    artifact_id=artifact_id,
                    artifact_type=submission.artifact_type,
                    logical_name=submission.logical_name,
                    content_uri=submission.content_uri,
                    content_hash=submission.content_hash,
                    summary=submission.summary,
                    produced_by_task_id=task.task_id,
                    produced_by_run_id=request.run_id,
                    parent_revision_ids=submission.parent_revision_ids,
                    evidence=submission.evidence,
                    status=ArtifactStatus.CANDIDATE,
                )
                await self._p.artifacts.insert_revision(conn, task.goal_id, revision)
                await self._p.artifacts.upsert_alias_initial(
                    conn,
                    ArtifactAlias(
                        goal_id=task.goal_id,
                        logical_name=revision.logical_name,
                        current_revision_id=revision.revision_id,
                        version=0,
                    ),
                )
                revisions.append(revision)
                await append_domain_event(
                    self._p.events,
                    conn,
                    event_type="artifact.created",
                    aggregate_type="artifact",
                    aggregate_id=revision.revision_id,
                    aggregate_version=0,
                    goal_id=task.goal_id,
                    task_id=task.task_id,
                    context=context,
                    payload={"logical_name": revision.logical_name},
                )

            # Honor the SubmitTaskResult contract: the disposition, summary, and the
            # reviewer-facing fields the wire advertises are persisted into the
            # runtime_runs.result_manifest jsonb column (ADR-0012) rather than dropped.
            result_manifest: dict[str, object] = {
                "disposition": request.disposition,
                "summary": request.summary,
                "finding_ids": [str(fid) for fid in request.finding_ids],
                "assumptions": list(request.assumptions),
                "unresolved_questions": list(request.unresolved_questions),
                "residual_risks": list(request.residual_risks),
            }
            await self._p.runs.set_state(
                conn, request.run_id, RunState.SUBMITTED.value, result_manifest
            )
            await self._p.assignments.complete_assignment(conn, task.task_id, task.assignment_epoch)
            self._require_transition(task, TaskState.SUBMITTED)
            submitted = await self._p.tasks.transition_cas(
                conn, task.task_id, task.version, task.state, TaskState.SUBMITTED
            )
            if submitted is None:
                raise StaleVersion()
            await self._task_event(conn, context, "task.submitted", submitted)

            review_task_ids = await self._create_review_tasks(conn, context, submitted)
            return TaskSubmissionReceipt(
                task=submitted,
                artifact_revisions=tuple(revisions),
                review_task_ids=tuple(review_task_ids),
            )

        return await self._command(
            context, "submit_task_result", request, TaskSubmissionReceipt, body
        )

    # --------------------------------------------------------------- accept task
    async def accept_task(
        self, context: CommandContext, request: AcceptTaskRequest
    ) -> CommandResult[Task]:
        """Advance a SUBMITTED producer task to ACCEPTED.

        Drives the two legal transitions SUBMITTED -> UNDER_REVIEW -> ACCEPTED in one
        transaction so the lead never applies the state change at the store by hand
        (the finalize-wedge fix). Idempotent: an already-ACCEPTED task returns as-is.
        """

        async def body(conn: Conn) -> Task:
            task = await self._p.tasks.get_for_update(conn, request.task_id)
            if task is None:
                raise NotFound("task", request.task_id)
            # Idempotent: re-accepting an accepted task is a no-op success.
            if task.state == TaskState.ACCEPTED:
                return task
            if task.state == TaskState.UNDER_REVIEW:
                # Resume a partial accept (advanced to UNDER_REVIEW but not yet ACCEPTED).
                mid = task
            elif task.state == TaskState.SUBMITTED:
                self._require_transition(task, TaskState.UNDER_REVIEW)
                mid = await self._p.tasks.transition_cas(
                    conn, task.task_id, task.version, task.state, TaskState.UNDER_REVIEW
                )
                if mid is None:
                    raise StaleVersion()
                await self._task_event(conn, context, "task.under_review", mid)
            else:
                raise PreconditionFailed(
                    f"Task is {task.state}; accept_task requires SUBMITTED or UNDER_REVIEW."
                )

            self._require_transition(mid, TaskState.ACCEPTED)
            accepted = await self._p.tasks.transition_cas(
                conn, mid.task_id, mid.version, mid.state, TaskState.ACCEPTED
            )
            if accepted is None:
                raise StaleVersion()
            await self._task_event(conn, context, "task.accepted", accepted)
            return accepted

        return await self._command(context, "accept_task", request, Task, body)

    # --------------------------------------------------------------- review tasks
    async def _create_review_tasks(
        self, conn: Conn, context: CommandContext, producer: Task
    ) -> list[UUID]:
        """Create (or re-open) one review task per review requirement.

        On a remediation revision the review tasks already exist from the prior
        revision; instead of inserting a duplicate (unique goal_id+task_key), we
        return the existing task to READY so the reviewer re-reviews the new
        revision. This is the "reviews against the old revision go stale, reviewers
        review the new revision" cycle (handoff §18).
        """
        ids: list[UUID] = []
        for req in producer.contract.review_requirements:
            task_key = f"{producer.task_key}:review:{req.review_type}"
            existing = await self._p.tasks.get_by_key(conn, producer.goal_id, task_key)
            if existing is not None:
                # Remediation: re-open the prior review task for the new revision.
                target = existing
                if existing.state != TaskState.READY:
                    reopened = await self._p.tasks.transition_cas(
                        conn,
                        existing.task_id,
                        existing.version,
                        existing.state,
                        TaskState.READY,
                    )
                    if reopened is not None:
                        target = reopened
                await self._task_event(conn, context, "review_task.reopened", target)
                ids.append(existing.task_id)
                continue
            review_contract = TaskContractCreate(
                goal_id=producer.goal_id,
                task_key=task_key,
                title=f"{req.review_type} review of {producer.task_key}",
                objective=f"Review the {producer.task_key} deliverables ({req.review_type}).",
                required_actor_kind=req.reviewer_kind,
                scope=(f"review {producer.task_key}",),
                deliverables=(),
                acceptance_criteria=(f"{req.review_type} review submitted",),
                review_requirements=(),
                may_create_blocking_finding=(req.reviewer_kind in REVIEWER_KINDS and req.blocking),
            )
            review_task = Task(
                goal_id=producer.goal_id,
                task_key=task_key,
                title=review_contract.title,
                objective=review_contract.objective,
                required_actor_kind=req.reviewer_kind,
                state=TaskState.READY,
                version=0,
                assignment_epoch=0,
                contract=review_contract,
            )
            await self._p.tasks.insert(conn, review_task)
            await self._task_event(conn, context, "review_task.created", review_task)
            ids.append(review_task.task_id)
        return ids

    # ------------------------------------------------------------------- helpers
    def _require_epoch(self, task: Task, context: CommandContext) -> None:
        if (
            context.assignment_epoch is not None
            and context.assignment_epoch != task.assignment_epoch
        ):
            raise StaleAssignment()

    def _require_actor(self, task: Task, context: CommandContext) -> None:
        if task.assigned_actor_id is not None and task.assigned_actor_id != context.actor.actor_id:
            raise Unauthorized(f"actor {context.actor.actor_id} does not own task {task.task_id}")

    def _require_transition(self, task: Task, new_state: TaskState) -> None:
        if not can_transition(task.state, new_state):
            raise InvalidTransition(task.state.value, new_state.value)

    async def _task_event(
        self, conn: Conn, context: CommandContext, event_type: str, task: Task
    ) -> None:
        await append_domain_event(
            self._p.events,
            conn,
            event_type=event_type,
            aggregate_type="task",
            aggregate_id=task.task_id,
            aggregate_version=task.version,
            goal_id=task.goal_id,
            task_id=task.task_id,
            context=context,
            payload={"task_key": task.task_key, "state": task.state.value},
        )
