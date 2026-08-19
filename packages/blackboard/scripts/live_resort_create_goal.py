"""LIVE RUN — create the Smart Seaside Resort goal + task graph on the blackboard.

Drives the real MCP command tools at :8010 (thin adapter, for real). Creates:
  - the goal (deterministic Blender resort scene → GPU render),
  - a design task (architect) that produces the scene-architecture artifact,
  - an implementation task (depends on design) carrying the blocking ReviewRequirements
    for the governing contexts, so the release gate derives exactly those conditions.

The lead then composes the roster per LEAD.md; this just seeds authoritative state.
Run AFTER `mise run mcp` is up. Prints the goal_id + task_ids as JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from fastmcp import Client

MCP_URL = os.environ.get("BLACKBOARD_MCP_URL", "http://127.0.0.1:8010/mcp/")

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
        goal = _val(
            await c.call_tool(
                "create_goal",
                {
                    "command": cmd(HUMAN),
                    "goal": {
                        "title": "Smart Seaside Resort — deterministic Blender 5.2 scene + GPU render",
                        "objective": (
                            "Compile the semantically-described smart seaside resort in "
                            "demo_app/ into a DETERMINISTIC Blender 5.2 scene, then headlessly "
                            "GPU-render an isometric architectural infographic. Build the resort in "
                            "3D (pale concrete, blue glass/water, sage landscaping, autonomous pods, "
                            "navy outlines, generous negative space). The autonomous-pod guideway "
                            "centerline is the source of truth from which deck, curbs, pod placement "
                            "and motion derive. Follow demo_app/references/BUILD_GUIDE.md and "
                            "demo_app/specs/resort.json. Blender is installed + GPU-verified at "
                            "/home/lalsaado/workplace/blender-5.2.0/blender (NVIDIA L40S, OptiX/CUDA)."
                        ),
                        "success_criteria": [
                            "scripts/build_scene.py + scripts/resort/*.py build scene/resort-generated.blend deterministically from specs/resort.json",
                            "Guideway centerline drives deck/curbs/pod placement/motion; centerline stays render-hidden and parametric",
                            "scripts/validate_scene.py enforces the 6 gates and exits 1 on any violation (min radius >= 7.5m, swept-envelope static clearance, safe headway d_safe >= 9.0m, structure, composition, render integrity)",
                            "scripts/render_scene.py produces a 3840x2160 isometric beauty render on the GPU, plus line + object-mask passes",
                            "The identical scene builds + renders in Blender Linux background mode via the guide's headless commands",
                        ],
                        "constraints": [
                            "Deterministic: same specs/resort.json always yields the same scene",
                            "Restrained white/blue/sage/navy palette; infographic style, not photoreal",
                            "Invoke Blender with --python-exit-code 1 so a validation exception fails the job",
                            "Pods must be spline-constrained kinematics + schedule, never rigid-body as primary controller",
                            "No reference image was supplied; reconstruct the visual grammar from the guide, do not trace pixels",
                        ],
                        "owner": HUMAN,
                    },
                },
            )
        )
        goal_id = goal["goal_id"]
        print(f"goal_id={goal_id}")

        design = _val(
            await c.call_tool(
                "create_task",
                {
                    "command": cmd(LEAD),
                    "task": {
                        "goal_id": goal_id,
                        "task_key": "design-resort-scene",
                        "title": "Design the deterministic resort scene architecture",
                        "objective": (
                            "Produce the architecture-decision artifact: the module decomposition "
                            "(architecture/assets/guideway/materials/pods/validation), the semantic "
                            "spec->scene compilation approach, the guideway-centerline-as-source-of-truth "
                            "design, how deck/curbs/pods/motion derive from it, the material palette "
                            "contract, and the six validation gates. Ground every decision in "
                            "demo_app/references/BUILD_GUIDE.md and specs/resort.json."
                        ),
                        "required_actor_kind": "architect",
                        "scope": ["demo_app"],
                        "deliverables": [
                            {
                                "artifact_type": "design",
                                "logical_name": "design/resort-scene-architecture",
                            }
                        ],
                        "acceptance_criteria": [
                            "Names the module decomposition and each module's responsibility",
                            "Specifies the guideway-centerline source-of-truth and derivation chain",
                            "Specifies the 6 validation gates and which invariant each enforces",
                            "Specifies the deterministic build + GPU render command surface",
                        ],
                    },
                },
            )
        )
        design_id = design["task_id"]
        print(f"design_task={design_id}")

        impl = _val(
            await c.call_tool(
                "create_task",
                {
                    "command": cmd(LEAD),
                    "task": {
                        "goal_id": goal_id,
                        "task_key": "implement-resort-scene",
                        "title": "Implement the deterministic resort scene builder, validator, and GPU renderer",
                        "objective": (
                            "Implement per the accepted design: scripts/build_scene.py + "
                            "scripts/resort/*.py (deterministic builder), scripts/validate_scene.py "
                            "(6 gates, exit 1 on violation), scripts/render_scene.py (3840x2160 GPU "
                            "beauty + line + mask passes). Run the full headless build->validate->render "
                            "path with the installed Blender and attach the render + validation report "
                            "as evidence."
                        ),
                        "required_actor_kind": "implementation",
                        "scope": ["demo_app"],
                        "constraints": [
                            "Deterministic build from specs/resort.json",
                            "GPU render on the L40S via OptiX; --python-exit-code 1",
                            "white/blue/sage/navy infographic palette",
                        ],
                        "deliverables": [
                            {"artifact_type": "source", "logical_name": "source/resort-scene-build"}
                        ],
                        "acceptance_criteria": [
                            "Deterministic build produces scene/resort-generated.blend",
                            "validate_scene.py passes all 6 gates and fails (exit 1) on injected violations",
                            "render_scene.py produces the 3840x2160 GPU beauty render + line + mask passes",
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
