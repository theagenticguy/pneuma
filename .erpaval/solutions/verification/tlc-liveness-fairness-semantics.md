# TLC liveness: WF kills only stuttering, and cycles are real findings

**Category**: verification
**Tags**: tla, tlc, liveness, fairness, termination, model-checking
**Modules**: src/pneuma/process/tla.py
**Session**: session-5abb9e (2026-08-07)

## Lesson

`WF_vars(Next)` rules out infinite *stuttering* only. A reachable cycle among
real transitions satisfies weak fairness (something in Next keeps happening),
so `Termination == <>(pc \in Terminals)` is violated — exit 13, lasso trace
closing with a `Back to state N:` line. Both directions verified empirically
against tla2tools 2.19.

Consequences that bit or nearly bit us:

- **Liveness cannot be a hard default** for mined process models: real mined
  models (permits@25, roadfines@5 self-loop) have cycles and are legitimately
  asserted ok on safety. A Termination violation on them is a true statement,
  but flipping green tests is a product decision. Hence `liveness=False` opt-in.
- **WF on the exit action is NOT the remedy** for a loop-toggled exit guard —
  the exit is not *continuously* enabled, so WF imposes nothing. It must be
  `SF_vars(Exit)` (strong fairness). The commonly repeated "add per-action WF"
  advice is too loose; we executed both variants: WF still exit 13, SF exit 0.
- **Safety masks liveness**: if an invariant is also violated TLC exits 12 and
  the temporal check never ran. `exit != 13` does not mean termination holds.
- **One PROPERTY per run**: TLC never names which temporal property failed
  ("Temporal properties were violated." is all you get).
- Stuttering counterexamples print `State N: Stuttering` with NO Back-to-state
  line; a lasso-only parser mis-reads them. Both are exit 13.
- `Done == pc \in Terminals /\ UNCHANGED vars` steps ARE stuttering (vars
  unchanged), so WF_vars(Next) imposes nothing at a pure terminal — terminal
  stalling never spuriously violates `<>`-Termination.

## Open follow-up

The renderer emits reserved identifiers (`Termination`, `Spec`, `Init`, `Next`,
`Done`, `TypeOK`, `NoDeadlock`); an IR transition/invariant with one of those
names collides. Fails loud (`outcome: failed`, parse error), never a wrong
verdict, but a reserved-name check in `Process._referentially_sound` would
cover the whole class.
