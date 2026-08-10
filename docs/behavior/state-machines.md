# pneuma · State machines

## CheckResult.outcome

The verdict TLC's output is classified into. The four states are declared as a closed
`Literal` at `src/pneuma/process/tla.py:30`, and `ok` is true for `verified` alone
(`src/pneuma/process/tla.py:74-76`). `_parse` walks the ladder below in source order, so an
earlier branch wins: a broken checker is `failed` before a reported violation can make it
`violated`. `with_witnesses` is the one edge between two settled states — a `verified` result
whose invariants had zero witness states is downgraded to `vacuous`
(`src/pneuma/process/tla.py:98-101`).

```mermaid
stateDiagram-v2
    [*] --> failed : returncode not in property_codes
    [*] --> violated : violated is not None
    [*] --> vacuous : distinct <= 0 or initial <= 0
    [*] --> verified : _SUCCESS_LINE
    verified --> vacuous : with_witnesses
```

- Entry classifier: `src/pneuma/process/tla.py:369-388`.
- Second transition site: `with_witnesses`, `src/pneuma/process/tla.py:90-101`.
- No state is marked terminal in source — these are values of a frozen dataclass field, and
  `with_witnesses` proves `verified` is not final. No `--> [*]` is drawn.

Defined at: `src/pneuma/process/tla.py:30`

## Process

The mined-process IR itself: a machine whose states, transitions, guards and effects are data
rather than code (`src/pneuma/process/ir.py:213-222`). `interpreter.run` walks it, and the
walk has its own enumerated outcomes — one success and four named refusals, each a
`ProcessError` subclass (`src/pneuma/process/interpreter.py:98-140`). `initial_state` is the
declared entry (`src/pneuma/process/ir.py:219`); a `State` carrying `terminal` is the declared
exit (`src/pneuma/process/ir.py:186`), and the IR refuses to validate without at least one
(`src/pneuma/process/ir.py:273-274`).

```mermaid
stateDiagram-v2
    state "State" as step
    [*] --> initial_state
    initial_state --> InvariantViolated : _assert_invariants
    initial_state --> terminal : terminal
    initial_state --> step : _elicit
    step --> InvariantViolated : _assert_invariants
    step --> Deadlock : not enabled
    step --> ProcessError : max_rejections
    step --> NoProgress : max_revisits
    step --> step : _elicit
    step --> terminal : terminal
    step --> ProcessError : max_steps
    terminal --> [*]
```

`State` is aliased to `step` in the diagram above only because `State` is a reserved word in
Mermaid's `stateDiagram-v2` grammar; the rendered label is the source identifier
(`src/pneuma/process/ir.py:175`) verbatim.

- Guards decide which transitions are enabled: `Transition.enabled`,
  `src/pneuma/process/interpreter.py:256` selecting over `src/pneuma/process/ir.py:171-172`.
- Effects are applied on the chosen edge before the target is entered:
  `src/pneuma/process/interpreter.py:262-264`.
- `_elicit` takes a lone enabled transition without consulting the decider
  (`src/pneuma/process/interpreter.py:338-339`), and raises `ProcessError` after
  `max_rejections` illegal proposals (`src/pneuma/process/interpreter.py:343-351`).
- `terminal` is checked at the top of the loop and again after the last budgeted step, so a run
  that lands on a terminal state with the budget exactly spent completes rather than raising
  (`src/pneuma/process/interpreter.py:252-254`, `src/pneuma/process/interpreter.py:315-317`).

Defined at: `src/pneuma/process/ir.py:213`

## RuleVerdict.cause

Why a rule could not be broken, named by the relaxation level at which it first becomes
breakable. The four levels are ordered weakest-constraint-last at
`src/pneuma/detect/vacuity.py:61` and that order is load-bearing: the sound widening
`free_initial` must precede the unsound `free_guards`, or the gate inverts
(`src/pneuma/detect/vacuity.py:19-28`). `audit` builds a system per level and only for the
levels still needed, dropping each rule that became breakable
(`src/pneuma/detect/vacuity.py:640-660`). `RuleVerdict.cause` then reads the per-level breach
counts back and names the diagnosis (`src/pneuma/detect/vacuity.py:458-474`); the six values it
can return are closed at `src/pneuma/detect/vacuity.py:63-71`.

```mermaid
stateDiagram-v2
    [*] --> exact
    exact --> live : violating_states
    exact --> unknown : truncated
    exact --> free_initial : still
    free_initial --> pinned_variable : relaxed
    free_initial --> unknown : relaxation_truncated
    free_initial --> free_guards : still
    free_guards --> guarded : relaxed
    free_guards --> unknown : relaxation_truncated
    free_guards --> free_both : still
    free_both --> pinned_and_guarded : relaxed
    free_both --> unknown : relaxation_truncated
    free_both --> unreachable_scope : antecedent_states
    free_both --> unsatisfiable : antecedent_states
```

- `live` is where `cause` returns None: the rule fires in the system as written, so there is
  nothing to explain (`src/pneuma/detect/vacuity.py:370-382`,
  `src/pneuma/detect/vacuity.py:464-465`).
- `unknown` is reachable from every level because a truncated sweep withdraws the diagnosis
  rather than the pass: `truncated` covers `exact`, `relaxation_truncated` covers the relaxed
  levels (`src/pneuma/detect/vacuity.py:466-467`, `src/pneuma/detect/vacuity.py:654-659`).
- `unreachable_scope` and `unsatisfiable` split on whether the rule's subject was reachable at
  all (`src/pneuma/detect/vacuity.py:474`).
- The five cause states are terminal in the sense that nothing re-derives them, but source marks
  no terminal explicitly — `cause` is a computed property, not a stored field. No `--> [*]` is
  drawn.

Defined at: `src/pneuma/detect/vacuity.py:63`

## Team.execute

The fixed run skeleton: four phases in ordinary `asyncio` with no model anywhere in the control
flow, which is what makes a run reproducible (`src/pneuma/team.py:1122-1128`). Each phase
boundary emits a `CustomEvent` whose `kind` is the edge label below, so the phase order is
observable on the event log rather than inferred. The cast is listed and the lead composed
*before* anything is spawned, so a wiring guard cannot fire after the barrier has already spent
what it protects (`src/pneuma/team.py:1158-1169`).

```mermaid
stateDiagram-v2
    [*] --> assemble
    assemble --> brief : team.assembled
    brief --> lead_running : team.briefings_in
    lead_running --> negotiate : team.lead_running
    negotiate --> retire : team.negotiated
    retire --> grade : finally
    grade --> [*] : team.graded
```

- `assemble` spawns members serially so the event log's order is the declared cast order
  (`src/pneuma/team.py:1219-1238`).
- `brief` gathers concurrently behind a barrier with `return_exceptions=True`, so a member that
  died becomes a `BRIEFING_ERROR` string rather than an exception that takes the run down
  (`src/pneuma/team.py:1265-1277`).
- `retire` is unconditional and covers the lead, in a `finally`, so a mid-run fault cannot leave
  live threads on the coordinator (`src/pneuma/team.py:1189-1199`).
- `grade` runs after the unwind and is defaulted to `(True, [])`, because the oracle has already
  gated by the time it is reached (`src/pneuma/team.py:1000-1016`,
  `src/pneuma/team.py:1201-1203`).

Defined at: `src/pneuma/team.py:1122`

## Team.negotiate

The optional per-round machine between the lead's draft and the graded verdict, bounded by
`negotiation_rounds` and off by default — at zero it returns before touching anything and
`execute` is byte-for-byte the pre-negotiation skeleton (`src/pneuma/team.py:1285-1287`). Each
round fans the rendered plan to every member, collects objections behind the same barrier
`brief` uses, and lands on one of three outcomes recorded verbatim on the transcript entry
(`src/pneuma/team.py:1345`, `src/pneuma/team.py:1357-1359`) and documented on
`TeamRun.negotiation` at `src/pneuma/team.py:319`.

```mermaid
stateDiagram-v2
    [*] --> round
    round --> unanimous : approved
    round --> cap_reached : negotiation_rounds
    round --> revised : render_objections
    revised --> round : round_number
    unanimous --> [*]
    cap_reached --> [*]
    revised --> [*]
```

- `unanimous` returns early with the current verdict and the transcript
  (`src/pneuma/team.py:1344-1353`).
- `revised` sends the objections back through `lead_handle.run`, so the revision faces the oracle
  exactly as the draft did (`src/pneuma/team.py:1355-1356`).
- `cap_reached` is the same revision, marked so the transcript says the team never reached
  unanimity rather than implying it did (`src/pneuma/team.py:1357-1359`).
- An empty cast returns `(verdict, [])` before any round, because a round over nobody is
  vacuously unanimous and would record a consensus no member gave
  (`src/pneuma/team.py:1314-1319`).

Defined at: `src/pneuma/team.py:1280`

## See also

- [Module map][module-map] — 5 shared source files
- [Processes][processes] — 5 shared source files
- [Business logic][business-logic] — 5 shared source files
- [Contract map][contract-map] — 5 shared source files
- [Debugging guide][debugging-guide] — 5 shared source files

[module-map]: ../architecture/module-map.md
[processes]: processes.md
[business-logic]: ../insights/business-logic.md
[contract-map]: ../insights/contract-map.md
[debugging-guide]: ../insights/debugging-guide.md
