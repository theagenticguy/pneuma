# omnigent-blackboard-poc · Module map

This index maps each top module to the behavior it owns. The kernel is one Python package, `sdlc_blackboard`, declared as the sole distribution in `pyproject.toml` and organized as four hexagonal layers under `src/` — `domain`, `application`, `infrastructure`, `interfaces` — surrounded by four non-package roots: `tests`, `sdlc_team`, `scripts`, and the Lean 4 `formal/` model. Modules are ordered by inbound-import count for the four `src/` layers (domain 194, application 99, infrastructure 39, interfaces 8), then by total LOC for the roots. This ordering matches the module-map flowchart in `docs/architecture/system-overview.md`, which draws `interfaces` and `infrastructure` pointing inward to `application` and `domain`.

## domain

The purest layer: frozen Pydantic value objects, aggregates, and state machines that run in a unit test with zero setup — stdlib plus Pydantic only, no ORM, HTTP, SDK, or I/O (`src/sdlc_blackboard/domain/common.py:1`). It is the most depended-upon module, with 194 inbound `from sdlc_blackboard.domain.*` imports across `src`, `tests`, and `scripts`, `domain.common` alone accounting for 44. `common.py` defines the shared base `DomainModel` (`src/sdlc_blackboard/domain/common.py:19`) and the `ActorKind` bounded-context enum (`src/sdlc_blackboard/domain/common.py:25`), while `tasks.py` carries the task-contract aggregate and its state machine (`src/sdlc_blackboard/domain/tasks.py:1`). The closed error hierarchy here maps 1:1 onto the wire `ErrorCode` and is returned as values, not raised (`src/sdlc_blackboard/domain/errors.py:1`).

- `src/sdlc_blackboard/domain/common.py` (126 LOC)
- `src/sdlc_blackboard/domain/errors.py` (94 LOC)
- `src/sdlc_blackboard/domain/tasks.py` (81 LOC)
- `src/sdlc_blackboard/domain/events.py` (77 LOC)
- `src/sdlc_blackboard/domain/reviews.py` (77 LOC)
- `src/sdlc_blackboard/domain/findings.py` (76 LOC)
- `src/sdlc_blackboard/domain/approvals.py` (56 LOC)
- `src/sdlc_blackboard/domain/artifacts.py` (54 LOC)

## application

The use-case layer: command services, the port protocols they depend on, and the structured results they return across the boundary. Every external capability is a `@runtime_checkable Protocol` defined next to its consumers here, never next to its infrastructure implementers (`src/sdlc_blackboard/application/ports.py:1`). The largest file, `task_service.py`, holds the create / refresh-ready / claim-with-fencing / bind / start-run / submit-result use cases where the reliability invariants live, and `start_runtime_run` now defaults a run's `routing_class` from `default_routing_class(required_actor_kind)` when the request carries none (`src/sdlc_blackboard/application/use_cases/task_service.py:1`). Concurrency conflicts are returned as `CommandResult[T]` values rather than raised (`src/sdlc_blackboard/application/results.py:1`), and the shared `CommandService` base owns the unit-of-work transaction, idempotent-by-`command_id` execution, and `DomainError`-to-result translation — with the catch deliberately outside `uow.begin()` so a failure rolls back cleanly and then records a best-effort command-failure ledger row (`src/sdlc_blackboard/application/use_cases/base.py:1`).

- `src/sdlc_blackboard/application/use_cases/task_service.py` (440 LOC)
- `src/sdlc_blackboard/application/ports.py` (277 LOC)
- `src/sdlc_blackboard/application/use_cases/review_service.py` (211 LOC)
- `src/sdlc_blackboard/application/use_cases/gate_service.py` (172 LOC)
- `src/sdlc_blackboard/application/use_cases/artifact_service.py` (107 LOC)
- `src/sdlc_blackboard/application/use_cases/goal_service.py` (106 LOC)
- `src/sdlc_blackboard/application/results.py` (104 LOC)
- `src/sdlc_blackboard/application/use_cases/base.py` (102 LOC)

## infrastructure

The driven-adapter layer: asyncpg repositories that implement the application ports by shape, translating between domain models and rows with explicit SQL rather than an ORM (`src/sdlc_blackboard/infrastructure/repositories/__init__.py:1`). The former flat `repositories.py` was split into an aggregate-per-module `repositories/` package (ADR-0013), with `__init__.py` re-exporting every repository so the import surface is unchanged; the load-bearing operations — compare-and-set transitions, partial unique constraints, `FOR UPDATE`, `SKIP LOCKED` — are hand-written SQL, with the shared version-guard CAS factored into `_common.cas_update` (`src/sdlc_blackboard/infrastructure/repositories/_common.py:34`). The newest submodule, `failures.py`, is the append-only command-failure ledger written on rollback, scoping counts to a goal via a task-to-goal join (`src/sdlc_blackboard/infrastructure/repositories/failures.py:22`). The connection pool registers a pool-wide jsonb codec so repositories pass plain `dict`/`list` for jsonb columns (`src/sdlc_blackboard/infrastructure/postgres.py:1`), `logging.py` wires structlog for the process entrypoints (`src/sdlc_blackboard/infrastructure/logging.py:24`), and `di.py` is the composition root, the only module that knows both ports and concrete adapters (`src/sdlc_blackboard/infrastructure/di.py:64`).

- `src/sdlc_blackboard/infrastructure/repositories/` (package, ~1286 LOC: `__init__`, `_common`, `goals`, `tasks`, `artifacts`, `quality`, `events_outbox`, `failures`, `idempotency`)
- `src/sdlc_blackboard/infrastructure/postgres.py` (126 LOC)
- `src/sdlc_blackboard/infrastructure/migrations.py` (94 LOC)
- `src/sdlc_blackboard/infrastructure/logging.py` (63 LOC)
- `src/sdlc_blackboard/infrastructure/di.py` (76 LOC)
- `src/sdlc_blackboard/infrastructure/clock.py` (12 LOC)

## interfaces

The driving-adapter layer: transport surfaces that translate to use-case calls and hold no logic. The MCP command tools expose coarse-grained domain intentions, each a thin application-service call carrying a `CommandContext` envelope and returning a structured `CommandResult` (`src/sdlc_blackboard/interfaces/mcp/tools_commands.py:1`). The FastMCP server is the driving adapter whose lifespan builds the process-lifetime DI container and resolves the `Services` facade per tool (`src/sdlc_blackboard/interfaces/mcp/server.py:1`). A separate Typer CLI serves operators only — its 8 commands include destructive maintenance like `reset-demo` and the read-only `thrash` coordination report, both deliberately kept off the MCP surface so agents cannot observe their own thrash metric (`src/sdlc_blackboard/interfaces/cli.py:105`, `src/sdlc_blackboard/interfaces/cli.py:158`).

- `src/sdlc_blackboard/interfaces/mcp/tools_commands.py` (178 LOC)
- `src/sdlc_blackboard/interfaces/cli.py` (178 LOC)
- `src/sdlc_blackboard/interfaces/mcp/server.py` (87 LOC)
- `src/sdlc_blackboard/interfaces/mcp/tools_read.py` (63 LOC)

## tests

The verification module, tiered into `unit`, `property`, `contract`, `integration`, `acceptance`, and `e2e` directories totaling 5001 LOC. The unit tier is the largest single file, exercising every command service against in-memory fakes (`tests/unit/test_use_case_services.py:1`), backed by a shared fakes module whose `FakeCommandFailureRepo` honors the real task-to-goal join (`tests/unit/fakes.py:1`). The deterministic E2E test drives the full report-export SDLC lifecycle without LLMs, proving the kernel independently of Omnigent and model behavior across every reliability invariant (`tests/e2e/test_scripted_flow.py:1`); the integration tier skips cleanly when Docker and dbmate are absent and provides a session-scoped migrated Postgres container with a per-test pool (`tests/integration/conftest.py:1`). Contract correctness, coordination-thrash plus routing, and the routing-policy table (Python vs Lean over all 18 `ActorKind`s) are each covered by dedicated suites (`tests/integration/test_contract_correctness.py:1`, `tests/integration/test_thrash_and_routing.py:1`, `tests/contract/test_routing_policy.py:1`).

- `tests/unit/test_use_case_services.py` (1117 LOC)
- `tests/unit/fakes.py` (557 LOC)
- `tests/integration/test_contract_correctness.py` (438 LOC)
- `tests/e2e/test_scripted_flow.py` (341 LOC)
- `tests/integration/test_reliability_invariants.py` (278 LOC)
- `tests/integration/test_thrash_and_routing.py` (265 LOC)
- `tests/unit/test_thrash_service.py` (225 LOC)
- `tests/contract/test_wire_contracts.py` (223 LOC)

## sdlc_team

The Omnigent agent-team definition the kernel drives: a lead plus specialist configs, skills, and roster, expressed as YAML and Markdown totaling 1197 LOC. `config.yaml` declares the cross-functional team lead that converts a human objective into blackboard task contracts and coordinates review and remediation (`sdlc_team/config.yaml:1`). `LEAD.md` is the largest artifact and holds the lead's operating instructions (`sdlc_team/LEAD.md:1`), while each `agents/*/config.yaml` defines one specialist persona and its sandbox resolution. The configs are validated through the real `omnigent.spec.parse` by `scripts/validate_team.py` (`scripts/validate_team.py:1`).

- `sdlc_team/LEAD.md` (109 LOC)
- `sdlc_team/config.yaml` (88 LOC)
- `sdlc_team/agents/visual/config.yaml` (69 LOC)
- `sdlc_team/agents/release_engineer/config.yaml` (57 LOC)
- `sdlc_team/agents/platform_sre/config.yaml` (57 LOC)
- `sdlc_team/agents/finops/config.yaml` (57 LOC)
- `sdlc_team/agents/compliance/config.yaml` (57 LOC)
- `sdlc_team/ROSTER.md` (57 LOC)

## scripts

Operator-runnable entry points that exercise the kernel outside the test harness, totaling 762 LOC. `run_scripted_demo.py` mirrors the E2E test as a standalone script so an operator can watch the kernel drive the whole report-export flow against a live Postgres with no LLMs (`scripts/run_scripted_demo.py:1`). `bb.py` is a thin blackboard MCP CLI that lets specialist agents call the real MCP tool surface — JSON in, JSON out — without hand-rolling a client (`scripts/bb.py:1`), and `serve_blackboard.py` boots the FastMCP server on an explicit host/port (`scripts/serve_blackboard.py:1`). The `live_*_create_goal.py` scripts and `validate_team.py` cover live goal creation and team validation.

- `scripts/run_scripted_demo.py` (265 LOC)
- `scripts/live_resort_create_goal.py` (189 LOC)
- `scripts/live_lead_create_goal.py` (176 LOC)
- `scripts/validate_team.py` (71 LOC)
- `scripts/bb.py` (44 LOC)
- `scripts/serve_blackboard.py` (17 LOC)

## formal

A Lean 4 lake project (toolchain `leanprover/lean4:v4.32.0`, `formal/lean-toolchain:1`) that pins the semantics of two kernel policies as machine-checked theorems, built by `mise run formal` (`mise.toml:70`). `Routing.lean` models the 18-row actor-kind-to-routing-class policy and proves totality, a cost-tier bound, and that pure reviewers never route at a higher tier than producers (`formal/Blackboard/Routing.lean:60`); the Python table in `src/sdlc_blackboard/domain/routing.py` must stay in lockstep, enforced by `tests/contract/test_routing_policy.py:1`. `Thrash.lean` models the coordination-thrash report as a fold over goal-scoped signals and proves zero-on-empty, monotonicity, and the goal-frame property (`formal/Blackboard/Thrash.lean:58`).

- `formal/Blackboard/Thrash.lean` (113 LOC)
- `formal/Blackboard/Routing.lean` (102 LOC)
- `formal/lakefile.toml` (6 LOC)
- `formal/Blackboard.lean` (2 LOC)

## See also

- [impact-analysis](../insights/impact-analysis.md) — 15 shared source citations
- [contract-map](../insights/contract-map.md) — 13 shared source citations
- [processes](../behavior/processes.md) — 9 shared source citations
- [debugging-guide](../insights/debugging-guide.md) — 9 shared source citations
- [tech-debt](../insights/tech-debt.md) — 7 shared source citations
