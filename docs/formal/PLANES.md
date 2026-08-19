# Which record-keeping system, when — the one-page matrix

Six systems record state. Five are pneuma's; the blackboard kernel is the external org
plane (`docs/design/org-plane.md`). The rule an agent can hold in one line: **conversation
is ambient, discoveries go to the worklog, content goes to artifacts, status goes to the
blackboard, lessons go to memory — and if you are not the lead, you propose rather than
commit.**

| | Thread event log | Worklog | ArtifactStore | Trajectory | Memory (Turso/sqlite) | Blackboard kernel |
|---|---|---|---|---|---|---|
| **Records** | every message and tool call on one conversation | discoveries worth a teammate knowing | versioned content revisions | one row per team run | learned guidance and parameters | goals, tasks, reviews, gates |
| **Written by** | the runtime, automatically | members, via `post_discovery` | members propose; the lead commits | the hook, on teardown | `train()` / consolidation only | lead and specialists via MCP tools |
| **Read by** | replay, audit, `traced_result` | teammates, at their next model call | any member; the integrating lead | future learning loops | every run, at recall time | the kernel's gate; humans |
| **Lifetime** | one thread | one run | across runs (file-backed) | append-only, forever | across runs; entries evolve | across goals, forever |
| **Mutability** | append-only | append-only | immutable revisions, moving heads | append-only | rewritten by gradients | state machine, fenced |
| **An agent uses it when…** | never explicitly — ambient | "dead end / this bears on you" | "here is my draft of the shared thing" | never — ambient | never explicitly — arrives as call arguments | "task done / finding / gate?" |
| **Wrong-use smell** | storing decisions here | putting content here | putting status here | querying it mid-run | putting facts-of-record here | putting content here |

Only two systems are ever written *deliberately by a member* (worklog, artifacts), and
each has exactly one verb. The rest of the matrix is enforced by wiring — members do not
hold `commit_change`, nobody holds a trajectory-write tool — so the table describes what
the tool surface already prevents, not discipline agents must remember.

## The formal verification behind this table

Two independent tracks checked the planes' logic, and they caught each other's errors:

- **TLA+ / TLC** (`docs/formal/*.tla`): exhaustive models of the artifact plane
  (no-lost-write, sole-integrator, conflict-not-overwrite, author-bound), the answer loop
  (the restart chain's no-unreviewed-ship property, with a 15-state counterexample showing
  the rejected per-hook-loop design ships an accepted-then-mutated answer), and the org
  plane (fenced assignment, legal transitions, gate soundness). 34 configs, all matching
  expected verdicts, including 13 vacuity witnesses and 14 deliberately broken variants.
- **EARS / symspec / Z3** (`docs/formal/requirements/`): 87 requirements restating the
  code, cross-checked for contradictions. Two were proven and resolved on main: the
  release gate's split-brain conjunct is affirmative (`NONE`, not merely not-`CONFIRMED`)
  — absence of evidence no longer opens the gate — and the artifacts hook's sole-commit
  invariant is now stated with its seeding boundary.

The cross-check earned its cost: the TLA+ model initially encoded the gate with the old
polarity and proved it "sound" against the mis-stated contract; the Z3 corpus proved the
contract itself contradictory. Stating the contract wrong is outside a model checker's
view — running both methods against the same design is what caught it. The corrected
model also caught a fidelity bug the old polarity had masked (a `Decide` action that
could retract a decision the append-only log cannot retract), and
`OrgPlane_Broken_XPL1Regression.cfg` stands as a regression test: if that config ever
goes green, the old polarity is back.

## Distributed-systems posture (why this is simpler than it looks)

Every plane is single-writer, append-only, or serialized by construction: artifact
commits go through one lead; trajectory writes happen in one teardown; `Squad` serializes
asks with a lock; `Expedition` rounds are sequential; SQLite WAL covers concurrent teams
sharing a file. There is no consensus problem because no plane has two writers contending
for one head — the lead-only-integrator rule does the work a merge queue does elsewhere.
The genuinely concurrent plane (the blackboard kernel) carries its own machinery
(optimistic concurrency, idempotency, fencing) in transactional Postgres, proven by a
zero-LLM scripted e2e in its own repo. The joins between planes are id-only by design
rule, and the cross-plane gate is the one place two planes are read together — which is
why it got both formal treatments.
