"""`Negotiation`: fan the lead's answer to the members, gather objections, revise to consensus.

The legacy module's phase 2½, riding the core's Accept/Revise loop instead of owning its own.
`on_answer` renders the answer as a plan, asks every member to review it concurrently (the
briefing barrier's twin — same `gather`, same `return_exceptions=True`, same error
rendering), and either every member approves (`Accept`, negotiation over) or the objections
go back to the lead as `Revise(feedback)` — a full lead cycle, bounded by the core's per-hook
cap read off the verdict. Evidence for the phase existing at all: AgentRadio
(arXiv 2607.28430) measured negotiation as its single biggest layer (+67 net rubrics) — the
members hold disjoint evidence by design, so a plan drafted from one-shot briefings can carry
a flaw any one of them would catch on sight.

Approval is the two-tier verdict parse (`review.verdict_token_present`), not bare
containment and not equality. Containment read an objection that *quotes* the token ("I
cannot say APPROVED while the plan misreads my evidence") as the approval it refuses to
give; equality would silently veto every typed member, whose pydantic answer embeds the
token as a field value (`field='APPROVED'` inside `str(model)`) rather than standing alone.
So the parse accepts exactly the two legitimate shapes — the token as the whole answer or a
line of it (terminal punctuation tolerated), or the token as a rendered field value — and
reads everything else, prose mentions included, as an objection. A rendered error can never
approve: a member whose thread died did not review anything, so it blocks unanimity rather
than faking it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import Accept, Revise, Workspace
from .briefing import BRIEFING_ERROR
from .review import verdict_token_present

__all__ = ["Negotiation"]


class Negotiation:
    """Bounded plan→objection→revision rounds, recorded in `hooks_data["negotiation"]`.

    Args:
        rounds: How many revision rounds this hook is worth — it becomes the `cap` on every
            `Revise` this hook returns, so the core's loop enforces it. Zero gives feedback
            no rounds (the core passes the first answer after recording `revise_cap`);
            negative is refused, because `Revise.__post_init__` refuses it later anyway and
            a construction-time refusal is where every wiring typo belongs.
    """

    APPROVAL = "APPROVED"
    """The token a member answers with to approve, single-sourced.

    Two places read it — the instruction `plan_request` renders and the check `approves`
    runs — and they have to agree: an instruction asking for one word and a check looking
    for another would make unanimity unreachable and every negotiation run to its cap,
    silently.
    """

    def __init__(self, rounds: int = 2) -> None:
        if rounds < 0:
            raise ValueError(
                f"Negotiation(rounds={rounds}) is negative, which would be refused mid-run "
                f"by Revise anyway — pass 0 to give objections no rounds, or a positive cap."
            )
        self.rounds = rounds

    # ── The seams a stricter team overrides together ──

    def render_plan(self, answer: Any) -> str:
        """The lead's answer as the text a member reviews. Text is the only channel every
        member shape shares — `Recruit` guarantees `ask` and nothing richer."""
        return str(answer)

    def plan_request(self, plan: str) -> str:
        """What each member is asked. Contains the plan verbatim, because the member's
        model sees only what this string carries — a delivery claim needs a wire."""
        return (
            f"Your lead proposes the following plan. Review it against what you alone know. "
            f"If it is sound, answer with the single word {self.APPROVAL}. Otherwise state "
            f"your objections and what you would change.\n\nPlan:\n{plan}"
        )

    def approves(self, answer: str) -> bool:
        """The two-tier verdict parse: the token alone (or alone on a line, terminal
        punctuation tolerated) approves, and so does a typed member's rendered field value
        (`field='APPROVED'`) — a prose *mention* of the token does not, because an objection
        quoting the word it withholds is still an objection. Errors are checked first and
        never approve, whatever their text quotes."""
        return not answer.startswith(BRIEFING_ERROR) and verdict_token_present(
            answer, self.APPROVAL
        )

    def render_objections(self, objections: Mapping[str, str], approved: Sequence[str]) -> str:
        """Every non-approving answer, attributed; the approvers named rather than dropped.

        A lead revising against two objections should know the other two signed off — a
        revision that undoes what the approvers approved is a worse plan wearing a fix's
        clothes. Errors ride along under their `BRIEFING_ERROR` rendering: a member that
        could not review is a fact about the plan's audit, not a secret.
        """
        lines = "\n".join(
            f"{name}: {text}" for name, text in objections.items() if name not in approved
        )
        approvals = f"\n\nAlready approved by: {', '.join(approved)}." if approved else ""
        return (
            f"Your team reviewed your plan and not everyone approved. Revise the plan to "
            f"answer the objections, or defend the parts they read wrongly."
            f"\n\nObjections:\n{lines}{approvals}"
        ).strip()

    # ── The hook ──

    async def on_answer(self, work: Workspace, answer: Any) -> Accept | Revise:
        """One round per call: fan out, count approvals, verdict. The core drives the loop.

        An empty cast accepts immediately *without recording a round*: an empty round is
        vacuously unanimous (everyone of nobody approved), so a transcript entry would
        record a consensus no member ever gave.
        """
        members = list(work.members)
        if not members:
            return Accept()

        plan = self.render_plan(answer)
        responses = await asyncio.gather(
            *(member.ask(self.plan_request(plan)) for member in members),
            return_exceptions=True,
        )
        objections = {
            member.name: (
                f"{BRIEFING_ERROR}{response!r}"
                if isinstance(response, BaseException)
                else str(response)
            )
            for member, response in zip(members, responses, strict=True)
        }
        approved = [name for name, text in objections.items() if self.approves(text)]

        rounds: list[dict[str, Any]] = work.data.setdefault("negotiation", [])
        entry: dict[str, Any] = {
            "round": len(rounds) + 1,
            "plan": plan,
            "objections": objections,
            "approved": approved,
        }

        if len(approved) == len(objections):
            entry["outcome"] = "unanimous"
            rounds.append(entry)
            return Accept()

        # Not unanimous. The core reads the cap off this verdict and, when the rounds
        # already spent reach it, records `revise_cap` and passes the answer on — so the
        # entry's outcome distinguishes a round whose revision ran from the one the cap
        # refused, exactly as the legacy transcript did.
        spent = sum(1 for r in rounds if r["outcome"] == "revised")
        entry["outcome"] = "revised" if spent < self.rounds else "cap_reached"
        rounds.append(entry)
        return Revise(
            feedback=self.render_objections(objections, approved),
            cap=self.rounds,
        )
