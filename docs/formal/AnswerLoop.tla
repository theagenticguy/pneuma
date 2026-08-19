------------------------------ MODULE AnswerLoop ------------------------------
(***************************************************************************)
(* The execution plane: `Team._answer_loop` in `team/core.py`, exactly its  *)
(* restart chain.                                                          *)
(*                                                                         *)
(* Every hook with `on_answer` reviews in order. `Accept` moves to the next *)
(* hook. `Revise` re-runs the lead -- which MUTATES the answer -- and then  *)
(* the walk restarts from the first reviewing hook, with per-hook budgets   *)
(* that persist across restarts. A hook whose rounds have reached its cap   *)
(* passes the answer through and records `revise_cap` once.                 *)
(*                                                                         *)
(* One CONSTANT switches between the shipped design and the one the code    *)
(* rejected:                                                               *)
(*                                                                         *)
(*   RestartOnRevise = TRUE   the shipped restart chain: i' = 1             *)
(*   RestartOnRevise = FALSE  per-hook loops: i' = i, the walk never goes   *)
(*                            back to a hook it already passed             *)
(*                                                                         *)
(* One module with one bit different rather than two modules, so the        *)
(* comparison is airtight: identical state space, identical properties, one *)
(* primed assignment apart. AnswerLoop_PerHook.cfg is the rejected design   *)
(* and TLC finds the counterexample there -- that counterexample is the     *)
(* whole justification for the restart chain.                               *)
(*                                                                         *)
(* What is deliberately abstracted away:                                    *)
(*                                                                         *)
(*   - No content. The answer is a version counter; every Revise increments *)
(*     it, which is the only thing about "the lead re-ran" that matters to   *)
(*     the property (the answer a hook already graded is no longer the       *)
(*     answer in hand).                                                     *)
(*   - No feedback text, no transcript entries beyond the `revise_cap`       *)
(*     once-per-hook bookkeeping the code keeps in `cap_recorded`.           *)
(*   - Each hook's verdict is nondeterministic: at any consultation it may   *)
(*     Accept or want to Revise. That is strictly more behaviour than any    *)
(*     real hook, which is the right direction -- the core makes no          *)
(*     assumption about what a hook decides.                                *)
(*   - The cap is fixed per hook here. In the code it rides on each verdict, *)
(*     so a hook may lower or raise it mid-loop. Fixing it loses the         *)
(*     raise-forever case, which is why BoundedRevisions below is stated     *)
(*     against the caps in play rather than as an absolute bound.            *)
(*   - The lead never fails. A mid-loop fault is `Team.run`'s finally, not   *)
(*     this loop's.                                                         *)
(***************************************************************************)
EXTENDS Integers

CONSTANTS
    K,                \* how many hooks implement on_answer
    MaxCap,           \* the largest per-hook cap the model explores
    RestartOnRevise   \* TRUE: the shipped design. FALSE: the rejected one.

Hooks == 1 .. K       \* integers so `in order` is the natural order

VARIABLES
    pc,          \* "reviewing" | "shipped"
    i,           \* which hook the walk is at; K+1 means the pass completed
    cap,         \* [Hooks -> 1..MaxCap] -- chosen in Init, never changes
    rounds,      \* [Hooks -> Nat] -- the code's `rounds` dict
    capRec,      \* SUBSET Hooks -- the code's `cap_recorded` set
    answerVer,   \* which answer is in hand; every Revise re-runs the lead
    cleared      \* [Hooks -> Nat] -- the answer version at which h last
                 \* Accepted or was cap-exhausted; 0 = never cleared

vars == <<pc, i, cap, rounds, capRec, answerVer, cleared>>

\* The sum of the caps in play: the code's own termination argument ("every
\* restart increments some hook's rounds, bounded by the sum of the caps").
SumCap == LET S[n \in 0 .. K] == IF n = 0 THEN 0 ELSE S[n - 1] + cap[n]
          IN S[K]

MaxVer == 1 + K * MaxCap

TypeOK ==
    /\ pc \in {"reviewing", "shipped"}
    /\ i \in 1 .. (K + 1)
    /\ cap \in [Hooks -> 1 .. MaxCap]
    /\ rounds \in [Hooks -> 0 .. MaxCap]
    /\ capRec \subseteq Hooks
    /\ answerVer \in 1 .. MaxVer
    /\ cleared \in [Hooks -> 0 .. MaxVer]

\* `cap \in [...]` rather than `=`: TLC explores every cap vector, so no property
\* holds because of one lucky assignment. process/tla.py makes the same move for
\* its nondeterministic variables and for the same reason.
Init ==
    /\ pc = "reviewing"
    /\ i = 1
    /\ cap \in [Hooks -> 1 .. MaxCap]
    /\ rounds = [h \in Hooks |-> 0]
    /\ capRec = {}
    /\ answerVer = 1
    /\ cleared = [h \in Hooks |-> 0]

-----------------------------------------------------------------------------
\*                                Actions                                   *
-----------------------------------------------------------------------------

\* `isinstance(verdict, Accept): continue`. The hook graded THIS answer version,
\* so that is what it is recorded as having cleared.
Accept(h) ==
    /\ pc = "reviewing"
    /\ i = h
    /\ cleared' = [cleared EXCEPT ![h] = answerVer]
    /\ i' = i + 1
    /\ UNCHANGED <<pc, cap, rounds, capRec, answerVer>>

\* `rounds[label] < verdict.cap`: spend one round, re-run the lead, and -- in the
\* shipped design -- break out of the for-loop so the while-loop walks again from
\* the first reviewing hook.
Revise(h) ==
    /\ pc = "reviewing"
    /\ i = h
    /\ rounds[h] < cap[h]
    /\ rounds' = [rounds EXCEPT ![h] = rounds[h] + 1]
    /\ answerVer' = answerVer + 1     \* the lead re-ran; the answer mutated
    /\ i' = IF RestartOnRevise THEN 1 ELSE i
    /\ UNCHANGED <<pc, cap, capRec, cleared>>

\* `rounds[label] >= verdict.cap`: the hook wanted a revision and has no budget.
\* Exhaustion is not an error -- the answer passes through, and `revise_cap` is
\* recorded once per hook (`cap_recorded`). Cap-exhausted counts as cleared: it is
\* the second of the two ways NoUnreviewedShip lets a hook be satisfied.
CapPass(h) ==
    /\ pc = "reviewing"
    /\ i = h
    /\ rounds[h] >= cap[h]
    /\ capRec' = capRec \cup {h}
    /\ cleared' = [cleared EXCEPT ![h] = answerVer]
    /\ i' = i + 1
    /\ UNCHANGED <<pc, cap, rounds, answerVer>>

\* The for-loop ran to the end without a `break`, so `revised` stayed False and the
\* while-loop exits. `_answer_loop` returns the answer.
Ship ==
    /\ pc = "reviewing"
    /\ i = K + 1
    /\ pc' = "shipped"
    /\ UNCHANGED <<i, cap, rounds, capRec, answerVer, cleared>>

\* The loop legitimately terminates, so the terminal state must be able to stall or
\* TLC reports its own deadlock on every completing run -- process/tla.py's `Done`.
Done == pc = "shipped" /\ UNCHANGED vars

Next ==
    \/ \E h \in Hooks : Accept(h) \/ Revise(h) \/ CapPass(h)
    \/ Ship
    \/ Done

Spec     == Init /\ [][Next]_vars
SpecFair == Init /\ [][Next]_vars /\ WF_vars(Next)

-----------------------------------------------------------------------------
\*                              The properties                              *
-----------------------------------------------------------------------------

\* THE property the restart chain was built for. Shipping requires one full
\* uninterrupted pass: every hook cleared the answer VERSION THAT IS SHIPPING, not
\* some earlier one it graded before a later hook's Revise mutated it.
\*
\* Under RestartOnRevise this holds because reaching i = K+1 means K consecutive
\* clearances with no intervening Revise, so answerVer never moved during the pass.
\* Under the rejected design it fails; see AnswerLoop_PerHook.cfg.
NoUnreviewedShip ==
    pc = "shipped" => \A h \in Hooks : cleared[h] = answerVer

\* A hook never spends more than its cap, and a restart re-consults a hook without
\* refilling its budget. The first clause is a state predicate; the second is an
\* action property, because no single state can witness a counter going backwards.
BudgetBounded == \A h \in Hooks : rounds[h] <= cap[h]

BudgetMonotone ==
    [][ /\ \A h \in Hooks : rounds'[h] >= rounds[h]
        /\ cap' = cap ]_vars

\* The bounded-steps argument, stated as a checkable invariant rather than left as
\* prose: the answer is re-run once per spent round and no more, so the number of
\* lead re-runs is at most the sum of the caps in play.
BoundedRevisions ==
    /\ answerVer = 1 + (rounds[1] + (IF K > 1 THEN rounds[2] ELSE 0)
                                  + (IF K > 2 THEN rounds[3] ELSE 0))
    /\ answerVer <= 1 + SumCap

\* `revise_cap` is recorded once per hook, never twice -- the `cap_recorded` set.
\* A hook is in capRec only if it genuinely ran out of budget.
CapRecordedOnlyWhenExhausted ==
    \A h \in capRec : rounds[h] >= cap[h]

\* The loop always ends.
Termination == <>(pc = "shipped")

-----------------------------------------------------------------------------
\*                          Vacuity witnesses                               *
-----------------------------------------------------------------------------
(* Asserted as invariants TLC is EXPECTED to violate. Each counterexample is the *)
(* witness that the corresponding property above is not passing because nothing  *)
(* ever reached its interesting configuration.                                   *)

\* NoUnreviewedShip is trivially true if the loop always ships the first answer.
NeverShipsARevisedAnswer == ~(pc = "shipped" /\ answerVer > 1)

\* The one that matters. The DANGEROUS state -- a hook holding a clearance for an
\* answer version that is no longer the answer in hand -- is reachable in the
\* shipped design too. NoUnreviewedShip holds not because that state never occurs
\* but because the restart chain refuses to SHIP from it. Without this witness,
\* NoUnreviewedShip could be passing for the wrong reason.
NoStaleClearanceEverExists ==
    ~ \E h \in Hooks : cleared[h] # 0 /\ cleared[h] < answerVer

\* CapRecordedOnlyWhenExhausted and the cap-exhausted disjunct of NoUnreviewedShip
\* are vacuous unless a hook actually runs out of budget.
NoHookEverExhausts == capRec = {}

=============================================================================
