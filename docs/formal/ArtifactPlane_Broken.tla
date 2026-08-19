------------------------- MODULE ArtifactPlane_Broken -------------------------
(***************************************************************************)
(* ArtifactPlane with the rejected designs wired back in, so every          *)
(* invariant is shown to be capable of failing. The guards-must-fire rule   *)
(* applied to specs: an invariant nobody ever watched break is an invariant *)
(* nobody has evidence is checking anything.                                *)
(*                                                                         *)
(* A copy rather than an EXTENDS, because the defect is in `Next` and TLA+  *)
(* has no override -- an extending module can add actions but cannot remove *)
(* the guard that makes the original safe.                                  *)
(*                                                                         *)
(* Four defects, each behind its own boolean constant so a violation is     *)
(* attributable to one cause. Each of the four .cfg files in this directory *)
(* turns exactly ONE on and names exactly the ONE invariant it should       *)
(* break:                                                                  *)
(*                                                                         *)
(*   MemberCommits  the rejected "every member commits to main" design      *)
(*                  (docs/design/artifacts.md)      breaks SoleIntegrator   *)
(*   ForceCommit    a stale proposal overwrites main's head, no conflict    *)
(*                  row -- last-writer-wins        breaks NoLostWrite       *)
(*   AutoMerge      the rejected "auto-merge everything" design: overlap    *)
(*                  resolved by rule           breaks OverlapNeverMerges    *)
(*   RewriteAuthor  authorship taken from the model instead of bound by the *)
(*                  wire                            breaks AuthorBound     *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
    Members, Lead, Origin, NoOne, MaxRev,
    MemberCommits, ForceCommit, AutoMerge, RewriteAuthor

VARIABLES
    author, parent, mergedFrom, branchHead, overlapping, nextRev,
    mainHead, ffLanded, mergeLanded, attempted, conflictRows, movedBy

vars == <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
          mainHead, ffLanded, mergeLanded, attempted, conflictRows, movedBy>>

Revs   == 1 .. MaxRev
Actors == Members \cup {Lead, Origin, NoOne}
Seed   == 1

Created(r)    == author[r] # NoOne
IsProposal(r) == author[r] \in Members
IsMerge(r)    == mergedFrom[r] # 0

Landed(r) == \/ r = Seed
             \/ r \in ffLanded
             \/ r \in mergeLanded
             \/ (Created(r) /\ IsMerge(r))

Pending(r) == /\ IsProposal(r)
              /\ branchHead[author[r]] = r
              /\ ~Landed(r)

Superseded(r) == \E s \in Revs : /\ IsProposal(s)
                                /\ author[s] = author[r]
                                /\ s > r

Parents(r) == ({parent[r]} \cup {mergedFrom[r]}) \ {0}
Ancestry ==
    LET R[n \in 0..MaxRev] ==
          IF n = 0
            THEN {mainHead} \ {0}
            ELSE R[n-1] \cup UNION { Parents(x) : x \in R[n-1] }
    IN R[MaxRev]

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
\*                      The correct actions, verbatim                       *
-----------------------------------------------------------------------------

Propose(m) ==
    /\ nextRev <= MaxRev
    /\ author' = [author EXCEPT ![nextRev] = m]
    /\ parent' = [parent EXCEPT ![nextRev] = mainHead]
    /\ branchHead' = [branchHead EXCEPT ![m] = nextRev]
    /\ \/ overlapping' = overlapping \cup {nextRev}
       \/ overlapping' = overlapping
    /\ nextRev' = nextRev + 1
    /\ UNCHANGED <<mergedFrom, mainHead, ffLanded, mergeLanded, attempted,
                   conflictRows, movedBy>>

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

CommitConflict(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ attempted' = attempted \cup {r}
    /\ conflictRows' = conflictRows \cup {r}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mainHead, ffLanded, mergeLanded, movedBy>>

MergeClean(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
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

MergeOverlap(r) ==
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ r \in overlapping
    /\ attempted' = attempted \cup {r}
    /\ conflictRows' = conflictRows \cup {r}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mainHead, ffLanded, mergeLanded, movedBy>>

-----------------------------------------------------------------------------
\*                              The defects                                 *
-----------------------------------------------------------------------------

\* DEFECT 1 -- the rejected design: a member holds a commit tool and fast-forwards
\* main itself. Identical to CommitFF except who is recorded as having moved main.
\* The plane then has no integration point and nowhere for a conflict to be seen.
MemberCommit(r) ==
    /\ MemberCommits
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] = mainHead
    /\ mainHead' = r
    /\ ffLanded' = ffLanded \cup {r}
    /\ attempted' = attempted \cup {r}
    /\ movedBy' = movedBy \cup {author[r]}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mergeLanded, conflictRows>>

\* DEFECT 2 -- last-writer-wins: main is set to a proposal written against a head
\* that has since moved, and no conflict row is written. The head it displaced is
\* now reachable from nothing, which is the lost write in exact form.
ForcePush(r) ==
    /\ ForceCommit
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ mainHead' = r
    /\ ffLanded' = ffLanded \cup {r}
    /\ attempted' = attempted \cup {r}
    /\ movedBy' = movedBy \cup {Lead}
    /\ UNCHANGED <<author, parent, mergedFrom, branchHead, overlapping, nextRev,
                   mergeLanded, conflictRows>>

\* DEFECT 3 -- the rejected design: auto-merge everything. MergeClean with the
\* overlap guard deleted, so an overlap is resolved by rule and one author's edit
\* is deleted by a coin toss.
AutoMergeOverlap(r) ==
    /\ AutoMerge
    /\ IsProposal(r)
    /\ ~Landed(r)
    /\ parent[r] # mainHead
    /\ r \in overlapping
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

\* DEFECT 4 -- authorship reported by the model rather than bound by the wire: an
\* existing revision is reassigned to another actor. The lead then goes to the wrong
\* agent to resolve a collision.
Reattribute(r, a) ==
    /\ RewriteAuthor
    /\ Created(r)
    /\ a # author[r]
    /\ a # NoOne
    /\ author' = [author EXCEPT ![r] = a]
    /\ UNCHANGED <<parent, mergedFrom, branchHead, overlapping, nextRev, mainHead,
                   ffLanded, mergeLanded, attempted, conflictRows, movedBy>>

-----------------------------------------------------------------------------

Settled(r) == Landed(r) \/ r \in conflictRows
Stalled == /\ nextRev > MaxRev
           /\ \A r \in Revs : IsProposal(r) => Settled(r)
Done == Stalled /\ UNCHANGED vars

Next ==
    \/ \E m \in Members : Propose(m)
    \/ \E r \in Revs : \/ CommitFF(r) \/ CommitConflict(r)
                       \/ MergeClean(r) \/ MergeOverlap(r)
                       \/ MemberCommit(r) \/ ForcePush(r) \/ AutoMergeOverlap(r)
    \/ \E r \in Revs, a \in Actors : Reattribute(r, a)
    \/ Done

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
\*        The properties, verbatim from ArtifactPlane -- these must FAIL    *
-----------------------------------------------------------------------------

NoLostWrite ==
    \A r \in Revs :
        IsProposal(r) =>
            \/ r \in Ancestry
            \/ Pending(r)
            \/ r \in conflictRows
            \/ Superseded(r)

SoleIntegrator == movedBy \subseteq {Origin, Lead}

ConflictNotOverwrite ==
    /\ \A r1, r2 \in Revs :
         (/\ r1 # r2
          /\ IsProposal(r1) /\ IsProposal(r2)
          /\ parent[r1] = parent[r2])
            => ~(r1 \in ffLanded /\ r2 \in ffLanded)
    /\ \A r \in attempted : ~Landed(r) => r \in conflictRows

OverlapNeverMerges == overlapping \cap mergeLanded = {}

AuthorBound ==
    [][ \A r \in Revs : Created(r) => author'[r] = author[r] ]_vars

=============================================================================
