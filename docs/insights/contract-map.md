# pneuma · Contract map

A **contract** in `pneuma` is a type, protocol, callable alias, or implicit frame shape declared in one module and depended on by at least one other module. Three kinds appear, and the middle one carries most of the risk:

1. **Validated data.** A `pydantic.BaseModel` whose `model_validator` establishes properties every downstream consumer assumes. `process/ir.py`'s seven models are the whole of this kind.
2. **Structural protocols.** A `typing.Protocol` that existing classes satisfy without being edited or importing it. `gated.Verdict`, `gated.Gate`, `team.Recruit`, `team.TeamHook`, and `vacuity.System` are all this shape, and two of them have zero import sites in `src/` — they are satisfied by classes in other modules that never mention them.
3. **Undeclared frames.** A `polars.DataFrame` column set reproduced by hand in a second module. The nine-column event-log frame is the only one, and no type anywhere states it.

Consumer counts below are distinct import sites, measured with an AST pass over `src/**`, `tests/**`, and `tools/**` that resolves relative imports to absolute `pneuma.*` paths — not grep, which counts every textual mention. Where the `src/`-versus-tests split changes the picture, it is stated.

The seven IR models are treated as one composite contract rather than seven, because consumers import them together on one line (`src/pneuma/casestudy/pipeline.py:28`, `src/pneuma/casestudy/rules.py:47`) and use them as one artifact.

## The process IR: `Process`, `State`, `Transition`, `Variable`, `Guard`, `Effect`, `Invariant`

**Producer:** `src/pneuma/process/ir.py:213` (`Process`), with the six models it composes at `src/pneuma/process/ir.py:61`, `src/pneuma/process/ir.py:109`, `src/pneuma/process/ir.py:139`, `src/pneuma/process/ir.py:162`, `src/pneuma/process/ir.py:175`, `src/pneuma/process/ir.py:189`.

**Consumer(s):**

- `src/pneuma/process/tla.py:26` — renders the IR to a TLA+ module for TLC.
- `src/pneuma/process/interpreter.py:24` — walks it, dispatching states to agents.
- `src/pneuma/process/properties.py:33` — renders it to a Hypothesis state machine.
- `src/pneuma/detect/adapter.py:26` — binds it to `vacuity`'s `System` protocol.
- `src/pneuma/process/agent.py:59` — the agent bound to one process.
- `src/pneuma/casestudy/miner.py:24` — constructs one from a mined event log.
- `src/pneuma/casestudy/aimine.py:43` — constructs one from an agent's `Discovered`.
- `src/pneuma/casestudy/rules.py:47` — attaches derived precedence invariants.
- `src/pneuma/casestudy/pipeline.py:28`, `src/pneuma/casestudy/live.py:31`, `src/pneuma/casestudy/learning.py:60`, `src/pneuma/casestudy/handlers.py:33`, `src/pneuma/casestudy/harnesslearn.py:74`, `src/pneuma/casestudy/ir_petri.py:17`.
- `src/pneuma/process/agent_driver.py:26`.

`Process` alone has 27 distinct import sites, 15 of them under `src/` (listed above). `Transition` has 22 (9 in `src/`), `State` 17 (4), `Invariant` 10 (4), `Variable` 9 (3), `Guard` 8 (2), `Effect` 5 (2) — the tail of the IR is imported mostly by tests, which is what makes the composite grouping honest: `pipeline.py` and `rules.py` are the only `src/` modules that take all seven.

**Shape:**

```python
class Process(BaseModel):
    """A whole mined process: states, transitions, variables, invariants."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = ""
    states: list[State]
    initial_state: str
    variables: list[Variable] = Field(default_factory=list)
    transitions: list[Transition]
    invariants: list[Invariant] = Field(default_factory=list)
```

**Assumptions consumers make:**

- **Every name resolves.** `_referentially_sound` rejects a dangling `transition.source`/`target` (`src/pneuma/process/ir.py:258-261`), an unknown guard or effect variable (`src/pneuma/process/ir.py:262-265`), and an `initial_state` that is not a declared state (`src/pneuma/process/ir.py:236-237`). So `tla.render` indexes `process.states` and `process.variables` without a membership check (`src/pneuma/process/tla.py:137`, `src/pneuma/process/tla.py:154`), and `interpreter.run` does `states[current]` on a plain dict (`src/pneuma/process/interpreter.py:251`).
- **A terminal state exists.** `_referentially_sound` refuses a process with none (`src/pneuma/process/ir.py:273-274`). `interpreter.run`'s loop therefore has a reachable exit at `src/pneuma/process/interpreter.py:251-253`, and `adapter.structural_rules` builds its `terminal` set assuming it is non-empty (`src/pneuma/detect/adapter.py:126`).
- **No name collides with a TLA+ definition the renderer emits.** `_TLA_RESERVED` (`src/pneuma/process/ir.py:56-58`) is a producer-side guard written for exactly one consumer: `tla.render` defines `vars`, `States`, `TypeOK`, `Init`, `Next`, `Done`, `NoDeadlock`, and `Spec` itself (`src/pneuma/process/tla.py:135-141`, `src/pneuma/process/tla.py:151`, `src/pneuma/process/tla.py:190-198`, `src/pneuma/process/tla.py:229`). State names are exempt because a state renders only as a quoted string (`src/pneuma/process/ir.py:52-55`).
- **Guard and effect semantics are the IR's, not each consumer's.** `adapter.ProcessSystem.successors` calls `transition.enabled` and `effect.apply` rather than re-reading the spec, and accumulates effects into the successor dict in order (`src/pneuma/detect/adapter.py:77-83`) so two effects on one variable agree with `interpreter.run`'s own accumulation at `src/pneuma/process/interpreter.py:261-262`. That is an ordering assumption no type expresses.
- **`state_map` is rebuilt on every access.** It is a property returning a fresh dict (`src/pneuma/process/ir.py:277-279`), so `ProcessAgent.work` resolves it once outside its per-step hook and says why (`src/pneuma/process/agent.py:315-317`).
- **`initial_assignments()` is the cross product over free variables, and its first element is the default start.** `interpreter.run` takes `process.initial_assignments()[0]` when no `start` is passed (`src/pneuma/process/interpreter.py:233`), while `properties.machine_for` samples the whole list (`src/pneuma/process/properties.py:64`) — so the interpreter's default run explores one starting state where Hypothesis explores all of them.
- **A pinned `initial` is a vacuity hazard, not just a value.** `Variable.nondeterministic` is `initial is None` (`src/pneuma/process/ir.py:100`), and `tla.render` emits `\in` over the domain for those variables specifically so a guarded branch is reached and an invariant about it cannot pass vacuously (`src/pneuma/process/tla.py:155-163`).

**Drift risk:** A new field on `Transition` or `Effect` that changes step semantics would be honoured by `interpreter.run` and silently ignored by `adapter.ProcessSystem.successors` and `tla.render`, so TLC would verify a different machine than the one that runs. Mitigation: any new IR field that affects stepping must land in `Transition.enabled`/`Effect.apply` — the two methods both the interpreter and the adapter already call — rather than in either walker.

## `interpreter.Decide` — the agent-as-untrusted-oracle callback

**Producer:** `src/pneuma/process/interpreter.py:25`

**Consumer(s):**

- `src/pneuma/process/agent.py:137` — `ProcessAgent.decider` returns one, adapting the `choose` `@ai_method`.
- `src/pneuma/casestudy/live.py:129` — the live experiment's per-trial decider factory.
- `src/pneuma/casestudy/pipeline.py:203` — `execute_case` takes one as a parameter.
- `src/pneuma/process/properties.py:60-67` — `adversarial_decider` returns a deterministic one for Hypothesis.
- `src/pneuma/casestudy/learning.py:354` — passes a traced decider into `interpreter.run`.

**Shape:**

```python
Decide = Callable[[str, list[Transition], dict[str, int | str]], Awaitable[str]]
```

**Assumptions consumers make:**

- **The assumption runs backwards: `run` does not trust `Decide`'s return value.** `_elicit` rejects a proposal not in the enabled set and re-asks up to `max_rejections + 1` times before raising (`src/pneuma/process/interpreter.py:340-350`). A decider may legally return a garbage name; what it may not do is expect the interpreter to take it.
- **`decide` is skipped entirely when exactly one transition is enabled.** `_elicit` returns `enabled[0]` without awaiting anything (`src/pneuma/process/interpreter.py:337-338`). Every consumer that maintained its own visit list would be missing exactly those steps, which is why the history is a `ContextVar` owned by `run` (`src/pneuma/process/interpreter.py:64`) and why `casestudy/live.py`'s decider explicitly declines to keep a local list and says so (`src/pneuma/casestudy/live.py:139-140`).
- **`offer` reads the enclosing run's history by default.** `visited=None` means "use `history()`" (`src/pneuma/process/interpreter.py:380`), so a decider calls `interpreter.offer(state, enabled, variables)` with three arguments and gets the complete path (`src/pneuma/casestudy/live.py:143`, `src/pneuma/process/agent.py:154`).
- **A `ProcessError` from `run` is an experimental result; a handler fault is not.** `src/pneuma/casestudy/live.py:174-175` counts `ProcessError` as `blocked`, and `NoProgress` is deliberately a `ProcessError` subclass so that accounting is undisturbed (`src/pneuma/process/interpreter.py:105-114`). `HandlerFailed` is deliberately *not* one, for the same reason stated from the other side (`src/pneuma/process/agent.py:71-80`).
- **The variables dict handed to `decide` is a copy.** `_elicit` passes `dict(variables)` (`src/pneuma/process/interpreter.py:343`), so a decider that mutates its argument cannot corrupt the run.

**Drift risk:** Adding a fourth parameter to `Decide` would break all five external deciders at once, loudly. The quiet failure is the opposite move — a consumer reconstructing run state from `Decide`'s arguments alone, which is missing every single-transition step. Mitigation: new run-scoped facts a decider needs get a `ContextVar` beside `_HISTORY` and `_REVISITS` (`src/pneuma/process/interpreter.py:64`, `src/pneuma/process/interpreter.py:71`), which is what keeps the signature fixed.

## `method.ai_method` and `method.MethodAgent` — the typed-capability contract

**Producer:** `src/pneuma/method.py:79` (`ai_method`), `src/pneuma/method.py:326` (`MethodAgent`), with `AIMethodSpec` at `src/pneuma/method.py:71` and the compiler at `src/pneuma/method.py:103`.

**Consumer(s):**

- `src/pneuma/gated.py:51` — `GatedProposer` subclasses `MethodAgent`.
- `src/pneuma/team/members.py:20` — `Member` adapts a `MethodAgent`; `DynamicAgent` is one.
- `src/pneuma/process/agent.py:57` — `ProcessAgent` subclasses it and declares `choose`.
- `src/pneuma/recall.py:60` — `Recall` binds one to a `MemoryBackend`.
- `src/pneuma/casestudy/aimine.py:42` — `Miner.discover`.
- `src/pneuma/casestudy/harnesslearn.py:69` — `HarnessProposer.propose`.
- `src/pneuma/casestudy/learning.py:58`, `src/pneuma/casestudy/minelearn.py:71`, `src/pneuma/casestudy/handlers.py:31`, `src/pneuma/demo/typed_cast.py:30`.

`ai_method` has 19 import sites (8 in `src/`), `MethodAgent` 16 (8 in `src/`).

**Shape:**

```python
def ai_method(output_type: type, /, **config: Any) -> Callable[[Callable[..., Any]], Any]:
    """Mark a method as an AI function whose prompt is its docstring.

    The method body is normally empty: returning `None` hands the docstring to
    the template renderer, exactly as the library's own decorator does. Return a
    string instead to compute the prompt directly and skip templating.
```

**Assumptions consumers make:**

- **An empty body means "render the docstring".** `prompt_fn` awaits the method, and a non-`None` result short-circuits templating (`src/pneuma/method.py:119-123`); a `None` result with no docstring raises (`src/pneuma/method.py:124-125`). Every consumer's decorated methods are bodyless (`src/pneuma/casestudy/aimine.py:204`, `src/pneuma/team/members.py:220-226`, `src/pneuma/process/agent.py:122`).
- **`{self.x}` in the docstring resolves.** `compile_ai_method` builds the render context as `{"self": instance, **bound.arguments}` (`src/pneuma/method.py:128`), which is why rendering happens here rather than through the library's own docstring path (`src/pneuma/method.py:107-110`). `ProcessAgent.choose` depends on this for `{self.process.name}` (`src/pneuma/process/agent.py:123`).
- **A learnable value must be a call argument, and a validator's fixed input must be on `self`.** Stated in the module header (`src/pneuma/method.py:17-19`) and re-stated by both consumers that split state this way: `GatedProposer` (`src/pneuma/gated.py:106-114`) and `ProcessAgent` (`src/pneuma/process/agent.py:91-97`).
- **The compiled tool name is `{owner}.{method}`, and an `@ai_method(name=...)` rename wins.** `_owner_name` is single-sourced for exactly this (`src/pneuma/method.py:61-68`), the config name is built at `src/pneuma/method.py:155-158`, and `spawn` re-reads the published name so an error names a thread the caller can find in the tool schema (`src/pneuma/method.py:419-421`).
- **`get_type_hints(..., include_extras=True)` is load-bearing, not tidy.** Resolving without extras flattens `Annotated[str, ProceduralMarker()]` to plain `str` and the code silently becomes an ordinary prompt argument (`src/pneuma/method.py:141-146`). `recall.recalled_params` depends on the surviving metadata (`src/pneuma/recall.py:148`).
- **Compiling through `self.compiled` rather than the module function is how tests bind scripted models.** `spawn` routes through it deliberately (`src/pneuma/method.py:409-411`), and `ProcessAgent.decider` cites that precedent for doing the same (`src/pneuma/process/agent.py:144-148`).
- **A `MethodAgent` compiles to `STRUCTURED` and is therefore unreachable by `send_message`.** Asserted in `MethodThread`'s docstring (`src/pneuma/method.py:192-199`) and depended on by the team layer, which mentions `send_message` nowhere for that reason, and by `DynamicAgent`, whose second `context` parameter exists solely to keep the shape `STRUCTURED` (`src/pneuma/team/members.py:196-203`).

**Drift risk:** `MethodAgent.ai_methods()` walks the whole MRO (`src/pneuma/method.py:345-352`), so an `@ai_method` added to `MethodAgent` itself would silently join every subclass's published tool set — including `DynamicAgent`, whose docstring pins the set as exactly `["answer"]` (`src/pneuma/team/members.py:194-199`). Mitigation: the base declares none, and the pinning test on `DynamicAgent` is what would fail.

## `gated.Verdict` — a structural protocol with no import sites in `src/`

**Producer:** `src/pneuma/gated.py:56-79`

**Consumer(s):**

- `src/pneuma/gated.py:240` — `_record` reads `ok` and calls `report_text()` on the post-condition path.
- `src/pneuma/gated.py:228-233` — `judge` reads both on the beam path.
- `src/pneuma/casestudy/harnesslearn.py:411` and `src/pneuma/casestudy/harnesslearn.py:442` — `Admission` satisfies it structurally, without importing it.
- `src/pneuma/detect/objective.py:400` — `Probe.ok`; its `report()` at `src/pneuma/detect/objective.py:407` is *not* named `report_text`, so `Probe` satisfies half the protocol only.
- `tests/library/test_gated.py:30` — the only site in the repo that imports the name, to assert a trivial verdict satisfies it (`tests/library/test_gated.py:190`).

**Shape:**

```python
@runtime_checkable
class Verdict(Protocol):
    """What a gate returns: a decision, and a report a model can act on."""

    @property
    def ok(self) -> bool:
        """Whether the candidate may be used."""
        ...

    def report_text(self) -> str:
        """The verdict as prose the model can act on. Reaches the model verbatim."""
        ...
```

**Assumptions consumers make:**

- **`report_text()` reaches the model verbatim as re-ask feedback.** `_record` raises `AssertionError(f"{report}\n\n{self.REASK}")` (`src/pneuma/gated.py:256`), and the runtime turns a validator's exception text into a `[VALIDATION ERROR]` user turn the next attempt reads (`src/pneuma/gated.py:12-16`). `Admission.report_text` is written to that requirement — it names the cause rather than the verdict (`src/pneuma/casestudy/harnesslearn.py:443-449`).
- **The verdict itself is untrusted.** Reading `ok` or rendering `report_text` can crash, and that crash is a fault rather than a rejection: it must neither land in `rejected` nor reach the model dressed as feedback (`src/pneuma/gated.py:249-253`). `judge` renders the report eagerly for the same reason — a `report_text` that crashes later would detonate far from the gate that produced it (`src/pneuma/gated.py:229-233`).
- **A coroutine is refused loudly rather than truth-tested.** Every coroutine is truthy, so `not verdict.ok` on one would silently admit everything (`src/pneuma/gated.py:180-182`); `admits` closes it and raises (`src/pneuma/gated.py:193-204`).
- **Deliberately not a base class, so existing verdicts satisfy it unedited.** Stated at `src/pneuma/gated.py:65-70`, naming both `Admission` and `detect.Probe`. `Admission` is a frozen dataclass with a conjunctive `ok` over two independent detector readings (`src/pneuma/casestudy/harnesslearn.py:411-412`) and carries `quality`, `threshold`, and `baseline_rules` besides.
- **`Gate.__call__`'s parameter name is part of the contract.** `HarnessProposer._gate` names its parameter `candidate` rather than `weight` precisely because `gate(weight=...)` and `gate(candidate=...)` are different signatures for anything callable by keyword (`src/pneuma/casestudy/harnesslearn.py:660-663`), against the protocol at `src/pneuma/gated.py:98`.

**Drift risk:** Adding a third required member to `Verdict` would break `Admission` with no import edge to follow and no compile-time error — the protocol has zero `src/` import sites, so a type checker sees the mismatch only at `GatedProposer.__init__`'s `gate: Gate` annotation. Mitigation: `tests/library/test_gated.py:187-191` asserts protocol satisfaction with `isinstance` against `@runtime_checkable`, which is the guard that fires.

## `detect.discrimination.Discrimination` — the three-valued shared verdict

**Producer:** `src/pneuma/detect/discrimination.py:44-70`

**Consumer(s):**

- `src/pneuma/detect/vacuity.py:50` — `RuleVerdict.discrimination` builds one per rule (`src/pneuma/detect/vacuity.py:430-437`), and `vacuous` is derived from `.idle` (`src/pneuma/detect/vacuity.py:456`).
- `src/pneuma/detect/objective.py:51` — `_check_components` builds one per declared objective term (`src/pneuma/detect/objective.py:1467-1474`).
- `src/pneuma/detect/gaming.py:41` — two more, at `src/pneuma/detect/gaming.py:141` and `src/pneuma/detect/gaming.py:329`.
- `src/pneuma/casestudy/harnesslearn.py:56` — `emptying_margin` (`src/pneuma/casestudy/harnesslearn.py:288`) and `rule_liveness` (`src/pneuma/casestudy/harnesslearn.py:364`).
- `src/pneuma/detect/__init__.py:46` — re-exported.

**Shape:**

```python
@dataclass(frozen=True)
class Discrimination:
    """A measurement of whether one check can tell its two cases apart."""

    subject: str
    observations: int
    separating: int
    withheld: tuple[str, ...] = ()
    unit: str = "observation"
    kind: str = "check"
```

**Assumptions consumers make:**

- **`discriminates` is three-valued and must never collapse to a bool.** `True` / `False` / `None` are documented as separate findings (`src/pneuma/detect/discrimination.py:13-24`), and the derivation is `separating` first, then `withheld`, then `False` (`src/pneuma/detect/discrimination.py:82-86`). Under a bool, "no witness found" and "the search gave up" would be the same answer.
- **Every bound a caller applies must appear in `withheld`, or it is a silent cap.** Stated at `src/pneuma/detect/discrimination.py:56-59`. `RuleVerdict.discrimination` puts both of its bounds there and names which search gave up (`src/pneuma/detect/vacuity.py:419-429`); `_check_components` puts its two there (`src/pneuma/detect/objective.py:1455-1464`).
- **`observations == 0` with empty `withheld` is a finding, not an abstention.** Only the caller knows whether the emptiness belongs to the subject or to the harness (`src/pneuma/detect/discrimination.py:26-31`). `harnesslearn.emptying_margin` uses exactly this distinction: a refusal reports `observations=walked, separating=0` (`src/pneuma/casestudy/harnesslearn.py:304-310`) while a check that never ran reports `observations=0` *with* a `withheld` reason (`src/pneuma/casestudy/harnesslearn.py:311-321`).
- **`observations` is a reference scale, not a strict denominator.** Not validated against `separating`, and no ratio is exposed that would imply it was (`src/pneuma/detect/discrimination.py:49-54`) — because `vacuity`'s relaxed sweep can reach more states than the exact one counted.
- **`separating` may be gathered at a wider level than `observations`.** `RuleVerdict.witnesses` maxes `exact` against `free_guards` (`src/pneuma/detect/vacuity.py:399`) and feeds that as `separating` while `observations` is the exact sweep's scope count (`src/pneuma/detect/vacuity.py:430-437`).
- **The polarity may be inverted, and then containment is only evidence on a completed sweep.** `detect/gaming.py` reports `separating=0` whenever an exploit exists, and zeroes a truncated sweep's contained count so it cannot settle `True` with the exploit in the unexamined tail (`src/pneuma/detect/gaming.py:133-138`, `src/pneuma/detect/gaming.py:140-152`).
- **A margin does not fit and is deliberately excluded.** `memory.turso_backend`'s retrieval discrimination is a margin between two distance distributions plus a self-retrieval check, and forcing it into a numerator would either lose the margin or make `separating` meaningless (`src/pneuma/detect/discrimination.py:33-36`).

**Drift risk:** Adding a `ratio` property would invite consumers to divide `separating` by `observations`, which the docstring says is invalid for `vacuity`'s relaxed sweeps. Mitigation: the field docs already refuse it explicitly (`src/pneuma/detect/discrimination.py:49-54`); a new derived property must state which callers it is valid for.

## `detect.vacuity.System` — the two-method sweep protocol

**Producer:** `src/pneuma/detect/vacuity.py:113-131`

**Consumer(s):**

- `src/pneuma/detect/vacuity.py:234` and `src/pneuma/detect/vacuity.py:255` — `sweep` calls `starts()` and `successors(...)`.
- `src/pneuma/detect/adapter.py:45-83` — `ProcessSystem` satisfies it for the process IR, without importing the name.
- `src/pneuma/detect/adapter.py:86-92` — `system_for` is `audit`'s `build` argument (`src/pneuma/detect/vacuity.py:606`).
- `src/pneuma/detect/__init__.py:69` — the only import site in the repo, a re-export.

**Shape:**

```python
@runtime_checkable
class System(Protocol):
    """A guarded transition system, reduced to the two things a sweep needs."""

    def starts(self) -> Iterator[tuple[str, dict[str, Value]]]:
        """Yield every initial `(location, assignment)`. May be large; keep it lazy."""
        ...

    def successors(
        self, location: str, assignment: Assignment
    ) -> Iterator[tuple[str, str, dict[str, Value]]]:
        """Yield `(edge_name, target_location, successor_assignment)` for one state."""
        ...
```

**Assumptions consumers make:**

- **`starts()` must stay lazy, because seeding is itself budgeted.** `sweep` breaks out of the seeding loop once `len(seen) >= limit` (`src/pneuma/detect/vacuity.py:234-237`) — a system with many free variables can have more starting states than another's whole reachable space, and a cap applied only to expansion would be a cap that lies (`src/pneuma/detect/vacuity.py:216-219`).
- **An exception out of `successors` is a modelling error, not "no successors".** `sweep` catches and re-raises as `SweepError` naming the state (`src/pneuma/detect/vacuity.py:254-257`), because treating a broken guard as not-enabled would silently shrink the state space — the same failure the module detects (`src/pneuma/detect/vacuity.py:195-201`).
- **Relaxation lives in the implementation's walk, never in a rewritten spec.** `ProcessSystem` carries `free_guards`/`free_initial` flags and applies them in `starts` and `successors` (`src/pneuma/detect/adapter.py:59-83`), so the object the checker sees and the object the detector sweeps are the same object (`src/pneuma/detect/adapter.py:50-53`).
- **The relaxation order in `RELAXATIONS` is load-bearing and `free_initial` must stay first.** Stated at `src/pneuma/detect/vacuity.py:20-28` and encoded at `src/pneuma/detect/vacuity.py:61`. The sound widening precedes the unsound one, and `adapter.system_for` maps the level names onto its two flags (`src/pneuma/detect/adapter.py:88-92`) — reordering one without the other inverts the gate.
- **Effects accumulate into one successor dict in transition order.** `ProcessSystem.successors` copies the assignment then applies each effect against the accumulating dict (`src/pneuma/detect/adapter.py:80-82`), matching `interpreter.run` (`src/pneuma/process/interpreter.py:261-262`) rather than a second reading of the spec.
- **`Visit.successors` carries edge names because out-degree is not a predicate over the assignment.** Declared at `src/pneuma/detect/vacuity.py:76-82`, built at `src/pneuma/detect/vacuity.py:259-264`, and consumed by `adapter.structural_rules`' deadlock check (`src/pneuma/detect/adapter.py:130-131`).

**Drift risk:** A third required method would break `ProcessSystem` with no import edge to follow — `adapter.py` never imports `System`. Mitigation: `src/pneuma/detect/vacuity.py:39-41` names `adapter.py` as the file you *replace* rather than edit, so the adapter is the single known implementation and the protocol's cost of change is bounded to it.

## `detect.objective.Domain` / `Space` / `Structure` / `Component` — the probe's declaration surface

**Producer:** `src/pneuma/detect/objective.py:79` (`Domain`), `src/pneuma/detect/objective.py:60` (`Space`), `src/pneuma/detect/objective.py:146` (`Structure`), `src/pneuma/detect/objective.py:198` (`Component`), consumed by `probe` at `src/pneuma/detect/objective.py:669`.

**Consumer(s):**

- `src/pneuma/casestudy/minelearn.py:60` — `METRIC_DOMAINS` (`src/pneuma/casestudy/minelearn.py:571-585`), `METRIC_STRUCTURE` (`src/pneuma/casestudy/minelearn.py:587-591`), and both probe calls (`src/pneuma/casestudy/minelearn.py:767`, `src/pneuma/casestudy/minelearn.py:792`).
- `src/pneuma/casestudy/harnesslearn.py:57` — the harness's `Structure` and two `Component`s (`src/pneuma/casestudy/harnesslearn.py:257-263`).
- `src/pneuma/detect/adversary.py:52` — imports `Brief` and `Degenerate` to implement the `Search` seam.
- `src/pneuma/detect/__init__.py:47` — re-exported.

`Domain`, `Space`, and `probe` each have 9 import sites; `Structure` 7, `Severity` 6, `Component` 3 (all in `src/`).

**Shape:**

```python
class Space(Enum):
    """Which space a sweep is over. Required, never defaulted: see the module docstring."""

    METRIC = "metric"
    """Axes are the objective's numeric inputs, varied freely and independently."""

    DECISION = "decision"
    """Axes are what the optimizer controls; the objective composes in the measurement."""


@dataclass(frozen=True)
class Domain:
    """The declared feasible range of one input."""

    name: str
    low: float
    high: float

    integral: bool = False
    bounded_by: str | None = None
    feasible: tuple[float, float] | None = None
```

**Assumptions consumers make:**

- **`space` is required and never defaulted, and passing the wrong one silently disables checks.** In `METRIC` space the boundary-max check does not run (`src/pneuma/detect/objective.py:821-826`), degenerate inputs are not enumerated (`src/pneuma/detect/objective.py:1212-1233`), and `emptying-is-free` is skipped (`src/pneuma/detect/objective.py:1347-1353`) — all reported as notes rather than findings. `minelearn.probe_the_objective` therefore runs both spaces and merges (`src/pneuma/casestudy/minelearn.py:767-800`), and states in prose that skipping the decision half loses the checks that catch a dead coverage term (`src/pneuma/casestudy/minelearn.py:781-786`).
- **`feasible` is distinct from `low`/`high`, and the boundary check reads it.** Documented at `src/pneuma/detect/objective.py:82-87`; `minelearn` declares `feasible=(1.0, float(top))` on its threshold axis (`src/pneuma/casestudy/minelearn.py:794`), which is what `_check_boundary` clips against (`src/pneuma/detect/objective.py:1640-1644`).
- **A `bounded_by` string downgrades a refusal to a warning, so it is load-bearing prose.** `_check_escape` picks severity from it (`src/pneuma/detect/objective.py:1537-1538`) and records it on the finding as `downgraded_by` (`src/pneuma/detect/objective.py:1552`). `minelearn`'s three metric domains each carry one naming the code that establishes the bound (`src/pneuma/casestudy/minelearn.py:572-584`).
- **`Structure.viable` is separate from `size > 0` because an empty answer and an unrepresentable one are different.** Stated at `src/pneuma/detect/objective.py:158-166`; `METRIC_STRUCTURE` declares both explicitly (`src/pneuma/casestudy/minelearn.py:587-591`).
- **A `Component`'s term may legitimately raise, and returning zero instead would be a lie.** `Component.measure` returns `None` on an exception (`src/pneuma/detect/objective.py:236-243`) and `_check_components` counts that into `withheld` (`src/pneuma/detect/objective.py:1455-1459`). `harnesslearn`'s term reader raises rather than returning zero, and says why: a zero would make a dead term look like it moved (`src/pneuma/casestudy/harnesslearn.py:246-251`).
- **A caller-declared `degenerate` list is checked but must not be relied on.** `probe` merges declarations with enumerated and searched candidates (`src/pneuma/detect/objective.py:758-794`), and the docstring tells callers to prefer `structure` because a hand-written list is written by the same hand as the formula (`src/pneuma/detect/objective.py:691-698`).
- **A searcher's claim is never the evidence.** Every candidate a `Search` proposes is re-scored in `_check_degenerate` (`src/pneuma/detect/objective.py:1142`), which `probe` records as a note (`src/pneuma/detect/objective.py:789-792`) and the `Search` alias itself states (`src/pneuma/detect/objective.py:335-340`).

**Drift risk:** A new check in `probe` that is decision-space-only but forgets the `Space` guard would fire on every sound metric objective, because the ideal corner is a boundary and is supposed to win (`src/pneuma/detect/objective.py:13-18`). Mitigation: the two existing space-gated checks both return a note explaining the skip rather than silently passing (`src/pneuma/detect/objective.py:1226-1232`, `src/pneuma/detect/objective.py:1348-1353`) — a new one copies that shape.

## `team.Recruit` — three verbs, satisfied by two unrelated classes

**Producer:** `src/pneuma/team/members.py:25-59`

**Consumer(s):**

- `src/pneuma/team/core.py:246-248` — `Team.run` calls `spawn`, reading only `.id` off the result.
- `src/pneuma/team/hooks/briefing.py:85-88` — the `Briefing` hook calls `ask` behind an `asyncio.gather` barrier.
- `src/pneuma/team/core.py:277-281` — the `finally` calls `retire` on every member; `Hiring.on_teardown` retires the hires (`src/pneuma/team/hooks/hiring.py:351-358`).
- `src/pneuma/team/members.py:62-181` — `Member`, the library's own adapter for a `MethodAgent`.
- `src/pneuma/demo/agent.py:138-161` — `demo.agent.Agent` satisfies it as written, with no adapter.
- `src/pneuma/demo/staffing.py` and the hook library annotate against it.

`Recruit` has 7 import sites, 2 of them in `src/`.

**Shape:**

```python
@runtime_checkable
class Recruit(Protocol):
    """A member or a hire: something with a name that spawns, answers once, and retires."""

    name: str

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        """Put this recruit on a live thread as a child of `parent_id`."""
        ...

    async def ask(self, request: str) -> Any:
        """Run one cycle with `request` and return its result."""
        ...

    async def retire(self) -> None:
        """Tear the thread down. Idempotent — an unwind loop must not crash mid-unwind."""
        ...
```

**Assumptions consumers make:**

- **`retire` must be idempotent, and the unwind depends on it.** Declared in the protocol (`src/pneuma/team/members.py:57-58`), honoured by `MethodThread.retire` which suppresses `ThreadNotFoundError` and returns early when already dead (`src/pneuma/method.py:303-307`). The core's `finally` and `Hiring.on_teardown` can therefore overlap harmlessly (`src/pneuma/team/core.py:263-283`, `src/pneuma/team/hooks/hiring.py:351-358`), and `dismiss` retires *before* unregistering so a failed retire leaves the recruit reachable for a retry (`src/pneuma/team/hooks/hiring.py:257-273`).
- **`name` is the only identity guaranteed, which makes duplicate names a silent loss.** Each member becomes a tool named after it on the lead's wire, and two tools sharing a name shadow silently, so `_check_no_duplicate_member_names` refuses a colliding cast at construction — including the dot-mapped collision (`a.b` vs `a_b`) the wire mapping creates (`src/pneuma/team/core.py:415-437`).
- **`spawn`'s return is `Any` and only `.id` is read.** Stated at `src/pneuma/team/members.py:42-45`; demanding a `ThreadHandle` would exclude `MethodThread`. `Hiring`'s `commission` reads it at `src/pneuma/team/hooks/hiring.py:150`.
- **`notify` and `equip` are *not* in the protocol, so both are probed with `getattr`.** The core's tool fold skips a recruit without `equip` (`src/pneuma/team/core.py:388-411`), the `Worklog` hook opens no channel for a handle without `notify` (`src/pneuma/team/hooks/worklog.py:227-243`), and `Hiring` does both for a hire (`src/pneuma/team/hooks/hiring.py:362-390`). A mixed cast is legitimate, not a fault.
- **Negotiation objections travel through `ask`, not `notify`, because an answer is wanted.** `notify` by construction produces no answer, and the `Member` adapter deliberately does not wrap it (`src/pneuma/team/hooks/negotiation.py:62-65`, `src/pneuma/team/members.py:84-85`).
- **A member's failure is a rendered string, not an exception — except when every member fails.** The `Briefing` hook uses `return_exceptions=True` and renders failures with the `BRIEFING_ERROR` prefix (`src/pneuma/team/hooks/briefing.py:85-96`), while `_check_some_briefing_survived` raises when all of them did, and explains the asymmetry (`src/pneuma/team/hooks/briefing.py:113-133`).
- **A mandate reaches a hire through the factory, never by attribute injection.** The protocol says nothing about a mandate, and injecting one would fail on a `__slots__` recruit or silently create a dead field (`src/pneuma/team/hooks/hiring.py:84-90`).

**Drift risk:** Adding a fourth verb would break `demo.agent.Agent`, which satisfies the protocol by coincidence of shape rather than by declaration (`src/pneuma/demo/agent.py:138-161`) and is cited as the reason the protocol has exactly three (`src/pneuma/team/members.py:29-33`). Mitigation: optional capabilities go through the `getattr` probe pattern the core's tool fold and the worklog's channel opener already use, not through the protocol.

## `detect.vacuity.RuleVerdict` — a record with a declared compatibility surface

**Producer:** `src/pneuma/detect/vacuity.py:327-353`

**Consumer(s):**

- `src/pneuma/detect/adapter.py:27` — `verdict_for` returns one (`src/pneuma/detect/adapter.py:217-228`).
- `src/pneuma/casestudy/rules.py:134` — `Liveness = RuleVerdict`, a bare alias.
- `src/pneuma/casestudy/rules.py:272` and `src/pneuma/casestudy/rules.py:324` — `Governed.measured` is keyed to it, and `live` / `vacuous` / `unknown` read `.live`'s three values (`src/pneuma/casestudy/rules.py:281-291`).
- `src/pneuma/casestudy/harnesslearn.py:359-368` — `rule_liveness` reads `governed.unknown` and `governed.live`.
- `src/pneuma/detect/__init__.py:69` — re-exported.

**Shape:**

```python
@dataclass(frozen=True)
class RuleVerdict:
    """One rule, and whether its pass would mean anything.

    `live` / `violating_states` / `antecedent_states` / `truncated` are a compatibility
    surface: `casestudy.rules.Liveness` is an alias for this record, so a caller that
    annotates against that name reads those four fields and must keep working.
```

**Assumptions consumers make:**

- **Four fields are frozen as a compatibility surface for the `Liveness` alias.** Named in the class docstring (`src/pneuma/detect/vacuity.py:331-335`) and confirmed from the consumer side, where the alias is annotated as getting "the record with the three-valued `live`, a named cause, a shortest witness trace and per-relaxation counts" (`src/pneuma/casestudy/rules.py:130-134`).
- **`live` is three-valued and each branch is a distinct verdict.** `True` on a violation, `None` on a truncated exact sweep, `False` otherwise (`src/pneuma/detect/vacuity.py:380-382`). `Governed` splits its precedences on exactly those three (`src/pneuma/casestudy/rules.py:281-291`), and `harnesslearn.rule_liveness` routes `unknown` into `Discrimination.withheld` rather than counting it either way (`src/pneuma/casestudy/harnesslearn.py:359-368`).
- **`relaxation_truncated` is separate from `truncated`, because the levels bind at wildly different sizes.** With n free booleans `free_initial`'s start set is 2^n against `exact`'s one (`src/pneuma/detect/vacuity.py:355-368`), so a model can finish at `exact` and exhaust the budget before `free_guards` — the level that earns a guarded rule its pass. Set by `audit` from its `abandoned` set (`src/pneuma/detect/vacuity.py:654-659`, `src/pneuma/detect/vacuity.py:678`).
- **`witnesses` deliberately excludes `free_initial`.** It maxes `exact` against `free_guards` only, because a checker pins initial values exactly as the model does (`src/pneuma/detect/vacuity.py:385-399`). `adapter.witness_counts` feeds that straight into `tla.CheckResult.with_witnesses` (`src/pneuma/detect/adapter.py:208-214`), which downgrades a `verified` outcome to `vacuous` when any count is zero (`src/pneuma/process/tla.py:90-101`).
- **`vacuous` is derived from `discrimination.idle`, never re-derived.** So the flag, the gate, and the shared primitive can never disagree (`src/pneuma/detect/vacuity.py:439-456`).
- **`witness_counts()` and `vacuous` deliberately disagree on a truncated rule, in the safe direction.** A truncated rule reports zero witnesses (so the pass is withdrawn) while `vacuous` stays `False` (`src/pneuma/detect/vacuity.py:578-591`) — "we do not know" is not "this is decoration", but neither is a pass.
- **`gates` stays outside the shared primitive on purpose.** It is a statement about what a pass would *mean*, not about discrimination; every correct process has a `TypeOK` that cannot fail (`src/pneuma/detect/vacuity.py:449-455`). `adapter.structural_rules` marks both `NoDeadlock` and `TypeOK` non-gating (`src/pneuma/detect/adapter.py:147`, `src/pneuma/detect/adapter.py:153`).

**Drift risk:** Renaming any of the four compatibility fields breaks `rules.Liveness` annotations with no error at the alias site — the alias is a bare assignment (`src/pneuma/casestudy/rules.py:134`). Mitigation: the field group is documented as a compatibility surface in the producer's own docstring (`src/pneuma/detect/vacuity.py:331-335`), and `tests/app/test_vacuity_on_real_logs.py:235` asserts the alias identity.

## `casestudy.aimine.Discovered` — validated twice, and one consumer bypasses both

**Producer:** `src/pneuma/casestudy/aimine.py:59-68`, with `Edge` at `src/pneuma/casestudy/aimine.py:51-56`.

**Consumer(s):**

- `src/pneuma/casestudy/aimine.py:202` — wired as `Miner.discover`'s output type with two post-conditions.
- `src/pneuma/casestudy/aimine.py:315-396` — `to_process` compiles it into the IR.
- `src/pneuma/casestudy/aimine.py:279-312` — `observed_threshold` derives the real cutoff from the log.
- `src/pneuma/casestudy/harnesslearn.py:198` and `src/pneuma/casestudy/harnesslearn.py:220-229` — constructs one directly, inside the swept objective.
- `src/pneuma/casestudy/minelearn.py:72` — imports it alongside `Edge` and `grade`.

**Shape:**

```python
class Discovered(BaseModel):
    """A process the agent discovered, plus its account of how."""

    start_activity: str = Field(description="The activity cases begin at")
    terminal_activities: list[str] = Field(description="Activities where cases end")
    edges: list[Edge] = Field(description="The handoffs worth keeping")
    threshold_used: int = Field(description="The support cutoff the agent chose")
    method: str = Field(
        description="Two or three sentences: what you computed, and why you cut where you did"
    )
```

**Assumptions consumers make:**

- **Pydantic validation is not the whole gate: two module-level post-conditions are.** `rejects_a_disconnected_model` requires every edge activity to be reachable from `start_activity` (`src/pneuma/casestudy/aimine.py:146-161`) and `rejects_a_misreported_threshold` requires `threshold_used` not to exceed the weakest edge's own `cases` (`src/pneuma/casestudy/aimine.py:164-183`). Both are attached at `src/pneuma/casestudy/aimine.py:202`.
- **`harnesslearn` constructs a `Discovered` directly and so is subject to neither post-condition.** `src/pneuma/casestudy/harnesslearn.py:220-229` builds one inside the objective from already-filtered handoffs; the model is a probe input rather than an agent proposal, so `to_process`'s own normalisation is the only guard it passes through.
- **`threshold_used` is a self-report and must never configure the baseline.** Stated in the module header (`src/pneuma/casestudy/aimine.py:28-32`), in `Graded` (`src/pneuma/casestudy/aimine.py:80-83`), and in `grade` with the measured cost: claiming a cutoff of 300 while leaving the edges untouched drops the baseline from 96.4% to 59.1% and turns a tie into a 37.2-point win (`src/pneuma/casestudy/aimine.py:408-417`).
- **`Edge.cases` is also agent-authored, so support is recounted from the log.** `observed_threshold` builds its support map from `directly_follows(events)` rather than from the edges (`src/pneuma/casestudy/aimine.py:305-312`), and says that trusting either field lets the agent choose its opponent's handicap (`src/pneuma/casestudy/aimine.py:283-287`).
- **`to_process` normalises rather than raises, because the agent is untrusted.** It prunes unreachable islands (`src/pneuma/casestudy/aimine.py:328-333`), drops self-loops as rework markers (`src/pneuma/casestudy/aimine.py:381-382`), truncates transition names to 60 characters and dedupes (`src/pneuma/casestudy/aimine.py:384-388`), and falls back three times to find a terminal state so the IR is not rejected outright (`src/pneuma/casestudy/aimine.py:345-366`).
- **Every compiled state carries `agent_method="handle"`, a placeholder no agent implements.** Set at `src/pneuma/casestudy/aimine.py:372` and at `src/pneuma/casestudy/miner.py:125`. `ProcessAgent.handler_for` returns `None` for an unrecognised name rather than raising, precisely so a mined process stays runnable (`src/pneuma/process/agent.py:169-173`), and `casestudy.handlers.handler_for` treats `agent_method` as an opt-in gate with a table as the real source of truth (`src/pneuma/casestudy/handlers.py:300-304`, `src/pneuma/casestudy/handlers.py:228-233`).

**Drift risk:** A new required field on `Discovered` would break `harnesslearn`'s direct construction at `src/pneuma/casestudy/harnesslearn.py:220-229` loudly, which is the good case. The quiet one is a new post-condition on `Miner.discover`, which that construction would not run at all. Mitigation: a check that must hold for every `Discovered` belongs in `to_process` or in a `model_validator`, not only in the post-condition list.

## The nine-column event-log frame — a contract no type declares

**Producer:** `src/pneuma/casestudy/eventlog.py:43-110` (`parse_xes`), which emits `case_id`, `activity`, `timestamp`, `resource`, `group`, `channel`, `department` from the XES rows (`src/pneuma/casestudy/eventlog.py:68-78`) plus a derived `ts` (`src/pneuma/casestudy/eventlog.py:105`) and `position` (`src/pneuma/casestudy/eventlog.py:109`).

**Consumer(s):**

- `src/pneuma/casestudy/transcriptlog.py:424-469` — `_finalise` reproduces the exact nine columns in a `.select(...)`.
- `src/pneuma/casestudy/transcriptlog.py:256-273` — `to_events` states the contract in prose.
- `src/pneuma/casestudy/miner.py:42-55` — `directly_follows` sorts by `case_id, position` and shifts `activity`.
- `src/pneuma/casestudy/miner.py:58-71` — `start_and_end_activities` reads `position` extremes per case.
- `src/pneuma/casestudy/eventlog.py:213-232` — `persist_events` selects eight of the columns and formats `ts`.
- `src/pneuma/casestudy/eventlog.py:113-128` — `case_durations` aggregates `ts` and `channel`.
- `src/pneuma/casestudy/aimine.py:250` — `to_csv` selects `case_id, position, activity`.
- `src/pneuma/casestudy/rules.py:92` — `derive_precedences`.

**Shape:** No type declares this. The frame is defined by two `select`/construction sites that must agree:

```python
        .select(
            "case_id",
            "activity",
            "timestamp",
            "resource",
            "group",
            "channel",
            "department",
            "ts",
            "position",
        )
```

with the dtype contract stated in prose:

```
    The returned frame carries the same nine columns `parse_xes` produces, with the
    same dtypes, so `miner.mine`, `rules.derive_precedences`, `pipeline` and
    `eventlog.persist_events` all work on it unchanged.
```

**Assumptions consumers make:**

- **`position` is a dense per-case `Int32` rank starting at 1.** `parse_xes` builds it with an ordinal rank over `case_id` cast to `Int32` (`src/pneuma/casestudy/eventlog.py:109`); `_finalise` builds it with `int_range(1, len+1)` over `case_id`, also `Int32` (`src/pneuma/casestudy/transcriptlog.py:456`). `start_and_end_activities` compares against `min()`/`max()` per case, so a gap or a zero base would misidentify start and end activities (`src/pneuma/casestudy/miner.py:60`, `src/pneuma/casestudy/miner.py:66`).
- **Events are sorted before `position` is assigned, and consumers re-sort anyway.** `parse_xes` sorts by `case_id, ts` (`src/pneuma/casestudy/eventlog.py:108`), `_finalise` by `case_id, ts, tool_use_id` — the extra tiebreak makes `position` deterministic across same-millisecond parallel calls (`src/pneuma/casestudy/transcriptlog.py:454`). `directly_follows` re-sorts by `case_id, position` regardless (`src/pneuma/casestudy/miner.py:49`).
- **`ts` is a naive UTC datetime, not offset-aware.** `parse_xes` parses the XES offset explicitly then strips the zone, so durations across a DST boundary are real elapsed time (`src/pneuma/casestudy/eventlog.py:95-105`); `_finalise` notes `claude-sql` returns naive UTC already (`src/pneuma/casestudy/transcriptlog.py:427-430`). `persist_events` formats it with `strftime` (`src/pneuma/casestudy/eventlog.py:219`) and `case_durations` subtracts it (`src/pneuma/casestudy/eventlog.py:125`).
- **The four XES-only columns must be filled with a real analogue, not left empty.** `to_events` fills `resource`/`group`/`channel`/`department` with model, MCP server, git branch, and cwd, and says why (`src/pneuma/casestudy/transcriptlog.py:270-273`); `_finalise` derives `group` from the tool's server so the resource-style columns stay meaningful after activities collapse to a family (`src/pneuma/casestudy/transcriptlog.py:448-453`).
- **`group` needs SQL quoting, because it is a reserved word.** `persist_events` and `read_events` both quote it (`src/pneuma/casestudy/eventlog.py:227`, `src/pneuma/casestudy/eventlog.py:238`), against the schema at `src/pneuma/casestudy/eventlog.py:155`.
- **Sampling is by whole case, never by row.** `to_csv` filters on a set of `case_id` values (`src/pneuma/casestudy/aimine.py:251-253`) because half a case is a different process and a truncated trace would teach a handoff that does not exist (`src/pneuma/casestudy/aimine.py:246-248`).

**Drift risk:** A tenth column added to `parse_xes` and not to `_finalise` breaks nothing at the boundary — both frames still satisfy every consumer's `select` — until a consumer reads the new column, at which point the transcript path raises a `ColumnNotFound` far from either producer. Mitigation: the contract is currently held by prose in two docstrings (`src/pneuma/casestudy/transcriptlog.py:266-273`, `src/pneuma/casestudy/transcriptlog.py:425`); a shared column tuple both `select` calls read would make the pair mechanically checkable.

## `gated.GatedProposer` — the propose-and-be-judged skeleton

**Producer:** `src/pneuma/gated.py:101-423`

**Consumer(s):**

- `src/pneuma/casestudy/harnesslearn.py:67` — `HarnessProposer` subclasses it (`src/pneuma/casestudy/harnesslearn.py:572`), overriding `gate` (`src/pneuma/casestudy/harnesslearn.py:602`), `REASK` (`src/pneuma/casestudy/harnesslearn.py:616-619`), and `candidate_of` (`src/pneuma/casestudy/harnesslearn.py:677-684`).
- `src/pneuma/casestudy/harnesslearn.py:1041` — `train` reads `proposer.rejected` and renders each entry's `report_text()`.
- `tests/library/test_gated.py:30`, `tests/library/test_model_cache.py:29`, `tests/app/test_kernel_live.py:140`.

**Shape:**

```python
class GatedProposer(MethodAgent):
    """An agent whose proposal is judged by the gate it is graded against.

    The subclass supplies the propose `@ai_method` and the gate; this base owns the skeleton:
    the post-condition, the `rejected` ledger, the wiring guard, and the beam search.
```

with the two composition points:

```python
    def gated(self, method: str = "propose", **overrides: Any) -> AIFunction[..., Any]:
        extra = tuple(overrides.pop("post_conditions", ()))
        self._check_no_collision(method, *extra)
        return self.compiled(method, post_conditions=(self.admits, *extra), **overrides)
```

**Assumptions consumers make:**

- **A post-condition's first parameter must not share a name with a propose parameter, and this is enforced at wiring time.** The runtime passes the result positionally *and* injects bound arguments by keyword, so a collision raises `TypeError: got multiple values for argument` which is then reported to the model as a validation failure — the gate appears to refuse everything for a reason no model can act on (`src/pneuma/gated.py:285-321`). Which is why `admits` names its parameter `response` rather than `proposal` (`src/pneuma/gated.py:171-177`).
- **`gated()` prepends rather than replaces, so a subclass cannot lose the gate.** `src/pneuma/gated.py:271-283`; the same argument at team scale is the core's `_lead_hook`, which recomposes the lead's own hook and `tools=` into the one hook it installs so a lead loses nothing (`src/pneuma/team/core.py:326-355`).
- **`rejected` holds refusals and nothing else.** A gate *fault* is re-raised as an internal-failure message and deliberately not recorded (`src/pneuma/gated.py:20-23`, `src/pneuma/gated.py:134-140`), so a loop that silently re-asked and then succeeded is distinguishable from one whose gate never fired.
- **`candidate_of` must stay synchronous and side-effect-free.** It runs inside a validator on every attempt, so an override that fetched or computed anything would put the gate's own work behind a hook named for an accessor (`src/pneuma/gated.py:144-157`). `HarnessProposer` overrides it to a single attribute read (`src/pneuma/casestudy/harnesslearn.py:684`).
- **The gate is a *value* on `self`, not an abstract method, and that is what keeps the library boundary honest.** The gates worth having need polars, a process miner, and a reachability sweep — none of which the library half may import (`src/pneuma/gated.py:85-96`), enforced by `tests/library/test_boundary.py:50`. `HarnessProposer` binds a closure over its event log from the inside (`src/pneuma/casestudy/harnesslearn.py:642`, `src/pneuma/casestudy/harnesslearn.py:644-675`).
- **`propose_k` does *not* attach the gate as a post-condition, so `k` means what it says.** One shot per branch then filter, because a branch that quietly re-asked until admitted would make `k` a count of branches-that-eventually-succeeded (`src/pneuma/gated.py:356-363`). It also imposes no ordering and computes no scalar — "best" is the caller's to define (`src/pneuma/gated.py:381-385`).
- **`propose_k`'s branches are byte-identical up to the cache point, and they run serially for that reason.** Branch 0's response writes the provider cache before branch 1's request (`src/pneuma/gated.py:371-379`).
- **Only an async gate reaches `judge`; `admits` refuses a coroutine.** `src/pneuma/gated.py:180-182`, `src/pneuma/gated.py:207-214`.

**Drift risk:** `_check_no_collision` checks only the *first* parameter, deliberately, because forbidding the rest would forbid the keyword injection the runtime documents (`src/pneuma/gated.py:298-300`). A subclass adding a post-condition whose second parameter shadows something is therefore unprotected. Mitigation: the trap only bites the result slot, and `gated()` runs the guard over every attached condition rather than the gate alone (`src/pneuma/gated.py:280-283`).

## Other contracts

- **`team.TeamRun`** (`src/pneuma/team/core.py:149-170`) — the published run artifact: `answer`, the core's own `transcript` (member calls, revise rounds), and `hooks_data` verbatim. `answer: Any` because the lead's output type is the caller's choice. Its `model_serializer` drops `transcript` and `hooks_data` when empty, so a bare run's artifact stays one key (`src/pneuma/team/core.py:163-170`). The demo re-shapes it into its own `Investigation` (`src/pneuma/demo/warroom.py:46-79`).
- **`interpreter.Run` / `Step` / `Revisit`** (`src/pneuma/process/interpreter.py:156`, `src/pneuma/process/interpreter.py:144`, `src/pneuma/process/interpreter.py:29`) — the trace `run` returns. `Run.path` and `Run.rejections` are derived (`src/pneuma/process/interpreter.py:166-172`) and read by `pipeline.execute_case` (`src/pneuma/casestudy/pipeline.py:226-227`). `ProcessAgent` deliberately does not extend it, because that would mean editing the fixed interpreter's data structure to carry a subclass payload (`src/pneuma/process/agent.py:299-302`).
- **`interpreter.OnEnter`** (`src/pneuma/process/interpreter.py:56`) — takes the state *name* and nothing else, deliberately: whoever installs the hook already holds the `Process` (`src/pneuma/process/interpreter.py:52-55`, restated at `src/pneuma/process/interpreter.py:218-221`). One `src/` consumer, `ProcessAgent.work` (`src/pneuma/process/agent.py:319-329`).
- **`State.agent_method`** (`src/pneuma/process/ir.py:185`) — a string naming an `@ai_method`, with two resolution policies. `ProcessAgent.handler_for` treats it as a method name and returns `None` for anything unrecognised (`src/pneuma/process/agent.py:182-185`); `casestudy.handlers.handler_for` treats it as an opt-in gate and resolves through a table keyed by the log's activity label (`src/pneuma/casestudy/handlers.py:300-304`).
- **`team.hooks.Roster`** (`src/pneuma/team/hooks/hiring.py:47-64`) — per-run by workspace identity, replaced as `type(self._roster)()` so a narrowing subclass keeps its class (`src/pneuma/team/hooks/hiring.py:324-332`); `demo.staffing.Staff` is the narrowing (`src/pneuma/demo/staffing.py:27`). `Roster.log` and the worklog's entries are plain dicts on purpose — an audit surface a reader walks, not a contract a caller binds to.
- **`team.hooks.DISCOVERY_KINDS`** (`src/pneuma/team/hooks/worklog.py:44`) — a closed four-value vocabulary; a kind the model invents is refused as text so the model posts again (`src/pneuma/team/hooks/worklog.py:206-208`).
- **`process.tla.CheckResult`** (`src/pneuma/process/tla.py:53-116`) — `with_witnesses` is the seam `detect.adapter.witness_counts` feeds (`src/pneuma/process/tla.py:90-101`, `src/pneuma/detect/adapter.py:208-214`), and `ok` is derived from a conjunction of gates rather than any single string (`src/pneuma/process/tla.py:56-61`).
- **`process.agent.HandlerFailed`** (`src/pneuma/process/agent.py:71-80`) — deliberately not a `ProcessError`, so a code fault cannot arrive in a report as evidence about a guardrail.
- **`casestudy.rules.Governed`** (`src/pneuma/casestudy/rules.py:269-300`) — iterable as `(process, applied)` so existing two-tuple callers keep working while `skipped` and the liveness split are available (`src/pneuma/casestudy/rules.py:264-266`, `src/pneuma/casestudy/rules.py:274-275`).
- **`recall.Recalled`** (`src/pneuma/recall.py:65-100`) — an `Annotated` marker naming which memory field a parameter arrives from. `k=None` recalls the parameter whole and `k=<int>` searches, which are different mechanisms rather than a knob: only the search path carries meta naming the retrieved entries, so consolidation edits only those (`src/pneuma/recall.py:69-77`). `k=0` is refused at construction because `search` accepts it and returns nothing, silently (`src/pneuma/recall.py:89-100`), and two distinct markers on one annotation are refused rather than resolved by order (`src/pneuma/recall.py:127-131`). One `src/` consumer: `src/pneuma/casestudy/learning.py:61`, recalling into a parameter named `playbook` (`src/pneuma/casestudy/learning.py:343`), read back off `traced.inputs` (`src/pneuma/casestudy/learning.py:345`).
- **`detect.objective.Search` / `Brief`** (`src/pneuma/detect/objective.py:335`, `src/pneuma/detect/objective.py:307-332`) — the adversary seam. `Brief` hands over the prober's own view unedited, including the scoring source when the caller can supply it (`src/pneuma/detect/objective.py:308-315`); implemented in `src/pneuma/detect/adversary.py:52`.
- **The library/application boundary** (`tests/library/test_boundary.py:46-50`) — `{detect, gated, memory, method, model, process, recall, team}` against `{casestudy, demo}`, with `polars`, `libsql`, and `pm4py` forbidden on the library side. Enforced by an AST walk that catches a lazy import in a function body, with membership derived from the source tree so a new package fails until it is declared. This is a contract between the two halves of the codebase enforced by a test rather than by types, and it is the reason `gated.Gate` takes its gate as a value (`src/pneuma/gated.py:85-92`).

## See also

- [Impact analysis][impact-analysis] — 37 shared source files
- [Module map][module-map] — 31 shared source files
- [Processes][processes] — 28 shared source files
- [Business logic][business-logic] — 25 shared source files
- [Debugging guide][debugging-guide] — 21 shared source files

[impact-analysis]: impact-analysis.md
[module-map]: ../architecture/module-map.md
[processes]: ../behavior/processes.md
[business-logic]: business-logic.md
[debugging-guide]: debugging-guide.md
