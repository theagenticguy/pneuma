---
title: Restart-chain answer loops, verdict parsing tiers, and the library-side DB driver
category: ai-functions-runtime
session: session-fc7f24
date: 2026-08-11
tags: [team, answer-loop, verdict, turso, libsql, boundary, contract-tests]
modules: [team/core.py, team/hooks/review.py, team/hooks/trajectory.py, tests/library/test_boundary.py]
---

# Lessons

1. **A sequential per-hook review loop ships unreviewed answers.** When each
   `on_answer` hook drives its own loop, a later hook's `Revise` mutates the
   answer after earlier hooks accepted it. The fix is a restart chain with
   budgets held in a dict OUTSIDE the walk (`rounds[label]` persists across
   restarts) and a once-per-hook `revise_cap` record guarded by a set.
   Termination argument: every restart increments some hook's spend; label
   collisions between same-class hooks would merge budgets, so suffix `#N`
   only on collision — single instances keep clean labels and existing
   transcript assertions survive.

2. **Verdict tokens need a two-tier parse, and the fixtures were relying on
   the bug.** Tier 1: token alone (whole text / own line / trailing [.?!]).
   Tier 2: pydantic field-value rendering (`=\s*'TOKEN'`). Mid-prose mention
   is a mention. When we hardened this, prose-wrapped approvals in OTHER
   files' fixtures ("looks right to me, APPROVED") started failing to
   approve — fixtures had encoded the containment defect. Grep for the token
   across all test fixtures when changing verdict parsing.

3. **The library side's SQL driver is `turso`, not `libsql`.**
   tests/library/test_boundary.py enforces polars/libsql/pm4py as
   application-only, and membership is DERIVED from the source tree, so a new
   library module importing `libsql` fails ~16 boundary tests at collection
   distance from the cause. `memory/turso_backend.py::connect` is the
   library-side connection exemplar (WAL + synchronous=NORMAL; no
   foreign_keys pragma). The two drivers' APIs are compatible enough that the
   swap is import-line + type annotations only.

4. **Docstring line-pins become contract tests cheaply and pay immediately.**
   Executable pins (config_hook one-per-cycle + tools-replace + key-pop,
   notify-no-cycle, ThreadKwargs keys, MemoryBackend privates, close
   idempotence) took one file. While writing them we found the pins
   themselves were stale by one line (548-553 vs 548-554) — the prose had
   already drifted while the semantics held.

5. **Subtree assertions cannot run post-run.** Team.run's unwind empties the
   registry, so parent-chain claims must snapshot `coordinator.list_threads()`
   MID-RUN from an inner hook's `on_assemble`. `ThreadInfo.parent_id` is
   public surface; no private `_infos` access needed.
