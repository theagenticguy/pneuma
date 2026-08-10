# Hooks over phases: an orchestrator core should own loops and lifecycles, features should be list entries

**Category**: architecture-patterns
**Tags**: team, hooks, protocol, composition, config-hook, accept-revise, textgrad, tracing
**Modules**: src/pneuma/team/core.py, src/pneuma/team/hooks/*
**Session**: session-94adc2 (2026-08-10)

## Lessons (all verified by execution)

1. **A hardcoded phase skeleton turns every optional feature into core complexity; a hook
   protocol with optional methods turns the core into ~460 lines that never grow.** The
   rebuild replaced a 1,656-line five-phase Team with core.py (pipeline + Accept/Revise loop
   + teardown) plus six single-purpose hook files; the port commit was +2,777/-5,931 with
   test coverage INCREASING (571 -> 881 suite-wide). The core owns exactly three things:
   the single composed runtime config_hook (the runtime calls one per cycle and its tools
   patch REPLACES), the bounded revise loop (cap rides ON the Revise verdict, so each hook
   prices its own persistence), and unconditional teardown. Everything else is a list entry.

2. **Grading must not be a rite of passage.** The old Team required an oracle before it
   could answer at all. Review as opt-in members (Critic/Council hooks returning
   Accept/Revise) keeps the bare team ungraded and makes review integrity explicit and
   testable: an errored/empty reviewer never settles Accept; a dead panelist counts against
   the full-panel denominator; advisory=True records without gating. The discriminating
   test configuration matters — a survivors-denominator bug is invisible at threshold 0.5
   with one error (1/2 accepts either way) and visible at threshold 1.0.

3. **Strands silently drops tools whose names fail `^[a-zA-Z0-9_\-]+$`** (tools.py:66 in the
   installed lib) — a log warning, no error. Dotted names ({owner}.{method}) never reach the
   model. Map to underscores at the composition seam and collide duplicates on the MAPPED
   name. Only wire-level tests (tool_specs captured from the model) catch this class.

4. **Team learning needed zero core changes because the event log survives thread
   teardown.** Deferred emission: recall guidance under no_thread_scope (view stays
   unemitted), interpolate the ParameterView with identity preserved (never f-string it),
   and after the run rebuild the Result by emitting the recall against the lead thread's
   post-teardown event log + collect_nodes. TextGrad's backward is not offline-scriptable
   (random-suffixed node ids) — script the optimizer object offline, use the real one live.
   Measured live: one step rewrote stored guidance text in 9s.

5. **Resumability is a property of the write protocol, not the agent.** Four upstream API
   timeouts and one host restart hit Wave 2; every agent resumed from packet + disk with
   zero rework EXCEPT the one that had written no packet entries yet — it restarted from
   scratch. Log design decisions to the packet BEFORE writing code.
