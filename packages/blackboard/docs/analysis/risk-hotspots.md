# omnigent-blackboard-poc · Risk hotspots

Risk here is composed as an activity-and-size score, because the two configured static analyzers report zero findings: `uv run ruff check src tests scripts` returns `All checks passed!` against the config at `pyproject.toml:50`, and `uv run pyright --outputjson` reports `errorCount=0, warningCount=0` across 85 files against `pyproject.toml:72`. A grep for `TODO`/`FIXME`/`HACK`/`XXX` across `src`, `tests`, and `scripts` also returns nothing, so the last-resort marker signal is null too. The working tree is now clean — the earlier in-flight churn has all been committed — so the earlier uncommitted-churn and untracked-file terms drop out and the score reduces to `committed_commits + LOC/500`, blending committed churn with file size as a complexity proxy.

Two limitations shape this ranking. First, git history is short: 27 commits spanning 2026-07-18 to 2026-07-20, so the 30-day activity window equals the entire history, and the committed-commit signal separates files only weakly (91 non-demo `.py` files, median 1 commit, max 8). The trend arrow marks a file `↑ rising` only when its commit count clears the median + 1σ threshold of 2.21 (i.e. ≥3 commits); everything in the top 12 clears it, so every ranked row is `↑ rising`, and no file is `↓ falling` in a repo this young. Second, the largest file in the tree, `HANDOFF.md` (3982 LOC, one inception commit), is excluded from the ranking: it has had no post-inception activity, so an activity-led risk score would rank it on size alone despite carrying no change risk. The `Open findings` column is retained but reads `0 warn, 0 error` for every row, since both analyzers pass clean. Ownership is now split across two committers (`Bonk` and `bgagent`), so the top-owner share varies per file. `demo_app/` is excluded entirely as the target application, not kernel code.

| File | Trend | Open findings | Top owner | Citation |
|---|---|---|---|---|
| README.md | ↑ rising | 0 warn, 0 error | bgagent (56%) | `README.md` (434 LOC) |
| asyncpg repository adapters | ↑ rising | 0 warn, 0 error | bgagent (67%) | `src/sdlc_blackboard/infrastructure/repositories/` (package, 1286 LOC across 9 modules) |
| Application ports (hexagonal seam) | ↑ rising | 0 warn, 0 error | Bonk / bgagent (50%) | `src/sdlc_blackboard/application/ports.py` (277 LOC) |
| Project + tool config | ↑ rising | 0 warn, 0 error | Bonk (62%) | `pyproject.toml` (80 LOC) |
| Task use-case service | ↑ rising | 0 warn, 0 error | bgagent (60%) | `src/sdlc_blackboard/application/use_cases/task_service.py` (440 LOC) |
| Operator CLI | ↑ rising | 0 warn, 0 error | bgagent (80%) | `src/sdlc_blackboard/interfaces/cli.py` (178 LOC) |
| Use-case service unit tests | ↑ rising | 0 warn, 0 error | bgagent (100%) | `tests/unit/test_use_case_services.py` (1117 LOC) |
| Reliability-invariant integration tests | ↑ rising | 0 warn, 0 error | Bonk (75%) | `tests/integration/test_reliability_invariants.py` (278 LOC) |
| Domain common types | ↑ rising | 0 warn, 0 error | Bonk (75%) | `src/sdlc_blackboard/domain/common.py` (126 LOC) |
| Command-service base | ↑ rising | 0 warn, 0 error | bgagent (75%) | `src/sdlc_blackboard/application/use_cases/base.py` (102 LOC) |
| Idempotency store | ↑ rising | 0 warn, 0 error | Bonk / bgagent (50%) | `src/sdlc_blackboard/application/idempotency.py` (92 LOC) |
| Fake ports for tests | ↑ rising | 0 warn, 0 error | bgagent (100%) | `tests/unit/fakes.py` (557 LOC) |

## Per-file drill-down

### README.md

What's there: the single operator-facing document for the PoC — architecture overview, setup, validation, the scripted-demo and MCP-server run paths, the live-team run path, and 14 Architecture Decision Records (`README.md:1`, `README.md:127`). It is the highest-churn artifact because it tracks every kernel and team decision as the design settles; the most recent entry, ADR-0014, records the Lean-pinned routing default plus the failure ledger and derived thrash report (`README.md:372`).

Recent activity: 9 committed revisions, the most of any file in the repo, clearing the rising threshold comfortably. Trend `↑ rising`.

Owners: split between `bgagent` (5 of 9 commits, 56%) and `Bonk` (4 of 9), reflecting the two committers who have touched the repo (`README.md:1`).

Findings: 0 warn, 0 error. README is Markdown and outside the ruff/pyright analysis set defined at `pyproject.toml:50` and `pyproject.toml:72`; no marker findings apply.

### asyncpg repository adapters (`repositories/` package)

What's there: the hand-written asyncpg adapters that implement the application ports by shape — no ORM, explicit SQL for the compare-and-set transitions, partial unique constraints, and `FOR UPDATE` locks that carry the kernel's correctness guarantees. The former flat module was split into an aggregate-per-module package (ADR-0013) — 1286 LOC across 9 modules including the shared `cas_update` helper (`src/sdlc_blackboard/infrastructure/repositories/_common.py:34`) and the new append-only command-failure ledger repo (`src/sdlc_blackboard/infrastructure/repositories/failures.py:1`) — spanning `GoalRepository`, `TaskRepository`, `AssignmentRepository`, and `RuntimeRunRepository` (`src/sdlc_blackboard/infrastructure/repositories/goals.py:36`, `src/sdlc_blackboard/infrastructure/repositories/tasks.py:65`).

Recent activity: 6 commits across the package plus its predecessor module; it ranks on aggregate size and its position on every write path as much as on raw churn. Trend `↑ rising`.

Owners: `bgagent` at 4 of 6 commits (67%), `Bonk` at 2.

Findings: 0 warn, 0 error under both analyzers (`pyproject.toml:50`, `pyproject.toml:72`).

### Application ports (`ports.py`)

What's there: the load-bearing hexagonal-architecture seam — every external capability is a `@runtime_checkable Protocol` defined next to its application-layer consumers, not its infrastructure implementers (`src/sdlc_blackboard/application/ports.py:1`). It declares the repository and unit-of-work contracts (`Clock`, `UnitOfWork`, `GoalRepo`, `TaskRepo`, and more) from `src/sdlc_blackboard/application/ports.py:39` onward, and the 19fd0da changeset added the `EventRepo.count_by_type` method (`src/sdlc_blackboard/application/ports.py:192`) and the new `CommandFailureRepo` protocol (`src/sdlc_blackboard/application/ports.py:233`).

Recent activity: 8 committed revisions, well over the rising threshold. Its churn reflects the contract evolving as adapters and services are added. Trend `↑ rising`.

Owners: an even split — `Bonk` and `bgagent` at 4 commits each (50% apiece); `bgagent` authored the most recent, so it leads.

Findings: 0 warn, 0 error under both analyzers (`pyproject.toml:72`).

### Project + tool config (`pyproject.toml`)

What's there: project metadata, optional-dependency and script entrypoints, and the toolchain gates — pytest at `pyproject.toml:41`, ruff at `pyproject.toml:50`, ruff lint rules at `pyproject.toml:59`, and pyright at `pyproject.toml:72`. It is the control surface that defines what "clean" means for every other file in this report.

Recent activity: 8 committed revisions, tied for the second-most in the repo. Trend `↑ rising`.

Owners: `Bonk` at 5 of 8 commits (62%), `bgagent` at 3.

Findings: 0 warn, 0 error — this file configures the analyzers rather than being flagged by them (`pyproject.toml:50`).

### Task use-case service (`task_service.py`)

What's there: the task lifecycle use cases — create, refresh-ready, claim-with-fencing, bind, start-run, submit-result, and accept (`src/sdlc_blackboard/application/use_cases/task_service.py:1`), where the handoff §11 reliability invariants live. The `TaskService` class carries the compare-and-set claim path guarded by a partial unique index (`src/sdlc_blackboard/application/use_cases/task_service.py:55`, `src/sdlc_blackboard/application/use_cases/task_service.py:123`), and `start_runtime_run` now defaults the routing class from `default_routing_class(task.required_actor_kind)` when the request carries none (`src/sdlc_blackboard/application/use_cases/task_service.py:179`).

Recent activity: 5 committed revisions. The `accept_task` transaction body that was in-flight at the previous refresh is now committed (`src/sdlc_blackboard/application/use_cases/task_service.py:306`). Trend `↑ rising`.

Owners: `bgagent` at 3 of 5 commits (60%), `Bonk` at 2.

Findings: 0 warn, 0 error under both analyzers (`pyproject.toml:50`, `pyproject.toml:72`).

## See also

- [impact-analysis](../insights/impact-analysis.md) — 6 shared source citations
- [tech-debt](../insights/tech-debt.md) — 5 shared source citations
- [module-map](../architecture/module-map.md) — 4 shared source citations
- [system-overview](../architecture/system-overview.md) — 4 shared source citations
- [contract-map](../insights/contract-map.md) — 4 shared source citations
