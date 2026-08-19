# pneuma-blackboard — Omnigent Blackboard MCP PoC

A locally runnable cross-functional **Agentic SDLC Team Runtime**: a transactional
PostgreSQL **blackboard kernel** (authoritative organizational state) exposed through a
**thin FastMCP adapter**, driven by an **Omnigent** team of specialist agents.

> This package is a member of the [pneuma workspace](../../README.md). The distribution is
> named `pneuma-blackboard`; the import package is still `sdlc_blackboard`, unchanged. It is
> a *sibling* of `pneuma`, not a layer of it — neither imports the other. They meet at the
> design seam in [`docs/design/org-plane.md`](../../docs/design/org-plane.md): this kernel
> owns organizational truth (who was supposed to produce work, whether it was reviewed),
> pneuma owns content and execution.

> Omnigent sessions execute work. The blackboard kernel owns truth. MCP exposes the
> kernel but does not define its semantics.

Built to the handoff in [`HANDOFF.md`](./HANDOFF.md), following the strict hexagonal
architecture in `hexagonal-arch-stack.md` (pure typed domain, protocol-bound ports,
one composition root, errors-as-values across the application boundary).

## Architecture

```
interfaces/   driving adapters — thin. Translate transport <-> use case, no logic.
  mcp/        thin FastMCP server: 5 read tools + 13 command tools + /health
  cli.py      Typer developer CLI (migrate, snapshot, events, gate, reset-demo)
     |
     v
application/  orchestration. use_cases/ return CommandResult[T]. ports.py = Protocols.
     |        commands/receipts/results DTOs, idempotency, event append.
     v
domain/       pure. Models, value objects, transition matrix, binding fingerprint.
     ^        stdlib + Pydantic only — zero infrastructure imports.
     |  (application/ports.py is the seam adapters bind against)
infrastructure/  driven adapters: asyncpg pool + jsonb codec, 11 repositories,
                 migrations runner, composition root (di.py).
```

The kernel is proven **independently of Omnigent and any model** by a scripted
deterministic end-to-end test (`tests/e2e/`) that drives the entire report-export
lifecycle — goal → analysis → implementation → QA → security-blocker → remediation →
revision-bound re-review → release gate → human approval → completion — with zero LLMs.

## Setup

Prerequisites: Python 3.14 (via `uv`), Docker, `dbmate`, and `mise` (all pinned in
`mise.toml`). The live agent run (M5/M6) also needs `omnigent`, pulled from PyPI on demand
by `mise run team` — it is **not** a project dependency (ADR-0010).

All `mise` tasks now live at the **workspace root** and are prefixed where they would
otherwise collide with pneuma's; run them from there (`mise` finds the root `mise.toml` from
any subdirectory).

```bash
cp packages/blackboard/.env.example packages/blackboard/.env
mise run install            # uv sync — one .venv for both members (no --extra: dev is a
                            # dependency-group now, which uv syncs by default)
mise run db:up              # docker compose up -d postgres  (see ADR-0002 if compose is absent)
mise run migrate            # apply SQL migrations via dbmate
```

### Run on your own repo

The **target repository** — the codebase the team builds against — is a parameter, not a
fixture. Set `BLACKBOARD_TARGET_REPO` (in `.env`) to your repo, then materialize the seam:

```bash
mise run link-target        # ln -sfn "$BLACKBOARD_TARGET_REPO" .target
```

The producing/writing specialists `cwd` into the repo-root `./.target` symlink, so the
committed agent YAMLs stay target-agnostic. Seed a goal with the generic
`scripts/new_goal.py` (see the runbook). Full operational playbook — bootstrap, gate,
startup ordering, seeding, observing, teardown, troubleshooting — is in
**[`docs/RUNBOOK.md`](docs/RUNBOOK.md)**. For the self-composition roadmap (dynamic / JIT /
multi-verifier / multi-team), see **[`docs/SELF-COMPOSITION.md`](docs/SELF-COMPOSITION.md)**.

## Validate

From the workspace root:

```bash
mise run check                    # lint BOTH members + BOTH test suites (definition of done)
mise run lint:blackboard          # this package only: ruff check + ruff format --check
mise run typecheck:blackboard     # pyright strict
mise run test:blackboard          # full suite: 335 passed, 2 skipped (Docker up)
mise run test:blackboard:unit     # fast inner loop — unit + contract, no Postgres
```

Or work inside this directory, where the commands are the familiar single-project ones:

```bash
cd packages/blackboard
uv run ruff check . && uv run pyright && uv run pytest
```

Integration + e2e tests spin up a real Postgres via testcontainers and **skip cleanly**
when Docker or dbmate are absent — 297 passed, 40 skipped, zero failures in that case.

Note that from the root, `uv run --package pneuma-blackboard pytest` needs the
`packages/blackboard` path argument: `--package` picks which member's dependencies to expose
but does not change the working directory, and pytest reads its rootdir and config from the
cwd. Without the path it would collect pneuma's suite too. The mise tasks above already pass it.

## Run the scripted demo (no LLMs)

```bash
mise run migrate
mise run demo:blackboard    # drives the full lifecycle, prints each step + final gate
                            # (`mise run demo` is pneuma's war-room demo)
uv run blackboard events <goal-id>   # inspect the event trace
uv run blackboard gate  <goal-id>
```

## Run the MCP server

```bash
mise run mcp                # http://127.0.0.1:8010/mcp  (loopback only)
curl http://127.0.0.1:8010/health         # {"status":"ok"}
uv run fastmcp list http://127.0.0.1:8010/mcp
```

Port 8010 (not 8000) is the default because 8000 is commonly occupied by another local MCP
server. It is a single source of truth: `BLACKBOARD_MCP_PORT` in `mise.toml` / `.env`, the
`scripts/serve_blackboard.py` launcher, and every `sdlc_team/**/config.yaml` tool URL agree.

## Run the Omnigent team (live, requires Bedrock access)

The team runs entirely on your machine. It pins `omnigent` from PyPI via `uvx`, so the run
never picks up a local editable checkout, and every specialist executes **in-process**
(`os_env.type: caller_process`) inside a **local** inner sandbox (`none` or `linux_bwrap`).
No managed hosts, no MicroVMs, no cloud sandbox provider (ADR-0010).

```bash
set -a; source .env; set +a
aws sso login --profile "$AWS_PROFILE"
mise run mcp &              # the blackboard MCP server must be up first (:8010)
mise run team               # uvx --from "omnigent==$OMNIGENT_VERSION" omnigent run sdlc_team/
```

Prove the team parses and resolves only to local sandboxes, without launching it:

```bash
mise run team:validate      # 18/18 configs, each sandbox.type ∈ {none, linux_bwrap}
```

`linux_bwrap` needs the `bwrap` binary (`dnf install bubblewrap` / `apt install bubblewrap`).
On a box without it, set the governing agents' `sandbox.type: none` (they still can't mutate
the repo — their tool allowlist is reads + `open_finding`/`submit_review`; see ADR-0009).

See `HANDOFF.md` §15A for the three Bedrock implementation profiles (Claude Opus 4.8,
Claude Fable 5, GPT-5.6 Sol) and §18 for the demo prompt.

### The team — 17 bounded contexts (`sdlc_team/ROSTER.md`)

Each specialist is a bounded context (an authority slice), not a persona; behavior is
driven by the task contract. The lead composes the roster per goal.

**Producing** (author artifacts): `analyst`, `architect`, `implementation_claude_opus`,
`implementation_claude_fable`, `implementation_codex_sol`, `data_engineer`,
`documentation`, `ux`.
**Governing** (review a revision, open findings, gate release): `quality`, `security`,
`compliance`, `release_engineer`, `platform_sre`, `operations`, `finops`, `support`.

The release gate **derives** its required reviews from the blocking `ReviewRequirement`s
the task contracts declare — adding a governing context to a goal is a contract change,
never a kernel change (ADR-0008). Governing contexts run `read_only_os`. The full team
validates through the real Omnigent 0.5.1 parser with 0 errors.

---

## Architecture Decision Records

Deviations from `HANDOFF.md`, all made to preserve the hexagonal boundary or to match
the verified behavior of the pinned dependencies. Each is grounded, not incidental.

### ADR-0001 — Relocate `mcp/`/`cli.py` under `interfaces/`; plain composition root; errors caught at the service edge

**Decision.** The handoff's §4 layout puts `mcp/` and `cli.py` as siblings of
`domain/application/infrastructure`. We relocate them under `interfaces/`, per
`hexagonal-arch-stack.md`. **Why.** The handoff's own DoD requires "domain logic
independent from FastMCP" and "MCP tools are thin application-service calls" — exactly the
hexagonal contract. Making `interfaces/` explicit enforces "interfaces drive, they don't
contain logic" structurally.

**Composition root.** `infrastructure/di.py` is a plain builder, not a dishka container:
the services are request-less (each command opens its own unit-of-work transaction over
one shared pool), so an APP-scoped builder is the honest shape and keeps a DI framework
out of the hot path. We do not depend on `dishka`.

**Error model.** The stack's §10 argues for `returns.Result` threaded through every
boundary. We chose a narrower, equivalent model that the mutating boundary observably
honors: domain outcomes are raised as typed `DomainError` subclasses inside a use-case
body and caught once at the service edge (`CommandService._command`), which converts them
to a `CommandResult[T]` value — so every *command* the MCP/CLI surface calls returns a
structured result, never an exception. The read side (`QueryService`, `GateService`)
returns bare values/`None`; an infrastructure exception there propagates to the interface
handler, which is acceptable for the PoC's read tools. We do not depend on `returns`. This
is a deliberate deviation from stack §10's mechanism; the guarantee it protects (no raw
exceptions across the *command* boundary) holds.

### ADR-0002 — `docker compose` optional; testcontainers is the test substrate

**Decision.** We ship `compose.yaml` (handoff §5) but tests never depend on it. The
integration + e2e tiers spin up Postgres via `testcontainers` and skip when Docker/dbmate
are absent. **Why.** The development box lacked the `docker compose` subcommand; binding
tests to it would make the suite un-runnable there. testcontainers gives hermetic,
per-run databases and matches the hexagonal playbook's §6 "integration skips, never
fails, when Docker is missing".

### ADR-0003 — Pool-wide jsonb codec instead of per-call-site encoding

**Decision.** `infrastructure/postgres.py` registers a pool-level asyncpg codec
(`set_type_codec("jsonb", ..., schema="pg_catalog")`); repositories pass and receive
plain `dict`/`list`. **Why.** asyncpg rejects a raw `dict` for a jsonb column without a
codec (verified on 3.14 / PG 18). The handoff's inline `orjson.dumps(...).decode()` at
every call site works but scatters the concern; the codec centralizes JSON handling in
the adapter, which is where persistence detail belongs. `schema="pg_catalog"` is required
for the builtin jsonb type (asyncpg's documented `"public"` default is wrong for builtins).

### ADR-0004 — Omnigent `tools.blackboard` is a generic inline MCP server, not a framework concept

**Decision.** The lead + specialist configs declare the blackboard as an inline MCP
server under `tools:` (`type: mcp`, `url: http://127.0.0.1:8010/mcp`). **Why.** Validated
against the installed Omnigent 0.5.1 source: the string "blackboard" appears nowhere in
the package — there is no framework blackboard. Any `tools:` key that is not a reserved
`ToolsConfig` field and carries `type: mcp` becomes a generic `MCPServerConfig`. The
handoff's `tools.blackboard` shape happens to be exactly that generic form.

### ADR-0005 — Omnigent executor: `harness` in `config`, `model` at executor top level

**Decision.** Specialist configs use `executor: {type: omnigent, model: <id>, config:
{harness: claude-sdk}}`, not the handoff's §15A flat `executor: {harness, model}`.
**Why.** The Omnigent 0.5.1 parser never reads a top-level `executor.harness`; harness is
read only from `executor.config.harness`, and `model` is a first-class top-level
`ExecutorSpec` field. The flat form would silently fall back to the `omnigent` pseudo-
harness. Codex-on-Bedrock uses `harness: codex-native`, pins its model in-YAML
(`executor.model: openai.gpt-5.6-sol`), and reads only its `amazon-bedrock` provider +
region + credentials from `~/.codex/config.toml` (Bedrock Mantle). See ADR-0011.

### ADR-0006 — Lead SKILL.md files carry YAML frontmatter (`name`, `description`)

**Decision.** Each `skills/*/SKILL.md` opens with `--- name: … description: …
user-invocable: false ---`. **Why.** The handoff's §17 skill examples are bare Markdown;
the Omnigent 0.5.1 parser hard-fails on a SKILL.md without frontmatter (`name` +
`description` required). Caught by validating the team through the real parser.

### ADR-0007 — Review tasks are reopened, not re-created, on remediation

**Decision.** On a remediation revision, `submit_task_result` re-opens the existing review
tasks (returns them to READY) rather than inserting new ones. **Why.** Review tasks carry
a stable `task_key` (`<producer>:review:<type>`); the `unique(goal_id, task_key)`
constraint forbids duplicates. Re-opening models the handoff §18 cycle ("reviews against
the old revision become stale; reviewers review the new revision") correctly. This bug was
caught by the scripted E2E on the first remediation pass.

### ADR-0008 — Personas are bounded contexts; the gate is data-driven from task contracts

**Decision.** `ActorKind` enumerates the canonical enterprise SDLC contexts (analyst,
architect, implementation, data, documentation, ux, quality, security, compliance,
release, platform, operations, finops, support), partitioned into `PRODUCER_KINDS` and
`REVIEWER_KINDS`. `open_finding` authorizes any `REVIEWER_KIND` whose contract sets
`may_create_blocking_finding`; the release gate `derives` its required review types (and
the governed artifact) from the blocking `ReviewRequirement`s across the goal's task
contracts, falling back to `(quality, security)`. **Why.** HANDOFF §29 ("a function name
is an organizational convenience; the task contract drives behavior") and §28 (add
functions "as task capabilities rather than theatrical personas"). Hardcoding
`{quality, security}` in the gate and finding-auth would make every new governing context
a kernel edit; deriving from contracts means adding compliance/finops/release to a goal is
a contract change with zero kernel change. Proven by `tests/unit/test_gate_derivation.py`
and `tests/integration/test_multi_context_gate.py` (a 4-governing-context goal that stays
UNSATISFIED until every declared review lands).

### ADR-0009 — Governing specialists run `read_only_os`; the 16-context team is the roster

**Decision.** The Omnigent team is a lead + 17 specialists (`sdlc_team/`, see
`ROSTER.md`); reviewing contexts carry the `read_only_os` guardrail policy and only the
`open_finding`/`submit_review` command tools (+reads), so a reviewer structurally cannot
mutate the repository. **Why.** A review context's authority is to judge a revision and
open findings, never to change source — enforcing that with `read_only_os` (a verified
0.5.1 policy) plus a minimal tool allowlist makes the boundary a runtime guarantee, not a
prompt request. Producing contexts get scoped `write_paths`. The whole team validates
through the real 0.5.1 parser with 0 errors.

### ADR-0010 — Self-contained + PyPI-pinned + local sandboxes: `omnigent` is a tool, not a dependency

**Decision.** The live team is launched with `mise run team`, which runs
`uvx --from "omnigent==$OMNIGENT_VERSION" omnigent run sdlc_team/`. `omnigent` is **not** in
`pyproject.toml` (neither a base dep nor an extra). The team lives at `sdlc_team/` at the repo
root — no `omnigent/` wrapper directory. Every specialist runs in-process
(`os_env.type: caller_process`) inside a **local** inner sandbox (`none` or `linux_bwrap`);
no session ever sets `host_type: managed`.

**Why — PyPI, not the fork.** `omnigent` is a runtime the PoC talks to over HTTP (the inline
`blackboard` MCP server); the PoC never imports it. Pulling it through an isolated `uvx` env
pins the released `0.5.1` and makes it structurally impossible for a local editable checkout
(e.g. a `0.6.0.dev0` fork on `PYTHONPATH`) to shadow it. Folding `omnigent` into the project
venv was rejected: its transitive pins (e.g. `websockets<15`) would downgrade the kernel's own
resolved stack for no benefit, since nothing in `src/` imports it. `OMNIGENT_VERSION` in
`mise.toml` is the single knob; bump it and re-run `mise run team:validate`.

**Why — no MicroVMs.** Omnigent's MicroVM / managed-host machinery
(`onboarding/sandboxes/lambda_microvm.py`, the cloud sandbox providers) only engages for
`host_type: managed` sessions. This team never requests one: the lead dispatches specialists
via `sys_session_send` as in-process children, and each `os_env` uses `caller_process` with a
local backend. Released `0.5.1` is MicroVM-free regardless; the fork is where that code lives.
`scripts/validate_team.py` (run by `mise run team:validate`) fails the build if any config
parses to a non-local sandbox, so a future edit can't silently reintroduce a managed host.

**Why — the rename.** `sdlc_team/` used to sit under `omnigent/`. A repo-root directory named
`omnigent/` with no `__init__.py` is an implicit namespace package: once `omnigent` is
importable and cwd is on `sys.path` (as under `uv run`), that data directory would shadow the
real package. Dropping the wrapper removes the collision and makes the layout self-describing.

### ADR-0011 — Two harnesses only (`claude-sdk`, `codex-native`); models pinned in-YAML

**Decision.** Every agent runs on exactly one of two harnesses: `claude-sdk` (16 agents,
Claude on Bedrock) or `codex-native` (1 agent, GPT-5.6 Sol on Bedrock Mantle). The `pi`
harness is not used. `analyst`, `quality`, and `security` — previously `pi` — are now
`claude-sdk` on `global.anthropic.claude-opus-4-8`, matching the other judgment-heavy
contexts. The single codex agent pins `executor.model: openai.gpt-5.6-sol`.

**Why — no `pi`.** `pi` is a separate TUI harness that resolves its provider from
`~/.omnigent/config.yaml`, needs a `pi` binary on PATH, and routes through neither the
Claude-on-Bedrock nor the Codex-on-Bedrock credential path. Standardizing on two
Bedrock-backed harnesses means one auth story (`CLAUDE_CODE_USE_BEDROCK` +
`AWS_BEARER_TOKEN_BEDROCK` for Claude; the `amazon-bedrock` provider in `~/.codex/config.toml`
for Codex) and no extra runtime to install. The three converted contexts are requirements
analysis, QA validation, and security review — all reasoning-bound, so Opus is the honest tier.

**Why — pin the codex model in-YAML.** `codex-native` honours `executor.model` (verified in
0.5.1), so the model rides in the agent config, not the shared global `~/.codex/config.toml`
(which other projects also read and may set to a different default). Codex still sources its
provider, region, and credentials from that file; only the model is overridden. Result: the
POC deterministically runs GPT-5.6 without mutating global state. Keep the Codex Bedrock
region aligned with `AWS_REGION` in `.env` so both model families resolve in the same region.

### ADR-0012 — Contract correctness: honor SubmitTaskResult, pass routing_class, enforce the gate

The wire contract advertised three things the kernel did not deliver; this ADR closes the gap
without changing any MCP tool signature.

**Why — persist the SubmitTaskResult optional fields.** `SubmitTaskResult` accepts
`disposition`, `summary`, `finding_ids`, `assumptions`, `unresolved_questions`, and
`residual_risks`, but the handler read only `task_id`, `run_id`, `input_manifest`, and
`artifacts` — the rest were silently dropped, so the schema was a lie. The `runtime_runs`
table already carries a `result_manifest jsonb` column that was never written. On submit we
now build a manifest from those fields and persist it via `RuntimeRunRepository.set_state`
(the pool-wide jsonb codec, ADR-0003, means we pass a plain `dict`). The model is unchanged;
the fields become honored. `set_state` gained an optional `result_manifest` param that
`coalesce`s to leave any prior manifest untouched when omitted.

**Why — pass `routing_class` through.** `start_runtime_run` hardcoded `routing_class=None`,
ignoring `StartRunRequest.routing_class` even though the `RoutingClass` enum, the DB column,
and its `CHECK` constraint all exist. It now passes the requested value through; an unknown
value is a client input error, so the enum's `ValueError` is translated to a domain
`ValidationFailed` (surfacing as `VALIDATION_FAILED`), not an uncaught exception.

**Why — make `authorize_goal_completion` enforce the gate.** The handler previously flipped a
goal to SATISFIED on the caller's word that the gate was met, documenting the precondition
only in its docstring — a TOCTOU: the gate could be read SATISFIED, then invalidated (a new
finding, a promotion staling a review) before the CAS. Authorize now re-evaluates the gate on
the SAME unit of work as the state change (`GateService.evaluate_on_conn`), raising
`PreconditionFailed("release gate not satisfied")` unless the gate reads SATISFIED. Authorize
is enforcing, not advisory. `get_gate_status` keeps identical public behavior; it now simply
opens a UoW and delegates to the same shared evaluation. Because the runtime is READ
COMMITTED, the same-conn re-check alone does not close concurrent-gate-input write skew (a
blocking finding or review commits without touching the goal row), so authorize first takes an
exclusive `SELECT ... FOR UPDATE` on the goal row and every gate-input mutation
(`open_finding`, `submit_review`, `record_human_approval`, `promote_artifact`) takes
`SELECT ... FOR SHARE` on the same row before writing; the conflicting lock modes serialize
gate-input commits against the authorize evaluation window while writers stay concurrent with
each other.

Two adapter-edge conflict translations ship alongside these: `open_assignment` and
`ReviewRepository.insert` now catch `asyncpg.UniqueViolationError` on their unique indexes and
raise the typed `Conflict` (non-retryable) instead of leaking a raw 500, mirroring the
`ProcessedCommandRepository.put` exemplar (hexagonal §4). The gate's ad-hoc single-binding
fingerprint was unified with the persisted `binding_fingerprint` via a
`single_binding_fingerprint` helper so the gate and the reviews/approvals tables share one
binding identity.

### ADR-0013 — Outbox relay ships as a CLI consumer; `repositories.py` splits into a package

Two structural changes land together; neither alters a wire contract or an MCP tool signature.

**Why — drain the transactional outbox.** `EventRepository.append` has always written an
`outbox` row in the same transaction as every domain event (§12), but nothing consumed them:
rows accumulated forever with `published_at` NULL and the `claim_unpublished`/`mark_published`
methods had zero callers. Per the HANDOFF §12 worker spec ("publishing may mean structured
logging ... and marking `published_at`. No Kafka is needed"), the consumer ships as the
`blackboard outbox-relay` developer command over a new `OutboxService.drain_outbox(limit)`
application use case. It claims a batch inside one unit-of-work transaction with
`for update skip locked` (so parallel relays never double-publish), structured-logs each row
as the publish step, marks it published, and commits atomically. `--once` (default, POC) drains
one batch and exits; `--loop --interval` polls. The claim now also bumps the `attempts` column
(present in the schema, never incremented) via a CTE `UPDATE ... RETURNING`, recording each
delivery try. The service lives in the application layer and reaches the DB only through the
existing `OutboxRepo` port — the CLI stays a thin adapter (hexagonal §3).

**Why — split the ~1000-LOC repositories module.** `infrastructure/repositories.py` grew to 11
repository classes in one flat file. It is now a package split by aggregate for cohesion —
`goals`, `tasks` (task/assignment/runtime-run), `artifacts`, `quality` (finding/review/approval),
`events_outbox`, `idempotency` — over a shared `_common` module holding the connection-narrowing
helper, the cross-aggregate row mappers, and a new `cas_update` helper. `cas_update` dedups the
version-guard compare-and-set idiom (`fetchrow` + None-on-miss + aggregate mapper) shared by
`GoalRepository.set_state_cas`, `TaskRepository.claim_cas`/`transition_cas`, and
`FindingRepository.set_state_cas`; the two odd shapes (`refresh_ready`, bulk with no version
guard; `promote_alias_cas`, a revision-token two-branch guard) stay verbatim. The re-exporting
`__init__.py` keeps every `from ...infrastructure.repositories import X` import working, so no
call site changed. The SQL is byte-identical except the CAS refactor and the attempts bump. The
ruff `S608` per-file ignore moved from the flat file to `repositories/*.py`; the trusted-table
rationale now lives in the package `__init__` and each module docstring. The dead
`AssignmentRepository.fail_assignment` (zero callers) and its `AssignmentRepo.fail_assignment`
port method were removed.

### ADR-0014 — Lean-pinned routing default + failure ledger + derived thrash report

Three verticals ship together under one spec-first, formally-certified change. Two coordination
levers the Cursor swarm-economics reading calls for: routing is the model-**cost** lever, thrash
is the coordination-**health** signal. Neither alters an MCP tool signature.

**Spec-first, Lean-pinned.** The work is driven by a certified EARS spec
(`.erpaval/specs/001-routing-thrash/requirements.json`, G1/G2 + R1-R5B + T1-T5) whose semantics
are pinned by a Lean model (`formal/Blackboard/{Routing,Thrash}.lean`, `lake build` green, no
`sorry`). `mise run formal` is now part of the gate. The routing table and the thrash counter
algebra each have a Python implementation and an independent Lean proof; a contract test
(`tests/contract/test_routing_policy.py`) transcribes the 18-row Lean `routingPolicy` by hand and
asserts `domain/routing.py` matches for every `ActorKind` (R3 totality) plus the reviewer≤producer
cost-tier ordering (R4) — the Python and Lean tables must move in lockstep.

**Why — a default routing class per run.** `start_runtime_run` honored an explicit `routing_class`
(ADR-0012) but persisted `None` otherwise. It now derives the default from the task's required
actor kind via the Lean-certified policy (`domain/routing.py`): planning/design contexts
(lead/architect/analyst) route to the frontier global profile, producing contexts to the geo
profile, mechanical review/governance to cheap in-region runtime, human/system to the cheapest
mantle. Operators steer cost per task shape without touching any tool. An explicit value still wins
(R1); an invalid string is still a `ValidationFailed` (R5B).

**Why — an append-only command-failure ledger.** A failed mutating command was previously
observable only in a structured log line — invisible to any query. A new `command_failures` table
(bigserial PK, `command_id/tool_name/actor_id/goal_id/task_id/error_code/occurred_at`, indexed on
`(goal_id, error_code)`) records one row per failed attempt. It is deliberately **outside
idempotency** (every failed attempt is a row — dedup would defeat the point) and carries **no FK**
to goals/tasks (a failure may reference a not-yet-existing or deleted aggregate; it is
observability, not an aggregate) — so it is truncated explicitly by the test harness / `reset-demo`
rather than riding the `goals` cascade.

**Why — move the DomainError catch outside the unit-of-work block.** The base command wrapper
caught `DomainError` **inside** `async with uow.begin()`, so control fell through to a clean
`__aexit__` that **committed** the empty/partial transaction — a latent partial-commit trap. The
catch now lives outside the `async with`, so a raised domain error unwinds the context manager and
the UoW rolls back naturally. In that `except` branch, a **second, short transaction** writes the
ledger row, wrapped in `try/except Exception` + `log.warning("command.failure_unrecorded")` so a
ledger write can never mask the original command failure.

**Why — a per-goal derived thrash report.** `ThrashService.get_thrash_report(goal_id)` is a
read-only derived read (its own `uow.begin()` like `GateService`, plain SELECT/COUNT, **no row
locks** — T3) returning four counters that mirror `Thrash.lean`'s `ThrashReport`: conflicts and
stale-versions (from the failure ledger, filtered by the exact `conflict`/`stale_version` wire
codes, with task-scoped failures resolved to their goal via a `tasks` join), review-rejections
(reviews whose disposition is anything but `APPROVED`), and reclaims (`review_task.reopened` events
plus `Σ greatest(assignment_epoch − 1, 0)` over the goal's tasks — every re-claim beyond the first).
Zero on an empty or unknown goal (T2 — `COUNT`/`COALESCE(SUM,0)` give this for free); each counter
is monotone under new events (T5); every counter is computed exclusively from the goal's own signals
(T1 frame property). Exposed only as the operator CLI `blackboard thrash GOAL_ID` (JSON, T4) —
**never an MCP tool**: agents gaming their own thrash metric is the failure mode.

### ADR-0015 — Target repo is a `.target` symlink seam, not an env var in `cwd`

The codebase the team builds against is a parameter (`BLACKBOARD_TARGET_REPO`), so the runtime
is repo-agnostic (see `docs/RUNBOOK.md`). The obvious implementation — `cwd: ${BLACKBOARD_TARGET_REPO}`
in the producing agents' `os_env` — **does not work**: omnigent 0.5.1 parses `os_env.cwd`
**verbatim** (`spec/parser.py:_parse_os_env` stores `str(cwd_raw)` and, unlike the `tools:` /
`llm:` / `executor:` parsers, is never given `expand_env`), so a `${VAR}` or Jinja reference
reaches the sandbox as a literal directory name. ADR-0004's env-interpolation note applies only
to the MCP tool blocks, which is consistent with this.

So the seam is a **stable symlink**: `mise run link-target` does `ln -sfn "$BLACKBOARD_TARGET_REPO"
.target`, and the six producing/writing specialists (`implementation_claude_opus/fable`,
`implementation_codex_sol`, `data_engineer`, `quality`, `security`) declare `cwd: ./.target`.
The literal path satisfies the verbatim parse and resolves to a real directory for
`team:validate`; the committed YAMLs never name a host-specific path; retargeting is one
`link-target` re-run. `.target` is git-ignored (host-specific). The read-only reviewers keep
`cwd: .` + `write_paths: ./artifacts/<role>` and are unaffected. Env vars still drive the
symlink target and `scripts/new_goal.py`'s `--scope` — just not the agent `cwd`.

---

## Project layout

See `HANDOFF.md` §4 for the full tree. Key entry points:

- `src/sdlc_blackboard/interfaces/mcp/server.py:mcp` — the FastMCP server object
- `src/sdlc_blackboard/interfaces/cli.py:app` — the `blackboard` CLI
- `scripts/run_scripted_demo.py` — the no-LLM lifecycle demo
- `tests/e2e/test_scripted_flow.py` — the deterministic kernel proof
- `sdlc_team/` — the Omnigent team (validated against 0.5.1)
