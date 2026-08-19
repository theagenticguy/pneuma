--------------------------- MODULE OrgPlane_Broken ---------------------------
(***************************************************************************)
(* OrgPlane with the release gate and the fence deliberately broken, so     *)
(* every property is shown to be capable of failing. The guards-must-fire   *)
(* rule applied to specs: an invariant nobody ever watched break is an      *)
(* invariant nobody has evidence is checking anything.                     *)
(*                                                                         *)
(* A copy rather than an EXTENDS, because the defects are inside `Complete`, *)
(* `Start` and `RevisionRequired` -- TLA+ has no override, so an extending   *)
(* module can add actions but cannot weaken a guard the original enforces.   *)
(*                                                                         *)
(* Eight defects, each behind its own boolean constant so a violation is     *)
(* attributable to one cause:                                              *)
(*                                                                         *)
(*   GateSkipsConflicts   the gate stops reading `conflicts` -- releases      *)
(*                        over an unresolved collision                       *)
(*   GateSkipsSplitBrain  the gate stops reading split_brain -- releases an   *)
(*                        answer built on two rival decisions                 *)
(*   GateSkipsReviews     the gate stops requiring the reviews                *)
(*   UnfencedStart        Start drops the LiveRuns = {} fence                 *)
(*   IllegalShortcut      ready -> submitted, an edge the POC's transition    *)
(*                        matrix does not have                              *)
(*   BankReviews          revision_required keeps the reviews that graded    *)
(*                        the answer about to be replaced                    *)
(*   LeakRun              Submit does not retire the run                     *)
(*   AcceptWithoutReviews Accepted drops its own review guard                 *)
(*                                                                         *)
(* The last two exist because of a MEASURED finding: GateSkipsReviews and    *)
(* UnfencedStart, each alone, do NOT break anything. Two guards protect each  *)
(* of those properties and knocking one out leaves the other holding. That   *)
(* is a fact about the design worth recording rather than a hole in the       *)
(* harness, so those two .cfg files pair the gate defect with the guard that  *)
(* shadows it and name which is which. See README.md.                        *)
(*                                                                         *)
(* Every property is reproduced verbatim from OrgPlane.tla.                  *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
    Runs,          \* the run slots a task may be assigned
    Branches,      \* the content-plane branches carrying decisions
    Decisions,     \* the distinct answers to the one design question
    Reviewers,     \* the reviews the gate requires
    MaxConflicts,  \* how many unresolved `conflicts` rows the model explores
    MaxRework,     \* revision_required -> ready round trips
    MaxBlocks,     \* how many times the task may be blocked
    MaxWaits,      \* running -> awaiting_input round trips
    \* The defect switches. Every .cfg sets all eight.
    GateSkipsConflicts, GateSkipsSplitBrain, GateSkipsReviews,
    UnfencedStart, IllegalShortcut, BankReviews,
    LeakRun, AcceptWithoutReviews

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
    everAccepted \* the task passed through `accepted` at least once

vars == <<task, runs, reviews, conflicts, decides, rework, blocks, waits,
          everAccepted>>

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

SplitBrain ==
    IF Decided = {} THEN "UNSETTLED"
    ELSE IF \E b1, b2 \in Decided : decides[b1] # decides[b2] THEN "CONFIRMED"
    ELSE "NONE"

\* The release gate, exactly as org-plane.md specifies it: the kernel's derived
\* evaluation over accepted reviews AND content-plane cleanliness. This is the
\* IMPLEMENTATION -- Complete's guard is literally this expression, and
\* GateSoundness checks that no other path reaches "completed".
\*
\* Note the polarity on UNSETTLED: it does NOT block. `split_brain` abstains when no
\* member declared what its change decides, and a gate that refused on an abstention
\* would make declaring a decision mandatory -- which the propose_change tool
\* deliberately does not do, because a member forced to name a design question would
\* invent one. Only CONFIRMED blocks.
GateOpen ==
    /\ task = "accepted"
    \* DEFECT 1-3: each conjunct becomes vacuously true under its own switch, which
    \* is exactly what "the gate stopped reading that plane" looks like.
    /\ (GateSkipsConflicts  \/ conflicts = 0)
    /\ (GateSkipsSplitBrain \/ SplitBrain # "CONFIRMED")
    /\ (GateSkipsReviews    \/ reviews = Reviewers)

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

-----------------------------------------------------------------------------
\*                        The content plane's own steps                     *
-----------------------------------------------------------------------------

\* A commit or merge attempt that was not a fast-forward: ArtifactPlane.tla's
\* CommitConflict / MergeOverlap, seen from up here as one more open row.
RecordConflict ==
    /\ conflicts < MaxConflicts
    /\ conflicts' = conflicts + 1
    /\ UNCHANGED <<task, runs, reviews, decides, rework, blocks, waits, everAccepted>>

\* ArtifactStore._resolve closing a row on commit or merge.
ResolveConflict ==
    /\ conflicts > 0
    /\ conflicts' = conflicts - 1
    /\ UNCHANGED <<task, runs, reviews, decides, rework, blocks, waits, everAccepted>>

\* A revision carrying `decides`. A branch that revises its own answer is one voice,
\* not two, so this overwrites rather than accumulates -- `split_brain` keys
\* branch -> digest for exactly that reason.
Decide(b, d) ==
    /\ decides[b] # d
    /\ decides' = [decides EXCEPT ![b] = d]
    /\ UNCHANGED <<task, runs, reviews, conflicts, rework, blocks, waits, everAccepted>>

ContentStep ==
    \/ RecordConflict
    \/ ResolveConflict
    \/ \E b \in Branches, d \in Answers : Decide(b, d)

-----------------------------------------------------------------------------
\*                        The task lifecycle's own steps                    *
-----------------------------------------------------------------------------

Retire(f) == [r \in Runs |-> IF f[r] = "live" THEN "done" ELSE f[r]]

Promote ==
    /\ task = "draft"
    /\ task' = "ready"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

Assign ==
    /\ task = "ready"
    /\ task' = "assigned"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* "A blackboard task contract <-> one team run." The fence: a run is claimed only
\* when no run is live, which is what makes FencedAssignment hold rather than
\* merely be hoped for.
\* DEFECT 4: the fence is dropped, so a second run is claimed while the first is
\* still live -- two team runs against one task contract.
Start ==
    /\ task = "assigned"
    /\ (UnfencedStart \/ LiveRuns = {})
    /\ \E r \in FreeRuns : runs' = [runs EXCEPT ![r] = "live"]
    /\ task' = "running"
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* awaiting_input <-> running. The run stays live across both -- it is waiting, not
\* finished, which is why Executing covers the pair.
AwaitInput ==
    /\ task = "running"
    /\ waits < MaxWaits
    /\ task' = "awaiting_input"
    /\ waits' = waits + 1
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, everAccepted>>

Resume ==
    /\ task = "awaiting_input"
    /\ task' = "running"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* "a team run completing corresponds to submitted". The run goes done in the same
\* step, so no live run outlives the execution phase.
\* DEFECT 7: the run is not retired on submit, so a live run outlives the execution
\* phase. This is what makes UnfencedStart bite -- see OrgPlane_Broken_Fence.cfg.
Submit ==
    /\ task = "running"
    /\ task' = "submitted"
    /\ (IF LeakRun THEN runs' = runs ELSE runs' = Retire(runs))
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

Review ==
    /\ task = "submitted"
    /\ task' = "under_review"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* One required review accepted. Only while under review, so an acceptance cannot be
\* banked before the work was submitted.
AcceptReview(v) ==
    /\ task = "under_review"
    /\ v \notin reviews
    /\ reviews' = reviews \cup {v}
    /\ UNCHANGED <<task, runs, conflicts, decides, rework, blocks, waits, everAccepted>>

\* DEFECT 8: the FIRST of the two review guards is dropped. On its own the release
\* gate still catches it -- which is what proves GateOpen's review conjunct is a real
\* second line of defence rather than dead weight. See OrgPlane_Broken_GateReviews.cfg.
Accepted ==
    /\ task = "under_review"
    /\ (AcceptWithoutReviews \/ reviews = Reviewers)
    /\ task' = "accepted"
    /\ everAccepted' = TRUE
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits>>

\* Sent back. The reviews are discarded: they graded an answer that is about to be
\* replaced, which is `_answer_loop`'s restart-chain argument one plane up.
RevisionRequired ==
    /\ task = "under_review"
    /\ rework < MaxRework
    /\ task' = "revision_required"
    \* DEFECT 6: the reviews that graded the answer about to be replaced are kept.
    /\ (IF BankReviews THEN reviews' = reviews ELSE reviews' = {})
    /\ rework' = rework + 1
    /\ UNCHANGED <<runs, conflicts, decides, blocks, waits, everAccepted>>

Requeue ==
    /\ task = "revision_required"
    /\ task' = "ready"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

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
    /\ UNCHANGED <<reviews, conflicts, decides, rework, waits, everAccepted>>

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
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* submitted/accepted -> superseded.
Supersede ==
    /\ task \in {"submitted", "accepted"}
    /\ task' = "superseded"
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

Abort(t) ==
    /\ task \in NonTerminal
    /\ t \in {"failed", "cancelled"}
    /\ task' = t
    /\ runs' = Retire(runs)
    /\ UNCHANGED <<reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* THE cross-plane gate. The only step into "completed", and its guard is the whole
\* of GateOpen -- org-plane.md's "Gate = the kernel's derived evaluation over
\* accepted reviews AND content-plane cleanliness".
Complete ==
    /\ GateOpen
    /\ task' = "completed"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits, everAccepted>>

\* DEFECT 5: ready -> submitted, an edge the POC's transition matrix does not have.
\* Legal() above is untouched, so NoIllegalTransition is what catches it.
Shortcut ==
    /\ IllegalShortcut
    /\ task = "ready"
    /\ task' = "submitted"
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits,
                   everAccepted>>

TaskStep ==
    \/ Shortcut
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
\* conflict rows, no confirmed split-brain, every required review accepted, and the
\* task actually having been accepted.
\*
\* An ACTION property rather than a state invariant, deliberately. Once the task is
\* terminal the content plane keeps moving -- a later run may record a conflict on
\* the same artifact -- and a conflict detected after release does not retroactively
\* make the release unsound. What must hold is that the STEP into completed saw a
\* clean plane, which is what this says.
GateSoundness ==
    [][ (task # "completed" /\ task' = "completed")
          => /\ conflicts = 0
             /\ SplitBrain # "CONFIRMED"
             /\ reviews = Reviewers
             /\ task = "accepted"
             /\ everAccepted' = TRUE ]_vars

\* Reviews are never banked across a rework round: a review that graded a replaced
\* answer must not still count. `accepted` is only ever entered with a full set.
ReviewsNotBanked ==
    /\ task = "revision_required" => reviews = {}
    /\ task = "accepted" => reviews = Reviewers

Termination == <>(task \in Terminal)


=============================================================================
