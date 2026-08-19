# omnigent-blackboard-poc · Documentation

Prose is generated; structure is mechanical. Cross-references are deterministic.

Generated 2026-07-20, refreshed 2026-07-21 at commit `19fd0da`, from the kernel source (`src/sdlc_blackboard/`, `tests/`, `scripts/`, `sdlc_team/`, `formal/`). `demo_app/` is the target application the SDLC agent team builds and is excluded from this documentation tree.

## Architecture — what the system is and how the pieces fit

- [System overview](architecture/system-overview.md) — narrative, stack table, top-level flow diagram
- [Module map](architecture/module-map.md) — the 8 modules ranked by dependency weight, with per-file inventories
- [Data flow](architecture/data-flow.md) — claim_task, submit_task_result, and get_gate_status traced step by step

## Reference — what you can call

- [Public API](reference/public-api.md) — 30 top exported symbols with verbatim signatures
- [CLI](reference/cli.md) — the `blackboard` Typer app: migrate, list-goals, snapshot, events, gate, outbox-relay, thrash, reset-demo
- [RPC tools](reference/rpc-tools.md) — the 19 FastMCP tools (5 readers, 14 command mutators)

## Behavior — when X happens, what runs

- [Processes](behavior/processes.md) — 8 load-bearing flows in full, 23 minor flows indexed
- [State machines](behavior/state-machines.md) — FindingState, GoalState, RunState, TaskState as stateDiagram-v2

## Analysis — where the sharp edges are

- [Risk hotspots](analysis/risk-hotspots.md) — activity-and-exposure ranking (lint/type findings are zero)
- [Ownership](analysis/ownership.md) — 2 in-scope authors, every folder a single point of failure
- [Dead code](analysis/dead-code.md) — 3 unreferenced exports, 0 dead files, 0 dead imports
- [Dependency freshness](analysis/dependency-freshness.md) — 2026-07-21 audit of every pin vs latest (incl. the Lean 4 toolchain), plus the Omnigent provenance finding

## Diagrams — show, don't tell

- [Components](diagrams/architecture/components.md) — classDiagram of the 8 top components
- [Dependency graph](diagrams/structural/dependency-graph.md) — 9 internal modules, 8 external dependencies
- [Sequences](diagrams/behavioral/sequences.md) — task submission, artifact promotion, gate evaluation

## Insights — what the codebase assumes and what changes if you touch it

- [Impact analysis](insights/impact-analysis.md) — blast radius of the 8 most-imported surfaces
- [Debugging guide](insights/debugging-guide.md) — failure-mode index, error surfaces, first-checks ladder
- [Contract map](insights/contract-map.md) — 12 inter-module contracts with drift risks
- [Business logic](insights/business-logic.md) — 18 validations, 20 invariants, 7 calculations, 11 policies
- [Tech debt](insights/tech-debt.md) — 12-item ranked register (zero TODO/FIXME markers; smell-driven)
