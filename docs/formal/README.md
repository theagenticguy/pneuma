# Formal models of pneuma's three coordination planes

Hand-written TLA+ specifications of the three record-keeping planes described in
`docs/design/org-plane.md`, model-checked exhaustively with TLC. These are not generated
by `process/tla.py` — that renderer turns a mined `Process` IR into a state machine, and
what is verified here is not a process but the *coordination rules* three modules enforce
in prose. The conventions are borrowed from it, though: `Done` as an explicit stutter at a
terminal state, one temporal property per config, and the vacuity discipline of
`CheckResult.with_witnesses` — a green run over a state space that never reached the
interesting configuration is not a verification.

| Plane | Spec | Models |
|---|---|---|
| Content | `ArtifactPlane.tla` | `team/artifacts.py` `ArtifactStore` + the `team/hooks/artifacts.py` tool split |
| Execution | `AnswerLoop.tla` | `team/core.py` `Team._answer_loop` — the restart chain |
| Organization | `OrgPlane.tla` | the blackboard kernel's task lifecycle, joined to team runs and the content plane |

Every spec is small enough to exhaust in seconds. Every property is shown non-vacuous, and
every property has a `*_Broken` companion or a rejected-design config in which TLC actually
watches it fail — the repo's guards-must-fire rule applied to specs, because an invariant
nobody has seen break is an invariant nobody has evidence is checking anything.

## How to re-run

`tools/tla2tools.jar` is gitignored. Copy it in, then run TLC from *this* directory:

```bash
cp ~/bonk-fs/projects/pneuma/tools/tla2tools.jar tools/     # from the repo root
cd docs/formal
java -XX:+UseParallelGC -cp ../../tools/tla2tools.jar tlc2.TLC -deadlock \
     -config ArtifactPlane.cfg ArtifactPlane.tla
```

`-deadlock` is passed everywhere. Each spec models its own end-of-run stutter (`Done`) and
states the real stuck-ness question as a property, so TLC's own deadlock check would only
report the legitimate terminal state.

Swap the `-config` and the `.tla` for any row in the tables below. The three primary
configs are the ones that must be green; the witness and broken configs are ones TLC is
*expected* to fail, and a green result there is the failure.

TLC writes a `states/` checkpoint directory next to the spec on a violation. It is
gitignored; pass `-cleanup` if you would rather it not appear at all.

## Results

Measured on this machine (Corretto 25.0.4, tla2tools 2.19, 16 cores, 1 worker), all runs
exhaustive with 0 states left on queue.

### Primary — must be green

| Config | Checks | States (generated / distinct) | Wall |
|---|---|---|---|
| `ArtifactPlane.cfg` | `TypeOK`, `NoLostWrite`, `SoleIntegrator`, `ConflictNotOverwrite`, `OverlapNeverMerges`, `AuthorBound` | 117,337 / 32,409 | 6.9s |
| `ArtifactPlane_Live.cfg` | `ProposalsSettle` under per-proposal fairness on the lead | 117,337 / 32,409 | 13.2s |
| `AnswerLoop.cfg` | `TypeOK`, `NoUnreviewedShip`, `BudgetBounded`, `BoundedRevisions`, `CapRecordedOnlyWhenExhausted`, `BudgetMonotone` | 3,122 / 1,900 | 1.0s |
| `AnswerLoop_Live.cfg` | `Termination` | 3,122 / 1,900 | 1.0s |
| `OrgPlane.cfg` | `TypeOK`, `FencedAssignment`, `RunningHasOneRun`, `ReviewsNotBanked`, `NoIllegalTransition`, `GateSoundness` | 88,784 / 11,700 | 1.4s |
| `OrgPlane_Live.cfg` | `Termination` under `WF_vars(TaskStep)` | 88,784 / 11,700 | 2.5s |

Search depths: 9 (ArtifactPlane), 17 (AnswerLoop), 24 (OrgPlane).

Both liveness assumptions were checked for load-bearingness by dropping them. Change
`SPECIFICATION SpecFair` to `SPECIFICATION Spec` in either `*_Live.cfg` and TLC reports
`Temporal properties were violated`, with a counterexample ending in `Stuttering` while work
is still outstanding — a proposal unsettled for `ArtifactPlane`, the task parked at a
non-terminal state for `OrgPlane`. (Which trace TLC picks varies between runs; the
`Stuttering` ending does not.) So neither termination result is an artifact of a state space
too small to stall. `OrgPlane` was also run under `WF_vars(Next)` instead of
`WF_vars(TaskStep)` and *also fails* — fairness on `Next` is satisfiable by a content plane
churning conflict rows forever while the task never moves, which is exactly why the fairness
is on the task's own steps only.

### Non-vacuity witnesses — TLC must VIOLATE each

Every one of these is an invariant asserting that the interesting configuration is
*unreachable*. TLC violating it is the witness that the corresponding real property has a
reachable antecedent. One invariant per config, because TLC stops at the first violation.

| Config | Invariant violated | Witnesses that | States | Wall |
|---|---|---|---|---|
| `ArtifactPlane_Witness_Siblings.cfg` | `NoSiblingProposals` | two proposals off one parent are both offered to the lead — `ConflictNotOverwrite`'s sibling clause | 170 / 170 | 0.9s |
| `ArtifactPlane_Witness_Merge.cfg` | `NoMergeEverLands` | a merge revision reaches main through `mergedFrom`, so `NoLostWrite`'s reachability disjunct is load-bearing on the *second* parent edge | 211 / 209 | 1.0s |
| `ArtifactPlane_Witness_Overlap.cfg` | `NoOverlapEverRefused` | an overlapping proposal is reached and refused — `OverlapNeverMerges` is not passing because overlap never happens | 170 / 170 | 0.9s |
| `AnswerLoop_Witness_Revised.cfg` | `NeverShipsARevisedAnswer` | a revised answer (`answerVer > 1`) does reach `shipped` | 254 / 233 | 0.9s |
| `AnswerLoop_Witness_StaleClearance.cfg` | `NoStaleClearanceEverExists` | **the important one** — see below | 26 / 26 | 0.9s |
| `AnswerLoop_Witness_Exhaustion.cfg` | `NoHookEverExhausts` | a hook really runs out of budget and passes through, so `CapRecordedOnlyWhenExhausted` and `NoUnreviewedShip`'s cap-exhausted disjunct have antecedents | 28 / 28 | 0.9s |
| `OrgPlane_Witness_Completes.cfg` | `NeverCompletes` | the release gate opens at all | 4,859 / 1,068 | 1.0s |
| `OrgPlane_Witness_CanonicalPath.cfg` | `NeverCompletesTheCanonicalWay` | it opens on a task that actually *ran* — see below | 15,463 / 2,848 | 1.1s |
| `OrgPlane_Witness_RunLive.cfg` | `NoRunEverLive` | a run slot is genuinely claimed, so `FencedAssignment` is not honoured by never assigning anything | 22 / 19 | 0.9s |
| `OrgPlane_Witness_SplitBrainBlocks.cfg` | `SplitBrainNeverBlocks` | a fully reviewed, conflict-free task is held shut by a CONFIRMED split-brain **alone** | 8,628 / 1,737 | 1.0s |
| `OrgPlane_Witness_ConflictsBlock.cfg` | `ConflictsNeverBlock` | a fully reviewed, split-brain-free task is held shut by an unresolved conflict row **alone** | 4,862 / 1,071 | 1.1s |

Two of these are worth more than the row they occupy.

**`NoStaleClearanceEverExists`** fires in the *shipped* design. The dangerous state — a
hook holding a clearance for an answer version that is no longer the answer in hand — is
reachable with the restart chain on. `NoUnreviewedShip` does not hold because stale
clearances never arise; it holds because the restart chain refuses to *ship* from that
state and re-consults the hook instead. Without this witness, `NoUnreviewedShip` could have
been green for entirely the wrong reason, and the spec would have proved nothing about the
mechanism it was written to check.

**`NeverCompletesTheCanonicalWay`** exists because of a finding about the kernel's own edge
set. `blocked` is reachable from every non-terminal and returns to five states including
`under_review`, so the matrix as given admits `draft → blocked → under_review → accepted →
completed`: a task at the release gate that never ran. Every such shortcut is shorter than
the real lifecycle, so BFS reports one of them for `NeverCompletes` and the join to the
execution plane would go unwitnessed. Pinning `blocks = 0` in the predicate forbids all of
them, and the counterexample is then necessarily the canonical chain: `draft → ready →
assigned → running → submitted → under_review → accepted → completed`, run claimed and
retired along the way. This is not a defect in the transition matrix — a blocked task
genuinely can be unblocked into review — but it is worth knowing that "reaches the gate"
and "reaches the gate having done the work" are different claims in this edge set.

### Broken variants — TLC must VIOLATE each

`ArtifactPlane_Broken.tla` and `OrgPlane_Broken.tla` are copies with the rejected designs
wired back in behind boolean constants. Copies rather than `EXTENDS`, because the defects
live inside guards and TLA+ has no override: an extending module can add an action but
cannot weaken a guard the original enforces.

| Config | Property violated | Defect | States | Wall |
|---|---|---|---|---|
| `ArtifactPlane_Broken_NoLostWrite.cfg` | `NoLostWrite` | `ForceCommit` — last-writer-wins: a stale proposal overwrites main's head with no conflict row, so the head it displaced becomes reachable from nothing | 179 / 177 | 1.0s |
| `ArtifactPlane_Broken_SoleIntegrator.cfg` | `SoleIntegrator` | `MemberCommits` — the rejected "every member commits to main" design | 11 / 11 | 0.9s |
| `ArtifactPlane_Broken_ConflictNotOverwrite.cfg` | `ConflictNotOverwrite` | `ForceCommit`, breaking both clauses: two siblings off one parent both land, and an attempt that did not land is nowhere in `conflicts` | 172 / 171 | 0.9s |
| `ArtifactPlane_Broken_Overlap.cfg` | `OverlapNeverMerges` | `AutoMerge` — the rejected "auto-merge everything" design; an overlap resolved by rule is one author's work deleted by a coin toss | 172 / 171 | 0.9s |
| `ArtifactPlane_Broken_AuthorBound.cfg` | `AuthorBound` | `RewriteAuthor` — authorship reported by the model rather than bound by the wire | 6 / 6 | 0.9s |
| `AnswerLoop_PerHook.cfg` | `NoUnreviewedShip` | the rejected per-hook-loops design — see the trace below | 16 / 15 | 0.9s |
| `OrgPlane_Broken_GateConflicts.cfg` | `GateSoundness` | the gate stops reading `conflicts`, so a task releases over an unresolved collision | 8,614 / 1,731 | 1.0s |
| `OrgPlane_Broken_GateSplitBrain.cfg` | `GateSoundness` | the gate stops reading `split_brain`, so an answer built on two rival decisions ships | 13,886 / 2,571 | 1.1s |
| `OrgPlane_Broken_GateReviews.cfg` | `GateSoundness` | the gate stops requiring reviews **and** `Accepted` drops its own copy — see the redundancy finding | 878 / 280 | 0.9s |
| `OrgPlane_Broken_Fence.cfg` | `FencedAssignment` | `UnfencedStart` **and** `LeakRun` — two live team runs on one task contract | 13,045 / 2,501 | 1.1s |
| `OrgPlane_Broken_LeakedRun.cfg` | `RunningHasOneRun` | `LeakRun` — `Submit` does not retire the run, so a live run outlives the execution phase | 164 / 75 | 1.0s |
| `OrgPlane_Broken_IllegalEdge.cfg` | `NoIllegalTransition` | `IllegalShortcut` — a `ready → submitted` step, an edge the matrix does not have (`Legal()` is untouched, so the check is genuinely against the matrix) | 11 / 11 | 0.9s |
| `OrgPlane_Broken_BankedReviews.cfg` | `ReviewsNotBanked` | `BankReviews` — `revision_required` keeps the reviews that graded the answer about to be replaced; `_answer_loop`'s defect one plane up | 884 / 286 | 0.9s |

#### The redundancy finding: two defects that individually do nothing

Two of the OrgPlane properties turned out to be protected *twice*, and knocking out one
guard leaves the other holding. Measured, not inferred:

| Defect(s) on | Verdict | Distinct states |
|---|---|---|
| `GateSkipsReviews` only | **no violation** | 11,700 |
| `AcceptWithoutReviews` only | **no violation** | 17,172 |
| both | `GateSoundness` violated | 280 |
| `UnfencedStart` only | **no violation** | 11,700 |
| `LeakRun` only | `RunningHasOneRun` violated | 75 |
| both | `FencedAssignment` violated | 2,501 |

The reviews are guarded by `Accepted` (which will not leave `under_review` without a full
set) *and* by the gate. The fence is guarded at the claim (`LiveRuns = {}`) *and* by the
retire on every path out of execution — `Start` only fires from `assigned`, and every path
into `assigned` passes through a step that retires the live run, so the dropped fence has
nothing to admit. Both are real defence in depth. Recording it here rather than quietly
using a paired config is the point: the guard-must-fire evidence for those two conjuncts is
conditional on the shadowing guard being out, and a reader deciding to simplify either one
should know the other is currently carrying it.

## The rejected design, and its counterexample

This is the deliverable that justifies the restart chain. `AnswerLoop.tla` carries both
designs behind one constant, so the comparison is a controlled experiment: identical state
space, identical property, one primed assignment apart.

```
RestartOnRevise = TRUE     Revise sets  i' = 1   the shipped design (core.py's `break`
                                                 out of the for-loop, while-loop walks again)
RestartOnRevise = FALSE    Revise sets  i' = i   the rejected design: per-hook loops, the
                                                 walk never returns to a hook it passed
```

With `K = 2`, `MaxCap = 1`, TLC violates `NoUnreviewedShip` in 15 distinct states:

```
State 1  Init            i=1  answerVer=1  cleared=<<0,0>>  rounds=<<0,0>>
State 2  Accept(1)       i=2  answerVer=1  cleared=<<1,0>>  rounds=<<0,0>>
State 3  Revise(2)       i=2  answerVer=2  cleared=<<1,0>>  rounds=<<0,1>>
State 4  Accept(2)       i=3  answerVer=2  cleared=<<1,2>>  rounds=<<0,1>>
State 5  Ship            pc="shipped"  answerVer=2  cleared=<<1,2>>
```

Read it in the code's terms. Hook 1 accepted **answer version 1**. Hook 2 then revised,
which re-ran the lead and produced **version 2**. In the rejected design the walk carries
on from hook 2, hook 2 accepts the new answer, the pass ends, and version 2 ships — with
hook 1's clearance still standing for version 1, an answer that no longer exists. The
final state is exactly the defect `_answer_loop`'s docstring names: *"an accepted-then-
mutated answer ships unreviewed."* `cleared = <<1, 2>>` against `answerVer = 2` is that
sentence in variables.

Run the same constants with `RestartOnRevise = TRUE` and it is green in 30 distinct states.
The restart from `i = 1` puts version 2 back in front of hook 1, and shipping becomes
possible only after one full uninterrupted pass — which is what `NoUnreviewedShip` says.
`BudgetMonotone` is what makes that terminate rather than loop: the restart re-consults
hook 1 without refilling its budget.

## What each spec deliberately abstracts away

Each module's header comment carries the full list; the load-bearing ones:

**All three.** No content, no text, no diffs, no model outputs, no cost. Every quantity is
bounded by a constant and every constant is small. These specs verify coordination
skeletons — which states are reachable under which rules — and nothing about whether the
agent inside a state did its job. That is what post-conditions, `detect/`'s probes and
Hypothesis are for, and the distinction is `process/tla.py`'s own opening paragraph.

**`ArtifactPlane`.** A revision is an integer. `three_way_merge` collapses to one boolean
per proposal chosen nondeterministically at `Propose` time — in the store, overlap is a
property of the `(ancestor, head, proposal)` triple and can change as main moves, so fixing
it per proposal is strictly coarser and covers both outcomes on every reachable head. No
content addressing, so an identical re-proposal is two revisions here where the store makes
it one row. One artifact path, because the planes never interact across paths. `decides`
and `split_brain` are `OrgPlane`'s subject.

**`AnswerLoop`.** The answer is a version counter; every `Revise` increments it, which is
all that "the lead re-ran" means to the property. The cap is fixed per hook, where the code
reads it off each verdict — so the model loses the case of a hook raising its own cap
mid-loop, which is why `BoundedRevisions` is stated against the caps in play rather than as
an absolute number. Each hook's verdict is nondeterministic at every consultation, which is
strictly more behaviour than any real hook: the core makes no assumption about what a hook
decides, so neither does the spec.

**`OrgPlane`.** One task. No goals, findings, idempotency keys or optimistic-concurrency
versions — fencing is modeled because it is a safety property, but not the mechanism that
implements it. `split_brain` is *derived* from one design question decided on two branches,
rather than being a free three-valued variable, because a gate proven sound against a fact
nothing produced is not proven sound. Rework, blocking and input-waiting are each bounded at
one round trip; the kernel's matrix permits all three to cycle forever, which is a genuine
non-terminating behaviour that would correctly refute `Termination`, so the bounds are what
make the liveness question askable and they are constants rather than hidden assumptions.

One deliberate polarity choice worth flagging, because it is a design decision and not an
abstraction: `GateOpen` treats `split_brain = UNSETTLED` as **not blocking**. The probe
abstains when no member declared what its change decides, and a gate that refused on an
abstention would make declaring a decision mandatory — which `propose_change` deliberately
does not do, on the grounds that a member forced to name a design question would invent
one. Only `CONFIRMED` holds the gate shut. `OrgPlane_Witness_SplitBrainBlocks.cfg` is the
evidence that `CONFIRMED` really does.
