# Gate protocols, fault-wrapping, and fork beams: what the GatedProposer lift taught

**Category**: ai-functions-runtime
**Tags**: gated-proposer, protocols, post-conditions, fork, beam-search, ty, typing
**Modules**: src/pneuma/gated.py, src/pneuma/casestudy/harnesslearn.py
**Session**: session-a17d0f (2026-08-07)

## Lessons

1. **A protocol attribute that admits async makes sync subclass methods inexpressible.**
   `gate: Gate` where `Gate.__call__ -> Verdict | Awaitable[Verdict]` means a subclass
   method literally named `gate` never narrows it — the base declaration wins and every
   `proposer.gate(x).field` read becomes unresolved. The one clean shape (measured against
   ty across four candidates): class-level narrowing declaration
   (`gate: Callable[[float], Admission]`) plus a separately named `_gate` method bound via
   `super().__init__(gate=self._gate)`. Protocol parameter NAMES are contract for
   keyword-callables — `_gate(self, candidate)` not `(self, weight)`. Every subclass of a
   protocol-typed injectable pays this; design the base knowing it.

2. **Every callable the framework re-raises for the model must be fault-wrapped — including
   the hooks you add.** The lift wrapped the gate but left its own new hook (`candidate_of`)
   outside the wrap; a typoed override surfaced as a raw AttributeError burning max_attempts
   retries. The rule generalizes: on any path where an exception becomes '[VALIDATION
   ERROR]' feedback, EVERY user-supplied callable (gate, extractor, extra post-conditions,
   the verdict's own ok/report_text reads) needs the fault-vs-verdict distinction. The
   critic found three of these edges in one pass; assume any new hook has one.

3. **fork() does not inherit a pending notify().** notify is worker-side inject state; fork
   copies the coordinator event log. Seeding forked branches means running real cycles
   before the fork. Measured, not inferred.

4. **A guard's cost-benefit can be measured by breaking it**: the collision guard's absence
   made the offline suite 50x slower (every branch burning retries on a TypeError no model
   fixes). When arguing for a wiring-time check, run the broken version once and cite the
   number.

5. **Offline train-loop tests can pass with the gate entirely unwired** if the scripted
   proposal is always admissible — only a scripted rejected-then-admitted sequence pins the
   wiring. When a loop's safety mechanism only fires on bad inputs, script a bad input.
