# pneuma · Documentation

`pneuma` is a Python library for building AI agents as ordinary classes: a method's docstring is its prompt, its parameters are the typed inputs, and its return annotation is the typed result. Alongside that it pursues a second concern — safety checks and scoring formulas that always pass and therefore measure nothing. This tree documents both. The generated sections below were written by reading the source and citing it by `path:line`; the design essays and write-ups were written by hand and argue for the decisions the code embodies. If you are new here, start with the system overview, then the module map, then whichever of the two concerns you came for.

Prose is generated; structure is mechanical. Cross-references are deterministic.

## Architecture

- [System overview](architecture/system-overview.md) — what the library is, the premise behind `@ai_method`, and the layer split between kernel, detectors, and case study.
- [Module map](architecture/module-map.md) — every subpackage of `src/pneuma`, ordered library-before-application, with per-file LOC and the classes each module owns.
- [Data flow](architecture/data-flow.md) — three flows traced end to end: the one console script, `ProcessAgent.work`, and the case-study pipeline.

## Reference

- [Public API](reference/public-api.md) — the 30 most-imported symbols, each with its declaration as it appears in source and the module to import it from.
- [CLI](reference/cli.md) — the single `pneuma` command, its flags, and its exit behaviour.

## Behavior

- [Processes](behavior/processes.md) — the library's orchestration entry points and the case study's measurement drivers, each traced from its initiator.
- [State machines](behavior/state-machines.md) — the enumerated outcomes and verdicts the code transitions between, with the transitions that are refused.

## Analysis

- [Risk hotspots](analysis/risk-hotspots.md) — files ranked by a composed score over escalated `ruff` findings and 30-day change activity.
- [Dead code](analysis/dead-code.md) — the pre-deletion audit of unreferenced exports, with a resolution note recording which rows were removed and which was kept on purpose.

## Diagrams

- [Components](diagrams/architecture/components.md) — class diagram of the kernel types and the relations between them.
- [Sequences](diagrams/behavioral/sequences.md) — three processes as sequence diagrams, every lifeline and message mapped back to a source citation.
- [Dependency graph](diagrams/structural/dependency-graph.md) — the ten internal modules and the seven external packages they import.

## Insights

- [Contract map](insights/contract-map.md) — the types, protocols, and undeclared frame shapes one module produces and another depends on, with the assumptions each consumer makes.
- [Impact analysis](insights/impact-analysis.md) — for the eight highest-fan-in surfaces, who breaks when you change them and how badly.
- [Business logic](insights/business-logic.md) — the domain rules the code enforces: validations, invariants, derived calculations, and policy gates.
- [Debugging guide](insights/debugging-guide.md) — symptom-first routing from a failure you are looking at to the code that produced it.
- [Tech debt](insights/tech-debt.md) — a register assembled over `src/` and `tests/`, each item with its location and what it costs.

## Design essays

Hand-written rationale, one per module. Each states why the module is shaped the way it is and what the more obvious designs would have cost.

- [`method.py`](design/method.md) — why `@ai_method` exists alongside `@ai_function`, and what the object-oriented route costs.
- [`gated.py`](design/gated.md) — why the gate is a post-condition rather than a check the loop runs.
- [`recall.py`](design/recall.md) — why a recalled parameter is declared on the signature and filled at the trace boundary.
- [`team/`](design/team.md) — why the team is a bare core plus a hook library, and why members join a lead as typed tools.
- [`process/agent.py`](design/process_agent.md) — why work inside a state is dispatched from a hook in the interpreter.
- [`detect/discrimination.py`](design/discrimination.md) — why both detectors share one primitive and why its verdict is three-valued.
- [`detect/objective.py`](design/objective.md) — why the objective prober is shaped the way it is.
- [`detect/vacuity.py`](design/vacuity.md) — why relaxation is the mechanism and why every bound is reported.
- [`detect/adversary.py`](design/adversary.md) — why adversarial search sits beside the prober, and how "worthless" is adjudicated without a human.
- [`detect/gaming.py`](design/gaming.md) — why a passing gate and a diverse-looking accepted set are the same defect.
- [`memory/turso_backend.py`](design/turso_backend.md) — why the Turso memory backend is shaped the way it is.
- [`memory/sqlite_backend.py`](design/sqlite_backend.md) — why there is a second, stdlib-`sqlite3` backend over the same contract, and the NULL-distance decision.
- [`casestudy/learning.py`](design/learning.md) — why the navigator's playbook is learned the way it is.
- [`casestudy/minelearn.py`](design/minelearn.md) — why the mining loop learns two parameters rather than one.
- [`casestudy/harnesslearn.py`](design/harnesslearn.md) — why exactly one harness parameter is learnable and the other five are not.
- [`casestudy/toolkit.py`](design/toolkit.md) — why the miner's seed toolkit is real functions rather than a string literal.
- [`demo/incident.py`](design/incident.md) — the synthetic four-plane incident dataset and its machine-checked information asymmetry.

## Write-ups

- [Case study](case-study.md) — automating a Dutch municipality's building-permit process against 1,434 real applications, safely.
- [Verified processes, executed by agents](process.md) — how one mined process becomes a typed artifact that two verifiers check and a third runs.
