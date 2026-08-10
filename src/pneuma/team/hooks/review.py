"""Review hooks: `Critic` (one adversarial reviewer) and `Council` (a voting panel).

Review here is ordinary members' work riding the Accept/Revise loop `core.Team` already
drives — no special phase, no privileged vocabulary. A `Critic` asks one reviewer to refute
the lead's answer; a `Council` fans the answer to a panel and counts approvals against a
threshold. Both return the loop's own verdicts (`Accept` / `Revise`), record what they saw
into `hooks_data["review"]`, and leave the bounding to the core's per-hook cap.

**The review-integrity rule, applied throughout**: an errored, empty, or never-spawned
reviewer must never settle `Accept`. Positive evidence is the only thing that may wave an
answer through — a reviewer whose thread died reviewed nothing, so its failure counts
*against* the answer (a `Revise` for `Critic`, an objection for `Council`), never for it.
Same asymmetry as `detect`'s truncated sweeps
(`.erpaval/solutions/verification/truncation-must-dominate-positive-evidence.md`): absence
of findings under failure settles nothing.

**Reviewers join as standalone threads, not cast members, unless already cast.** A member
becomes a tool on the lead's wire (`core.py`, `_member_tools`), and a lead that can consult
— and lobby — its own adversarial reviewer mid-draft defeats the framing. So each hook
spawns the reviewers it was given as private threads in `on_assemble` and retires them in
`on_teardown`; a reviewer that is *already* in the cast (checked by identity) is left to the
core's lifecycle entirely, so nothing spawns or retires twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from ..core import Accept, Revise, Workspace
from ..members import Recruit

__all__ = ["Council", "Critic"]

# A review that could not happen, rendered as text. Shared with the legacy briefing
# convention deliberately: a prefix (rather than a wrapping token) survives any suffix the
# error carries, and `startswith` cannot be spoofed by an answer that merely *quotes* an
# error. Nothing carrying this prefix ever approves or reads as clean.
REVIEW_ERROR = "error: "


def _record(work: Workspace, entry: dict[str, Any]) -> None:
    """Append one review entry to the shared `hooks_data["review"]` list.

    A shared list rather than per-hook keys so a `Critic` and a `Council` on one team
    interleave in the order they actually ran; every entry carries `"hook"` for attribution.
    """
    work.data.setdefault("review", []).append(entry)


def _rendered(response: Any, reviewer: Recruit) -> str:
    """One reviewer response as reviewable text, errors and emptiness made explicit.

    `asyncio.gather(return_exceptions=True)` hands back exceptions as values, and a typed
    member answers with a pydantic model — both arrive here and leave as one string. An
    empty answer is rendered as an error rather than passed through, because downstream the
    empty string contains no token: it would silently read as findings/objection with
    nothing for the lead to act on, which is the review-integrity failure wearing a
    different coat.
    """
    if isinstance(response, BaseException):
        return f"{REVIEW_ERROR}{response!r}"
    text = str(response)
    if not text.strip():
        return f"{REVIEW_ERROR}{reviewer.name} returned an empty review"
    return text


class Critic:
    """One adversarial reviewer: refute the answer, or say — in one word — that you cannot.

    The reviewer sees the run's request *and* the lead's answer, framed adversarially: find
    what is wrong and cite which part. It is explicitly permitted to find nothing, and the
    permission is load-bearing — a reviewer forced to produce findings manufactures them,
    and the loop burns its cap on noise. Findings come back as `Revise(feedback=findings,
    cap=rounds)`; a clean response (`NO_FINDINGS` in the text, not error-prefixed) is
    `Accept`; an error or empty response is findings by the integrity rule above.

    Args:
        reviewer: Any `Recruit`. Spawned as a standalone thread in `on_assemble` unless it
            is already one of the cast (identity check), in which case the core owns it.
        rounds: The cap `Revise` carries — how many re-runs this reviewer's findings are
            worth. `0` is legitimate: findings are recorded and the transcript says the cap
            (not a clean review) ended the loop, but the lead never re-runs.
        advisory: When true, findings and errors are recorded but the verdict is always
            `Accept` — review as annotation, not gate. The record still distinguishes
            `"clean"` from `"findings"` and `"error"`; advisory changes what the verdict
            *does*, never what the record *says*.
    """

    NO_FINDINGS = "NO-FINDINGS"

    def __init__(self, reviewer: Recruit, *, rounds: int = 2, advisory: bool = False) -> None:
        if rounds < 0:
            raise ValueError(
                f"Critic(rounds={rounds}) is negative; pass 0 to record findings without "
                f"ever re-running the lead, or a positive cap."
            )
        self.reviewer = reviewer
        self.rounds = rounds
        self.advisory = advisory
        self._owns = False
        self._round = 0

    async def on_assemble(self, work: Workspace) -> None:
        """Spawn the reviewer privately — unless the cast already carries it."""
        self._round = 0
        self._owns = all(member is not self.reviewer for member in work.members)
        if self._owns:
            await self.reviewer.spawn(work.coordinator)

    def refute_request(self, request: str, answer: str) -> str:
        """What the reviewer is asked. Carries the request AND the answer verbatim.

        Both are embedded rather than referenced because the reviewer's model sees only what
        this string carries — a delivery claim needs a wire. Overridable, like the legacy
        prompt seams: the instruction and `NO_FINDINGS` have to agree on the token, so a
        subclass with a different vocabulary overrides both together.
        """
        return (
            f"You are this team's adversarial reviewer. Refute the answer below: find what "
            f"is wrong with it and cite which part you dispute. Do not summarise and do not "
            f"praise. If, after genuinely trying, you find nothing wrong, answer with the "
            f"single word {self.NO_FINDINGS}.\n\n"
            f"The team was asked:\n{request}\n\nThe answer under review:\n{answer}"
        )

    async def on_answer(self, work: Workspace, answer: Any) -> Accept | Revise:
        self._round += 1
        try:
            response: Any = await self.reviewer.ask(self.refute_request(work.request, str(answer)))
        except Exception as error:  # a dead reviewer is an outcome, not a run-ender
            response = error
        review = _rendered(response, self.reviewer)
        # Error is checked before the token: an error that *quotes* NO-FINDINGS reviewed
        # nothing, and reading it as clean would be the silent-accept fallback.
        if review.startswith(REVIEW_ERROR):
            outcome = "error"
        elif self.NO_FINDINGS in review:
            outcome = "clean"
        else:
            outcome = "findings"
        _record(
            work,
            {
                "hook": "Critic",
                "reviewer": self.reviewer.name,
                "round": self._round,
                "outcome": outcome,
                "review": review,
            },
        )
        if outcome == "clean" or self.advisory:
            return Accept()
        return Revise(
            feedback=f"Your reviewer {self.reviewer.name} disputes the answer:\n{review}",
            cap=self.rounds,
        )

    async def on_teardown(self, work: Workspace) -> None:
        if self._owns:
            await self.reviewer.retire()

    def __repr__(self) -> str:
        return f"<Critic reviewer={self.reviewer.name!r} rounds={self.rounds}>"


class Council:
    """A voting panel: every panelist reviews concurrently; approvals against a threshold.

    Each panelist sees the request and the answer and is asked to approve (the single word
    `APPROVED`) or object. `approvals / len(panel) >= threshold` is `Accept`; anything less
    is `Revise` with every objection concatenated and attributed. The comparison is `>=`,
    deliberately and tested at the boundary: `threshold=0.5` means half the panel suffices.

    A panelist's error is stringified and counted as an objection — an errored reviewer must
    not wave an answer through, and with the denominator fixed at the full panel size it
    cannot shrink the quorum either. Approval detection is containment, the
    negotiation tradeoff (`hooks/negotiation.py`, `approves`): a typed panelist's answer embeds
    the token inside `str(model)`, so equality would silently veto every typed member;
    error-prefixed answers can never approve regardless of what they quote.

    Args:
        members: The panel, each a `Recruit`. Panelists not already in the team's cast are
            spawned as standalone threads (see the module docstring for why they are not
            tools on the lead's wire); cast members are asked over their live threads.
            An empty panel is refused: `0/0` compares vacuously against any threshold, and
            a review by nobody settling `Accept` is the silent-accept defect verbatim.
        threshold: Approval fraction in `(0, 1]`. `<= 0` would accept against a unanimous
            objection; `> 1` is unreachable and every run would burn to its cap — both are
            wiring bugs, refused at construction where the wirer is looking.
        rounds: The cap each `Revise` carries.
        advisory: Record the vote, never gate on it — same contract as `Critic.advisory`.
    """

    APPROVAL = "APPROVED"

    def __init__(
        self,
        members: Sequence[Recruit],
        *,
        threshold: float = 0.5,
        rounds: int = 1,
        advisory: bool = False,
    ) -> None:
        self.panel = list(members)
        if not self.panel:
            raise ValueError(
                "Council(members=[]) has nobody to vote; an empty panel would accept every "
                "answer vacuously — give it panelists or drop the hook."
            )
        if not 0 < threshold <= 1:
            raise ValueError(
                f"Council(threshold={threshold}) is outside (0, 1]: at or below 0 the panel "
                f"approves against unanimous objection, above 1 approval is unreachable and "
                f"every run burns its revision cap."
            )
        if rounds < 0:
            raise ValueError(
                f"Council(rounds={rounds}) is negative; pass 0 to record objections without "
                f"ever re-running the lead, or a positive cap."
            )
        self.threshold = threshold
        self.rounds = rounds
        self.advisory = advisory
        self._own: list[Recruit] = []
        self._round = 0

    async def on_assemble(self, work: Workspace) -> None:
        """Spawn the panelists the cast does not already carry (identity check)."""
        self._round = 0
        cast = {id(member) for member in work.members}
        self._own = [p for p in self.panel if id(p) not in cast]
        for panelist in self._own:
            await panelist.spawn(work.coordinator)

    def vote_request(self, request: str, answer: str) -> str:
        """What each panelist is asked. Instruction and check agree on `APPROVAL`."""
        return (
            f"You sit on this team's review panel. Review the answer below against what you "
            f"alone know. If it is sound, answer with the single word {self.APPROVAL}. "
            f"Otherwise state your objection and cite which part you dispute.\n\n"
            f"The team was asked:\n{request}\n\nThe answer under review:\n{answer}"
        )

    def approves(self, review: str) -> bool:
        """Containment, never for errors — the legacy `approves` tradeoff, kept."""
        return not review.startswith(REVIEW_ERROR) and self.APPROVAL in review

    async def on_answer(self, work: Workspace, answer: Any) -> Accept | Revise:
        self._round += 1
        prompt = self.vote_request(work.request, str(answer))
        responses = await asyncio.gather(
            *(panelist.ask(prompt) for panelist in self.panel), return_exceptions=True
        )
        reviews = {
            panelist.name: _rendered(response, panelist)
            for panelist, response in zip(self.panel, responses, strict=True)
        }
        approved = [name for name, review in reviews.items() if self.approves(review)]
        # The denominator is the full panel, not the reviews that survived: an errored
        # panelist lowers the approval fraction, it never leaves the room.
        accepted = len(approved) / len(self.panel) >= self.threshold
        _record(
            work,
            {
                "hook": "Council",
                "round": self._round,
                "approved": approved,
                "reviews": reviews,
                "accepted": accepted,
            },
        )
        if accepted or self.advisory:
            return Accept()
        objections = "\n".join(
            f"{name}: {review}" for name, review in reviews.items() if name not in approved
        )
        approvals = f"\n\nAlready approved by: {', '.join(approved)}." if approved else ""
        return Revise(
            feedback=(
                f"Your review panel did not reach its approval threshold. Revise the answer "
                f"to meet the objections, or defend the parts they read wrongly.\n\n"
                f"Objections:\n{objections}{approvals}"
            ),
            cap=self.rounds,
        )

    async def on_teardown(self, work: Workspace) -> None:
        await asyncio.gather(*(panelist.retire() for panelist in self._own))

    def __repr__(self) -> str:
        names = [panelist.name for panelist in self.panel]
        return f"<Council panel={names!r} threshold={self.threshold} rounds={self.rounds}>"
