# omnigent-blackboard-poc · Tech debt

This register answers one question: *where is the rot, and what would I pay to fix it?* It is assembled from four sources, each applied to the kernel code only (`src/sdlc_blackboard/`, `tests/`, `scripts/`, `sdlc_team/`; `demo_app/` and generated dirs excluded). First, **explicit comment markers** — a case-sensitive and case-insensitive grep for `TODO`/`FIXME`/`HACK`/`XXX`/`REFACTOR`/`DEPRECATED` and the softer `NOTE`/`WORKAROUND`/`TEMP`/`BUG` family. Second, **deprecation decorators and dead-code** patterns. Third, **manifest version pins** in `pyproject.toml`. Fourth, **pattern-level smells** the reviewer read the source to confirm — god-modules, duplicated SQL, placeholder tests, missing colocated tests, and copy-pasted scripts.

The headline finding shapes everything below: **this codebase contains zero conventional debt markers.** There is no `TODO`, `FIXME`, `HACK`, or `XXX` anywhere in scope. The team's convention is descriptive prose comments and a single justified `# noqa`, not debt tags. The register is therefore driven by structural smells rather than annotations, and the Explicit markers section is short by necessity, not by omission.

**Remediation update (`3fe2630`, `f461390`):** the six highest-consequence structural items in the original register are now resolved and marked `RESOLVED` below, kept in place so the history stays legible. The god-module was split into an aggregate-per-module `infrastructure/repositories/` package; the seven-way CAS duplication collapsed into one shared `cas_update` helper; the placeholder skeleton tests were deleted and replaced with real unit, contract, and acceptance tiers; and the empty acceptance tier now carries a live smoke test.

**Delta update (`19fd0da`, ADR-0014):** the routing-policy + coordination-thrash changeset added three items to the open register without disturbing the resolved ones. It also fixed a latent partial-commit trap by moving the `DomainError` catch outside `async with uow.begin()` in the command base (`src/sdlc_blackboard/application/use_cases/base.py:47`), so failed commands now roll back cleanly and record a best-effort ledger row in a second short transaction. What remains open is demo/ops scaffolding duplication, one long service method, two error-handling idioms in scripts, the version pins, an N+1 binding-fetch on the review/approval read path, the routing table's Python/Lean lockstep burden, and the per-goal gate serialization ceiling.

Category vocabulary is closed: `marker`, `wrong abstraction`, `error handling`, `dead code adjacent`, `deprecated pattern`, `version pin`, `duplicated logic`, `missing tests`. Cost is `S` / `M` / `L`. Rank reflects `cost-to-fix × consequence-of-leaving` — a cheap fix guarding a real hazard outranks an expensive fix for a cosmetic one.

## Ranked register

| Rank | Debt item | Category | Cost to fix | Citation |
|------|-----------|----------|-------------|----------|
| 1 | ~~Placeholder skeleton tests still ship as `assert True` in three tiers, advertising coverage that does not exist~~ **RESOLVED (`3fe2630`)** — the three `test_skeleton.py` files were deleted and replaced with real tiers | missing tests | S | `tests/unit/test_use_case_services.py:1`, `tests/contract/test_wire_contracts.py:1` |
| 2 | ~~No application use-case module has a colocated unit test; services are exercised only indirectly through the MCP integration suite~~ **RESOLVED (`3fe2630`)** — `test_use_case_services.py` exercises every service over fake ports | missing tests | L | `src/sdlc_blackboard/application/use_cases/task_service.py:54`, `tests/unit/test_use_case_services.py:1` |
| 3 | ~~`repositories.py` is a 959-LOC god-module holding 12 repository classes in one file~~ **RESOLVED (`3fe2630`, ADR-0013)** — split into an aggregate-per-module `infrastructure/repositories/` package | wrong abstraction | L | `src/sdlc_blackboard/infrastructure/repositories/__init__.py:1`, `src/sdlc_blackboard/infrastructure/repositories/idempotency.py:20` |
| 4 | ~~Optimistic-CAS UPDATE (`set state=…, version=version+1, updated_at=now()`) hand-repeated 7 times across repositories instead of one shared helper~~ **RESOLVED (`3fe2630`)** — collapsed into the shared `cas_update` helper | duplicated logic | M | `src/sdlc_blackboard/infrastructure/repositories/_common.py:34`, `src/sdlc_blackboard/infrastructure/repositories/goals.py:83` |
| 5 | The two live-run scripts share 126 identical lines including duplicated `cmd()` and `_val()` helpers | duplicated logic | M | `scripts/live_lead_create_goal.py:26`, `scripts/live_resort_create_goal.py:29` |
| 6 | `append_domain_event(...)` called with the same multi-kwarg shape at every service call site (9 direct calls plus 11 `_task_event` wrapper calls) | duplicated logic | M | `src/sdlc_blackboard/application/use_cases/review_service.py:67`, `src/sdlc_blackboard/application/use_cases/goal_service.py:39`, `src/sdlc_blackboard/application/events.py:20` |
| 7 | ~~`tests/acceptance/` tier is declared but empty — a 0-byte `__init__.py` and no test modules~~ **RESOLVED (`3fe2630`)** — tier now carries an opt-in live smoke test | missing tests | S | `tests/acceptance/test_live_omnigent_smoke.py:1` |
| 8 | `submit_task_result` is a ~100-line service method bundling revision insert, run/assignment completion, transition, and review creation | wrong abstraction | M | `src/sdlc_blackboard/application/use_cases/task_service.py:206` |
| 9 | `ReviewRepository`/`ApprovalRepository` `list_for_goal` issue one extra per-row `SELECT` for artifact bindings — an N+1 read on the goal-snapshot path (deferred, documented) | duplicated logic | M | `src/sdlc_blackboard/infrastructure/repositories/quality.py:193`, `src/sdlc_blackboard/infrastructure/repositories/quality.py:270` |
| 10 | Routing policy table is maintained in two places — Python `ROUTING_POLICY` and the Lean `routingPolicy` — with a hand-transcribed contract test as the only drift guard; both must move in lockstep | duplicated logic | M | `src/sdlc_blackboard/domain/routing.py:32`, `formal/Blackboard/Routing.lean:60`, `tests/contract/test_routing_policy.py:22` |
| 11 | `validate_team.py` catches broad `except Exception` and continues, masking parse-error types behind a printed string | error handling | S | `scripts/validate_team.py:47` |
| 12 | Live scripts swallow-then-reraise `SystemExit`, an idiom that only prints and adds no handling value | error handling | S | `scripts/live_lead_create_goal.py:174`, `scripts/live_resort_create_goal.py:187` |
| 13 | Gate authorization takes a per-goal `FOR UPDATE` lock, serializing all gate-input commits for one goal — a documented throughput ceiling, not a bug (ADR-0012) | wrong abstraction | L | `src/sdlc_blackboard/application/use_cases/goal_service.py:67` |
| 14 | Direct dependency pins are current but capped tight (`fastmcp>=3.4.4,<4`, `pydantic>=2.11,<3`); upper bounds will need periodic bumps | version pin | S | `pyproject.toml:8` |
| 15 | Pinned to a pre-release Python floor (`requires-python = ">=3.14,<3.15"`), narrowing the runnable interpreter set | version pin | M | `pyproject.toml:6`, `pyproject.toml:76` |

## Explicit markers

A full case-sensitive and case-insensitive grep for `TODO`, `FIXME`, `HACK`, `XXX`, `REFACTOR`, `DEPRECATED`, plus the softer `NOTE`/`WORKAROUND`/`TEMP`/`KLUDGE`/`BUG` family, over `src/`, `tests/`, `scripts/`, and `sdlc_team/` returned **no matches**. There are no conventional debt markers in the codebase. The only in-code suppression annotation is a single, justified linter waiver:

- `# noqa: S603` — `src/sdlc_blackboard/infrastructure/migrations.py:42` (paired with a two-line comment explaining every argument is a trusted constant or normalized DSN, so it is documentation rather than latent debt).

The absence of markers is itself a finding: any known-incomplete work is tracked outside the source (handoff docs / ADR references appear in comments such as `commands.py` and `.env.example`), so a reviewer cannot rely on grepping the tree to find deferred work.

## Pattern-level smells

### God-module: one file owns all persistence — RESOLVED (`3fe2630`, ADR-0013)

**This smell is fixed.** The former flat `infrastructure/repositories.py` (959 lines, 12 repository classes plus 12 row-mapper functions) was split into an aggregate-per-module `infrastructure/repositories/` package. The `__init__.py` re-exports every public repository, so `from sdlc_blackboard.infrastructure.repositories import X` still works while each aggregate's storage now lives in its own cohesive module. The historical concern was that any change to one aggregate's storage forced a reader to load the entire module, which was more than twice the size of the next-largest source file.

Now split across:
- `src/sdlc_blackboard/infrastructure/repositories/goals.py:36` (`GoalRepository`)
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py:65` (`TaskRepository`, plus `AssignmentRepository` and `RuntimeRunRepository`)
- `src/sdlc_blackboard/infrastructure/repositories/artifacts.py:42` (`ArtifactRepository`)
- `src/sdlc_blackboard/infrastructure/repositories/quality.py:80` (`FindingRepository`, `ReviewRepository`, `ApprovalRepository`)
- `src/sdlc_blackboard/infrastructure/repositories/events_outbox.py:45` (`EventRepository`, `OutboxRepository`)
- `src/sdlc_blackboard/infrastructure/repositories/idempotency.py:20` (`ProcessedCommandRepository`)

Cost was L — the split touched every importer and the DI wiring, but each piece was mechanically separable, and the `__init__.py` re-export shim preserved the import surface.

### Duplicated optimistic-concurrency SQL — RESOLVED (`3fe2630`)

**This smell is fixed.** The compare-and-swap UPDATE idiom (`set … version = version + 1, updated_at = now() where … and version = $expected returning *`) was hand-written seven times across the repositories; it is now factored into the shared `cas_update` helper in `_common.py`, which each version-guarded transition calls. The historical hazard was that a future edit (say, adding an audit column) had to be applied identically to every copy, and a missed copy silently diverged the CAS semantics for one aggregate. The two genuinely different shapes (task bulk refresh-ready with no version guard; artifact alias promote with an expected-token guard) deliberately stay verbatim, each with a comment pointing at the `cas_update` docstring.

Now consolidated in:
- `src/sdlc_blackboard/infrastructure/repositories/_common.py:34` (the shared `cas_update` helper)
- `src/sdlc_blackboard/infrastructure/repositories/goals.py:83` (goal `set_state_cas` calls it)
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py:149` (task claim calls it)
- `src/sdlc_blackboard/infrastructure/repositories/quality.py:112` (finding `set_state_cas` calls it)
- `src/sdlc_blackboard/infrastructure/repositories/tasks.py:125` (bulk refresh-ready — deliberately NOT the helper)

Cost was M — a shared helper was straightforward, but each call has slightly different column sets and enum-value handling, so extraction needed care.

### Placeholder and missing tests behind a full-looking taxonomy — RESOLVED (`3fe2630`)

**This smell is fixed.** The `assert True` skeleton markers in the `unit`, `contract`, and `integration` tiers were deleted, and the empty acceptance tier now carries a live smoke test. The application service layer — the code that owns all domain orchestration — was previously reached only transitively through the MCP integration suite; it now has a dedicated unit suite that exercises every service over fake ports (`test_use_case_services.py`), plus an import-only wire-contract totality tier. The historical hazard was that a service-logic regression the MCP shape happened to tolerate would go uncaught. The suite now stands at 335 passed / 2 skipped.

Now covered by:
- `tests/unit/test_use_case_services.py:1` (per-service unit tests over fake ports)
- `tests/contract/test_wire_contracts.py:1` (import-only wire-contract totality)
- `tests/acceptance/test_live_omnigent_smoke.py:1` (opt-in live smoke against a running server)
- `src/sdlc_blackboard/application/use_cases/goal_service.py:24` (`GoalService`, now unit-tested)
- `src/sdlc_blackboard/application/use_cases/query_service.py:20` (`QueryService`, now unit-tested)

Cost was L — writing real per-service unit tests was substantial, though deleting the skeletons was trivial.

### Copy-pasted live-run scripts

`scripts/live_lead_create_goal.py` and `scripts/live_resort_create_goal.py` share 126 identical lines when sorted, including verbatim copies of the `cmd()` command-context factory and the `_val()` result-unwrapping helper. The two scripts differ only in the goal/task payloads they build; the plumbing is duplicated wholesale. A fix to the MCP-client call convention must be applied to both.

Shows up in:
- `scripts/live_lead_create_goal.py:26` (`cmd`) vs `scripts/live_resort_create_goal.py:29`
- `scripts/live_lead_create_goal.py:30` (`_val`) vs `scripts/live_resort_create_goal.py:33`
- `scripts/live_lead_create_goal.py:38` (`main`) vs `scripts/live_resort_create_goal.py:41`

Cost: M — a shared helper module for these operator scripts is easy, but they are demo/ops scaffolding, so the payoff is lower than for kernel code.

### Repeated event-append boilerplate at every service call site

`append_domain_event(...)` is called directly 9 times across the service bodies (plus 11 more indirect calls through the `_task_event` wrapper in `task_service`), each with the same eight-to-nine keyword arguments (`event_type`, `aggregate_type`, `aggregate_id`, `aggregate_version`, `goal_id`, `task_id`, `context`, `payload`). The shape is identical enough that a per-aggregate wrapper would remove the visual noise and the risk of passing a mismatched `aggregate_type`/`aggregate_id` pair. *judgment-call* — this is close to necessary boilerplate for an event-sourced write path, but flagged because the argument list is long enough that a transposition would type-check yet be wrong.

Shows up in:
- `src/sdlc_blackboard/application/use_cases/goal_service.py:39` (2 call sites in this module)
- `src/sdlc_blackboard/application/use_cases/review_service.py:67` (4 call sites in this module)
- `src/sdlc_blackboard/application/use_cases/artifact_service.py:64` (3 call sites)
- `src/sdlc_blackboard/application/use_cases/task_service.py:426` (`_task_event` wrapper — the partial remedy already exists here, called 11 times)

Cost: M — extend the `_task_event`-style wrapper pattern to the other aggregates; low risk, moderate reach.

## See also

- [impact-analysis](../insights/impact-analysis.md) — 14 shared source citations
- [processes](../behavior/processes.md) — 11 shared source citations
- [business-logic](../insights/business-logic.md) — 10 shared source citations
- [contract-map](../insights/contract-map.md) — 10 shared source citations
- [module-map](../architecture/module-map.md) — 7 shared source citations
