# The organizational plane — pneuma artifacts under a blackboard kernel, Claude Code as a recruit

Status: design sketch, nothing built. This document records the integration design between
three systems that already exist, so the build can be judged before it starts.

## The three planes, and who owns what

Three systems occupy three rungs of the same ladder, and each owns exactly one kind of
truth:

| Plane | System | Owns | Does not own |
|---|---|---|---|
| Content | pneuma `ArtifactStore` + `Artifacts` hook (`team/artifacts.py`) | revisions, branches, three-way merge, conflicts, `split_brain` | who was supposed to produce the content, whether it was reviewed |
| Organization | the blackboard kernel (`~/workplace/omnigent-blackboard-poc`) | goals, task contracts, the transition matrix, assignments with fencing, findings, reviews, the release gate, idempotency, optimistic concurrency | file content — its "artifact revision" is an opaque handle |
| Execution | pneuma `Team` + hooks, ai_functions threads, `ClaudeAgent` | who runs, with what tools, under which gates, at what cost | durable org state (a run is per-run by design) |

The design rule that keeps the seams honest: **each plane references the others by id and
never reaches into their semantics.** The blackboard's artifact-revision row carries a
pneuma revision id; pneuma's store never learns what a "task" is; the team layer reads
both through typed tools.

## Verified foundation: Claude Code is already a thread

`ai_functions.claude_code.ClaudeAgent` (present at the pinned rev `e47dc94`, verified in
the installed package) is a `Spawnable[[str], str]` template that drives a
`claude_agent_sdk.ClaudeSDKClient` subprocess:

- The SDK owns the conversation; ai_functions shadows every message into
  `Coordinator.append_event`, so a Claude Code session appears in the same event log as
  every pneuma thread — the observability story is unified for free.
- `post_conditions: tuple[PostCondition, ...]` with `max_attempts` gives it the
  `GatedProposer` shape natively: a failed validator becomes the next user turn.
- Tool-approval requests route through `ctx.on_interrupt`; injected messages drain at
  work boundaries; pause is supported.
- `input_shape` is `STR_PROMPT` — one prompt in, final answer out.
- `KiroAgent` is the same shape for Kiro.

Constraint: `claude-agent-sdk` is an **optional extra, not installed** in pneuma's venv
today. The adapter below must import lazily and skip cleanly, the same discipline as
pm4py.

## Phase 1 — `ClaudeRecruit`: Claude Code as a team member

A thin adapter satisfying the `Recruit` protocol (`name`, `spawn`, `ask`, `retire`),
wrapping a `ClaudeAgent` template the caller configures (`cwd`, allowed tools, MCP
servers, post_conditions). Placement: `team/members.py` grows nothing; this lives in a
new `team/recruits_claude.py` with a lazy import guard, exported only when the SDK is
present.

- `spawn` → `worker.spawn_locally(template)` against the team's coordinator, so the
  subprocess's shadow events land in the team's own event log.
- `ask` → `handle.run(request)`; the answer returns as text, exactly what a lead's
  member tool expects.
- `retire` → `terminate_now()`, idempotent per the unwind rule.
- The team's `Hiring` catalog can then offer `implementation_claude` as a role factory:
  Claude Code becomes hireable mid-run under the same headcount cap and audit log as any
  catalog hire.

What this deliberately does not do: expose the SDK's own subagents or hooks to the team.
One recruit, one subprocess, one typed join. Cost control is the template's
`post_conditions` plus the team's existing revise caps.

Test story: offline tests fake the SDK at the adapter seam (the `Recruit` protocol is the
contract, not the subprocess); one live test behind `PNEUMA_LIVE_CLAUDE=1` runs a real
`claude` subprocess on a trivial task, the same env-gate pattern as every other live
gate.

## Phase 2 — the worktree loop: execution meets content

The Cursor-shaped loop, expressed in pneuma's idioms:

1. The lead delegates an implementation request to a `ClaudeRecruit` whose `cwd` is a
   **git worktree branch owned by that member** (the runner provisions it; the template
   records it).
2. On submit, the member's diff lands in the `ArtifactStore` as a **proposal on the
   member's branch** (the `Artifacts` hook already binds authorship to the member name —
   the model cannot spoof it).
3. The lead integrates through the existing `commit_change` / `merge_change` /
   `Conflict` machinery: fast-forward when clean, typed conflict when a sibling landed
   first, never a silent overwrite.
4. A surfaced `Conflict` is work, not an error: the lead re-delegates with the rendered
   three-way diff, or hires a neutral merge specialist from the catalog (Cursor's
   third-party resolver as an ordinary reviewed role).

## Phase 3 — the blackboard binding: organization above both

The blackboard kernel stays exactly as the POC built it (Postgres, hexagonal, 18 MCP
tools, scripted-e2e-proven). Pneuma binds to it **through MCP tools on the lead, not
through imports** — the kernel's FastMCP server is already the schema-safe adapter, and
consuming it as tools keeps asyncpg and the kernel's dependencies out of pneuma entirely
(the library boundary test never even sees them).

- **A blackboard task contract ↔ one team run.** The lead's request is rendered from the
  task contract; `TeamRun` outcomes post back as task transitions (`submitted`,
  `under_review`, …) through the command tools. The kernel's transition matrix — not the
  model — decides legality, the same untrusted-oracle pattern as pneuma's process
  interpreter.
- **Artifact revision rows carry pneuma revision ids.** `publish_artifact` calls pass
  `content_ref = (store_path, revision_id, sha256)`. The kernel remains content-opaque;
  provenance becomes joinable across planes.
- **The release gate spans both planes.** Gate = the kernel's derived evaluation over
  accepted reviews AND content-plane cleanliness: no unresolved `Conflict` rows, and
  `split_brain` affirmatively clean. The symspec corpus (docs/formal/requirements/,
  finding XPL-1/XPL-2) proved the earlier wording — "not CONFIRMED" — contradicts the
  review-integrity rule in `hooks/review.py`: a two-valued test over a three-valued
  probe lets the could-not-tell verdict (no `decides` recorded anywhere, the likeliest
  first-run state) pass the gate on absence of evidence. Resolution: the gate-evidence
  step publishes the probe's three-valued verdict as a review artifact, and only the
  affirmative "no divergence observed over recorded decisions" settles that review as
  accepted; could-not-tell leaves it unsettled, which the kernel's existing
  positive-evidence gate semantics already refuse. The kernel still needs no new
  concept, and teams that never use `decides` must say so once (a single recorded
  decision, or a waiver review) rather than passing silently.
- **Trajectory rows and kernel events stay separate planes**, joined by
  `(goal_id, task_id, run_id)` carried in both. One is rollout evidence for learning;
  the other is organizational audit. Merging them would couple retention and schema
  lifecycles that want to differ.

## What is deliberately not built

- No kernel-in-pneuma. The transition matrix, fencing, and idempotency stay in the POC's
  Postgres kernel; pneuma does not grow a second organizational brain.
- No pneuma-in-kernel. The kernel does not learn what a branch or merge is.
- No SDK-subagent passthrough. A `ClaudeRecruit` is one opaque worker; its internal
  parallelism is its own business and invisible to the roster cap — this is the honest
  reading, and the cost ledger item (backlog) is where visibility would come from.
- No cross-team store. Same rule as the worklog and the artifact plane: one team, one
  plane; composition happens through `Squad`.

## Order of work

1. `ClaudeRecruit` adapter + offline fakes + one live gate (small, self-contained,
   immediately useful without any blackboard).
2. Worktree provisioning for producing recruits + the proposal-on-submit wiring
   (touches the runner script, not the library).
3. MCP tool binding from a lead to the running kernel, task-contract round-trip on the
   POC's scripted demo goal.
4. Gate-evidence step publishing `split_brain` + conflict-count as a blocking review.

Each step ships alone; none blocks the others' value.
