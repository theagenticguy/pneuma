# pneuma · Components

```mermaid
classDiagram
    class MethodAgent {
        +spawn(name, coordinator)
        +compiled(name)
        +agents()
        +ai_methods()
    }
    class MethodThread {
        +run(args)
        +notify(text)
        +fork()
        +retire()
    }
    class GatedProposer {
        +gated(method)
        +judge(candidate)
        +admits(response)
        +propose_k(n)
    }
    class Recall {
        +bound(method)
        +trace(args)
        +backend()
    }
    class ProcessAgent {
        +choose(state, options)
        +decider(facts)
        +dispatch(state)
        +work(start)
    }
    class Team {
        +execute(ctx, request)
        +assemble(ctx, cast)
        +brief(members)
        +grade(verdict)
        +teardown()
    }
    class Process {
        +outgoing(state)
        +state_map()
        +initial_assignments()
        +unreachable_states()
    }
    class TursoMemoryBackend {
        +add_entry(name, value)
        +search_entries(name, query)
        +probe_retrieval(name)
        +numeric_value(name)
        +calibrate_ceiling(name)
    }

    MethodAgent <|-- GatedProposer : extends
    MethodAgent <|-- ProcessAgent : extends
    MethodAgent --> MethodThread : spawns
    Recall --> MethodAgent : binds
    Recall --> TursoMemoryBackend : reads
    Team --> MethodThread : spawns
    Team --> GatedProposer : gates
    ProcessAgent --> Process : walks
```
