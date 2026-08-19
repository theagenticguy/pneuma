"""LIVE RUN — lead orchestration through the real MCP client.

Creates the WebGPU/WGSL shader-UX goal and a dependency-aware task graph on the
blackboard at http://127.0.0.1:8010/mcp. Every mutation goes through the actual MCP
command tools (not direct service calls), so this exercises the thin adapter for real.

The design task carries blocking ReviewRequirements for the governing contexts, so the
release gate derives exactly those conditions (data-driven, ADR-0008).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from fastmcp import Client

MCP_URL = "http://127.0.0.1:8010/mcp/"

HUMAN = {"actor_id": "laith", "kind": "human"}
LEAD = {"actor_id": "lead-1", "kind": "lead"}


def cmd(actor: dict) -> dict:
    return {"command_id": str(uuid.uuid4()), "actor": actor}


def _val(result) -> dict:
    sc = result.structured_content
    assert sc is not None, f"no structured_content: {result}"
    if sc.get("error"):
        raise SystemExit(f"command failed: {sc['error']}")
    return sc["value"]


async def main() -> None:
    async with Client(MCP_URL) as c:
        # 1. The goal.
        goal = _val(
            await c.call_tool(
                "create_goal",
                {
                    "command": cmd(HUMAN),
                    "goal": {
                        "title": "WebGPU/WGSL shader UX for the ATC-for-agents kit",
                        "objective": (
                            "Add a world-class WebGPU + WGSL shader layer to "
                            "frontier-field-note's @frontier/motion package that visualizes "
                            "thousands of agents-as-flights under air-traffic-control, fitting "
                            "the Graphite design system (calm, subtractive, teal+navy rationed, "
                            "never pink, light+dark from one token set). Radar/ATC concepts must "
                            "be extremely subtle — atmosphere-tint register, no literal sweep."
                        ),
                        "success_criteria": [
                            "A WGSL/WebGPU shader component lands in packages/motion with a WebGL/canvas fallback",
                            "Obeys Graphite: teal #0a6961 + navy #274d7a only, never pink, tokens for light+dark",
                            "Radar/ATC motifs are whisper-subtle (atmosphere opacity 0.06-0.20), no cliche sweep line",
                            "Honors reducedMotion and confines GPU-context loss to one tile",
                            "typecheck + lint + build pass",
                        ],
                        "constraints": [
                            "No new heavy runtime dep without justification",
                            "Must integrate the existing agent-visual taxonomy (@frontier/motion/state)",
                            "Reduced-motion-safe by default",
                        ],
                        "owner": HUMAN,
                    },
                },
            )
        )
        goal_id = goal["goal_id"]
        print(f"goal_id={goal_id}")

        # 2. Design task (architect) — produces the design artifact, no upstream dep.
        design = _val(
            await c.call_tool(
                "create_task",
                {
                    "command": cmd(LEAD),
                    "task": {
                        "goal_id": goal_id,
                        "task_key": "design-shader-ux",
                        "title": "Design the WebGPU/WGSL ATC shader UX",
                        "objective": (
                            "Produce the architecture-decision artifact: the visual concept "
                            "(agents-as-flights, subtle ATC), the WGSL technique (instanced "
                            "point-field / flow-field), the token contract (teal/navy, "
                            "light+dark), the fallback strategy, and the @frontier/motion "
                            "integration surface."
                        ),
                        "required_actor_kind": "architect",
                        "scope": ["packages/motion", "docs/design-principles.md"],
                        "deliverables": [
                            {"artifact_type": "design", "logical_name": "design/atc-shader-ux"}
                        ],
                        "acceptance_criteria": [
                            "Names the WGSL technique and why",
                            "Specifies the Graphite token contract for the shader",
                            "Specifies the WebGL/canvas fallback + reducedMotion behavior",
                        ],
                    },
                },
            )
        )
        design_id = design["task_id"]
        print(f"design_task={design_id}")

        # 3. Implementation task — depends on the design; carries the governing reviews.
        impl = _val(
            await c.call_tool(
                "create_task",
                {
                    "command": cmd(LEAD),
                    "task": {
                        "goal_id": goal_id,
                        "task_key": "implement-shader-ux",
                        "title": "Implement the WebGPU/WGSL ATC shader component",
                        "objective": (
                            "Implement the shader component in packages/motion per the accepted "
                            "design: WGSL shaders, a WebGPU renderer with a WebGL/canvas fallback, "
                            "Graphite tokens for light+dark, reducedMotion, and a CanvasErrorBoundary "
                            "so a lost GPU context confines to one tile. Wire it into a showcase."
                        ),
                        "required_actor_kind": "implementation",
                        "scope": ["packages/motion", "apps/field-notes"],
                        "constraints": [
                            "teal/navy only, never pink",
                            "subtle radar — atmosphere-tint register only",
                        ],
                        "deliverables": [
                            {"artifact_type": "source", "logical_name": "source/atc-shader-ux"}
                        ],
                        "acceptance_criteria": [
                            "WGSL + WebGPU renderer with fallback",
                            "typecheck + lint + build pass",
                            "Graphite-compliant, reducedMotion-safe",
                        ],
                        "dependency_task_ids": [design_id],
                        "review_requirements": [
                            {
                                "reviewer_kind": "quality",
                                "review_type": "quality",
                                "blocking": True,
                            },
                            {
                                "reviewer_kind": "security",
                                "review_type": "security",
                                "blocking": True,
                            },
                            {
                                "reviewer_kind": "platform",
                                "review_type": "platform",
                                "blocking": True,
                            },
                            {"reviewer_kind": "finops", "review_type": "finops", "blocking": True},
                            {"reviewer_kind": "ux", "review_type": "ux", "blocking": True},
                        ],
                        "may_modify_repository": True,
                    },
                },
            )
        )
        impl_id = impl["task_id"]
        print(f"impl_task={impl_id}")

        print(json.dumps({"goal_id": goal_id, "design_task": design_id, "impl_task": impl_id}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        print(e, file=sys.stderr)
        raise
