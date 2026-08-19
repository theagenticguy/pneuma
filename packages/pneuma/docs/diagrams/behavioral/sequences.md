# pneuma · Sequences

Three processes, diagrams only. Each participant is a module or class read from source; the citation list under each diagram maps every lifeline and message to its `path:LOC`.

## War-room investigation

```mermaid
sequenceDiagram
    participant CLI
    participant Room as WarRoom
    participant Team
    participant Brief as Briefing hook
    participant Spec as Specialists
    participant Lead

    CLI->>Room: investigate(coordinator)
    Room->>Team: run(question)
    Team->>Spec: spawn as children of the lead
    Team->>Brief: on_assemble
    Brief->>Spec: ask the opening, barrier
    Spec-->>Brief: briefings or error strings
    Brief->>Team: on_request folds the brief in
    Team->>Lead: run(request + brief)
    Note over Lead: WarRoom.oracle rides the lead's own post_conditions - refuse and re-ask
    Lead-->>Team: verdict
    Team->>Spec: retire all (finally)
    Team-->>Room: TeamRun
    Room-->>CLI: Investigation
```

- Entry point `main` at `src/pneuma/demo/cli.py:115`; the async body `investigate` at `src/pneuma/demo/cli.py:25`.
- `CLI` — `src/pneuma/demo/cli.py:1`. The coordinator is an `InMemoryCoordinator` constructed at `src/pneuma/demo/cli.py:41`, `LocalWorker` registered at `src/pneuma/demo/cli.py:42`.
- `WarRoom` — `src/pneuma/demo/warroom.py:100`. `Specialists` — one `Specialist` per plane, `src/pneuma/demo/warroom.py:178`, class at `src/pneuma/demo/cast.py:69`. `Lead` — `IncidentLead` at `src/pneuma/demo/cast.py:204`, composed at `src/pneuma/demo/warroom.py:179-183`.
- `investigate(coordinator)` — `src/pneuma/demo/cli.py:52`, body at `src/pneuma/demo/warroom.py:168`.
- `run(question)` — `Team(lead, specialists, hooks=[watch, briefing])` at `src/pneuma/demo/warroom.py:185-189`; the core pipeline at `src/pneuma/team/core.py:202-262`.
- `ask the opening, barrier` and `briefings or error strings` — `asyncio.gather` over each member's `ask` with `return_exceptions=True` at `src/pneuma/team/hooks/briefing.py:85-96`; re-keyed by plane at `src/pneuma/demo/warroom.py:199-203`.
- `on_request folds the brief in` — `src/pneuma/team/hooks/briefing.py:98-111`.
- The refuse-and-re-ask note — `WarRoom.oracle` prepended to the lead's own `post_conditions` at `src/pneuma/demo/warroom.py:182`; failures return to the model as assertion text, `src/pneuma/demo/warroom.py:127-158`. The team layer itself grades nothing.
- `retire all (finally)` — unconditional teardown covering members and the lead, `src/pneuma/team/core.py:263-283`; hires are the `Hiring` seam's own to release (`src/pneuma/demo/staffing.py`).
- `Investigation` — graded by `WarRoom.grade` and built at `src/pneuma/demo/warroom.py:189-219`, persisted at `src/pneuma/demo/cli.py:55`.

## Verified-process walk

```mermaid
sequenceDiagram
    participant Caller
    participant Agent as ProcessAgent
    participant Interp as Interpreter
    participant IR as Process IR
    participant Decider
    participant Handler

    Caller->>Agent: work(facts)
    Agent->>Interp: run(on_enter)
    Interp->>IR: invariants
    loop until terminal
        Interp->>IR: outgoing(state)
        IR-->>Interp: enabled set
        Interp->>Decider: offer(options)
        Decider-->>Interp: transition
        Interp->>Interp: reject illegal
        Interp->>Agent: on_enter(state)
        Agent->>Handler: dispatch
        Handler-->>Agent: result
    end
    Interp-->>Agent: Run trace
    Agent-->>Caller: Run
```

- Entry point `ProcessAgent.work` at `src/pneuma/process/agent.py:267`.
- `ProcessAgent` — `src/pneuma/process/agent.py:83`. `Interpreter` — `src/pneuma/process/interpreter.py:176`. `Process IR` — `src/pneuma/process/ir.py`, imported at `src/pneuma/process/interpreter.py:24`. `Decider` — the `choose` `@ai_method` at `src/pneuma/process/agent.py:122`, adapted at `src/pneuma/process/agent.py:137-157`. `Handler` — the per-state `@ai_method` resolved by `handler_for` at `src/pneuma/process/agent.py:161`.
- `work(facts)` runs the wiring guard first — `src/pneuma/process/agent.py:314`, guard body at `src/pneuma/process/agent.py:357-368`.
- `run(on_enter)` — `src/pneuma/process/agent.py:322-330`.
- `invariants` — `_assert_invariants` at `src/pneuma/process/interpreter.py:241` and again per step at `:295`; body at `src/pneuma/process/interpreter.py:354-357`.
- `outgoing(state)` / `enabled set` — `src/pneuma/process/interpreter.py:256`; an empty set raises `Deadlock` at `:258`.
- `offer(options)` / `transition` — `_elicit` at `src/pneuma/process/interpreter.py:325-351`; a lone enabled transition skips the decider at `:338-339`; the decider renders `interpreter.offer` at `src/pneuma/process/agent.py:154`.
- `reject illegal` — proposals outside `legal` are appended to `rejected` and re-asked up to `max_rejections`, `src/pneuma/process/interpreter.py:343-351`.
- `on_enter(state)` — called once per visit at `src/pneuma/process/interpreter.py:248-249` and `:306-307`; the closure is installed at `src/pneuma/process/agent.py:319-320`.
- `dispatch` / `result` — `src/pneuma/process/agent.py:222`, model call at `:251`, `on_result` at `:257-258`, faults re-raised as `HandlerFailed` at `:253`.
- `Run trace` — returned on reaching a terminal state, `src/pneuma/process/interpreter.py:252-254` and `:315-317`; `Run` defined at `src/pneuma/process/interpreter.py:157`.

## Case-study pipeline

```mermaid
sequenceDiagram
    participant Caller
    participant Pipe as pipeline
    participant Log as eventlog
    participant Miner as miner
    participant DB as libSQL
    participant TLA as tla
    participant Props as properties

    Caller->>Pipe: run(log, db)
    Pipe->>Log: parse_xes
    Log-->>Pipe: events frame
    Pipe->>Log: persist_events
    Pipe->>Miner: mine(events)
    Miner-->>Pipe: Discovery
    Pipe->>DB: record model
    Pipe->>TLA: check(process)
    TLA-->>Pipe: CheckResult
    Pipe->>Props: machine_for
    Props-->>Pipe: violation?
    Pipe-->>Caller: Findings
```

- Entry point `pipeline.run` at `src/pneuma/casestudy/pipeline.py:120`.
- `pipeline` — `src/pneuma/casestudy/pipeline.py:1`. `eventlog` — `src/pneuma/casestudy/eventlog.py`, imported at `src/pneuma/casestudy/pipeline.py:29`. `miner` — `src/pneuma/casestudy/miner.py`, same import. `libSQL` — the `libsql.Connection` returned by `eventlog.connect` at `src/pneuma/casestudy/eventlog.py:194`. `tla` and `properties` — `src/pneuma/process/tla.py` and `src/pneuma/process/properties.py`, imported at `src/pneuma/casestudy/pipeline.py:27`.
- `parse_xes` / `events frame` — `src/pneuma/casestudy/pipeline.py:129`, function at `src/pneuma/casestudy/eventlog.py:43`.
- `persist_events` — `src/pneuma/casestudy/pipeline.py:131-133`, function at `src/pneuma/casestudy/eventlog.py:213`.
- `mine(events)` / `Discovery` — `src/pneuma/casestudy/pipeline.py:135-140`, function at `src/pneuma/casestudy/miner.py:84`, dataclass at `src/pneuma/casestudy/miner.py:28`.
- `record model` — `src/pneuma/casestudy/pipeline.py:141`, INSERT at `src/pneuma/casestudy/pipeline.py:238-249`.
- `check(process)` / `CheckResult` — called twice, once for the mined model and once for the `governed` copy, at `src/pneuma/casestudy/pipeline.py:159-163`; `governed` attaches `NoDetermineWithoutCheck` at `src/pneuma/casestudy/pipeline.py:88-96`; `tla.check` shells out to TLC at `src/pneuma/process/tla.py:309-328`, gated on `tlc_available` at `src/pneuma/process/tla.py:265`.
- `machine_for` / `violation?` — `src/pneuma/casestudy/pipeline.py:165`, `_property_test` at `src/pneuma/casestudy/pipeline.py:179-197`, machine built at `src/pneuma/process/properties.py:72`.
- `Findings` — dataclass at `src/pneuma/casestudy/pipeline.py:36`, returned at `src/pneuma/casestudy/pipeline.py:176`. Step 6 of the study is a separate async entry, `execute_case` at `src/pneuma/casestudy/pipeline.py:200`.

## See also

- [Module map][module-map] — 11 shared source files
- [Processes][processes] — 11 shared source files
- [Impact analysis][impact-analysis] — 10 shared source files
- [Data flow][data-flow] — 9 shared source files
- [Contract map][contract-map] — 9 shared source files

[module-map]: ../../architecture/module-map.md
[processes]: ../../behavior/processes.md
[impact-analysis]: ../../insights/impact-analysis.md
[data-flow]: ../../architecture/data-flow.md
[contract-map]: ../../insights/contract-map.md
