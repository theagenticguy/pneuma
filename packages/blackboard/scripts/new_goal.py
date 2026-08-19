"""LIVE RUN — seed an arbitrary goal + implementation task on the blackboard.

The repo-agnostic goal seeder. Where `live_resort_create_goal.py` /
`live_lead_create_goal.py` hardcode one target and one objective, this drives the
same real MCP command tools at :8010 from command-line arguments, so the system can
be pointed at ANY target repo without editing a script.

It creates a goal and one implementation task whose blocking ReviewRequirements the
release gate then derives its required-review set from (gate_service.required_review_types).
The lead composes the actual roster per LEAD.md; this just seeds authoritative state.

Run AFTER `mise run mcp` is up. Prints goal_id + task_id as JSON on the last line.

Examples:
  # Minimal: objective only; scope defaults to the target repo's basename; reviews
  # default to the kernel gate's (quality, security).
  uv run python scripts/new_goal.py --objective "Add rate limiting to the API"

  # Point at another repo, custom scope, extra verifier types (see docs/SELF-COMPOSITION.md):
  uv run python scripts/new_goal.py \
    --target-repo ~/workplace/myapp \
    --objective "Harden auth" \
    --scope src/auth --scope docs/threat-model.md \
    --success "All auth endpoints require a valid session" \
    --review quality:quality --review security:security \
    --review security:security_adversarial --review platform:platform:nonblocking
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from fastmcp import Client

from sdlc_blackboard.domain.common import ActorKind

MCP_URL = os.environ.get("BLACKBOARD_MCP_URL", "http://127.0.0.1:8010/mcp/")

HUMAN = {"actor_id": os.environ.get("BLACKBOARD_HUMAN_ACTOR", "operator"), "kind": "human"}
LEAD = {"actor_id": "lead-1", "kind": "lead"}

_VALID_KINDS = {k.value for k in ActorKind}


def cmd(actor: dict) -> dict:
    return {"command_id": str(uuid.uuid4()), "actor": actor}


def _val(result) -> dict:
    sc = result.structured_content
    assert sc is not None, f"no structured_content: {result}"
    if sc.get("error"):
        raise SystemExit(f"command failed: {sc['error']}")
    return sc["value"]


def _parse_review(spec: str) -> dict:
    """Parse a ``kind:type[:blocking|:nonblocking]`` review spec into a ReviewRequirement.

    ``reviewer_kind`` must be a valid ActorKind; ``review_type`` is free text (it becomes a
    gate condition automatically when blocking). Blocking defaults to True.
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise SystemExit(
            f"--review {spec!r} must be 'kind:type' or 'kind:type:blocking|nonblocking'"
        )
    kind, review_type = parts[0], parts[1]
    if kind not in _VALID_KINDS:
        raise SystemExit(
            f"--review {spec!r}: reviewer_kind {kind!r} is not a valid ActorKind. "
            f"Valid kinds: {', '.join(sorted(_VALID_KINDS))}"
        )
    blocking = True
    if len(parts) == 3:
        flag = parts[2].lower()
        if flag not in ("blocking", "nonblocking"):
            raise SystemExit(f"--review {spec!r}: third field must be 'blocking' or 'nonblocking'")
        blocking = flag == "blocking"
    return {"reviewer_kind": kind, "review_type": review_type, "blocking": blocking}


def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed a goal + implementation task on the blackboard (repo-agnostic).",
    )
    p.add_argument(
        "--target-repo",
        default=os.environ.get("BLACKBOARD_TARGET_REPO"),
        help="Path to the repo the team builds against (default: $BLACKBOARD_TARGET_REPO). "
        "Used to derive the default --scope and recorded in the objective.",
    )
    p.add_argument("--objective", required=True, help="What the team should accomplish.")
    p.add_argument("--title", default=None, help="Goal title (default: first line of objective).")
    p.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Repo path(s) in scope (repeatable). Default: the target repo's basename.",
    )
    p.add_argument(
        "--success",
        action="append",
        default=[],
        dest="success_criteria",
        help="A goal success criterion (repeatable). Default: one derived from the objective.",
    )
    p.add_argument(
        "--constraint", action="append", default=[], help="A goal/task constraint (repeatable)."
    )
    p.add_argument(
        "--review",
        action="append",
        default=[],
        dest="reviews",
        help="Blocking (or :nonblocking) review requirement 'kind:type[:blocking|nonblocking]' "
        "(repeatable). Default: quality:quality and security:security.",
    )
    p.add_argument(
        "--actor-kind",
        default="implementation",
        help="required_actor_kind for the implementation task (default: implementation).",
    )
    p.add_argument(
        "--no-modify-repo",
        action="store_true",
        help="Set may_modify_repository=False (default is True for an implementation task).",
    )
    return p.parse_args()


async def _run(ns: argparse.Namespace) -> None:
    if ns.actor_kind not in _VALID_KINDS:
        raise SystemExit(
            f"--actor-kind {ns.actor_kind!r} is not a valid ActorKind. "
            f"Valid kinds: {', '.join(sorted(_VALID_KINDS))}"
        )

    scope = tuple(ns.scope) or ((Path(ns.target_repo).name,) if ns.target_repo else ("repo",))
    title = ns.title or ns.objective.splitlines()[0][:120]
    success = tuple(ns.success_criteria) or (f"The objective is met: {ns.objective}",)
    reviews = [_parse_review(s) for s in ns.reviews] or [
        {"reviewer_kind": "quality", "review_type": "quality", "blocking": True},
        {"reviewer_kind": "security", "review_type": "security", "blocking": True},
    ]
    target_note = f" (target repo: {ns.target_repo})" if ns.target_repo else ""

    async with Client(MCP_URL) as c:
        goal = _val(
            await c.call_tool(
                "create_goal",
                {
                    "command": cmd(HUMAN),
                    "goal": {
                        "title": title,
                        "objective": ns.objective + target_note,
                        "success_criteria": list(success),
                        "constraints": list(ns.constraint),
                        "owner": HUMAN,
                    },
                },
            )
        )
        goal_id = goal["goal_id"]
        print(f"goal_id={goal_id}")

        task = _val(
            await c.call_tool(
                "create_task",
                {
                    "command": cmd(LEAD),
                    "task": {
                        "goal_id": goal_id,
                        "task_key": "implement",
                        "title": title,
                        "objective": ns.objective,
                        "required_actor_kind": ns.actor_kind,
                        "scope": list(scope),
                        "constraints": list(ns.constraint),
                        "deliverables": [
                            {"artifact_type": "source", "logical_name": "source/implementation"}
                        ],
                        "acceptance_criteria": list(success),
                        "review_requirements": reviews,
                        "may_modify_repository": not ns.no_modify_repo,
                    },
                },
            )
        )
        task_id = task["task_id"]
        print(f"impl_task={task_id}")
        print(json.dumps({"goal_id": goal_id, "impl_task": task_id, "reviews": reviews}))


def main() -> None:
    try:
        asyncio.run(_run(_build_args()))
    except SystemExit as e:
        if e.code not in (0, None):
            print(e, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
