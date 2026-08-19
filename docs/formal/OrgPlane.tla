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
    /\ conflicts = 0
    /\ SplitBrain # "CONFIRMED"
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
Start ==
    /\ task = "assigned"
    /\ LiveRuns = {}
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
Submit ==
    /\ task = "running"
    /\ task' = "submitted"
    /\ runs' = Retire(runs)
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

Accepted ==
    /\ task = "under_review"
    /\ reviews = Reviewers
    /\ task' = "accepted"
    /\ everAccepted' = TRUE
    /\ UNCHANGED <<runs, reviews, conflicts, decides, rework, blocks, waits>>

\* Sent back. The reviews are discarded: they graded an answer that is about to be
\* replaced, which is `_answer_loop`'s restart-chain argument one plane up.
RevisionRequired ==
    /\ task = "under_review"
    /\ rework < MaxRework
    /\ task' = "revision_required"
    /\ reviews' = {}
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

\* GateOpen's conflict conjunct, same argument: an accepted, fully reviewed,
\* split-brain-free task held shut by an unresolved conflict row alone.
ConflictsNeverBlock ==
    ~(task = "accepted" /\ reviews = Reviewers /\ conflicts > 0
      /\ SplitBrain # "CONFIRMED")

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
