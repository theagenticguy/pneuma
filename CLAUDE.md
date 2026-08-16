# pneuma

Object-oriented AI agents as ordinary Python classes (docstring = prompt, params = typed
inputs), plus `detect/` probes that test whether a safety check or scoring formula actually
checks anything. Built on strands-ai-functions (pinned git rev `e47dc94`), Python 3.14, uv.

## Commands

```bash
uv sync                          # install (uv only; requires-python >= 3.14)
uv run pytest                    # full suite, offline, ~3 min: 881 pass / 20 skip
uv run pytest tests/library      # library-layer tests only
uv run pytest tests/app          # application-layer tests only
uv run ruff check src tests     # lint (E,F,I,UP,B,SIM; line-length 100; py314)
uv run pneuma                    # live Bedrock war-room demo, writes artifacts/
uv run pneuma --truth            # print the demo's planted ground truth and exit
```

- Tests need no credentials; live tests skip unless env-gated (`PNEUMA_LIVE_KERNEL`,
  `PNEUMA_LIVE`, `PNEUMA_LIVE_EMBED`, `PNEUMA_LIVE_TEAM_LEARNING`, etc. — table in README).
- Live runs need Bedrock access to `global.anthropic.claude-opus-5` and `cohere.embed-v4`.
- TLC model checking needs `java` + `tools/tla2tools.jar` (gitignored; tests skip if absent).
- A `.codegraph/` index exists; the `codegraph` CLI (mise shim) is available for
  impact/reference queries across the 99 indexed files.

## Layout and the enforced library/app boundary

```
src/pneuma/
  method.py gated.py recall.py model.py   # kernel: @ai_method, MethodAgent/Thread, GatedProposer, Recall
  team/          # Team core + hooks/ (see below)
  process/       # process IR -> TLA+/TLC verification -> interpreter -> ProcessAgent
  detect/        # vacuity, objective, adversary, gaming probes; shared 3-valued verdict
  memory/        # libSQL/Turso backend + Cohere Embed v4 embeddings
  casestudy/     # APPLICATION: process mining on real logs, learning loops
  demo/          # APPLICATION: war-room incident demo (the `pneuma` CLI)
tests/library/   # tests for the library layer (offline, no heavy deps)
tests/app/       # tests for casestudy/ and demo/ (includes live-gated tests)
docs/design/     # one rationale doc per module (module docstrings capped at 40 lines)
```

**The boundary is enforced, not asserted.** `tests/library/test_boundary.py` AST-walks every
library module and fails on any import of `casestudy`/`demo` — even one inside a function
body — and on any library import of `polars`, `libsql`, or `pm4py` (application-only
packages). A second test imports each library module in a fresh process with those engines
blocked. Membership is derived from the source tree: a new top-level package under
`src/pneuma/` fails tests until declared on one side (`LIBRARY` / `APPLICATION` sets in
that file). Put reusable code in the library side; anything needing dataframes or process
mining belongs in `casestudy/`.

## Team architecture: minimal core, features as hooks

`team/core.py` does exactly three things: spawn members, run the lead with each member as a
typed tool, retire everybody in a `finally`. The answer returns ungraded — there is no
built-in oracle. Every other capability is an opt-in `TeamHook` in `team/hooks/`:

- `briefing.py` — ask every member first, fold answers into the lead's prompt
- `negotiation.py` — bounded objection/revision rounds over the lead's draft
- `worklog.py` — typed `post_discovery` fan-out; passive teammate awareness
- `hiring.py` — lead-side hire/delegate/dismiss over a role catalog (+ `dynamic=True`
  for lead-written subagent instructions, logged verbatim)
- `review.py` — `Critic` (one adversarial reviewer) and `Council` (voting panel)
- `learning.py` — recalled prose guidance as a training target; `train()` runs one
  TextGrad step from what review/revise rounds recorded

When adding a team feature, write it as a hook — do not grow the core. Old phase-based
team code was deleted in the 2026-08-10 `team-hooks-rebuild` merge.

## Conventions

- Prompts live in method docstrings; private context on `self`; anything a caller supplies
  or training improves comes in through typed parameters (`Recalled(...)` for memory).
- Module docstrings capped at 40 lines; rationale goes to `docs/design/<module>.md`.
- Detectors return three-valued verdicts (works / decoration / could-not-tell) — never
  report a truncated search as a confident pass.
- pytest runs with `asyncio_mode = "auto"` and `pythonpath = ["tests"]`
  (`from paths import ...` works from any test subdir).
