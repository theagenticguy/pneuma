# Orchestrator state lifetimes, concurrent tool races, and delivery claims that need a wire

**Category**: ai-functions-runtime
**Tags**: team, spawnable, roster, concurrency, tool-executor, config-hook, state-lifetime, briefings
**Modules**: src/pneuma/team.py, src/pneuma/demo/warroom.py, src/pneuma/demo/staffing.py
**Session**: session-9df6ea (2026-08-07)

## Lessons (all verified by execution)

1. **Every piece of orchestrator state needs a declared lifetime, and "per run" state must be
   reset per run.** A Spawnable's thread can be driven by `handle.run` more than once (nothing
   refuses re-entry, and STR_PROMPT advertises chat-style reuse). A roster that survives runs
   means run 2 delegates to run 1's retired threads, inherits its consumed hire budget and its
   log. Reset with `type(self.roster)()`, not the base class literal — a subclass roster
   (Staff's mandate hook) must survive the reset. Pin BOTH the reset and the within-run
   persistence; drop any test that cannot fail (config_hook fires once per cycle, so a
   narrower reset placement was indistinguishable — coverage theatre, removed).

2. **The runtime's DEFAULT tool executor is concurrent (strands ConcurrentToolExecutor), so any
   guard-then-await-then-register tool is a race.** Two `hire` calls in one assistant turn both
   passed the cap and duplicate-name checks before either registered; the same-name race
   spawned two threads and the overwrite made one unreachable by every unwind path — a leaked
   live thread. Reserve-before-await: run all checks AND insert the placeholder registration
   synchronously, then await the spawn, roll back on failure. The same shape applies to any
   tool that spends: check-spend-record must have no await between check and record.

3. **Unregister-then-release is the wrong order for teardown tools.** `dismiss` popped the
   recruit from the roster before awaiting retire; a retire fault then left the thread alive
   AND unreachable (the finally iterates the roster). Release first, unregister on success — a
   raise leaves the object registered where the unwind retries it.

4. **A docstring claiming X reaches Y is a wiring claim — verify the wire exists.** The
   skeleton documented that a failed member's error string "tells the lead in its own briefing
   text that one source is missing", but no code path delivered any briefing to the lead; an
   all-members-failed run graded correct=True. When a delivery claim is design-load-bearing,
   make it a template method (render_brief) plus a refusal for the degenerate case (all
   briefings errored -> refuse before the lead spawns), and pin delivery by asserting the
   content appears in the model's actual prompt.

5. **Dict-keyed aggregation over caller-supplied names silently drops collisions.** briefings
   keyed by member.name lost a member when two shared a name (one typo away in any cast list —
   both were still spawned and billed). A protocol guarantees a name EXISTS, not that it is
   unique; refuse duplicates at wiring time with the other guards.

6. **Cross-file line-number cites go stale the moment the cited file is edited in the same
   session.** Wave 2 wrote demo docstrings citing team.py:NNN; the critic-fix wave shifted
   team.py and five cites pointed at wrong lines while every claim still held. After any
   multi-wave session, grep the untouched files for `<edited-file>:` and re-verify anchors.

7. **Probe scripts must carry scripted models BY CONSTRUCTION.** One Wave-2 probe left a hired
   agent on its real model and burned 17,859 live Bedrock tokens before being caught. Every
   subsequent subagent prompt carried an explicit scripted-model-only rule; the cheaper fix is
   a fixture/helper that refuses to build an agent without an injected model in offline
   contexts.
