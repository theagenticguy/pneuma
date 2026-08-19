------------------------------- MODULE OrgPlane -------------------------------
(***************************************************************************)
(* The org plane joined to the other two: the blackboard kernel's task      *)
(* lifecycle (`~/workplace/omnigent-blackboard-poc`, modeled abstractly     *)
(* from its transition matrix), the team run that carries out a task, and   *)
(* the content-plane facts the release gate reads.                          *)
(*                                                                         *)
(* docs/design/org-plane.md states the three joins this module checks:      *)
(*                                                                         *)
(*   "A blackboard task contract <-> one team run"  -- FencedAssignment      *)
(*   "a task in running has an associated team run" -- RunningHasOneRun      *)
(*   "The release gate spans both planes"           -- GateSoundness         *)
(*                                                                         *)
(* and the rule that keeps the seams honest: each plane references the      *)
(* others by id and never reaches into their semantics. So the content plane *)
(* appears here as exactly the two facts the gate reads -- a count of        *)
(* unresolved `conflicts` rows, and `split_brain`'s three-valued verdict --  *)
(* and nothing else. Revisions, branches and merges are ArtifactPlane.tla's  *)
(* subject, not this module's.                                              *)
(*                                                                         *)
(* The gate requires split_brain AFFIRMATIVELY clean (`= "NONE"`). An        *)
(* earlier revision of this spec accepted anything `# "CONFIRMED"`, which    *)
(* the symspec/Z3 corpus proved contradictory (finding XPL-1/XPL-2, resolved *)
(* on main in 19073a4): a two-valued test over a three-valued probe opens the *)
(* gate on could-not-tell. `RecordWaiver` is the path that keeps that strict  *)
(* gate reachable for a team that never uses `decides` -- see GateOpen and   *)
(* the action itself for the full argument, including what the earlier        *)
(* revision got wrong and why TLC could not have caught it.                  *)
(*                                                                         *)
(* What is deliberately abstracted away:                                    *)
(*                                                                         *)
(*   - One task. Kernel tasks do not interact except through the shared      *)
(*     content plane and the gate, both of which are modeled; a second task  *)
(*     would square the state space and every property here is per-task by   *)
(*     construction.                                                        *)
(*   - No goals, no findings, no idempotency keys, no optimistic-concurrency *)
(*     versions. Fencing is modeled (at most one live run) because it is a   *)
(*     safety property; the mechanism that implements it is not.             *)
(*   - `split_brain`'s verdict is DERIVED from one design question decided   *)
(*     on two branches, which is the shape `split_brain` actually measures    *)
(*     (a `(path, decides)` key, branch -> content digest). A free three-     *)
(*     valued variable would let the gate be proven sound against a fact     *)
(*     nothing produced.                                                     *)
(*   - Rework, blocking and input-waiting are each bounded. The kernel's own *)
(*     matrix permits all three to cycle forever, which is a genuine         *)
(*     non-terminating behaviour and would correctly refute Termination; the  *)
(*     bounds are what make the liveness question askable at all, and they   *)
(*     are stated as constants rather than hidden.                            *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
    Runs,          \* the run slots a task may be assigned, e.g. {1, 2}
    Branches,      \* the content-plane branches carrying decisions
    Decisions,     \* the distinct answers to the one design question
    Reviewers,     \* the reviews the gate requires
    MaxConflicts,  \* how many unresolved `conflicts` rows the model explores
    MaxRework,     \* revision_required -> ready round trips
    MaxBlocks,     \* how many times the task may be blocked
    MaxWaits       \* running -> awaiting_input round trips

-----------------------------------------------------------------------------
\*                  The transition matrix, as an edge set                   *
-----------------------------------------------------------------------------

Terminal == {"failed", "cancelled", "superseded", "completed"}

TaskStates ==
    {"draft", "ready", "assigned", "running", "awaiting_input", "submitted",
     "under_review", "accepted", "revision_required", "blocked"} \cup Terminal

NonTerminal == TaskStates \ Terminal

\* Where a live team run must exist, because the run IS what the task is doing.
Executing == {"running", "awaiting_input"}

\* The POC's transition matrix. NoIllegalTransition checks every step against this
\* relation, so an action added below that forgets an edge is caught rather than
\* silently legalised.
Legal(s, t) ==
    \/ (s = "draft"             /\ t = "ready")
    \/ (s = "ready"             /\ t = "assigned")
    \/ (s = "assigned"          /\ t = "running")
    \/ (s = "running"           /\ t \in {"awaiting_input", "submitted"})
    \/ (s = "awaiting_input"    /\ t = "running")
    \/ (s = "submitted"         /\ t \in {"under_review", "superseded"})
    \/ (s = "under_review"      /\ t \in {"accepted", "revision_required"})
    \/ (s = "revision_required" /\ t = "ready")
    \/ (s = "accepted"          /\ t \in {"superseded", "completed"})
    \* blocked is reachable from any non-terminal, and so are the two aborts.
    \/ (s \in NonTerminal       /\ t \in {"blocked", "failed", "cancelled"})
    \/ (s = "blocked"           /\ t \in {"ready", "assigned", "running",
                                          "awaiting_input", "under_review"})

-----------------------------------------------------------------------------

VARIABLES
    task,       \* the task's state in the matrix above
    runs,       \* [Runs -> {"idle","live","done"}] -- the assignment, fenced
    reviews,    \* SUBSET Reviewers -- which required reviews have been accepted
    conflicts,  \* how many `conflicts` rows are unresolved (content plane)
    decides,    \* [Branches -> 0..|Decisions|] -- 0 = this branch recorded no
                \* `decides` for the question; otherwise which answer it gave
    rework,     \* revision_required -> ready round trips spent
    blocks,     \* blocked entries spent
    waits,      \* awaiting_input entries spent
    everAccepted, \* the task passed through `accepted` at least once
    waived      \* the team affirmatively recorded "no design question to declare"

vars == <<task, runs, reviews, conflicts, decides, rework, blocks, waits,
          everAccepted, waived>>

Answers == 0 .. Cardinality(Decisions)

LiveRuns == {r \in Runs : runs[r] = "live"}
FreeRuns == {r \in Runs : runs[r] = "idle"}

-----------------------------------------------------------------------------
\*             The content plane, as the gate is allowed to see it           *
-----------------------------------------------------------------------------

\* `team/artifacts.py`'s `split_brain`, three-valued for its stated reason: under a
\* boolean, "the plane recorded decisions and none diverged" and "the plane recorded
\* no decisions at all" collapse into one False, and a team nobody asked to declare
\* what its changes decide would read as a team that agreed.
\*
\*   "UNSETTLED"  no revision recorded what it decides (SplitBrain.withheld)
\*   "CONFIRMED"  two branches settled the one question differently
\*   "NONE"       every recorded decision was examined and none diverged
Decided == {b \in Branches : decides[b] # 0}

\* `waived` is the affirmative "this team has no design question to declare", recorded
\* once. It settles ONLY the empty case: the moment any branch records a real decision,
\* the divergence test below takes over and a waiver cannot suppress a CONFIRMED
\* verdict. WaiverCannotMaskDivergence states that as a checked property rather than
\* leaving it to be read off the nesting, because an escape hatch from a strict gate is
\* exactly the shape a silent-accept backdoor takes, and this one is guarded.
SplitBrain ==
    IF Decided = {}
      THEN IF waived THEN "NONE" ELSE "UNSETTLED"
      ELSE IF \E b1, b2 \in Decided : decides[b1] # decides[b2] THEN "CONFIRMED"
      ELSE "NONE"

\* The release gate, exactly as org-plane.md specifies it: the kernel's derived
\* evaluation over accepted reviews AND content-plane cleanliness. This is the
\* IMPLEMENTATION -- Complete's guard is literally this expression, and
\* GateSoundness checks that no other path reaches "completed".
\*
\* The split-brain conjunct is AFFIRMATIVE -- `SplitBrain = "NONE"`, not
\* `# "CONFIRMED"`. The symspec/Z3 corpus (docs/formal/requirements/, finding
\* XPL-1/XPL-2, resolved on main in 19073a4) proved the negative form contradicts the
\* review-integrity rule `hooks/review.py` states for every reviewer: an errored,
\* empty or never-spawned reviewer must never settle Accept, because absence of
\* findings under failure settles nothing. `# "CONFIRMED"` is a two-valued test over a
\* three-valued probe, so it lets UNSETTLED -- could-not-tell, and the LIKELIEST
\* first-run state, since nothing requires a member to fill in `decides` -- open the
\* gate on absence of evidence. That is the silent-accept defect wearing a verdict.
\*
\* An earlier revision of this spec argued the opposite polarity: that refusing on an
\* abstention would make declaring a decision mandatory, which `propose_change`
\* deliberately does not. That argument was wrong, and its error is worth naming,
\* because it is the reason a model-checked spec is not self-certifying -- TLC proved
\* the gate sound against the rule the spec itself stated, and stating the rule wrong
\* is outside what TLC can see. The error was treating "the gate must not force a
\* member to invent a design question" and "the gate must not open on no evidence" as
\* one constraint, so honouring the first looked like grounds to give up the second.
\* They are separable, and RecordWaiver below is what separates them: a team that
\* genuinely has no design question to declare says so ONCE, affirmatively, and the
\* gate reads a real verdict rather than an absence. Nobody is forced to invent a
\* question; somebody is required to state that there isn't one.
GateOpen ==
    /\ task = "accepted"
    /\ conflicts = 0
    /\ SplitBrain = "NONE"
    /\ reviews = Reviewers

-----------------------------------------------------------------------------

TypeOK ==
    /\ task \in TaskStates
    /\ runs \in [Runs -> {"idle", "live", "done"}]
    /\ reviews \subseteq Reviewers
    /\ conflicts \in 0 .. MaxConflicts
    /\ decides \in [Branches -> Answers]
    /\ rework \in 0 .. MaxRework
    /\ blocks \in 0 .. MaxBlocks
    /\ waits \in 0 .. MaxWaits
    /\ everAccepted \in BOOLEAN
    /\ waived \in BOOLEAN

Init ==
    /\ task = "draft"
    /\ runs = [r \in Runs |-> "idle"]
    /\ reviews = {}
    /\ conflicts = 0
    /\ decides = [b \in Branches |-> 0]
    /\ rework = 0
    /\ blocks = 0
    /\ waits = 0
    /\ everAccepted = FALSE
    /\ waived = FALSE

-----------------------------------------------------------------------------
\*                        The content plane's own steps                     *
-----------------------------------------------------------------------------

\* A commit or merge attempt that was not a fast-forward: ArtifactPlane.tla's
\* CommitConflict / MergeOverlap, seen from up here as one more open row.
RecordConflict ==
    /\ conflicts < MaxConflicts
    /\ conflicts' = conflicts + 1
    /\ UNCHANGED <<task, runs, reviews, decides, rework, blocks, waits, everAccepted, waived>>

\* ArtifactStore._resolve closing a row on commit or merge.
ResolveConflict ==
    /\ conflicts > 0
    /\ conflicts' = conflicts - 1
    /\ UNCHANGED <<task, runs, reviews, decides, rework, blocks, waits, everAccepted, waived>>

\* A revision carrying `decides`. A branch that revises its own answer is one voice,
\* not two, so this overwrites rather than accumulates -- `split_brain` keys
\* branch -> digest for exactly that reason.
\*
\* `d # 0` is a FIDELITY guard, and it was missing until TLC caught it through
\* NoReleaseOnAbsentEvidence. `split_brain` reads `store.revisions()`, which is
\* immutable and append-only, so a branch cannot un-record a decision: 0 means "has not
\* recorded one yet" and is reachable only from Init. Allowing d = 0 modeled a
\* retraction the store cannot perform, and it let an already-completed task fall back
\* to UNSETTLED one step after release.
\*
\* The bug predates this revision -- it was in the spec as first committed -- and no
\* property was sensitive to it while the gate accepted anything `# "CONFIRMED"`, since
\* UNSETTLED passed either way. Tightening the gate is what made the model's own
\* infidelity observable, which is the argument for adding properties rather than only
\* actions: the new invariant paid for itself on the run that introduced it.
Decide(b, d) ==
    /\ d # 0
    /\ decides[b] # d
    /\ decides' = [decides EXCEPT ![b] = d]
    /\ UNCHANGED <<task, runs, reviews, conflicts, rework, blocks, waits, everAccepted, waived>>

\* The waiver review: "this team has no design question to declare." The path that lets
\* a team which never uses `decides` still reach a gate demanding an AFFIRMATIVE
\* split_brain verdict. org-plane.md names this remedy directly: "teams that never use
\* `decides` must say so once (a single recorded decision, or a waiver review) rather
\* than passing silently."
\*
\* What removing it actually costs, measured rather than assumed. Deleting RecordWaiver
\* from ContentStep does NOT refute `Termination` -- 11,662 distinct states, still
\* verified -- because `failed` and `cancelled` are reachable from every non-terminal, so
\* the task always terminates SOMEHOW. What it costs is the ability to terminate
\* SUCCESSFULLY: `<>(task = "completed")` is refutable in both models (an abort is always
\* available, so no fair run is obliged to release), but with the waiver removed,
\* `task = "completed" => Decided # {}` becomes an invariant -- a team that never records
\* a decision can no longer reach `completed` at all, only an abort.
\*
\* So the honest statement is reachability, not liveness: without the waiver the strict
\* gate is unreachable for such a team, and `NeverCompletesOnAWaiver` is the witness that
\* the waiver restores it. Worth stating precisely, because "Termination would break" is
\* the intuitive claim and it is false here -- a spec that terminates by failing is still
\* terminating, and a liveness property that cannot distinguish shipping from giving up
\* would have hidden this entirely.
\*
\* Set-once and never cleared, because it is a statement about the team's work rather
\* than about the current answer -- unlike `reviews`, which RevisionRequired discards
\* precisely because those graded a superseded answer. A waiver does not grade an
\* answer, so a rework round does not invalidate it.
\*
\* It is deliberately NOT a get-out-of-gate card: it settles only the empty case (see
\* SplitBrain), so once any branch records a real decision the divergence test governs
\* and a prior waiver cannot suppress CONFIRMED. WaiverCannotMaskDivergence checks that,
\* and OrgPlane_Broken's WaiverOverrides defect is what proves the check can fail.
RecordWaiver ==
    /\ ~waived
    /\ waived' = TRUE
    /\ UNCHANGED <<task, runs, reviews, conflicts, decides, rework, blocks, waits,
                   everAccepted>>

ContentStep ==
    \/ RecordConflict
    \/ ResolveConflict
    \/ RecordWaiver
    \/ \E b \in Branches, d \in Answers : Decide(b, d)

-----------------------------------------------------------------------------
\*                        The task lifecycle's own steps                    *
-----------------------------------------------------------------------------

Retire(f) == [r \in Runs |-> IF f[r] = "live" THEN "done" ELSE f[r]]

Promote ==
    /\ task = "draft"
    /\ task' = "ready"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

Assign ==
    /\ task = "ready"
    /\ task' = "assigned"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* "A blackboard task contract <-> one team run." The fence: a run is claimed only
\* when no run is live, which is what makes FencedAssignment hold rather than
\* merely be hoped for.
Start ==
    /\ task = "assigned"
    /\ LiveRuns = {}
    /\ \E r \in FreeRuns : runs' = [runs EXCEPT ![r] = "live"]
    /\ task' = "running"
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* awaiting_input <-> running. The run stays live across both -- it is waiting, not
\* finished, which is why Executing covers the pair.
AwaitInput ==
    /\ task = "running"
    /\ waits < MaxWaits
    /\ task' = "awaiting_input"
    /\ waits' = waits + 1
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, everAccepted, waived>>

Resume ==
    /\ task = "awaiting_input"
    /\ task' = "running"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* "a team run completing corresponds to submitted". The run goes done in the same
\* step, so no live run outlives the execution phase.
Submit ==
    /\ task = "running"
    /\ task' = "submitted"
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

Review ==
    /\ task = "submitted"
    /\ task' = "under_review"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* One required review accepted. Only while under review, so an acceptance cannot be
\* banked before the work was submitted.
AcceptReview(v) ==
    /\ task = "under_review"
    /\ v \notin reviews
    /\ reviews' = reviews \cup {v}
    /\ UNCHANGED <<task, runs, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

Accepted ==
    /\ task = "under_review"
    /\ reviews = Reviewers
    /\ task' = "accepted"
    /\ everAccepted' = TRUE
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, waived>>

\* Sent back. The reviews are discarded: they graded an answer that is about to be
\* replaced, which is `_answer_loop`'s restart-chain argument one plane up.
RevisionRequired ==
    /\ task = "under_review"
    /\ rework < MaxRework
    /\ task' = "revision_required"
    /\ reviews' = {}
    /\ rework' = rework + 1
    /\ UNCHANGED <<runs, conflicts, decides, blocks, waits, everAccepted, waived>>

Requeue ==
    /\ task = "revision_required"
    /\ task' = "ready"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* blocked from any non-terminal. A live run does not survive it: a blocked task is
\* not executing, and a live run with the task blocked would be a fence held by
\* nobody.
Block ==
    /\ task \in NonTerminal
    /\ task # "blocked"
    /\ blocks < MaxBlocks
    /\ task' = "blocked"
    /\ blocks' = blocks + 1
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, waits, everAccepted, waived>>

\* blocked returns to any of five states. The two Executing targets must re-claim a
\* run slot, under the same fence as Start.
Unblock(t) ==
    /\ task = "blocked"
    /\ t \in {"ready", "assigned", "running", "awaiting_input", "under_review"}
    /\ task' = t
    /\ IF t \in Executing
         THEN /\ LiveRuns = {}
              /\ \E r \in FreeRuns : runs' = [runs EXCEPT ![r] = "live"]
         ELSE runs' = runs
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* submitted/accepted -> superseded.
Supersede ==
    /\ task \in {"submitted", "accepted"}
    /\ task' = "superseded"
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

Abort(t) ==
    /\ task \in NonTerminal
    /\ t \in {"failed", "cancelled"}
    /\ task' = t
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

\* THE cross-plane gate. The only step into "completed", and its guard is the whole
\* of GateOpen -- org-plane.md's "Gate = the kernel's derived evaluation over
\* accepted reviews AND content-plane cleanliness".
Complete ==
    /\ GateOpen
    /\ task' = "completed"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted, waived>>

TaskStep ==
    \/ Promote \/ Assign \/ Start \/ AwaitInput \/ Resume \/ Submit \/ Review
    \/ Accepted \/ RevisionRequired \/ Requeue \/ Block \/ Supersede \/ Complete
    \/ \E v \in Reviewers : AcceptReview(v)
    \/ \E t \in TaskStates : Unblock(t)
    \/ \E t \in TaskStates : Abort(t)

\* A terminal task must be able to stall -- process/tla.py's Done. The content plane
\* keeps moving after the task is terminal (a later run may record a conflict), so
\* Done fires only when the task is terminal AND nothing else is pending.
Done == task \in Terminal /\ UNCHANGED vars

Next == TaskStep \/ ContentStep \/ Done

Spec == Init /\ [][Next]_vars

\* "a task with a fair lead and fair reviewers eventually reaches a terminal state".
\* Fairness on TaskStep only -- NOT on Next -- and that distinction is the whole
\* content of the assumption. WF_vars(Next) would be satisfied by a content plane
\* that churns conflict rows forever while the task never moves, so Termination
\* would fail for a reason that is about the content plane rather than the task.
SpecFair == Init /\ [][Next]_vars /\ WF_vars(TaskStep)

-----------------------------------------------------------------------------
\*                              The properties                              *
-----------------------------------------------------------------------------

\* Every task step is in the POC's edge set. An action property, because a single
\* state cannot witness an edge. Content-plane steps leave `task` alone and are
\* admitted by the first disjunct.
NoIllegalTransition ==
    [][ task' = task \/ Legal(task, task') ]_vars

\* "at most one live team run per task" -- the fence.
FencedAssignment == Cardinality(LiveRuns) <= 1

\* The other half of the same join: a task that is executing has exactly one live
\* run, and a task that is not executing has none. Stated alongside the fence
\* because a fence that is honoured by never assigning anything is not a fence.
RunningHasOneRun ==
    /\ task \in Executing => Cardinality(LiveRuns) = 1
    /\ task \notin Executing => LiveRuns = {}

\* THE cross-plane property. Reaching "completed" requires the gate: no unresolved
\* conflict rows, an AFFIRMATIVELY clean split-brain verdict, every required review
\* accepted, and the task actually having been accepted.
\*
\* The split-brain conjunct is `= "NONE"`, matching GateOpen after XPL-1/XPL-2. Stated
\* as `= "NONE"` rather than as `GateOpen` spelled out, so the property is an
\* INDEPENDENT restatement of the requirement and not a tautology against the guard --
\* if Complete's guard were weakened, this would still be checking the old contract.
\*
\* An ACTION property rather than a state invariant, deliberately. Once the task is
\* terminal the content plane keeps moving -- a later run may record a conflict on
\* the same artifact -- and a conflict detected after release does not retroactively
\* make the release unsound. What must hold is that the STEP into completed saw a
\* clean plane, which is what this says.
GateSoundness ==
    [][ (task # "completed" /\ task' = "completed")
          => /\ conflicts = 0
             /\ SplitBrain = "NONE"
             /\ reviews = Reviewers
             /\ task = "accepted"
             /\ everAccepted' = TRUE ]_vars

\* The gate never opens on could-not-tell. Stated separately from GateSoundness even
\* though `= "NONE"` already implies it at the step, because THIS is the sentence
\* XPL-1/XPL-2 is about and a reader auditing the resolution should find it as its own
\* named property rather than derive it from an equality.
\*
\* Deliberately a STATE invariant, and stronger than the step version for it: a released
\* task must never READ as could-not-tell, not merely have been clean at the instant it
\* released. That strength is what caught the missing `d # 0` guard in Decide -- the step
\* form would have passed, because the release itself was clean and the regression came
\* one step later. Kept as-is rather than weakened to match the step, since "the record
\* still shows why this shipped" is the property an auditor actually wants.
\*
\* It is sound in the state form for a specific and checked reason: UNSETTLED is never
\* RE-ENTERED. Measured -- `[][SplitBrain # "UNSETTLED" => SplitBrain' # "UNSETTLED"]_vars`
\* is verified over the full 23,362 states -- because leaving UNSETTLED means either a
\* branch recorded a decision or the team waived, and (given `d # 0` and set-once `waived`)
\* neither is reversible.
\*
\* The analogous state invariant for CONFIRMED would NOT be sound, and that asymmetry is
\* why only this one is stated this way: `SplitBrain = "NONE"` is NOT stable -- measured, it
\* is violated in 56 states -- since a released task's branches can diverge afterwards. A
\* post-release divergence is the content plane moving on, not a bad release, which is
\* exactly why GateSoundness is an action property and this one can afford not to be.
NoReleaseOnAbsentEvidence == task = "completed" => SplitBrain # "UNSETTLED"

\* The waiver is not a backdoor. It settles ONLY the empty case, so a team that waived
\* and then genuinely diverged is still CONFIRMED and still gate-blocked. Without this,
\* RecordWaiver would be exactly the silent-accept mechanism XPL-1/XPL-2 removed,
\* reintroduced under a friendlier name.
WaiverCannotMaskDivergence ==
    (\E b1, b2 \in Decided : decides[b1] # decides[b2]) => SplitBrain = "CONFIRMED"

\* Reviews are never banked across a rework round: a review that graded a replaced
\* answer must not still count. `accepted` is only ever entered with a full set.
ReviewsNotBanked ==
    /\ task = "revision_required" => reviews = {}
    /\ task = "accepted" => reviews = Reviewers

Termination == <>(task \in Terminal)

-----------------------------------------------------------------------------
\*                          Vacuity witnesses                               *
-----------------------------------------------------------------------------
(* Invariants TLC is EXPECTED to violate. Each counterexample is the witness that *)
(* a property above has a reachable antecedent.                                   *)

\* GateSoundness is vacuous unless the gate ever opens. This is the happy path:
\* draft all the way to completed with a clean content plane.
NeverCompletes == task # "completed"

\* RunningHasOneRun's first clause and FencedAssignment are vacuous unless a run is
\* ever claimed.
NoRunEverLive == LiveRuns = {}

\* GateOpen's split-brain conjunct is vacuous unless CONFIRMED is reachable while
\* the task sits at accepted -- i.e. unless the content plane can actually hold the
\* gate shut on a task that reviews approved.
SplitBrainNeverBlocks ==
    ~(task = "accepted" /\ reviews = Reviewers /\ conflicts = 0
      /\ SplitBrain = "CONFIRMED")

\* THE witness for XPL-1/XPL-2. The gate is held shut SOLELY because the probe could not
\* tell -- reviews all in, no conflict rows, nothing diverging, and still no release,
\* because nobody ever said what their change decides and nobody waived.
\*
\* Under the OLD polarity (`# "CONFIRMED"`) this state opened the gate, so this witness
\* is the precise difference the resolution made. It is also what proves the new
\* conjunct is load-bearing rather than a stricter spelling of the same test: the state
\* it excludes is reachable, and it is the likeliest first-run state.
UnsettledNeverBlocks ==
    ~(task = "accepted" /\ reviews = Reviewers /\ conflicts = 0
      /\ SplitBrain = "UNSETTLED")

\* GateOpen's conflict conjunct, same argument: an accepted, fully reviewed,
\* split-brain-free task held shut by an unresolved conflict row alone. `= "NONE"`
\* rather than `# "CONFIRMED"` so the conflict row is genuinely the only thing shut.
ConflictsNeverBlock ==
    ~(task = "accepted" /\ reviews = Reviewers /\ conflicts > 0
      /\ SplitBrain = "NONE")

\* The waiver path is vacuous unless a team actually completes through it: no branch
\* ever recorded a decision, the waiver is what made the verdict affirmative, and the
\* gate opened. This is the state that would be UNREACHABLE if the strict gate had been
\* added without the waiver -- i.e. the witness that Termination's repair is real and
\* not just a bound that happens to hide a livelock.
NeverCompletesOnAWaiver ==
    ~(task = "completed" /\ waived /\ Decided = {})

\* The canonical path, witnessed separately, and the reason it needs its own witness
\* is a finding about the matrix rather than about this model.
\*
\* `blocked` is reachable from every non-terminal and returns to five states
\* including `under_review` and `running`, so the edge set as given admits
\* draft -> blocked -> under_review -> accepted -> completed: a task that reached the
\* release gate WITHOUT EVER RUNNING. Every such shortcut is strictly shorter than
\* the real lifecycle, so BFS reports one of them for NeverCompletes and the join to
\* the execution plane would be left unwitnessed.
\*
\* This invariant is EXPECTED to be violated too, and its counterexample can only be
\* the canonical chain, because `blocks = 0` forbids every shortcut:
\* draft -> ready -> assigned -> running -> submitted -> under_review -> accepted
\* -> completed.
NeverCompletesTheCanonicalWay ==
    ~(/\ task = "completed"
      /\ blocks = 0
      /\ \E r \in Runs : runs[r] = "done")

=============================================================================
