---------------------------- MODULE ArtifactPlane ----------------------------
(***************************************************************************)
(* The content plane: `team/artifacts.py`'s ArtifactStore and the          *)
(* `team/hooks/artifacts.py` tool split, as a state machine.               *)
(*                                                                         *)
(* Members propose on their own branch; the lead alone advances main, by    *)
(* fast-forward or by a proven non-overlapping merge. Everything else is a  *)
(* row in `conflicts`. The four claims this module checks are the four the  *)
(* store's docstrings assert in prose:                                     *)
(*                                                                         *)
(*   NoLostWrite         nothing a member proposed silently vanishes       *)
(*   SoleIntegrator      only the lead ever advances main's head           *)
(*   ConflictNotOverwrite  two siblings off one parent never both land FF, *)
(*                       and every landing attempt that did not land is    *)
(*                       written down                                      *)
(*   OverlapNeverMerges  an overlapping proposal is never merged in        *)
(*   AuthorBound         a revision's author never changes (action prop)   *)
(*                                                                         *)
(* What is deliberately abstracted away:                                   *)
(*                                                                         *)
(*   - No content. A revision is an integer id. `three_way_merge` is       *)
(*     replaced by one boolean per proposal, `overlapping`, chosen         *)
(*     nondeterministically at Propose time. In the store overlap is a     *)
(*     property of the (ancestor, head, proposal) triple and can therefore *)
(*     change as main moves; fixing it per proposal is strictly coarser and *)
(*     covers both outcomes on every reachable head.                       *)
(*   - No digests, no content addressing, no idempotent re-proposal. Two    *)
(*     Propose steps are always two revisions here.                        *)
(*   - One artifact path. The store keys everything by artifact_id and the  *)
(*     planes never interact across paths, so a second path would multiply  *)
(*     the state space and prove nothing new.                              *)
(*   - `decides` and split_brain live in OrgPlane, not here.                *)
(*   - Bounded: MaxRev revisions total, including the seed and every merge  *)
(*     revision the lead creates.                                          *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
    Members,   \* the proposing members, e.g. {"m1", "m2"}
    Lead,      \* the sole integrator, e.g. "lead"
    Origin,    \* the author of the seeded revision (Artifacts.origin)
    NoOne,     \* the author slot of a revision that does not exist yet
    MaxRev     \* every revision id, seed and merge revisions included

VARIABLES
    author,        \* [Revs -> Actors] -- NoOne until the revision is created
    parent,        \* [Revs -> 0..MaxRev] -- main's head at proposal time; 0 = none
    mergedFrom,    \* [Revs -> 0..MaxRev] -- a merge revision's second parent
    branchHead,    \* [Members -> 0..MaxRev] -- the `refs` row for each member branch
    overlapping,   \* SUBSET Revs -- proposals whose edits collide with main's
    nextRev,       \* the next revision id to hand out
    mainHead,      \* the `refs` row for MAIN
    ffLanded,      \* proposals that advanced main by fast-forward
    mergeLanded,   \* proposals folded into main by a merge revision
    attempted,     \* proposals the lead tried to land (commit or merge)
    conflictRows,  \* proposals named by a row in `conflicts`
    movedBy        \* every actor that has ever advanced main's head

vars == <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
          mainHead, ffLanded, mergeLanded, attempted, conflictRows, movedBy>>

Revs   == 1 .. MaxRev
Actors == Members \cup {Lead, Origin, NoOne}
Seed   == 1                       \* Artifacts.on_assemble's seeded revision

Created(r)    == author[r] # NoOne
IsProposal(r) == author[r] \in Members
IsMerge(r)    == mergedFrom[r] # 0

\* Every revision that has ever been main's head, or was folded into one.
Landed(r) == \/ r = Seed
             \/ r \in ffLanded
             \/ r \in mergeLanded
             \/ (Created(r) /\ IsMerge(r))

\* The lead's inbox, exactly as ArtifactStore.proposals computes it: the head of a
\* branch main has not already absorbed.
Pending(r) == /\ IsProposal(r)
              /\ branchHead[author[r]] = r
              /\ ~Landed(r)

\* A conflict row still open -- `_resolve` closes it on commit or merge.
OpenConflict(r) == /\ r \in conflictRows
                   /\ r \notin ffLanded
                   /\ r \notin mergeLanded

\* The author proposed again later, which moved its branch ref off r. The revision
\* itself stays in `revisions` forever; the author is the party that replaced it.
Superseded(r) == \E s \in Revs : /\ IsProposal(s)
                                /\ author[s] = author[r]
                                /\ s > r

\* ArtifactStore._reachable: both parent edges, walked from main's head. MaxRev
\* iterations suffice because a revision's parents always have smaller ids.
Parents(r) == ({parent[r]} \cup {mergedFrom[r]}) \ {0}
Ancestry ==
    LET R[n \in 0..MaxRev] ==
          IF n = 0
            THEN {mainHead} \ {0}
            ELSE R[n-1] \cup UNION { Parents(x) : x \in R[n-1] }
    IN R[MaxRev]

TypeOK ==
    /\ author \in [Revs -> Actors]
    /\ parent \in [Revs -> 0 .. MaxRev]
    /\ mergedFrom \in [Revs -> 0 .. MaxRev]
    /\ branchHead \in [Members -> 0 .. MaxRev]
    /\ overlapping \subseteq Revs
    /\ nextRev \in 1 .. (MaxRev + 1)
    /\ mainHead \in 0 .. MaxRev
    /\ ffLanded \subseteq Revs
    /\ mergeLanded \subseteq Revs
    /\ attempted \subseteq Revs
    /\ conflictRows \subseteq Revs
    /\ movedBy \subseteq Actors
    \* A revision id is handed out once and never reused.
    /\ \A r \in Revs : Created(r) <=> r < nextRev

Init ==
    /\ author = [r \in Revs |-> IF r = Seed THEN Origin ELSE NoOne]
    /\ parent = [r \in Revs |-> 0]
    /\ mergedFrom = [r \in Revs |-> 0]
    /\ branchHead = [m \in Members |-> 0]
    /\ overlapping = {}
    /\ nextRev = Seed + 1
    /\ mainHead = Seed
    /\ ffLanded = {}
    /\ mergeLanded = {}
    /\ attempted = {}
    /\ conflictRows = {}
    /\ movedBy = {Origin}

-----------------------------------------------------------------------------
\*                                Actions                                   *
-----------------------------------------------------------------------------

\* `propose_change`. The author is bound by the wire (hooks/artifacts.py), the
\* branch is the member's own, and the parent is main's head *at this moment* --
\* which is what makes the fast-forward rule in commit meaningful.
Propose(m) ==
    /\ nextRev <= MaxRev
    /\ author' = [author EXCEPT ![nextRev] = m]
    /\ parent' = [parent EXCEPT ![nextRev] = mainHead]
    /\ branchHead' = [branchHead EXCEPT ![m] = nextRev]
    \* The nondeterministic overlap flag: whether this proposal's edits claim base
    \* lines that anything landing on main since its parent also claims.
    /\ \/ overlapping' = overlapping \cup {nextRev}
       \/ overlapping' = overlapping
    /\ nextRev' = nextRev + 1
    /\ UNCHANGED <<mergedFrom, mainHead, ffLanded, mergeLanded, attempted,
                   conflictRows, movedBy>>

\* `commit_change` on a proposal written against the head it replaces. Main moves.
CommitFF(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] = mainHead
    /\ mainHead' = r
    /\ ffLanded' = ffLanded \cup {r}
    /\ attempted' = attempted \cup {r}
    /\ movedBy' = movedBy \cup {Lead}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mergeLanded, conflictRows>>

\* `commit_change` on a stale proposal: a sibling landed first. Main does NOT move,
\* and the collision becomes a row -- "a conflict the plane did not write down is a
\* lost write wearing a verdict".
CommitConflict(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ attempted' = attempted \cup {r}
    /\ conflictRows' = conflictRows \cup {r}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mainHead, ffLanded, mergeLanded, movedBy>>

\* `merge_change` whose three-way merge came back clean. A NEW revision lands on
\* main, authored by the lead (the merged text is a document neither side wrote),
\* carrying the proposal as its second parent.
MergeClean(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead          \* a fast-forward is committed, not merged
    /\ r \notin overlapping
    /\ nextRev <= MaxRev
    /\ author' = [author EXCEPT ![nextRev] = Lead]
    /\ parent' = [parent EXCEPT ![nextRev] = mainHead]
    /\ mergedFrom' = [mergedFrom EXCEPT ![nextRev] = r]
    /\ mainHead' = nextRev
    /\ nextRev' = nextRev + 1
    /\ mergeLanded' = mergeLanded \cup {r}
    /\ attempted' = attempted \cup {r}
    /\ movedBy' = movedBy \cup {Lead}
    /\ UNCHANGED <<branchHead, overlapping, ffLanded, conflictRows>>

\* `merge_change` whose sides claim the same base lines. Nothing lands, ever: the
\* author whose edit disappeared is the one who knew why it was there.
MergeOverlap(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ r \in overlapping
    /\ attempted' = attempted \cup {r}
    /\ conflictRows' = conflictRows \cup {r}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mainHead, ffLanded, mergeLanded, movedBy>>

Settled(r) == Landed(r) \/ r \in conflictRows

\* Revisions exhausted and every proposal accounted for. Stuttering here rather
\* than leaving the state successor-less is process/tla.py's own trick: a run that
\* legitimately finished must be able to stall, or a liveness check reads the end
\* of the run as a stuck process.
Stalled == /\ nextRev > MaxRev
           /\ \A r \in Revs : IsProposal(r) => Settled(r)
Done == Stalled /\ UNCHANGED vars

LeadStep(r) == CommitFF(r) \/ CommitConflict(r) \/ MergeClean(r) \/ MergeOverlap(r)

Next ==
    \/ \E m \in Members : Propose(m)
    \/ \E r \in Revs : LeadStep(r)
    \/ Done

Spec == Init /\ [][Next]_vars

\* Fairness on the lead, per proposal. Weak fairness is enough: a stale proposal
\* the lead never merges still has CommitConflict permanently enabled, and that
\* step changes state, so WF forces it.
SpecFair == Init /\ [][Next]_vars /\ \A r \in Revs : WF_vars(LeadStep(r))

-----------------------------------------------------------------------------
\*                              The properties                              *
-----------------------------------------------------------------------------

\* Nothing a member proposed silently vanishes. The load-bearing disjunct is the
\* first: a landed proposal stays reachable from main's head forever. A
\* last-writer-wins overwrite is exactly what takes it out of Ancestry, which is
\* what ArtifactPlane_Broken's "overwrite" defect does.
NoLostWrite ==
    \A r \in Revs :
        IsProposal(r) =>
            \/ r \in Ancestry        \* on main, through either parent edge
            \/ Pending(r)            \* its branch head, waiting on the lead
            \/ r \in conflictRows    \* a row in `conflicts`
            \/ Superseded(r)         \* its own author proposed again

\* Only the lead advances main. Members hold `propose_change` and nothing else;
\* the rejected alternative (every member commits) is what fires this.
SoleIntegrator == movedBy \subseteq {Origin, Lead}

\* Two halves of one claim. First: two proposals written against one parent never
\* both fast-forward, so the second is forced through conflict or merge. Second:
\* every landing attempt that did not land is written down.
ConflictNotOverwrite ==
    /\ \A r1, r2 \in Revs :
         (/\ r1 # r2
          /\ IsProposal(r1) /\ IsProposal(r2)
          /\ parent[r1] = parent[r2])
            => ~(r1 \in ffLanded /\ r2 \in ffLanded)
    /\ \A r \in attempted : ~Landed(r) => r \in conflictRows

\* Overlap yields a conflict, never a landed merge.
OverlapNeverMerges == overlapping \cap mergeLanded = {}

\* Authorship is immutable: an action property, because a single state cannot
\* witness a change. The hook binds the author to the member's name on the wire;
\* nothing downstream may rewrite it.
AuthorBound ==
    [][ \A r \in Revs : Created(r) => author'[r] = author[r] ]_vars

\* Every pending proposal is eventually committed, merged, or conflicted.
ProposalsSettle == \A r \in Revs : [](IsProposal(r) => <>Settled(r))

-----------------------------------------------------------------------------
\*                          Vacuity witnesses                               *
-----------------------------------------------------------------------------
(* Each of these is asserted as an INVARIANT that we EXPECT TLC to violate. The *)
(* counterexample trace is the witness state proving the corresponding property *)
(* is not passing because its antecedent was never reached. A green run here    *)
(* would mean the model never reaches the interesting configuration.            *)

\* ConflictNotOverwrite's first clause quantifies over sibling proposals off one
\* parent. If no two proposals ever share a parent, that clause is vacuous.
NoSiblingProposals ==
    ~ \E r1, r2 \in Revs :
        /\ r1 # r2
        /\ IsProposal(r1) /\ IsProposal(r2)
        /\ parent[r1] = parent[r2]
        /\ r1 \in attempted /\ r2 \in attempted

\* NoLostWrite's Ancestry disjunct is only load-bearing if a merge revision ever
\* lands, putting a proposal on main through `mergedFrom` rather than `parent`.
NoMergeEverLands == mergeLanded = {}

\* OverlapNeverMerges is vacuous unless an overlapping proposal is actually
\* offered to the lead and refused.
NoOverlapEverRefused == overlapping \cap conflictRows = {}

=============================================================================
