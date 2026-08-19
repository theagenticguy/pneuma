# Runbook — running the SDLC team on any repo

This is the operational playbook for standing up the Agentic SDLC Team Runtime and
pointing it at **your** repository. The system has three parts:

- a **blackboard kernel** (transactional Postgres, the authoritative organizational state),
- a **thin FastMCP adapter** (5 read tools + 14 command tools on `:8010`), and
- an **Omnigent team** of specialist agents under `sdlc_team/` that execute the work.

The kernel owns truth; MCP exposes it; the team acts against it. Nothing here is specific
to the bundled `demo_app` target — that is just the worked example. The **target repository
is a parameter** (`BLACKBOARD_TARGET_REPO`); this doc shows how to set it and drive a goal
end to end.

> Two axes worth holding in mind (see `docs/SELF-COMPOSITION.md`): the **authority axis**
> (`ActorKind`, a closed, formally-pinned enum of 18 bounded contexts) and the **composition
> axis** (which agents/personas the lead engages per goal, driven by the task contract, not
> the kind label). Everyday operation lives entirely on the composition axis.

> **Task names since this became a workspace member.** Every `mise` task now lives in the
> **workspace root** `mise.toml`, two levels up, and runs from there (`mise` finds it from any
> subdirectory). The tasks below read the same except where a name would have collided with
> pneuma's, which are renamed rather than overloaded:
>
> | This runbook says | Now run |
> |---|---|
> | `mise run test` | `mise run test:blackboard` (`mise run test` runs **both** members) |
> | `mise run test:unit` | `mise run test:blackboard:unit` |
> | `mise run test:integration` | `mise run test:blackboard:integration` |
> | `mise run lint` / `typecheck` | `mise run lint:blackboard` / `mise run typecheck:blackboard` |
> | `mise run demo` | `mise run demo:blackboard` (`mise run demo` is pneuma's war-room) |
> | `mise run formal` | `mise run formal:blackboard` |
> | `mise run check` | unchanged — but now gates **both** members |
> | `install`, `migrate`, `db:up`, `db:down`, `mcp*`, `team*`, `link-target` | unchanged |
>
> `mise run install` is now plain `uv sync` (no `--extra dev`): the dev tooling moved from an
> extra to a `[dependency-groups]` entry, which uv syncs for every workspace member by
> default. Paths written relative to the package root — `.env`, `.target`, `sdlc_team/`,
> `scripts/`, `migrations/` — are unchanged, and the mise tasks that touch them set their
> working directory to `packages/blackboard` so those relative paths still resolve.

## 1. Prerequisites

- Python 3.14, `uv`, `mise`, `dbmate`, Docker — all pinned in `mise.toml`.
- For a **live** team run: AWS Bedrock access (`aws sso login`), and optionally the
  `bubblewrap` binary for the `linux_bwrap` inner sandbox.
- `omnigent` is **not** a project dependency — `mise run team` pulls it from PyPI via `uvx`
  (pinned by `OMNIGENT_VERSION`, ADR-0010). It never imports the local fork.

## 2. Bootstrap (once per checkout)

```bash
cp .env.example .env
# Point the team at your repo (defaults to the bundled demo_app):
#   edit BLACKBOARD_TARGET_REPO in .env, or export it
mise run install            # uv sync --extra dev (kernel + tooling; no omnigent)
mise run link-target        # ln -sfn "$BLACKBOARD_TARGET_REPO" .target   ← the repo-agnostic seam
mise run db:up              # docker compose up -d postgres (ADR-0002 if compose is absent)
mise run migrate            # apply SQL migrations via dbmate
```

`mise run link-target` materializes a repo-root `.target` symlink pointing at
`$BLACKBOARD_TARGET_REPO`. The producing/writing specialists (`implementation_*`,
`data_engineer`, `quality`, `security`) `cwd` into `./.target`, so they operate on your repo
without any change to the committed agent YAMLs. Re-run `link-target` whenever you change the
target. (Why a symlink and not `cwd: ${BLACKBOARD_TARGET_REPO}`? Omnigent parses `os_env.cwd`
**verbatim** — no env/Jinja expansion — so the target cannot be an env var inside the agent
YAML. See ADR-0015.)

## 3. The gate (definition of done)

```bash
mise run check              # lint (ruff check + format) + typecheck (pyright strict) + test (pytest -n auto)
```

**Known gap:** ADR-0014 states `mise run formal` (`cd formal && lake build`, the Lean routing
proof) is part of the gate, but it is **not** in `check.depends` (which is
`lint, typecheck, test`). Run it separately — and you **must** run it for any change to
`ActorKind` / the routing policy, or the Python↔Lean lockstep can silently drift:

```bash
mise run formal            # required after any ActorKind / routing-policy change
```

Fast inner loop (no Postgres): `mise run test:unit` (unit + contract).

## 4. Scripted demo (no LLMs, no Bedrock)

Prove the whole gate + remediation lifecycle deterministically before spending live tokens:

```bash
mise run migrate
mise run demo              # goal → impl → QA → security blocker → remediation → gate → approval → satisfied
uv run blackboard events <goal-id>
uv run blackboard gate   <goal-id>
```

## 5. Live team run — startup order

All of these must be up **before** `mise run team`, in this order:

1. **Postgres** — `mise run db:up` (postgres:18-alpine on `127.0.0.1:5432`).
2. **Migrations** — `mise run migrate`.
3. **Target symlink** — `mise run link-target` (if not already done at bootstrap, or if the
   target changed).
4. **MCP server** — `mise run mcp:start` (binds `:8010`, **detached** — logs to `.mcp.log`,
   returns your prompt). Do **not** use `mise run mcp &`: mise stays attached and its banner
   plus the server's logs keep printing into your interactive shell. Use the foreground
   `mise run mcp` only when you want to watch it live. Stop with `mise run mcp:stop`,
   tail with `mise run mcp:logs`. Health check (give it ~2s to bind):
   ```bash
   curl -s http://127.0.0.1:8010/health      # expect {"status":"ok"}
   ```
5. **Bedrock creds** — `set -a; source .env; set +a` then `aws sso login --profile "$AWS_PROFILE"`.
   `$AWS_PROFILE` must be a profile that exists in `~/.aws/config` with Bedrock access (the
   `.env` default is `bedrock-a`). A non-existent profile surfaces later as a confusing
   DynamoDB `InternalFailure` from `omnigent run`, not a clean credential error.
6. **(Conditional) research gateway** — if your agents declare the `gateway` MCP block
   (context7 / awsknowledge / brave / exa / tavily), start Bonk's mcp-gateway on `:9400`
   (`mise run gw-start` in the mcp-gateway repo) first. The lead itself does not need it.
7. **(Conditional) bubblewrap** — for `linux_bwrap` sandboxes; else set the governing agents'
   `sandbox.type: none` (they still can't mutate the repo — reads + `open_finding`/`submit_review`).

Pre-flight (no launch) — proves every config parses and resolves to a **local** sandbox:

```bash
mise run team:validate     # expect "N/N parsed and resolved to a local sandbox"
```

Then launch:

```bash
mise run team              # uvx --from "omnigent==$OMNIGENT_VERSION" omnigent run sdlc_team/
```

## 6. Seeding a goal

The generic, repo-agnostic seeder (talks to `:8010`, so run `mise run mcp` first):

```bash
uv run python scripts/new_goal.py \
  --objective "Add request rate limiting to the public API" \
  --scope src/api --scope docs/limits.md \
  --success "All public endpoints enforce a per-key limit" \
  --review quality:quality --review security:security
```

- `--target-repo` defaults to `$BLACKBOARD_TARGET_REPO`; `--scope` defaults to the target's
  basename.
- Each `--review kind:type[:blocking|nonblocking]` becomes a `ReviewRequirement` on the
  implementation task. **The release gate derives its required-review set from these blocking
  requirements** (`gate_service.required_review_types`) — so adding
  `--review security:security_adversarial` makes `security_adversarial` a gate condition with
  zero kernel change (see `docs/SELF-COMPOSITION.md`, Feature 1).
- Prints `goal_id` + `impl_task` as JSON on the last line.

Raw / ad-hoc alternative — the generic per-tool MCP CLI:

```bash
uv run python scripts/bb.py create_goal '{"command":{...},"goal":{...}}'
uv run python scripts/bb.py create_task '{"command":{...},"task":{...}}'
```

The bundled `scripts/live_resort_create_goal.py` / `live_lead_create_goal.py` remain as
worked-example fixtures.

## 7. Observing a run (operator CLI — never agent-facing)

```bash
uv run blackboard list-goals
uv run blackboard snapshot <goal-id>    # tasks, artifact aliases, findings, reviews, approvals
uv run blackboard events   <goal-id>    # the append-only event trace, in order
uv run blackboard gate     <goal-id>    # UNSATISFIED / HUMAN_REQUIRED / SATISFIED + what's missing
uv run blackboard thrash   <goal-id>    # coordination-health counters (operator-only, not an MCP tool)
```

The gate walks: `UNSATISFIED` (missing reviews or open blocking findings) → `HUMAN_REQUIRED`
(only the human release approval is missing) → `SATISFIED` (then `authorize_goal_completion`
flips the goal to `satisfied`).

## 8. Teardown

```bash
uv run blackboard reset-demo   # truncate blackboard state (destructive; CLI-only)
mise run db:down               # stop Postgres
```

`OMNIGENT_DATA_DIR` isolates omnigent 0.5.1's session store; the `uvx` env is hermetic
(ADR-0010), so nothing leaks into or out of the local fork.

## 9. Troubleshooting

- **Agents can't find your repo / operate on the wrong tree.** You changed
  `BLACKBOARD_TARGET_REPO` but didn't re-run `mise run link-target`. Check `readlink .target`.
- **`cwd: ${BLACKBOARD_TARGET_REPO}` "doesn't work."** It can't — `os_env.cwd` is parsed
  verbatim (ADR-0015). Use the `.target` symlink seam; env vars only drive `link-target` and
  `new_goal.py`, not the agent YAML.
- **`team:validate` fails / a config resolves to a non-local sandbox.** Something set
  `sandbox.type` to a managed/MicroVM backend — violates ADR-0010. Fix the config; only
  `none` / `linux_bwrap` / `darwin_seatbelt` / `windows_jobobject` are allowed.
- **MCP health check hangs or `:8000` conflicts.** The server binds `:8010` on purpose
  (`:8000` is commonly taken). Confirm `BLACKBOARD_MCP_PORT` agrees across `.env`,
  `mise.toml`, and the agent YAMLs.
- **An enum/routing change passes `mise run check` but the Lean proof is stale.** `formal`
  is not in `check.depends` — run `mise run formal` explicitly.
