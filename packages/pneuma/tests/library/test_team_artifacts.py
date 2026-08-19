"""Offline tests for the artifact plane: the store, the `Artifacts` hook, and `split_brain`.

Two disciplines, both inherited rather than invented here.

**Tool-surface and delivery claims are asserted from the wire** — `Counting` records what
each model call was offered and what tool results it carried, because "the member got a
propose tool" and "the lead read the conflict" are claims about the model's own context, and
a `hooks_data` entry saying a proposal landed is exactly what a mis-wired hook would also
record (the render_brief precedent, `test_team_worklog.py`'s header).

**Every durable claim is re-read from the store**, and the file-backed case is read back
through a *second* `ArtifactStore` on the same path, so "persisted" means a different object
found it rather than the first one remembering it.

`Counting` composes `ScriptedModel` (which is `@final`); output types are module level
because `compile_ai_method` resolves annotations against module globals (`method.py:146`).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import ArtifactError, ArtifactStore, Conflict, Member, Team, split_brain
from pneuma.team.artifacts import MAIN, three_way_merge
from pneuma.team.hooks import Artifacts, Negotiation

if TYPE_CHECKING:
    from collections.abc import AsyncIterable
    from pathlib import Path

# ── Output types, module level for get_type_hints ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows, or the review verdict")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="What it relies on")


# ── The cast ──


class Analyst(MethodAgent):
    def __init__(self, source: str) -> None:
        self.name = f"{source}-analyst"
        self.source = source

    @ai_method(Reading, description="Read one source and report what it alone shows")
    def read(self, focus: str, depth: int = 2) -> Reading:
        """Read the {self.source} source with {focus} in mind, to depth {depth}."""


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class Counting(Model):
    """Records contexts, tool offers and tool results — the whole wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []
        self.offered_tools: list[list[str]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(
        self, messages: Any, tool_specs: Any = None, *args: Any, **kwargs: Any
    ) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        self.offered_tools.append([spec["name"] for spec in tool_specs or []])
        return self._inner.stream(messages, tool_specs, *args, **kwargs)

    def tool_results(self, call: int) -> list[str]:
        return [
            inner["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "toolResult" in block
            for inner in block["toolResult"].get("content", [])
            if "text" in inner
        ]

    def all_tool_results(self) -> str:
        return "\n".join(
            text for call in range(len(self.contexts)) for text in self.tool_results(call)
        )


def reading(detail: str, *, source: str = "left") -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": detail}),))


def ruling(*cites: str, admitted: bool = True) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def proposing(path: str, content: str, rationale: str, decides: str = "") -> Turn:
    return Turn(
        tool_calls=(
            (
                "propose_change",
                {
                    "path": path,
                    "new_content": content,
                    "rationale": rationale,
                    "decides": decides,
                },
            ),
        )
    )


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


APPROVING = Negotiation.APPROVAL

# The seed document. Five distinct lines, so one edit at the top and one at the bottom are
# genuinely non-overlapping while two edits to line three genuinely collide.
BASE = "the store is undecided\nsecond paragraph\nthird paragraph\nfourth paragraph\nthe end\n"


def seeded(**kwargs: Any) -> Artifacts:
    """An `Artifacts` hook over a fresh in-memory store carrying `BASE` at `design.md`."""
    return Artifacts(seed={"design.md": BASE}, **kwargs)


def edited(old: str, new: str, *, base: str = BASE) -> str:
    return base.replace(old, new)


# ── 1. The store alone: proposal, fast-forward, and bound attribution ──


def test_a_proposal_lands_on_the_authors_own_branch_and_does_not_move_main() -> None:
    """The staging property the whole design rests on: a proposal is durable and invisible.

    Asserted both ways — the revision is readable on its own branch, and `main` still reads
    the seeded text — because a plane where proposing moved `main` would pass any test that
    only checked the proposal exists.
    """
    store = ArtifactStore()
    seed = store.propose(
        "design.md", BASE, author="origin", rationale="as received", branch="origin-seed"
    )
    store.commit(seed.revision_id)

    proposal = store.propose(
        "design.md", edited("undecided", "sqlite"), author="left-analyst.read", rationale="sqlite"
    )
    assert proposal.branch == "left-analyst.read", "the branch defaults to the author"
    assert proposal.author == "left-analyst.read"
    assert proposal.parent_revision == seed.revision_id, "parented at main's head, not at nothing"
    assert store.read("design.md") == BASE, "main has not moved; the proposal is staged"
    assert store.read("design.md", proposal.branch) == proposal.content

    landed = store.commit(proposal.revision_id)
    assert not isinstance(landed, Conflict)
    assert store.read("design.md") == proposal.content, "commit fast-forwards main"
    assert store.head("design.md").revision_id == proposal.revision_id


def test_a_proposal_may_not_be_written_straight_onto_main_and_needs_a_rationale() -> None:
    """The two refusals that keep commit authority real and proposals weighable."""
    store = ArtifactStore()
    with pytest.raises(ArtifactError, match="may not propose directly onto"):
        store.propose("design.md", BASE, author="left", rationale="why", branch=MAIN)
    with pytest.raises(ArtifactError, match="needs a rationale"):
        store.propose("design.md", BASE, author="left", rationale="   ")


def test_reading_an_unknown_path_raises_rather_than_answering_an_empty_document() -> None:
    """A mistyped path answered `""` would make the member propose a whole new file over the
    one it meant to edit, and the plane could not tell that from a genuine new artifact."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    with pytest.raises(ArtifactError, match="no artifact at 'desing.md'"):
        store.read("desing.md")
    assert store.read("design.md") == BASE


def test_the_same_change_proposed_twice_is_one_revision_not_two() -> None:
    """Content-addressed: the id is a digest over content and lineage, so an idempotent
    retry (a model repeating a tool call) does not double the plane's history."""
    store = ArtifactStore()
    first = store.propose("design.md", BASE, author="left", rationale="same reason")
    again = store.propose("design.md", BASE, author="left", rationale="same reason")
    assert again.revision_id == first.revision_id
    assert len(store.revisions("design.md")) == 1, "one row, not two ids for one document"
    other = store.propose("design.md", BASE, author="left", rationale="a different reason")
    assert other.revision_id != first.revision_id, "a different reason is a different revision"


# ── 2. The sibling collision: Conflict, never an overwrite ──


def test_a_sibling_that_committed_first_turns_the_second_commit_into_a_conflict() -> None:
    """The load-bearing claim of Phase 1, asserted as an absence *and* a presence: the
    second commit returns a `Conflict`, and — the part an overwriting plane would fail —
    `main` still carries the first sibling's text afterwards."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)

    alice = store.propose(
        "design.md", edited("the store is undecided", "ALICE-TOP"), author="alice", rationale="top"
    )
    bob = store.propose(
        "design.md", edited("the end", "BOB-BOTTOM"), author="bob", rationale="bottom"
    )
    assert not isinstance(store.commit(alice.revision_id), Conflict), "alice fast-forwards"

    conflict = store.commit(bob.revision_id)
    assert isinstance(conflict, Conflict), "bob's proposal is no longer a fast-forward"
    assert conflict.head == alice.revision_id and conflict.head_author == "alice"
    assert conflict.ancestor == seed.revision_id, "the common ancestor both were written on"
    assert conflict.mergeable is True, "different lines, so a merge is available"
    assert "ALICE-TOP" in store.read("design.md"), "main still carries alice's change"
    assert "BOB-BOTTOM" not in store.read("design.md"), "…and bob's did NOT overwrite it"
    rendered = str(conflict)
    assert "not a fast-forward" in rendered and "merge_change" in rendered
    assert "ALICE-TOP" in rendered and "BOB-BOTTOM" in rendered, "the lead sees both sides"


def test_merge_change_lands_non_overlapping_edits_and_keeps_both() -> None:
    """The clean merge: one new revision on `main` whose text carries both authors' lines,
    authored by the merger, with the proposal preserved as `merged_from`."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    alice = store.propose(
        "design.md", edited("the store is undecided", "ALICE-TOP"), author="alice", rationale="top"
    )
    bob = store.propose(
        "design.md", edited("the end", "BOB-BOTTOM"), author="bob", rationale="bottom"
    )
    store.commit(alice.revision_id)

    merged = store.merge(bob.revision_id, author="chair")
    assert not isinstance(merged, Conflict), f"a line-disjoint merge must land: {merged}"
    assert merged.branch == MAIN and merged.author == "chair", "the merger authors the merge"
    assert merged.merged_from == bob.revision_id, "the proposal stays on the record"
    assert merged.parent_revision == alice.revision_id
    content = store.read("design.md")
    assert "ALICE-TOP" in content and "BOB-BOTTOM" in content, "neither edit was lost"
    assert content.count("\n") == BASE.count("\n"), "and no line was duplicated"


def test_overlapping_edits_refuse_to_merge_and_name_the_lines_they_collide_on() -> None:
    """The refusal that makes the plane trustworthy: two rewrites of one line never merge.

    Asserted with the merge *attempted*, not merely with a conflict predicted, and the
    document re-read afterwards — a plane that resolved this by rule would still return
    something plausible, and only the re-read catches which author it deleted.
    """
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    carol = store.propose(
        "design.md", edited("third paragraph", "CAROL-SAYS"), author="carol", rationale="mine"
    )
    dave = store.propose(
        "design.md", edited("third paragraph", "DAVE-SAYS"), author="dave", rationale="no, mine"
    )
    store.commit(carol.revision_id)

    refused = store.merge(dave.revision_id, author="chair")
    assert isinstance(refused, Conflict), "an overlapping merge must refuse"
    assert refused.mergeable is False
    assert refused.overlapping, "and name where it collides"
    assert "line 3" in refused.overlapping[0], refused.overlapping
    content = store.read("design.md")
    assert "CAROL-SAYS" in content and "DAVE-SAYS" not in content, (
        "neither side was silently overwritten — main is exactly what carol committed"
    )
    rendered = str(refused)
    assert "the SAME lines" in rendered and "which change the team keeps" in rendered, (
        "a conflict with no next move is what makes an agent abandon the work"
    )


def test_two_insertions_at_one_point_are_a_conflict_rather_than_an_arbitrary_order() -> None:
    """A pure insertion covers zero base lines, so a naive interval check reads two
    insertions at one point as disjoint and lands them in whichever order the sort produced.
    `_Hunk.span` widens an insertion by one line precisely to make this collide."""
    base = "alpha\nomega\n"
    merged, overlapping = three_way_merge(
        base, "alpha\nFROM-MAIN\nomega\n", "alpha\nFROM-PROPOSAL\nomega\n"
    )
    assert merged is None, "two insertions at one point must not be silently ordered"
    assert overlapping and "line 2" in overlapping[0]


def test_the_identical_edit_from_both_sides_is_agreement_not_a_collision() -> None:
    """The one overlap that is waved through, because there is nothing to choose between:
    both members made the same edit, and a conflict here would be unactionable."""
    base = "alpha\nbeta\n"
    merged, overlapping = three_way_merge(base, "alpha\nAGREED\n", "alpha\nAGREED\n")
    assert overlapping == () and merged == "alpha\nAGREED\n"


def test_every_conflict_is_a_row_and_landing_the_proposal_closes_it() -> None:
    """Conflicts are first-class rows: a conflict reported and not written down is, one run
    later, indistinguishable from a change nobody proposed. The resolution field is what
    keeps a resolved collision from reading as an open one."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    alice = store.propose(
        "design.md", edited("the store is undecided", "ALICE-TOP"), author="alice", rationale="top"
    )
    bob = store.propose(
        "design.md", edited("the end", "BOB-BOTTOM"), author="bob", rationale="bottom"
    )
    store.commit(alice.revision_id)
    conflict = store.commit(bob.revision_id)
    assert isinstance(conflict, Conflict)

    (row,) = store.conflicts("design.md")
    assert row["conflict_id"] == conflict.conflict_id
    assert row["proposal_revision"] == bob.revision_id
    assert row["head_revision"] == alice.revision_id
    assert row["mergeable"] is True
    assert row["resolution"] is None, "open while nothing has landed it"

    store.merge(bob.revision_id, author="chair")
    (row,) = store.conflicts("design.md")
    assert row["resolution"] == "merged", "a resolved collision stops reading as open"


# ── 3. The hook on a live team: bound author, sole commit authority, wire-level claims ──


async def test_without_the_hook_no_artifact_tool_reaches_any_model_and_the_run_has_no_key() -> None:
    """The off state is the hook's absence, asserted from the wire: `offered_tools` is what
    the models were actually shown."""
    async with RuntimeHarness() as h:
        member_model = Counting([reading(APPROVING)])
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("done")])
        run = await Team(lead, members, hooks=[Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    artifact_tools = {"read_artifact", "propose_change", "commit_change", "merge_change"}
    assert all(not artifact_tools & set(offer) for offer in member_model.offered_tools)
    assert all(not artifact_tools & set(offer) for offer in lead_model.offered_tools)
    assert "artifacts" not in run.hooks_data


async def test_members_get_read_and_propose_while_only_the_lead_gets_commit() -> None:
    """The authority split, asserted where it is enforced — on the wire. A member offered
    `commit_change` could land its own change whatever the store's rules said."""
    async with RuntimeHarness() as h:
        member_model = Counting([reading(APPROVING)])
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, lead_model = scripted_lead([ruling("done")])
        await Team(lead, members, hooks=[seeded(), Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    member_offer = set(member_model.offered_tools[0])
    assert {"read_artifact", "propose_change"} <= member_offer
    assert "commit_change" not in member_offer and "merge_change" not in member_offer, (
        "a member that could commit would make the lead's authority advisory"
    )
    lead_offer = set(lead_model.offered_tools[0])
    assert {"read_artifact", "list_proposals", "commit_change", "merge_change"} <= lead_offer
    assert "propose_change" not in lead_offer, "the lead integrates; it does not propose"


async def test_a_members_proposal_carries_the_author_the_team_wired_not_one_the_model_chose() -> (
    None
):
    """Attribution the model cannot spoof: `propose_change` takes no author parameter, so
    the only name that can appear is the one the hook bound. Asserted from the store as well
    as from the report, and the tool's own parameter set is asserted from the wire."""
    async with RuntimeHarness() as h:
        hook = seeded()
        member_model = Counting(
            [
                proposing("design.md", edited("undecided", "SQLITE"), "sqlite is enough"),
                reading(APPROVING),
            ]
        )
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead, _ = scripted_lead([ruling("done")])
        run = await Team(lead, members, hooks=[hook, Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    (proposal,) = [e for e in run.hooks_data["artifacts"] if e["action"] == "propose"]
    assert proposal["author"] == "left-analyst.read" == proposal["branch"]
    (revision,) = [r for r in hook.store.revisions("design.md") if r.branch != "origin-seed"]
    assert revision.author == "left-analyst.read", "the store agrees with the report"
    assert "SQLITE" in revision.content
    assert hook.store.read("design.md") == BASE, "and main did not move on a proposal"
    result = member_model.all_tool_results()
    assert "proposed" in result and revision.short in result, (
        "the member reads back the id its lead will need"
    )


async def test_the_leads_inbox_shows_the_pending_proposal_with_its_id_and_its_author() -> None:
    """The lead's own wire, end to end: a member proposes, and the id plus the attribution
    the lead needs to land it arrive in the LEAD's model context through `list_proposals`.

    `Briefing` is what orders this — the member's cycle runs at the barrier, before the lead
    has one, which is also the ordinary shape (a member proposes from what it alone knows and
    the lead integrates afterwards). Without it the lead lists first and correctly reports an
    empty inbox, which is how this test first failed. The commit itself is driven through the
    store rather than through the lead's tool because a scripted turn cannot carry a revision
    id the member will only mint at runtime; the tool's own commit path is asserted in the
    conflict and error-text tests.
    """
    from pneuma.team.hooks import Briefing

    async with RuntimeHarness() as h:
        hook = seeded()
        proposed = edited("the store is undecided", "the store is SQLITE")
        member_model = Counting(
            [proposing("design.md", proposed, "sqlite is enough"), reading("briefed")]
        )
        members = [Member(Analyst("left"), "read", model=member_model)]
        lead_model = Counting(
            [
                Turn(tool_calls=(("list_proposals", {"path": "design.md"}),)),
                Turn(tool_calls=(("read_artifact", {"path": "design.md"}),)),
                ruling("committed"),
            ]
        )
        lead = Chair().compiled("decide", model=lead_model)
        team = Team(lead, members, hooks=[hook, Briefing()])
        run = await team.run("go", h.worker.coordinator)

    listing = lead_model.all_tool_results()
    (proposal,) = [e for e in run.hooks_data["artifacts"] if e["action"] == "propose"]
    assert proposal["revision"][:12] in listing, "the id the lead must type back was shown"
    assert "left-analyst.read" in listing, "attributed to the member, in the lead's own context"
    assert "sqlite is enough" in listing, "with the rationale the lead has to weigh"
    assert "the store is undecided" in listing, (
        "and read_artifact still showed the AGREED version — a proposal is not the document"
    )

    landed = hook.store.commit(proposal["revision"])
    assert not isinstance(landed, Conflict)
    assert "SQLITE" in hook.store.read("design.md")
    assert hook.store.proposals("design.md") == [], "a landed proposal leaves the inbox"
    assert run.answer.admitted is True


async def test_the_lead_reads_the_whole_collision_and_nothing_is_overwritten() -> None:
    """Phase 1 through the hook: two members propose against one head, the lead commits one
    and then tries to commit the other. The conflict text lands in the LEAD'S OWN context —
    the delivery claim a `hooks_data` conflict entry would satisfy without a wire."""
    async with RuntimeHarness() as h:
        hook = seeded()
        left_model = Counting(
            [
                proposing("design.md", edited("the store is undecided", "LEFT-TOP"), "top"),
                reading(APPROVING),
            ]
        )
        right_model = Counting(
            [
                proposing("design.md", edited("the end", "RIGHT-BOTTOM"), "bottom"),
                reading(APPROVING),
            ]
        )
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead_model = Counting([ruling("noted"), ruling("noted")])
        lead = Chair().compiled("decide", model=lead_model)
        team = Team(lead, members, hooks=[hook, Negotiation(rounds=1)])
        run = await team.run("go", h.worker.coordinator)

    proposals = [e for e in run.hooks_data["artifacts"] if e["action"] == "propose"]
    assert len(proposals) == 2, "both members proposed against the same head"
    assert {e["author"] for e in proposals} == {"left-analyst.read", "right-analyst.read"}
    assert len({e["parent"] for e in proposals}) == 1, "…written against ONE version"

    first, second = proposals
    assert not isinstance(hook.store.commit(first["revision"]), Conflict)
    conflict = hook.store.commit(second["revision"])
    assert isinstance(conflict, Conflict) and conflict.mergeable
    kept = hook.store.read("design.md")
    assert first["digest"] == hook.store.head("design.md").digest, "main is the first proposal"
    merged = hook.store.merge(second["revision"], author="chair")
    assert not isinstance(merged, Conflict)
    both = hook.store.read("design.md")
    assert "LEFT-TOP" in both and "RIGHT-BOTTOM" in both, "the merge kept both edits"
    assert kept != both, "precondition: the merge really changed main"


async def test_a_stale_revision_id_and_a_wrong_path_come_back_as_text_the_model_can_fix() -> None:
    """Failures are text (`hiring.py`'s rule), asserted through the tool boundary: the
    refusal rides back as a SUCCESSFUL tool result the model reads, so the lead can retry."""
    async with RuntimeHarness() as h:
        hook = seeded()
        lead_model = Counting(
            [
                Turn(
                    tool_calls=(
                        ("commit_change", {"path": "design.md", "revision_id": "deadbeef"}),
                    )
                ),
                Turn(tool_calls=(("read_artifact", {"path": "desing.md"}),)),
                ruling("recovered"),
            ]
        )
        lead = Chair().compiled("decide", model=lead_model)
        run = await Team(lead, [], hooks=[hook]).run("go", h.worker.coordinator)

    results = lead_model.all_tool_results()
    assert "error: no revision 'deadbeef'" in results, "an unknown revision is advice, not a fault"
    assert "error: no artifact at 'desing.md'" in results
    assert "design.md" in results, "the refusal names what does exist"
    assert run.answer.admitted is True, "and the run completed — nothing raised mid-cycle"


# ── 4. Split-brain: all three verdicts ──


def test_split_brain_confirms_a_divergence_when_two_branches_decide_one_question_apart() -> None:
    """The finding: one question, two branches, two different documents."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    store.propose(
        "design.md",
        edited("the store is undecided", "the store is SQLITE"),
        author="alice",
        rationale="stdlib is enough",
        decides="which store backs the plane",
    )
    store.propose(
        "design.md",
        edited("the store is undecided", "the store is POSTGRES"),
        author="bob",
        rationale="we will need concurrency",
        decides="Which store backs the plane?",
    )

    verdict = split_brain(store)
    assert verdict.diverged is True, str(verdict)
    assert verdict.settled is True
    ((path, question, branches),) = verdict.divergences
    assert path == "design.md"
    assert set(branches) == {"alice", "bob"}, "both branches travel with the finding"
    assert len(set(branches.values())) == 2, "…and their digests differ, which IS the finding"
    assert question == "which store backs the plane", "normalised: case and spacing are not a fork"
    assert verdict.contested == 1 and verdict.questions == 1 and verdict.decisions == 2
    assert "DIVERGENCE CONFIRMED" in str(verdict)


def test_split_brain_observes_no_divergence_when_the_branches_agree_or_never_met() -> None:
    """The other finding, and it is a finding rather than an abstention: the questions were
    examined. Two shapes in one test because they must not be confused — a question only one
    branch decided (`contested == 0`), and one two branches decided identically."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    store.propose(
        "design.md",
        edited("second paragraph", "ALONE"),
        author="alice",
        rationale="only I care",
        decides="who owns paragraph two",
    )
    solo = split_brain(store)
    assert solo.diverged is False and solo.contested == 0
    assert "never in a position to diverge" in str(solo)

    agreed = edited("third paragraph", "WE-BOTH-SAY-THIS")
    store.propose(
        "design.md",
        agreed,
        author="carol",
        rationale="obvious",
        decides="what paragraph three says",
    )
    store.propose(
        "design.md",
        agreed,
        author="dave",
        rationale="also obvious",
        decides="what paragraph three says",
    )
    both = split_brain(store)
    assert both.diverged is False, str(both)
    assert both.contested == 1, "the question WAS contested — and the two answers matched"
    assert "NONE OBSERVED" in str(both)


def test_split_brain_could_not_tell_when_nothing_recorded_what_it_decides() -> None:
    """The abstention, and the reason a boolean would be wrong: a plane full of proposals
    that never named a question is not a team that agreed."""
    store = ArtifactStore()
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    store.propose("design.md", edited("the end", "A"), author="alice", rationale="mine")
    store.propose("design.md", edited("the end", "B"), author="bob", rationale="no, mine")

    verdict = split_brain(store)
    assert verdict.diverged is None, "two rival texts and no declared question settles nothing"
    assert verdict.settled is False
    assert verdict.withheld and "no revision recorded what it decides" in verdict.withheld[0]
    assert "UNSETTLED" in str(verdict)


async def test_decides_rides_the_propose_tool_and_is_optional_on_the_wire() -> None:
    """The probe's input reaches the plane through the member's own tool, and a member that
    says nothing is not forced to invent a question — which is what keeps the third verdict
    meaningful rather than a state only a test can reach."""
    async with RuntimeHarness() as h:
        hook = seeded()
        left_model = Counting(
            [
                proposing(
                    "design.md",
                    edited("the store is undecided", "SQLITE"),
                    "stdlib is enough",
                    decides="which store backs the plane",
                ),
                reading(APPROVING),
            ]
        )
        right_model = Counting(
            [
                proposing(
                    "design.md",
                    edited("the store is undecided", "POSTGRES"),
                    "concurrency",
                    decides="which store backs the plane",
                ),
                reading(APPROVING),
            ]
        )
        members = [
            Member(Analyst("left"), "read", model=left_model),
            Member(Analyst("right"), "read", model=right_model),
        ]
        lead, _ = scripted_lead([ruling("noted"), ruling("noted")])
        run = await Team(lead, members, hooks=[hook, Negotiation(rounds=1)]).run(
            "go", h.worker.coordinator
        )

    decided = [e["decides"] for e in run.hooks_data["artifacts"] if e["action"] == "propose"]
    assert decided == ["which store backs the plane"] * 2
    verdict = split_brain(hook.store)
    assert verdict.diverged is True, str(verdict)
    assert set(verdict.divergences[0][2]) == {"left-analyst.read", "right-analyst.read"}


# ── 5. Lifetimes: the log resets, the file-backed store does not ──


async def test_a_second_run_reports_only_its_own_proposals_while_the_file_store_persists(
    tmp_path: Path,
) -> None:
    """The two lifetimes that must differ, in one test because confusing them is the defect.

    `hooks_data["artifacts"]` is per run — run 2's report carrying run 1's proposals would
    make a reader attribute work to the wrong run. The STORE is not: run 2 must read what
    run 1 landed, and its proposal must be parented at run 1's head rather than at nothing.
    Read back through a SECOND `ArtifactStore` on the same path, so "persisted" means a
    different object found it on disk.
    """
    async with RuntimeHarness() as h:
        db = tmp_path / "artifacts.db"
        store = ArtifactStore(db)
        hook = Artifacts(store, seed={"design.md": BASE}, run_id="run-1")

        first_model = Counting(
            [
                proposing("design.md", edited("the end", "RUN-ONE-EDIT"), "run one"),
                reading(APPROVING),
            ]
        )
        lead1, _ = scripted_lead([ruling("one")])
        first = await Team(
            lead1,
            [Member(Analyst("left"), "read", model=first_model)],
            hooks=[hook, Negotiation(rounds=1)],
        ).run("one", h.worker.coordinator)

        (proposed,) = [e for e in first.hooks_data["artifacts"] if e["action"] == "propose"]
        landed = store.commit(proposed["revision"])
        assert not isinstance(landed, Conflict), "run 1's change is the agreed version"

        hook.run_id = "run-2"
        second_model = Counting(
            [
                proposing(
                    "design.md",
                    edited("second paragraph", "RUN-TWO-EDIT", base=landed.content),
                    "run two",
                ),
                reading(APPROVING),
            ]
        )
        lead2, _ = scripted_lead([ruling("two")])
        second = await Team(
            lead2,
            [Member(Analyst("center"), "read", model=second_model)],
            hooks=[hook, Negotiation(rounds=1)],
        ).run("two", h.worker.coordinator)

    assert first.hooks_data["artifacts"] is not second.hooks_data["artifacts"]
    assert [e["action"] for e in second.hooks_data["artifacts"]] == ["propose"], (
        "run 2's report is run 2's: no inherited seed entry, no inherited proposal"
    )
    (run_two,) = [e for e in second.hooks_data["artifacts"] if e["action"] == "propose"]
    assert run_two["author"] == "center-analyst.read"
    assert run_two["parent"] == landed.revision_id, (
        "run 2 proposed against what run 1 landed — a store reset per run would parent this "
        "at nothing and make every commit a fast-forward over an empty document"
    )

    reopened = ArtifactStore(db)
    assert "RUN-ONE-EDIT" in reopened.read("design.md"), "a different object found it on disk"
    runs = {r.run_id for r in reopened.revisions("design.md")}
    assert runs == {"run-1", "run-2"}, f"both runs are attributable on the durable plane: {runs}"
    assert len(reopened.revisions("design.md")) == 3, "seed + run 1 + run 2"


async def test_seeding_does_not_overwrite_what_an_earlier_run_landed(tmp_path: Path) -> None:
    """One hook instance, two runs, one seed: the second assembly must not restore the
    original document over the change the first run agreed to."""
    async with RuntimeHarness() as h:
        db = tmp_path / "artifacts.db"
        store = ArtifactStore(db)
        hook = Artifacts(store, seed={"design.md": BASE})
        lead1, _ = scripted_lead([ruling("one")])
        await Team(lead1, [], hooks=[hook]).run("one", h.worker.coordinator)
        changed = store.propose("design.md", "REWRITTEN\n", author="alice", rationale="rewrite")
        store.commit(changed.revision_id)

        lead2, _ = scripted_lead([ruling("two")])
        run = await Team(lead2, [], hooks=[hook]).run("two", h.worker.coordinator)

    assert store.read("design.md") == "REWRITTEN\n", "the seed did not un-do run 1's commit"
    assert [e["action"] for e in run.hooks_data["artifacts"]] == [], "and re-seeded nothing"


# ── 6. Persistence failures raise ──


def corrupt(db: Path) -> None:
    """Make the database file unreadable as a database, the way a real fault arrives.

    Dropping a table was the first attempt and it did NOT fire: every operation runs
    `_init_schema` first, so `CREATE TABLE IF NOT EXISTS` politely recreated what the test
    had removed and the write succeeded against an empty table. Overwriting the file's bytes
    is a fault no schema init can repair — `sqlite3.DatabaseError: file is not a database` —
    which is the shape of the class this guard is about (a corrupt or full disk), and it is
    only reachable on the file-backed path, which is the one that has a disk.
    """
    db.write_bytes(b"this is not a database, it is a hard disk having a bad day\n" * 64)


def test_a_storage_failure_raises_rather_than_being_rendered_as_advice(tmp_path: Path) -> None:
    """The trajectory rule: an artifact plane that swallows a write failure reads as
    agreement — every consumer sees "no proposal" and concludes "nobody proposed".

    Guard-must-fire: the file is corrupted under a live store, so a real `sqlite3.Error`
    propagates. `sqlite3.Error` and `ArtifactError` are asserted DISJOINT, because the one
    bug this guards is a bare `except Exception` at the tool boundary turning a broken disk
    into advice the model politely retries.
    """
    db = tmp_path / "artifacts.db"
    store = ArtifactStore(db)
    seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
    store.commit(seed.revision_id)
    assert store.read("design.md") == BASE, "precondition: the plane worked a moment ago"

    corrupt(db)

    with pytest.raises(sqlite3.Error) as raised:
        store.propose("design.md", "anything\n", author="alice", rationale="doomed")
    assert not isinstance(raised.value, ArtifactError), (
        "a storage fault must never arrive as the model-facing error type, or the hook "
        "renders a broken plane as advice"
    )
    with pytest.raises(sqlite3.Error):
        store.read("design.md")


async def test_a_storage_failure_during_a_members_tool_call_is_not_swallowed_as_text(
    tmp_path: Path,
) -> None:
    """The same claim at the tool boundary, which is where the mistake would be made.

    Two facts had to be measured rather than assumed here, and both shaped the test.

    First, the fault has to land inside `propose_change` and not during the seed, or the run
    dies before the tool the guard is about ever runs (a first version corrupted the file
    before `on_assemble`, and a mutant hook catching bare `Exception` passed it). So the plane
    is seeded outside the hook, corrupted after, and the member's proposal is the operation
    that hits the broken disk.

    Second, the raise does *not* end the run: the runtime's tool executor catches any
    exception a tool raises and renders it to the model as a tool FAULT. That is exactly the
    distinction `hooks/hiring.py` draws — text is a mistake the model can fix, a fault is not
    — so the claim under test is the one that survives: the storage failure reaches the model
    as the runtime's fault and never as this hook's own `"error: ..."` advice, and no proposal
    is recorded. The unconditional raise is asserted one test up, at the store, where nothing
    stands between the failure and the caller.
    """
    async with RuntimeHarness() as h:
        db = tmp_path / "artifacts.db"
        store = ArtifactStore(db)
        seed = store.propose("design.md", BASE, author="o", rationale="r", branch="o-seed")
        store.commit(seed.revision_id)
        hook = Artifacts(store)  # already seeded above, so assembly touches nothing

        corrupting = Counting(
            [proposing("design.md", edited("the end", "NEVER-LANDS"), "doomed"), reading(APPROVING)]
        )
        members = [Member(Analyst("left"), "read", model=corrupting)]
        lead, _ = scripted_lead([ruling("never")])
        team = Team(lead, members, hooks=[hook, Negotiation(rounds=1)])
        corrupt(db)
        run = await team.run("go", h.worker.coordinator)

    results = corrupting.all_tool_results()
    assert "not a database" in results, "precondition: the broken disk really was reached"
    assert "error: " not in results, (
        f"a corrupt plane must not be rendered as this hook's own fixable-mistake text, or "
        f"the member politely retries into a void: {results!r}"
    )
    assert "proposed" not in results, "and it certainly must not read as a success"
    assert [e for e in run.hooks_data["artifacts"] if e["action"] == "propose"] == [], (
        "nothing was recorded as proposed, so no reader concludes a change is pending"
    )
