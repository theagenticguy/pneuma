# pneuma workspace

A uv workspace holding two packages that solve two halves of one problem: getting a team of
AI agents to produce work you can actually check.

| Package | Distribution | What it is |
|---|---|---|
| [`packages/pneuma`](packages/pneuma/) | `pneuma` | AI agents as ordinary Python classes (docstring = prompt, params = typed inputs), plus `detect/` probes that test whether a safety check or scoring formula actually checks anything. |
| [`packages/blackboard`](packages/blackboard/) | `pneuma-blackboard` | A transactional PostgreSQL blackboard kernel — the authoritative organizational state (goals, task contracts, assignments with fencing, reviews, the release gate) — behind a thin FastMCP adapter. |

The two are siblings, not layers: neither imports the other, and pneuma's boundary test
enforces that mechanically rather than by convention. They meet at a design seam documented
in [`docs/design/org-plane.md`](docs/design/org-plane.md) — pneuma owns *content* (artifact
revisions, branches, merges) and *execution* (who runs, with what tools); the blackboard owns
*organization* (who was supposed to produce the work, and whether it was reviewed). Each
plane references the others by id and never reaches into their semantics.

## Layout

```
pyproject.toml          # workspace root: [tool.uv.workspace] members, no [project]
mise.toml               # the single front door — `mise run check` is the definition of done
uv.lock                 # ONE lockfile for the whole workspace
packages/
  pneuma/               # src/pneuma/, tests/{library,app}/, data/, docs/, tools/
  blackboard/           # src/sdlc_blackboard/{domain,application,infrastructure,interfaces}/
                        # tests/{unit,contract,property,integration,e2e}/, migrations/,
                        # scripts/, sdlc_team/, formal/ (Lean), compose.yaml
docs/
  design/org-plane.md   # repo-level: the seam BETWEEN the two packages
  formal/               # repo-level: TLA+ models of all three planes + the symspec corpus
```

Docs sit at whichever level their subject spans. `docs/design/org-plane.md` and `docs/formal/`
are repo-level because they describe both packages at once — `OrgPlane.tla` model-checks the
blackboard's task lifecycle joined to pneuma team runs, and `PLANES.md` tabulates all six
record-keeping systems across both. Everything narrower stays with its package: pneuma's
per-module design essays and generated architecture tree in `packages/pneuma/docs/`, the
blackboard's ADRs, runbook, and `HANDOFF.md` in `packages/blackboard/`.

## Quick start

Prerequisites: `mise`, which pins Python 3.14, uv, ruff, and dbmate. One `uv sync` at the
root builds a single `.venv` serving both packages, each installed editable.

```bash
mise run install          # uv sync — both members, dev tooling included
mise run check            # lint both + both test suites (the definition of done)
```

### pneuma

```bash
mise run test:pneuma      # 1077 tests: 1057 pass, 20 skip (live models / optional files)
mise run demo             # live Bedrock war-room run, writes packages/pneuma/artifacts/
uv run pneuma --truth     # print the demo's planted ground truth and exit
```

Tests need no credentials. A live run needs Bedrock access to `global.anthropic.claude-opus-5`
(plus `cohere.embed-v4` for the embedding path). The TLC model-check tests need `java` and
`packages/pneuma/tools/tla2tools.jar`, which is gitignored — copy it in and they run, leave it
out and they skip rather than fail:

```bash
cp ~/bonk-fs/projects/pneuma/tools/tla2tools.jar packages/pneuma/tools/
```

### blackboard

```bash
mise run test:blackboard:unit   # fast inner loop — unit + contract, no Postgres
mise run test:blackboard        # full suite: 335 pass, 2 skip with Docker up
```

The integration and e2e tiers stand up real Postgres through testcontainers on an ephemeral
port, and skip rather than fail when Docker or `dbmate` is absent (297 pass, 40 skip in that
case). For a long-lived instance and the operator flows:

```bash
cp packages/blackboard/.env.example packages/blackboard/.env
mise run db:up                  # docker compose up -d postgres (host port 5432)
mise run migrate                # apply SQL migrations via dbmate
mise run mcp                    # FastMCP server on 127.0.0.1:8010 (foreground)
mise run mcp:start              # ... or detached; mcp:stop / mcp:logs to manage it
mise run demo:blackboard        # scripted deterministic E2E, zero LLMs
mise run link-target            # point the SDLC team at $BLACKBOARD_TARGET_REPO
mise run team                   # live Omnigent team, uvx-isolated from PyPI
```

`db:up` publishes on host port 5432 and will fail if something else already holds it; the
test suites are unaffected either way, since testcontainers picks its own port. See
[`packages/blackboard/README.md`](packages/blackboard/README.md) for the ADRs behind each step
and [`docs/RUNBOOK.md`](packages/blackboard/docs/RUNBOOK.md) for the full operational playbook.

## Working in the workspace

`uv run --package <name>` selects which member's dependencies to expose, but it does **not**
change the working directory — and pytest and ruff both read their config from the cwd. So
from the root, a test command needs the package path, or it silently collects both suites:

```bash
uv run --package pneuma pytest packages/pneuma                # correct
uv run --package pneuma-blackboard pytest packages/blackboard # correct
uv run --package pneuma pytest                                # WRONG: collects both members
```

`uv run --directory packages/<name> <cmd>` is the other spelling, and the one the mise lint
tasks use, because each package keeps its own ruff config on purpose: pneuma lints under
`E,F,I,UP,B,SIM`, the blackboard adds `ASYNC,S,RUF` with scoped per-file ignores. The styles
are deliberately not unified. Both packages likewise keep their own dependencies, dev group,
and pytest settings, so each stays independently publishable — the root `pyproject.toml`
carries no `[project]` table and no shared dev dependencies at all.

Working inside a package directory also just works, with the familiar single-project commands:

```bash
cd packages/pneuma && uv run pytest
cd packages/blackboard && uv run pytest && uv run pyright
```

## License

See [`packages/pneuma/README.md`](packages/pneuma/README.md#license).
