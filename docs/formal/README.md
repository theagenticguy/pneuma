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

Swap the `-config` and the `.tla` for any row in the tables below. There are 34 configs. The
six primary ones must be green; the other 28 are ones TLC is *expected* to fail, and a green
result there is the failure — including `OrgPlane_Broken_XPL1Regression.cfg`, which is the
standing regression test for the gate polarity.

TLC writes a `states/` checkpoint directory next to the spec on a violation. It is
gitignored; pass `-cleanup` if you would rather it not appear at all.

## Results

Measured on this machine (Corretto 25.0.4, tla2tools 2.19, 16 cores, 1 worker), all runs
exhaustive with 0 states left on queue.

State counts are exact and reproducible — they are a property of the model, not of the
machine. Wall times are indicative only: this box was under other load while these ran (load
average ~28), and repeated runs of the same config varied by ~20% (13.7s / 15.4s / 16.1s for
`ArtifactPlane.cfg`). Every config finishes in seconds either way, which is the claim that
matters; treat the times as an order of magnitude and the counts as the record.

### Primary — must be green

| Config | Checks | States (generated / distinct) | Wall |
|---|---|---|---|
| `ArtifactPlane.cfg` | `TypeOK`, `NoLostWrite`, `SoleIntegrator`, `ConflictNotOverwrite`, `OverlapNeverMerges`, `AuthorBound` | 117,337 / 32,409 | ~14s |
| `ArtifactPlane_Live.cfg` | `ProposalsSettle` under per-proposal fairness on the lead | 117,337 / 32,409 | ~25s |
| `AnswerLoop.cfg` | `TypeOK`, `NoUnreviewedShip`, `BudgetBounded`, `BoundedRevisions`, `CapRecordedOnlyWhenExhausted`, `BudgetMonotone` | 3,122 / 1,900 | ~4s |
| `AnswerLoop_Live.cfg` | `Termination` | 3,122 / 1,900 | ~4s |
| `OrgPlane.cfg` | `TypeOK`, `FencedAssignment`, `RunningHasOneRun`, `ReviewsNotBanked`, `NoReleaseOnAbsentEvidence`, `WaiverCannotMaskDivergence`, `NoIllegalTransition`, `GateSoundness` | 157,782 / 23,362 | ~6s |
| `OrgPlane_Live.cfg` | `Termination` under `WF_vars(TaskStep)` | 157,782 / 23,362 | ~8s |

Search depths: 9 (ArtifactPlane), 17 (AnswerLoop), 25 (OrgPlane).

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
| `ArtifactPlane_Witness_Siblings.cfg` | `NoSiblingProposals` | two proposals off one parent are both offered to the lead — `ConflictNotOverwrite`'s sibling clause | 170 / 170 | ~3s |
| `ArtifactPlane_Witness_Merge.cfg` | `NoMergeEverLands` | a merge revision reaches main through `mergedFrom`, so `NoLostWrite`'s reachability disjunct is load-bearing on the *second* parent edge | 211 / 209 | ~3s |
| `ArtifactPlane_Witness_Overlap.cfg` | `NoOverlapEverRefused` | an overlapping proposal is reached and refused — `OverlapNeverMerges` is not passing because overlap never happens | 170 / 170 | ~3s |
| `AnswerLoop_Witness_Revised.cfg` | `NeverShipsARevisedAnswer` | a revised answer (`answerVer > 1`) does reach `shipped` | 254 / 233 | ~3s |
| `AnswerLoop_Witness_StaleClearance.cfg` | `NoStaleClearanceEverExists` | **the important one** — see below | 26 / 26 | ~3s |
| `AnswerLoop_Witness_Exhaustion.cfg` | `NoHookEverExhausts` | a hook really runs out of budget and passes through, so `CapRecordedOnlyWhenExhausted` and `NoUnreviewedShip`'s cap-exhausted disjunct have antecedents | 28 / 28 | ~3s |
| `OrgPlane_Witness_Completes.cfg` | `NeverCompletes` | the release gate opens at all | 12,768 / 2,800 | ~4s |
| `OrgPlane_Witness_CanonicalPath.cfg` | `NeverCompletesTheCanonicalWay` | it opens on a task that actually *ran* — see below | 35,433 / 6,856 | ~5s |
| `OrgPlane_Witness_RunLive.cfg` | `NoRunEverLive` | a run slot is genuinely claimed, so `FencedAssignment` is not honoured by never assigning anything | 24 / 21 | ~3s |
| `OrgPlane_Witness_SplitBrainBlocks.cfg` | `SplitBrainNeverBlocks` | a fully reviewed, conflict-free task is held shut by a CONFIRMED split-brain **alone** | 12,784 / 2,807 | ~4s |
| `OrgPlane_Witness_UnsettledBlocks.cfg` | `UnsettledNeverBlocks` | **the XPL-1/XPL-2 witness** — the gate held shut solely because the probe could not tell; see the gate-polarity section | 3,219 / 882 | ~3s |
| `OrgPlane_Witness_ConflictsBlock.cfg` | `ConflictsNeverBlock` | a fully reviewed, split-brain-free task is held shut by an unresolved conflict row **alone** | 12,762 / 2,795 | ~4s |
| `OrgPlane_Witness_WaiverPath.cfg` | `NeverCompletesOnAWaiver` | a team with no recorded decision waives once and releases — the state that is unreachable if the strict gate ships without the waiver | 12,768 / 2,800 | ~4s |

Three of these are worth more than the row they occupy. `UnsettledNeverBlocks` is the third,
and it has its own section below, since it is the witness the XPL-1/XPL-2 resolution turns on.

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
| `ArtifactPlane_Broken_NoLostWrite.cfg` | `NoLostWrite` | `ForceCommit` — last-writer-wins: a stale proposal overwrites main's head with no conflict row, so the head it displaced becomes reachable from nothing | 179 / 177 | ~3s |
| `ArtifactPlane_Broken_SoleIntegrator.cfg` | `SoleIntegrator` | `MemberCommits` — the rejected "every member commits to main" design | 11 / 11 | ~3s |
| `ArtifactPlane_Broken_ConflictNotOverwrite.cfg` | `ConflictNotOverwrite` | `ForceCommit`, breaking both clauses: two siblings off one parent both land, and an attempt that did not land is nowhere in `conflicts` | 172 / 171 | ~3s |
| `ArtifactPlane_Broken_Overlap.cfg` | `OverlapNeverMerges` | `AutoMerge` — the rejected "auto-merge everything" design; an overlap resolved by rule is one author's work deleted by a coin toss | 172 / 171 | ~3s |
| `ArtifactPlane_Broken_AuthorBound.cfg` | `AuthorBound` | `RewriteAuthor` — authorship reported by the model rather than bound by the wire | 6 / 6 | ~3s |
| `AnswerLoop_PerHook.cfg` | `NoUnreviewedShip` | the rejected per-hook-loops design — see the trace below | 16 / 15 | ~3s |
| `OrgPlane_Broken_GateConflicts.cfg` | `GateSoundness` | the gate stops reading `conflicts`, so a task releases over an unresolved collision | 21,016 / 4,282 | ~4s |
| `OrgPlane_Broken_GateSplitBrain.cfg` | `GateSoundness` | the gate relaxes to the pre-resolution `# "CONFIRMED"` wording | 6,965 / 1,664 | ~4s |
| `OrgPlane_Broken_XPL1Regression.cfg` | `NoReleaseOnAbsentEvidence` | **the standing regression test** for XPL-1/XPL-2 — same defect, named against the property that *is* the finding | 6,965 / 1,664 | ~4s |
| `OrgPlane_Broken_GateReviews.cfg` | `GateSoundness` | the gate stops requiring reviews **and** `Accepted` drops its own copy — see the redundancy finding | 3,198 / 887 | ~4s |
| `OrgPlane_Broken_Fence.cfg` | `FencedAssignment` | `UnfencedStart` **and** `LeakRun` — two live team runs on one task contract | 19,492 / 4,085 | ~4s |
| `OrgPlane_Broken_LeakedRun.cfg` | `RunningHasOneRun` | `LeakRun` — `Submit` does not retire the run, so a live run outlives the execution phase | 192 / 93 | ~3s |
| `OrgPlane_Broken_IllegalEdge.cfg` | `NoIllegalTransition` | `IllegalShortcut` — a `ready → submitted` step, an edge the matrix does not have (`Legal()` is untouched, so the check is genuinely against the matrix) | 12 / 12 | ~3s |
| `OrgPlane_Broken_BankedReviews.cfg` | `ReviewsNotBanked` | `BankReviews` — `revision_required` keeps the reviews that graded the answer about to be replaced; `_answer_loop`'s defect one plane up | 1,101 / 388 | ~3s |
| `OrgPlane_Broken_WaiverBackdoor.cfg` | `WaiverCannotMaskDivergence` | `WaiverOverrides` — the waiver becomes a blanket clean verdict, so a team that waived can diverge freely and still read clean | 465 / 193 | ~3s |

#### The redundancy finding: two defects that individually do nothing

Two of the OrgPlane properties turned out to be protected *twice*, and knocking out one
guard leaves the other holding. Measured, not inferred:

| Defect(s) on | Verdict | Distinct states |
|---|---|---|
| `GateSkipsReviews` only | **no violation** | 23,362 |
| `AcceptWithoutReviews` only | **no violation** | 34,306 |
| both | `GateSoundness` violated | 887 |
| `UnfencedStart` only | **no violation** | 23,362 |
| `LeakRun` only | `RunningHasOneRun` violated | 93 |
| both | `FencedAssignment` violated | 4,085 |

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
implements it. `split_brain` is *derived* from one design question decided on two branches
plus the waiver flag, rather than being a free three-valued variable, because a gate proven
sound against a fact nothing produced is not proven sound. `Decide` is guarded `d # 0`
because the store's revision log is append-only and a decision cannot be un-recorded — see
the fidelity-bug note above, which is what that guard came from. Rework, blocking and
input-waiting are each bounded at
one round trip; the kernel's matrix permits all three to cycle forever, which is a genuine
non-terminating behaviour that would correctly refute `Termination`, so the bounds are what
make the liveness question askable and they are constants rather than hidden assumptions.

## The gate polarity: XPL-1/XPL-2, and what it cost this spec to be wrong

`GateOpen` requires `split_brain = "NONE"` — affirmatively clean. It did not always. The
first version of this spec accepted anything `# "CONFIRMED"`, and argued for it: refusing on
an abstention would make declaring a decision mandatory, which `propose_change` deliberately
does not do, since a member forced to name a design question would invent one.

That argument was wrong, and the parallel symspec/Z3 track proved it (finding XPL-1/XPL-2,
resolved on main in `19073a4`). `# "CONFIRMED"` is a two-valued test over a three-valued
probe, so it lets `UNSETTLED` — could-not-tell, and the *likeliest first-run state*, since
nothing requires a member to fill in `decides` — open the gate on absence of evidence. That
contradicts the review-integrity rule `hooks/review.py` applies to every reviewer: an
errored, empty or never-spawned reviewer must never settle `Accept`, because absence of
findings under failure settles nothing. The probe verdict is a review, and only its
affirmative outcome settles it.

The specific error is worth naming, because it is the one a model-checked spec cannot catch
about itself. TLC proved the gate sound *against the contract the spec stated*, exhaustively
and correctly; stating the contract wrong is outside what it can see. The mistake was
treating two separable constraints as one — "don't force a member to invent a design
question" and "don't open the gate on no evidence" — so honouring the first looked like
grounds to abandon the second. `RecordWaiver` separates them: a team with genuinely nothing
to declare says so **once**, affirmatively, and the gate reads a real verdict rather than an
absence. Nobody is forced to invent a question; somebody is required to state there isn't
one.

Three things now pin this down, and all three are new:

- **`NoReleaseOnAbsentEvidence`** — `task = "completed" => SplitBrain # "UNSETTLED"`, named
  as its own property because it *is* the finding's sentence, rather than left to be derived
  from an equality. Sound in this *state* form for a checked reason: `UNSETTLED` is never
  re-entered (verified over all 23,362 states — leaving it means a decision was recorded or
  the team waived, and neither is reversible). The analogous state claim about `CONFIRMED`
  would **not** be sound — `SplitBrain = "NONE"` is not stable (violated in 56 states),
  because a released task's branches can diverge afterwards. That is the content plane moving
  on rather than a bad release, and it is why `GateSoundness` is an action property.
- **`OrgPlane_Witness_UnsettledBlocks.cfg`** — the witness the resolution demands. Its
  counterexample is a task with both reviews accepted, zero conflict rows, nothing
  diverging, `waived = FALSE`, and the gate still shut, held *solely* because the probe could
  not tell. Under the old polarity that exact state opened the gate, so this trace is the
  precise behavioural difference the fix made.
- **`OrgPlane_Broken_XPL1Regression.cfg`** — the standing regression test. It relaxes the
  conjunct back to `# "CONFIRMED"` and nothing else, and TLC reports
  `NoReleaseOnAbsentEvidence` violated. If that config ever comes back green, the old
  polarity has been reintroduced and the resolution silently undone.

`OrgPlane_Broken_WaiverBackdoor.cfg` guards the remedy rather than trusting it: an escape
hatch from a strict gate is exactly the shape a silent accept returns in, so
`WaiverCannotMaskDivergence` states that a waiver settles only the empty case, and a ninth
defect (`WaiverOverrides`, the waiver as a blanket clean verdict) proves that check can fail.

### A model-fidelity bug the new invariant caught immediately

Adding `NoReleaseOnAbsentEvidence` failed on its first run — not on the gate, but on the
model. `Decide(b, d)` ranged `d` over `Answers`, which **includes 0**, so a branch could
un-record a decision and an already-released task could fall back to `UNSETTLED` one step
after release. `split_brain` reads `store.revisions()`, which is immutable and append-only:
0 means "has not recorded one yet" and is reachable only from `Init`. The action was modeling
a retraction the store cannot perform.

The bug was present in the spec as first committed. Nothing was sensitive to it while the
gate accepted anything `# "CONFIRMED"`, because `UNSETTLED` passed either way — tightening
the gate is what made the model's own infidelity observable. `Decide` now guards `d # 0`,
with the reasoning recorded at the action. Two lessons, both kept in the spec text: a
property added late can pay for itself on the run that introduces it, and the state-invariant
form is what caught this — the step-scoped form would have passed, since the release itself
was clean and the regression arrived one step later.

### Why the waiver is justified by reachability, not liveness

The intuitive argument for `RecordWaiver` is that without it `Termination` breaks for a team
that never uses `decides`. **Measured, that is false**, and the spec says so rather than
repeating it. Deleting `RecordWaiver` from `ContentStep` leaves `Termination` verified
(11,662 distinct states) — because `failed` and `cancelled` are reachable from every
non-terminal, so the task always terminates *somehow*. A spec that terminates by failing is
still terminating.

What the waiver actually restores is the ability to terminate *successfully*. With it
removed, `task = "completed" => Decided # {}` becomes an invariant: such a team can no longer
reach `completed` at all, only an abort. So the justification is reachability, and
`OrgPlane_Witness_WaiverPath.cfg` is the witness — a team that recorded no decision, waived
once, and released. (`<>(task = "completed")` is refutable in *both* models, since an abort is
always available, which is exactly why a liveness property could not have distinguished
shipping from giving up here.)
