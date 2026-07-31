# pneuma

A check that passes without ever being in a position to fail is not a passing check, and this
repo is organised around making that defect class mechanically detectable. A rule no reachable
state can break cannot tell a compliant run from a violation; a scoring term whose value never
moves cannot tell a good answer from a bad one. Both print green, both are the same defect, and
`src/pneuma/detect/` reduces them to one primitive with a three-valued verdict, so "nothing
separated the cases" and "the search gave up" stop being the same answer.

Around that sit the parts that produce something worth checking: a business process mined from a
real event log into a typed Pydantic IR, model-checked with TLA+/TLC, then executed with an LLM
agent as an untrusted proposer that may only take transitions the verified skeleton permits.
Built on [`strands-ai-functions`](https://github.com/strands-labs/ai-functions), pinned to
upstream commit `e47dc94`. uv + Python 3.14, `global.anthropic.claude-opus-5` on Bedrock.

## What this adds over the library

Verified against the vendored library source, not inferred.

**No library analogue at all.**

1. **Objective probing** (`detect/objective.py`). The library ships `TextGradOptimizer` and
   `GradFeedback(text, score)` and gives you no way to inspect the objective those climb.
   Nothing in its `optimizer/` (956 lines, five files) sweeps a domain, bounds a window, or
   refuses. Ours sweeps, refines, and refuses before a training loop runs, over four failure
   modes: a degenerate input is the optimum, feedback states a different quantity than
   selection uses, the objective raises inside its declared domain, and the optimum sits on
   the swept window's own edge. Pure stdlib, so it could be upstreamed as-is.
2. **Rule-vacuity detection** (`detect/vacuity.py` + `adapter.py`). A model-checker answers
   "did any reachable state break this rule", never "was the rule ever in a position to
   break". A reachability sweep plus four relaxations (`exact`, `free_initial`, `free_guards`,
   `free_both`) answers the second, and the level at which a rule first becomes breakable is
   its diagnosis. No analogue.
3. **The discrimination primitive** (`detect/discrimination.py`, 136 lines, pure stdlib). The
   shared shape of 1 and 2: a three-valued verdict plus named `withheld` reasons, so every
   bound applied is visible in the result it produced. No analogue.
4. **Adversarial search over an objective** (`detect/adversary.py`). A fan-out of adversaries
   given a tool that *calls* the objective and its source, searching for an input that scores
   well and is worthless. Enumeration only finds what the declared structure implies. No
   analogue.
5. **A verified-IR execution model** (`process/`). One `Process` IR, three consumers: a TLA+
   renderer TLC checks, a hand-written interpreter, and a Hypothesis machine that drives the
   real interpreter and shrinks failing traces. The model emits data, never code, so the
   artifact is model-checked before anything runs.
6. **`@ai_method` on bound methods** (`method.py`). The library is decorator-first on
   module-level functions and `AIFunction` is not a descriptor. Verified: `AIFunction` has no
   `__get__`, and accessing a decorated method on an instance returns the same `AIFunction`
   with `self` unbound and no `tool_spec` at all. The library's own
   `examples/compose_research_team.py:103` does this. A bound method recovers the typed
   schema, the docstring-as-prompt, and learnable parameters at once.
7. **Self-staffing subagents** (`demo/staffing.py`). The cleanest documented gap: the library
   injects `list_threads` and `send_message` into every thread so an agent can talk to peers
   that exist, and ships no way to create one. Its `docs/tutorial.md:442` names
   `ThreadConfig.config_hook` as the place to inject one. `hire`/`delegate`/`dismiss`, each
   bound to the live cycle context so the hiring agent is recorded as parent, which is what
   makes the library's token rollup work across a tree the agent built itself.

**Hardening something the library does provide.**

8. **The memory backend** (`memory/turso_backend.py`). Three improvements over
   `JSONMemoryBackend`. Retrieval is Cohere Embed v4 vectors with in-database
   `vector_distance_cos` where both shipped backends use BM25
   (`memory/json_backend.py:400`, `memory/agentcore_backend.py:101`). Numeric parameters are
   learned from `GradFeedback.score` via a trust-region search over the schema-declared
   domain, where neither shipped *memory* backend reads `score`, both explicitly deferring it to
   "score-learning hosts". And retrieval quality is *measured* (`probe_retrieval`,
   `calibrate_ceiling`), which has no library analogue. Two honest limits: the library does
   ship a score-reading `ParameterHost` outside `memory/`
   (`experimental/economics/function.py:683`), so score learning is not unprecedented, only
   absent from the memory path; and the narrow-gradient `meta["results"]` plumbing is the
   library's, which we consume rather than invent.

## The two fixtures, as evidence

**A curated business-process log.** `data/receipt.xes`: 1,434 real Dutch municipality building
permits, 8,577 events, 27 activities. It proved the central claim is not hypothetical. A mined
model can be structurally sound and still violate policy, and 118 of 1,434 cases (8.2%) never
perform a mandatory verification step, proved two independent ways before any agent runs. The
live run made 100 real decisions with zero illegal proposals and surfaced a failure mode nobody
predicted: the agent never broke a rule, it *dithered*, cycling between valid states until the
step cap stopped it in 6 of 10 cases.

**An agent-transcript log.** `data/transcripts_fleet.json`: this project's own Claude Code
tool-use, 3,055 events over 88 cases as committed, sampled from a live corpus of about 5,300
cases and 336k events. Structurally the opposite fixture: 91% of cases walk a trace nobody else
walks, against 6% in the permit log.

Validating the detectors on the second fixture found two defects in the detectors themselves,
and this is the most credible thing here:

- One detector reported a truncated search as a confident finding, the very defect it exists to
  detect. Under a boolean verdict, "examined everything and nothing separated" and "gave up
  before it could" collapse into the same `False`. That is why `discriminates` is three-valued
  and why `withheld` carries named reasons.
- The objective prober passed a genuinely degenerate objective with zero findings, because it
  relied on a caller-supplied list of bad answers, which is a harness artifact written by the
  same hand as the scoring formula and wrong in the same direction. Callers now declare
  `Structure` (the shape of the search space) and the degenerate points are computed from it.
  On the permit log alone the missing declaration was invisible by construction; one fixture
  could not show the list was load-bearing, two could.

Both are fixed. A third correction the second fixture forced: an earlier validation froze
live-corpus measurements as constants, and the corpus is live-appended. It grew 9,081 to 9,850
events inside one session and 12 of 51 frozen entries had already drifted hours later. The
constants are gone; the substantive finding survived because it never depended on their values.

Where a mechanism was measured and did not win, it says so. The agent that writes its own mining
code beats the fixed miner's *default* threshold on permits and loses to that same miner run at
the agent's own threshold, on both logs. The honest reading is that it reproduced the frozen
algorithm and did not improve on it; what it bought is a per-log threshold with a stated
rationale instead of a constant someone picked once.

## Run it

Every command below was run.

```bash
uv sync
uv run pytest -p no:randomly     # 478 passed, 10 skipped in about 2 min
uv run pneuma                    # live Bedrock run, writes artifacts/
uv run pneuma --truth            # print the demo's planted ground truth and exit
```

Pass `-p no:randomly`: the suite randomizes order by default and a bare run can look
differently broken. `pneuma` exits non-zero when the oracle rejects the verdict.

The 10 skips are the live-gated tests, each measuring something a scripted model cannot:

| Variable | What setting it to `1` measures |
| --- | --- |
| `PNEUMA_LIVE` | the adversarial search against a real objective |
| `PNEUMA_LIVE_HARNESS` | the agent proposing a harness parameter, with the detectors as gate |
| `PNEUMA_LIVE_MINE` | gradient routing to two parameters, and toolkit vs. its own seed baseline |
| `PNEUMA_LIVE_EMBED` | real Cohere Embed v4 retrieval quality |

## Layout

**Core.** Reusable; nothing here imports from `demo/`.

| File | What it holds |
| --- | --- |
| `src/pneuma/detect/discrimination.py` | The three-valued verdict both detectors share |
| `src/pneuma/detect/vacuity.py` | Reachability sweep plus four relaxations; rule vacuity |
| `src/pneuma/detect/objective.py` | Sweeps, refines, and refuses a scoring function |
| `src/pneuma/detect/adversary.py` | LLM adversaries searching for a worthless high scorer |
| `src/pneuma/detect/adapter.py` | The one seam binding `vacuity` to pneuma's `Process` IR |
| `src/pneuma/memory/turso_backend.py` | libSQL `MemoryBackend`: vector recall, score learning |
| `src/pneuma/memory/embedding.py` | Cohere Embed v4 on Bedrock, cached in the same file |
| `src/pneuma/process/ir.py` | The process IR: states, guards, effects, invariants |
| `src/pneuma/process/tla.py` | Renders the IR to TLA+ and runs TLC over it |
| `src/pneuma/process/interpreter.py` | Executes a verified IR, validating every agent choice |
| `src/pneuma/process/properties.py` | Hypothesis machine built from the same IR |
| `src/pneuma/process/agent_driver.py` | An `@ai_method` as the interpreter's decider |
| `src/pneuma/method.py` | `@ai_method`: typed AI functions over instance state |
| `src/pneuma/model.py` | Opus 5 Bedrock config (adaptive thinking, effort tiers) |

**Case studies.** The measurements the core's claims rest on.

| File | What it holds |
| --- | --- |
| `src/pneuma/casestudy/eventlog.py` | XES → Polars → libSQL (WAL) persistence |
| `src/pneuma/casestudy/miner.py` | Process discovery, conformance, bottlenecks, rework |
| `src/pneuma/casestudy/pipeline.py` | The six-step study, end to end |
| `src/pneuma/casestudy/rules.py` | Derive precedence rules from any log, attach to any process |
| `src/pneuma/casestudy/handlers.py` | Per-state agents: the work inside a step, not the choice |
| `src/pneuma/casestudy/live.py` | The live-LLM experiment: neutral vs. pressured framing |
| `src/pneuma/casestudy/learning.py` | Backpropagation over the navigator, to fix the looping |
| `src/pneuma/casestudy/aimine.py` | The model writes the mining code; graded against the fixed one |
| `src/pneuma/casestudy/minelearn.py` | The miner's guidance *and* its tools, both learnable |
| `src/pneuma/casestudy/toolkit.py` | The accumulating toolkit, shipped as `Procedural` source |
| `src/pneuma/casestudy/harnesslearn.py` | A harness parameter learned only if the detectors gate it |
| `src/pneuma/casestudy/transcriptlog.py` | Agent tool-use transcripts as the second fixture |
| `src/pneuma/casestudy/benchmark.py` | Scores our model against the standard miners |
| `src/pneuma/casestudy/ir_petri.py` | `Process` IR → Petri net, so pm4py will score it |

**Demo.** The original incident war-room, and the only shipping console script.

| File | What it holds |
| --- | --- |
| `src/pneuma/demo/agent.py` | `Agent` base class; compiles an instance into an `AIFunction` |
| `src/pneuma/demo/typed_cast.py` | The incident cast in the decorator paradigm |
| `src/pneuma/demo/staffing.py` | The `hire`/`delegate`/`dismiss` tools the library lacks |
| `src/pneuma/demo/cast.py` | Specialists, the hireable roster, the incident lead |
| `src/pneuma/demo/warroom.py` | Custom `Spawnable` orchestrator plus the oracle post-condition |
| `src/pneuma/demo/incident.py` | Synthetic incident with machine-checked information asymmetry |
| `src/pneuma/demo/live.py` | Renders the coordinator's event stream as it happens |
| `src/pneuma/demo/cli.py` | The `pneuma` console script: one war-room run, writes artifacts |
| `docs/report.html` | Write-up template; `docs/build_pdf.py` renders it with real run data |

Longer write-ups: `docs/case-study.md` and `docs/process.md`.

## Notes

The library is pinned by git rev rather than PyPI, to
`e47dc94e7b8e4b1e3f3e85587d0bc60e78c30296` in both `pyproject.toml` and `uv.lock`. `0.3.0`
predates `runtime/usage.py`, which holds the subtree token-rollup helpers this project uses. The
vendored checkout used to verify the claims above is at that same commit.

`data/receipt.xes` (permits) and `data/roadfines.xes` (the portability log) are public XES logs;
tests that need them skip if absent. `data/transcripts_fleet.json` and
`data/transcripts_sample.json` are committed, so no transcript test calls out to `claude-sql`.

The TLC step needs `java` and `tools/tla2tools.jar`, which is gitignored. Fetch it from the TLA+
releases page. Tests needing it skip when it is absent; everything else runs offline against
scripted models.

A live run needs AWS credentials with Bedrock access to `global.anthropic.claude-opus-5`, plus
`cohere.embed-v4` for the embedding path. The test suite needs neither.
