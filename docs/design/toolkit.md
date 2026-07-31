# `casestudy/toolkit.py` — design rationale

Why the miner's seed toolkit is a module of real functions rather than a string literal, what
the sandbox actually permits, and what the seed baseline honestly is. The module docstring
states the invariants and the numbers; this file carries the arguments.

## Real functions, not a triple-quoted literal

The learnable parameter is a `str` of Python source, so the tempting shape is a triple-quoted
literal in `minelearn.py`. It is the wrong shape. Source in a string is not importable, not
type-checkable, not lintable, and not directly callable by a test — so the seed helpers would
be the only part of this project nothing verifies, and they are the part the agent's first
round depends on entirely.

Written as real functions they are all four, and `SEED_TOOLKIT` is assembled from
`inspect.getsource`. Verified: `getsource` on a function in this module round-trips through
`ast.parse` and loads into the sandbox namespace unchanged, annotations and all, because the
sandbox never evaluates annotations.

The residual cost is honest to state. The seed is *source text*, so the round-trip is the
contract, and a helper that depends on module-level state in this file would load fine and
fail at call time — `getsource` on a `def` carries the function, not its module. Everything
here is therefore self-contained by construction, and that is a constraint on future edits
rather than an observation.

## What the sandbox permits, measured not assumed

`io` is **not** an authorised import. `SAFE_BUILTINS` in
`ai_functions.tools.local_python_executor` does not list it and `aimine.ANALYSIS_IMPORTS` does
not add it, so `polars.read_csv(io.StringIO(log_csv))` raises
`InterpreterError: Import of io is not allowed` — despite being the route `aimine`'s own prompt
recommends. `polars.read_csv(log_csv.encode())` works and is what `load_log` does.

That single fact is most of `load_log`'s reason to exist, and it generalises into the design
rule for this module: a helper the agent cannot get wrong beats an instruction it has to
remember. Every sandbox restriction is a place the agent will spend a round rediscovering, and
a helper spends that round once, in advance, in code a test covers.

`polars.__version__` also raises, because the interpreter forbids dunder attribute access.
Anything in here that reaches for a dunder fails at call time rather than at load time, which
is why `minelearn`'s rehearsal exists: load success is not evidence the toolkit works.

## Only function signatures and docstrings are advertised

`procedural_signatures` walks the module's top-level `def`s, skips `_`-prefixed names, and
emits the `def` line plus the docstring. Module docstrings, comments, and module-level
constants are silently dropped. Measured, not inferred.

Two consequences. First, anything the agent must read has to be a docstring on a function it
can call — a policy written as a comment here is invisible to the agent that would have to
follow it, which is why the prose parameter in `minelearn` is a separate gradient target
rather than comments inside this code. See [minelearn.md](minelearn.md). Second, a private
helper is genuinely private: `_`-prefixed names load and are callable, but the agent is never
told they exist, so it will not use them.

## What these helpers are for

They are the primitives a support-threshold argument is actually made of: counting support,
finding where the support distribution breaks, checking the graph is connected, and measuring
replay coverage at a candidate cutoff.

`sweep_thresholds` is the one with real leverage, because it turns "choose a threshold" from a
judgment call into an argmax over a measured curve — and it computes the same
coverage-versus-selectivity trade `minelearn.Attempt.score` grades, so the agent can optimise
the objective directly instead of guessing at it.

## The baseline, and what it honestly is

Measured on `data/receipt.xes` at a 400-case sample, driving `final_answer` from
`sweep_thresholds`' argmax with no model judgment at all: threshold 19, 13 edges, 84.38%
coverage, edge share 0.1884, no invented edges, `Attempt.score` 0.8274.

That number needs a correction, and it is worth stating plainly because the flattering reading
was available. **The argmax does not beat the frozen miner's method.** Running `miner.mine` on
the same 400-case sample at `min_edge_cases=19` produces the identical 13 edges and the
identical 0.8274 — `handoff_support` plus a threshold *is* the directly-follows count, so the
seed toolkit reimplements the frozen algorithm and adds one thing: it picks the threshold by
measuring instead of taking it as an argument. The frozen miner on that same sample at its
default 25 scores 0.8210, so choosing the cutoff by argmax is worth +0.0064 and nothing else.

So the honest claim for the seed toolkit is narrow: it is the frozen method with its one free
parameter tuned, which is exactly what `aimine`'s docstring says the agent's advantage should
be ("written once in advance versus written per log"). The interesting number is what the
*agent* does starting from these helpers, and that needs a live run.
`tests/test_minelearn.py::test_live_toolkit_beats_its_own_seed_baseline` uses 0.8274 as the bar
because beating the prose-only loop, or the frozen miner at its default, would be comparing
against something easier.
