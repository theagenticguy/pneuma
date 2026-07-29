# pneuma

An object-oriented agent layer over [`strands-ai-functions`](https://github.com/strands-labs/ai-functions), where a running agent hires its own subagents.

Runs on `global.anthropic.claude-opus-5` on Amazon Bedrock with adaptive thinking at `xhigh` effort. uv + Python 3.14.

## What it demonstrates

**Agents as objects.** The library is decorator-first: an agent is a module-level function whose docstring is the prompt. `pneuma.Agent` inverts that — a subclass declares the static parts in its class body and carries the varying parts as instance state, then compiles itself into one `AIFunction`. `Specialist("metrics")` and `Specialist("logs")` are two live threads from one class, each holding private evidence the other cannot read.

**Tools on `self`.** A method decorated with strands' `@tool` becomes a tool bound to the instance. `Agent.tools()` walks the MRO and reads each one off `self`, so `DecoratedFunctionTool.__get__` binds it: two instances get two distinct tools closing over their own state, and `self` never reaches the schema the model sees. `Specialist.search_plane` is the payoff — each analyst can grep its own plane and no other.

**Agents as typed functions, on a class.** `pneuma.method` is the same object-oriented idea taken back to the library's own paradigm. `@ai_method` decorates a *method*: Python drops `self` from a bound method's signature, so `analyze(window: str, focus: Focus = "errors", max_records: int = 12)` is what the model sees, while `self` stays reachable inside the docstring template. That keeps three things `Agent` gives up — the typed tool schema (enums, defaults, required fields), the docstring as declarative prompt, and learnable parameters, since `TextGradOptimizer` finds gradient targets in call arguments and cannot see state hidden on `self`. `typed_cast.Analyst` is the incident cast rewritten this way, and one agent holds another as a typed tool with no message bus in between.

**Self-staffing subagents.** The library injects `list_threads` and `send_message` into every thread, so an agent can talk to peers that already exist. It cannot create one. `ThreadConfig.config_hook` is documented as the place to inject a spawn tool, and nothing ships it. `pneuma.staffing` does: `hire(role, name, mandate)`, `delegate(name, request)`, `dismiss(name)`, each bound to the live cycle context so the hiring agent is recorded as parent — which is what makes the library's own token rollup work across a tree the agent built itself.

**Three layers of orchestration in one run.** A deterministic plain-Python `Spawnable` fans out specialists with `asyncio.gather`; the lead delegates to peers at runtime through the library's tools; the lead also builds its own team through ours.

**A verdict graded by an oracle.** The demo investigates a synthetic incident whose evidence is split across four telemetry planes such that no single plane identifies the cause. A post-condition checks the lead's verdict against planted ground truth and re-prompts with the specific shortfall on failure.

## Run it

```bash
uv sync
uv run pneuma                    # live Bedrock run, writes artifacts/
uv run pneuma --truth            # print the planted ground truth and exit
uv run pytest                    # 34 offline tests, scripted models, no network
```

`pneuma` exits non-zero when the oracle rejects the verdict.

## Layout

| File | What it holds |
| --- | --- |
| `src/pneuma/agent.py` | `Agent` base class; compiles an instance into an `AIFunction` |
| `src/pneuma/method.py` | `@ai_method`: typed AI functions over instance state |
| `src/pneuma/typed_cast.py` | The incident cast in the decorator paradigm |
| `src/pneuma/staffing.py` | The `hire`/`delegate`/`dismiss` tools the library lacks |
| `src/pneuma/cast.py` | Specialists, the hireable roster, the incident lead |
| `src/pneuma/warroom.py` | Custom `Spawnable` orchestrator plus the oracle post-condition |
| `src/pneuma/incident.py` | Synthetic incident with machine-checked information asymmetry |
| `src/pneuma/model.py` | Opus 5 Bedrock config (adaptive thinking, effort tiers) |
| `src/pneuma/live.py` | Renders the coordinator's event stream as it happens |
| `docs/report.html` | Write-up template; `docs/build_pdf.py` renders it with real run data |

## Notes

The library is pinned to upstream commit `e47dc94` rather than PyPI: `0.3.0` predates `runtime/usage.py`, which holds the subtree token-rollup helpers this project uses.

A live run needs AWS credentials with Bedrock access to `global.anthropic.claude-opus-5`. The test suite uses scripted models and needs neither.
