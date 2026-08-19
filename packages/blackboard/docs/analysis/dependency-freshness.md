# omnigent-blackboard-poc · Dependency freshness

Audit date: 2026-07-21. Latest versions verified against PyPI (`https://pypi.org/pypi/<pkg>/json`), GitHub releases, and endoflife.date. Locked versions read from `uv.lock`. `demo_app/` is out of scope per the audit brief.

This refresh follows commit `19fd0da` (landed 2026-07-21), which added a Lean 4 formal-model toolchain but made no Python dependency changes. It also reconciles the table with the runtime-dependency changes that landed earlier in the M-series (`2961996` raised structlog/pytest/pytest-cov to their newest majors and dropped tenacity; `3fe2630` dropped the unused uvicorn + OpenTelemetry deps). The three items the previous audit flagged as "behind-major (blocked by pin)" have since been upgraded and are now current.

Status legend: **current** (locked == latest, or latest within the pinned ceiling and no newer release exists) · **behind-patch** · **behind-minor** · **behind-major** (a newer major exists but is blocked by the pin ceiling in `pyproject.toml`).

## Runtime dependencies

Declared at `pyproject.toml:7-15`.

| Package | Pinned constraint | Locked (uv.lock) | Latest | Status | Notes |
|---|---|---|---|---|---|
| fastmcp | `>=3.4.4,<4` (`pyproject.toml:8`) | 3.4.4 | 3.4.4 | current | Exactly on the newest 3.x release (2026-07-09). Pin floor equals latest. |
| pydantic | `>=2.11,<3` (`pyproject.toml:9`) | 2.13.4 | 2.13.4 | current | On latest 2.x. |
| pydantic-settings | `>=2.10,<3` (`pyproject.toml:10`) | 2.14.2 | 2.14.2 | current | On latest 2.x. |
| asyncpg | `>=0.30,<1` (`pyproject.toml:11`) | 0.31.0 | 0.31.0 | current | On latest. |
| structlog | `>=26.1,<27` (`pyproject.toml:12`) | 26.1.0 | 26.1.0 | current | Upgraded from the 25.x line in `2961996` and wired into logging config. structlog is CalVer — 26.x is release-year 2026, not a semantic breaking major. On latest. |
| orjson | `>=3.10,<4` (`pyproject.toml:13`) | 3.11.9 | 3.11.9 | current | On latest 3.x. |
| typer | `>=0.16,<1` (`pyproject.toml:14`) | 0.27.0 | 0.27.0 | current | On latest (2026-07-15). |

Runtime summary: 7 of 7 current. uvicorn, opentelemetry-api, and opentelemetry-sdk were removed as unused in `3fe2630`; tenacity was removed in `2961996`. None remain in `pyproject.toml` or `uv.lock`.

## Dev dependencies

Declared at `pyproject.toml:18-29`.

| Package | Pinned constraint | Locked (uv.lock) | Latest | Status | Notes |
|---|---|---|---|---|---|
| pytest | `>=9.1,<10` (`pyproject.toml:19`) | 9.1.1 | 9.1.1 | current | Upgraded from `<9` in `2961996`. pytest 9.0 (2025-11-08) turned `PytestRemovedIn9Warning` deprecations into errors; 9.1 removed the deprecated `fspath` Node ctor param and `importorskip` ImportError behavior. Suite is clean on 9.1.1. |
| pytest-asyncio | `>=1.4,<2` (`pyproject.toml:20`) | 1.4.0 | 1.4.0 | current | On latest 1.x. |
| pytest-xdist | `>=3.8,<4` (`pyproject.toml:21`) | 3.8.0 | 3.8.0 | current | On latest 3.x. |
| pytest-cov | `>=7.1,<8` (`pyproject.toml:22`) | 7.1.0 | 7.1.0 | current | Upgraded from `<7` in `2961996`. pytest-cov 7.0 (2025-09-09) dropped `.pth`-based subprocess coverage and requires coverage >= 7.10.6. No `--cov` in `addopts` (`pyproject.toml:45`) and no subprocess-coverage setup, so the migration was low-impact. |
| ruff | `>=0.12,<1` (`pyproject.toml:23`) | 0.15.22 | 0.15.22 | current | On latest (2026-07-16). |
| pyright | `>=1.1.403,<2` (`pyproject.toml:24`) | 1.1.411 | 1.1.411 | current | On latest (2026-06-25). |
| hypothesis | `>=6.100,<7` (`pyproject.toml:25`) | 6.157.1 | 6.157.2 | behind-patch | 6.157.2 released 2026-07-21 (audit day). Within the `<7` cap; a plain `uv lock --upgrade-package hypothesis` picks it up. |
| testcontainers[postgres] | `>=4.8,<5` (`pyproject.toml:26`) | 4.14.2 | 4.14.2 | current | On latest 4.x. |
| asyncpg-stubs | `>=0.31,<0.32` (`pyproject.toml:28`) | 0.31.3 | 0.31.3 | current | On latest; version-matched to asyncpg 0.31 by design (`pyproject.toml:27`). |

Dev summary: 8 of 9 current. hypothesis is one patch behind (trivially fixable). The pytest and pytest-cov major-version ceilings the previous audit flagged were raised in `2961996` and are now current.

## Build system

| Package | Pinned constraint | Locked (uv.lock) | Latest | Status | Notes |
|---|---|---|---|---|---|
| hatchling | `>=1.27` (`pyproject.toml:35`) | not in lock | 1.31.0 | current | Build backend; unbounded floor, resolves to latest (1.31.0, 2026-07-08) at build time. Not pinned into `uv.lock` because it is a PEP 517 build requirement, not a project dependency. |

## Toolchain

Non-PyPI toolchain dependencies managed outside the Python resolver.

| Tool | Pinned constraint | Resolved | Latest | Status | Notes |
|---|---|---|---|---|---|
| Lean 4 | `leanprover/lean4:v4.32.0` (`formal/lean-toolchain:1`) | v4.32.0 | v4.32.0 | current | Elan/lake toolchain for the formal kernel model. v4.32.0 is the newest **stable** Lean 4 release (2026-07-13). v4.33.0-rc1 exists (2026-07-15) but is a prerelease, so the pin is on the current stable. Kernel-checked via `mise run formal` → `cd formal && lake build` (`mise.toml:68-70`). |
| lake | bundled with toolchain (`formal/lake-manifest.json:1`) | 1.2.0 | 1.2.0 | current | Lean's build tool; ships inside the pinned `v4.32.0` toolchain rather than being independently pinned. Manifest records `version: 1.2.0` with no external Lean package deps. |

## Tools (mise)

Declared at `mise.toml:4-8`. All use `latest` except Python.

| Tool | Pinned constraint | Resolved | Latest | Status | Notes |
|---|---|---|---|---|---|
| python | `3.14` (`mise.toml:5`) | 3.14.x | 3.14.6 | current | `requires-python = ">=3.14,<3.15"` (`pyproject.toml:6`); pyright targets 3.14 (`pyproject.toml:76`). Latest 3.14 patch is 3.14.6 (2026-06-10). |
| uv | `latest` (`mise.toml:6`) | latest | — | current | Always resolves to newest on install. |
| ruff | `latest` (`mise.toml:7`) | latest | 0.15.22 | current | mise pulls newest; note the dev-dep pin (`<1`) governs the `uv run ruff` used by `mise run lint`. |
| dbmate | `latest` (`mise.toml:8`) | latest | 2.34.1 | current | Migration runner; newest GitHub release is v2.34.1 (2026-07-09). |

## Omnigent findings

**Provenance correction (load-bearing): Omnigent is not an Amazon-internal package.** Despite the repo name, `omnigent` is **Databricks' open-source project `omnigent-ai/omnigent`** (Apache-2.0), published on **public PyPI**. There is no internal Brazil/CodeArtifact package named Omnigent: `code.amazon.com/packages/Omnigent` 404s and an internal code search for "omnigent" returns zero hits across Amazon repos. It surfaces internally only as competitive-analysis material inside the `StrandsSlice` repo, which did a source-read of Omnigent as the closest public analog to that team's own meta-harness. This repo's `uvx --from "omnigent==0.5.1"` pin (`mise.toml:92`) resolves straight from public PyPI, consistent with that.

Omnigent is a **"meta-harness"** — a common orchestration layer over multiple coding agents (Claude Code, Codex, Cursor, and custom YAML-defined agents) with a swappable-harness design, an "Omnibox" OS sandbox abstraction (`linux_bwrap` / `darwin_seatbelt` / `windows_jobobject` behind one `SandboxBackend` contract), a stateful ALLOW/ASK/DENY policy engine, an L7 TLS-MITM egress proxy, and a host+runner split that runs the same agent locally or in cloud sandboxes. Its docs frame the sandbox+policy as "governance, not a hard security boundary."

It is **not** a project dependency here — the PoC talks to it over HTTP (the MCP blackboard server) and never imports it (`mise.toml:22-24`). `mise run team` launches it in an isolated `uvx --from "omnigent==$OMNIGENT_VERSION"` env (`mise.toml:92`), and each agent config declares `executor.type: omnigent` with `harness: claude-sdk` (`sdlc_team/config.yaml:13-17`).

- **Latest released version: 0.5.1**, published to **public PyPI** on **2026-07-10** (verified via `https://pypi.org/pypi/omnigent/json` — `info.version = 0.5.1`; corroborated by the GitHub tag `v0.5.1`). This matches the repo's pin exactly: `OMNIGENT_VERSION` defaults to `0.5.1` (`mise.toml:25`), and the in-repo comment states "0.5.1 is the latest release" (`mise.toml:20-21`). The pin is current — there is no newer published release to move to. 0.5.0 shipped the same day (~2h earlier); 0.5.1 is a desktop-only bug-fix patch. Public cadence: 0.1.0 (2026-06-13) through 0.5.1 (2026-07-10), roughly weekly.
- **0.6.0 line:** does **not exist publicly** — PyPI 404s for both `0.6.0` and `0.6.0.dev0`. The `0.6.0.dev0` this repo references is the **local editable fork's** dev-version marker (`~/workplace/omnigent`, per `mise.toml:20-21`, `mise.toml:89`), not a Databricks release. Because it is not on PyPI, `uvx` cannot resolve it and it can never shadow the pinned 0.5.1 (`mise.toml:88-89`). The fork writes a newer SQLite session-store schema (repo notes migration revision `d7f1a2b3c4e5`) that 0.5.1 cannot migrate, which is why the repo isolates data dirs via `OMNIGENT_DATA_DIR` (`mise.toml:26-30`). Upstream's next release is an unnumbered `[Unreleased]` CHANGELOG section — no `0.6.0` staged.
- **Relevance to this repo:** the config surface the repo depends on — `spec_version: 1`, `executor.type: omnigent` + nested `config.harness: claude-sdk` (nested, not a flat `executor.harness`, per README ADR-0007), generic inline MCP servers (`type: mcp`, `url`, HTTP transport), `os_env.sandbox.type` of `none`/`linux_bwrap`, and `spawn_bounds` policies at `omnigent.inner.nessie.policies.spawn_bounds` (`sdlc_team/config.yaml:31-88`) — all match 0.5.x's stable API. In 0.5.0, runner MCP servers became shared across matching agent specs and start lazily, and the `intent_gate` policy shifted from hard `DENY` to `ASK`. README ADR-0004/ADR-0010 record that 0.5.1 has no first-class "blackboard" concept (any `tools:` entry with `type: mcp` becomes a generic `MCPServerConfig`), the contract the whole team relies on (`sdlc_team/config.yaml:35-37`).

Sources: public PyPI `omnigent/json`, GitHub `omnigent-ai/omnigent` tags + CHANGELOG (v0.4.0 / v0.5.0 release notes), and internal StrandsSlice Omnigent deep-dive explorations (`code.amazon.com/packages/StrandsSlice`).

Bottom line: the omnigent pin (0.5.1) is on the newest published release. Do not bump to `0.6.0.dev0` — it is an unpublished local fork with an incompatible session-store schema and would downgrade shared deps (e.g. `websockets<15`, per `mise.toml:22-24`).

## Recommended actions

Everything within its pin ceiling is current except one patch. The three optional major upgrades the previous audit flagged (structlog 26, pytest 9, pytest-cov 7) have since been applied and re-locked; no ceiling remains capped below its latest major.

**Trivial (no breaking change) — do now:**

```bash
# hypothesis 6.157.1 -> 6.157.2 (patch, inside the <7 cap)
uv lock --upgrade-package hypothesis
```

**Toolchain:** no action. Lean is pinned to `v4.32.0` (`formal/lean-toolchain:1`), the newest stable release. `v4.33.0-rc1` is a prerelease — revisit when `v4.33.0` promotes to stable, then re-run `mise run formal` (`mise.toml:68-70`) to prove the Lean model still kernel-checks.

**Omnigent:** no action. `OMNIGENT_VERSION=0.5.1` (`mise.toml:25`) is the latest published release. Revisit only when a `0.6.0` (or later) lands on PyPI, then re-run `mise run team:validate` to prove the team still parses (`mise.toml:94-96`).

After any upgrade, run the full gate: `mise run check` (`mise.toml:72-74`).

## See also

- [system-overview](../architecture/system-overview.md) — 2 shared source citations
