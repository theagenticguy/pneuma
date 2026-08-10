# pneuma · Public API

The distribution installs one package, `pneuma`, and its root `__init__.py` is empty (`src/pneuma/__init__.py`) — there is no single re-export barrel. The surface below is the 30 symbols with the most inbound imports across `src/`, `tests/` and `tools/`, ordered by that count. Each fenced block is the declaration as it appears in source; for classes built through a constructor the `__init__` signature is quoted instead of the bare `class` line. Import from the module in the citation, or from the sub-package barrel that re-exports it (`pneuma.detect` at `src/pneuma/detect/__init__.py:150`, `pneuma.memory` at `src/pneuma/memory/__init__.py:24`).

### Process

```py
class Process(BaseModel):
```

A whole mined process validated as one pydantic model — `states`, `initial_state`, `variables`, `transitions`, `invariants` — so the TLA+ renderer only ever sees a well-formed spec.

`src/pneuma/process/ir.py:213`

### Transition

```py
class Transition(BaseModel):
```

One edge of the process — `name`, `source`, `target`, `guards`, `effects` — taken when its guards hold, applying its effects.

`src/pneuma/process/ir.py:162`

### ai_method

```py
def ai_method(output_type: type, /, **config: Any) -> Callable[[Callable[..., Any]], Any]:
```

Marks a method as an AI function whose prompt is its own docstring and whose output is the declared structured type.

`src/pneuma/method.py:79`

### State

```py
class State(BaseModel):
```

A step in the process, optionally handled by the `@ai_method` named in `agent_method`; states with no agent are pure control points.

`src/pneuma/process/ir.py:175`

### MethodAgent

```py
class MethodAgent:
```

An object whose `@ai_method`s compile to typed AI functions over its own state, so one agent hands another a typed tool per capability rather than a chat box.

`src/pneuma/method.py:326`

### Invariant

```py
class Invariant(BaseModel):
```

A safety property that must hold in every reachable state, expressed as `forbidden_state` plus `forbidden_when` guards.

`src/pneuma/process/ir.py:189`

### Variable

```py
class Variable(BaseModel):
```

A process variable with a finite domain — `low`/`high` for an integer one, `values` for a symbolic one — because TLC enumerates every combination.

`src/pneuma/process/ir.py:61`

### Domain

```py
@dataclass(frozen=True)
class Domain:
```

The declared feasible range of one objective input, keeping the hard `feasible` limit distinct from the `low`/`high` window being swept.

`src/pneuma/detect/objective.py:79-80`

### Space

```py
class Space(Enum):
```

Which space a sweep is over: `METRIC`, where axes are the objective's numeric inputs, or `DECISION`, where axes are what the optimizer controls.

`src/pneuma/detect/objective.py:60`

### probe

```py
def probe(
    objective: Objective,
    domains: Sequence[Domain],
    *,
    space: Space,
    structure: Structure | None = None,
    components: Sequence[Component] = (),
    degenerate: Sequence[Degenerate] = (),
    search: Search | None = None,
    source: str | None = None,
    resolution: int = DEFAULT_RESOLUTION,
    reach: float = DEFAULT_REACH,
    refine: int = DEFAULT_REFINE,
    growth: float = DEFAULT_GROWTH,
    trust_declared_bounds: bool = True,
) -> Probe:
```

Sweeps an objective over its declared domain and just outside it, then reports the pathologies found; call `raise_if_pathological()` on the returned `Probe` before a training loop runs.

`src/pneuma/detect/objective.py:669-684`

### Guard

```py
class Guard(BaseModel):
```

A comparison condition on one variable — `variable`, `op`, `value` — with the natural-language original kept in `stated_as`.

`src/pneuma/process/ir.py:109`

### TursoMemoryBackend

```py
    def __init__(
        self,
        schema: type[BaseModel],
        actor_id: str,
        path: Path | str | None = None,
        model: Model | str | None = None,
        embedder: Embedder | None = None,
        distance_ceiling: float | None = None,
        connection: turso.Connection | None = None,
    ) -> None:
```

Memory over Turso: addressable entries keyed by never-reused ids, vector retrieval through the supplied `embedder`, and numeric parameters learned from feedback scores.

`src/pneuma/memory/turso_backend.py:406-415`

### Team

```py
@dataclass
class Team:
```

Stands up a cast, runs a lead against an oracle and grades itself; the base owns the skeleton while `members`, `briefing`, `lead_function` and `oracle` are required subclass overrides.

`src/pneuma/team.py:779-780`

### Discrimination

```py
@dataclass(frozen=True)
class Discrimination:
```

A measurement of whether one check can tell its two cases apart, counting `separating` outcomes against `observations` with every applied bound named in `withheld`.

`src/pneuma/detect/discrimination.py:44-45`

### Structure

```py
@dataclass(frozen=True)
class Structure:
```

The shape of the answer space as a `size` callable, from which degenerate inputs follow mechanically instead of being hand-listed by the same author as the scoring formula.

`src/pneuma/detect/objective.py:146-147`

### Member

```py
    def __init__(
        self,
        agent: Any,
        method: str,
        *,
        parameter: str | None = None,
        **overrides: Any,
    ) -> None:
```

Adapts one `MethodAgent` capability into a `Recruit` by naming which typed `parameter` the briefing arrives as.

`src/pneuma/team.py:135-142`

### Recruit

```py
@runtime_checkable
class Recruit(Protocol):
```

The protocol a team member satisfies: a `name`, and three verbs — `spawn`, `ask`, `retire`.

`src/pneuma/team.py:71-72`

### threshold_objective

```py
def threshold_objective(
    events: pl.DataFrame, *, sample_cases: int | None = 400, baseline_threshold: int = 25
) -> tuple[Objective, Structure, int, tuple[Component, ...]]:
```

Builds the mining objective as a function of the one variable the learning loop moves, composed through the real scoring path rather than re-derived.

`src/pneuma/casestudy/minelearn.py:607-609`

### Severity

```py
class Severity(Enum):
```

How hard a probe finding bites: `REFUSE` findings clear `Probe.ok` and are raised, `WARN` findings are only recorded.

`src/pneuma/detect/objective.py:70`

### opus5

```py
def opus5(
    effort: Effort = "xhigh",
    *,
    max_tokens: int = 40_000,
    show_thinking: bool = True,
    cache: bool = True,
) -> BedrockModel:
```

Builds a Claude Opus 5 Bedrock model with adaptive thinking at the given effort and an automatic cache point.

`src/pneuma/model.py:15-21`

### Attempt

```py
@dataclass
class Attempt:
```

One mining attempt and how it scored, carrying the coverage, threshold and edge-share the feedback text is built from.

`src/pneuma/casestudy/minelearn.py:249-250`

### DOCUMENT

```py
DOCUMENT = "search_document"
```

The Cohere Embed v4 `input_type` for embedding stored text; `Embedder.embed` rejects any value that is not this or `QUERY`.

`src/pneuma/memory/embedding.py:49`

### QUERY

```py
QUERY = "search_query"
```

The Cohere Embed v4 `input_type` for embedding a search query rather than a stored document.

`src/pneuma/memory/embedding.py:50`

### Degenerate

```py
@dataclass(frozen=True)
class Degenerate:
```

An input the objective must not be maximised by, carrying its `label`, the `point`, what `found_by` it, and why it is worthless.

`src/pneuma/detect/objective.py:108-109`

### Effect

```py
class Effect(BaseModel):
```

An assignment to one variable, or an increment of an integer one.

`src/pneuma/process/ir.py:139`

### Roster

```py
@dataclass
class Roster:
```

Who a team hired, keyed by the name the model chose, plus the ordered `log` of every hire, delegation, dismissal and failure.

`src/pneuma/team.py:275-276`

### audit_process

```py
def audit_process(
    process: Process,
    *,
    limit: int = DEFAULT_LIMIT,
    relaxations: Sequence[Relaxation] = RELAXATIONS,
    structural: bool = True,
) -> Audit:
```

Sweeps every invariant in a process, the checker's own structural two included, because measuring one invariant on request is how a vacuous rule survives.

`src/pneuma/detect/adapter.py:178-184`

### BedrockCohereEmbedder

```py
    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        region_name: str = DEFAULT_REGION,
        client: Any | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> None:
```

Cohere Embed v4 via `bedrock-runtime.invoke_model`, with the boto3 client built lazily so importing the backend never requires credentials.

`src/pneuma/memory/embedding.py:153-158`

### Brief

```py
@dataclass(frozen=True)
class Brief:
```

Everything a degenerate-input searcher is shown — the `objective`, the swept `axes` and the `ceiling` every finding is measured against — handed over unedited.

`src/pneuma/detect/objective.py:307-308`

### GatedProposer

```py
    def __init__(self, gate: Gate) -> None:
```

An agent whose proposal is judged by the `gate` it is graded against; the base owns the post-condition, the rejection ledger and the beam search.

`src/pneuma/gated.py:132`

## See also

- [Module map][module-map] — 13 shared source files
- [Impact analysis][impact-analysis] — 12 shared source files
- [Business logic][business-logic] — 11 shared source files
- [Processes][processes] — 9 shared source files
- [Contract map][contract-map] — 9 shared source files

[module-map]: ../architecture/module-map.md
[impact-analysis]: ../insights/impact-analysis.md
[business-logic]: ../insights/business-logic.md
[processes]: ../behavior/processes.md
[contract-map]: ../insights/contract-map.md
