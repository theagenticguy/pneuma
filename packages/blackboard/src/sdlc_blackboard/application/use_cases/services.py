"""The Services facade — bundles every use-case service over one ServicePorts.

The composition root builds a ``Services`` and hands it to the interfaces layer
(MCP tools, CLI). Interfaces call ``services.goals.create_goal(...)`` etc.; they
never touch repositories or the pool directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_blackboard.application.use_cases.artifact_service import ArtifactService
from sdlc_blackboard.application.use_cases.gate_service import GateService
from sdlc_blackboard.application.use_cases.goal_service import GoalService
from sdlc_blackboard.application.use_cases.outbox_service import OutboxService
from sdlc_blackboard.application.use_cases.query_service import QueryService
from sdlc_blackboard.application.use_cases.review_service import ReviewService
from sdlc_blackboard.application.use_cases.task_service import TaskService
from sdlc_blackboard.application.use_cases.thrash_service import ThrashService
from sdlc_blackboard.application.use_cases.wiring import ServicePorts


@dataclass(frozen=True)
class Services:
    goals: GoalService
    tasks: TaskService
    artifacts: ArtifactService
    reviews: ReviewService
    gate: GateService
    query: QueryService
    outbox: OutboxService
    thrash: ThrashService

    @classmethod
    def build(cls, ports: ServicePorts) -> Services:
        return cls(
            goals=GoalService(ports),
            tasks=TaskService(ports),
            artifacts=ArtifactService(ports),
            reviews=ReviewService(ports),
            gate=GateService(ports),
            query=QueryService(ports),
            outbox=OutboxService(ports),
            thrash=ThrashService(ports),
        )
