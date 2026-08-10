"""`Briefing`: every member reports before the lead runs, and the report reaches the lead.

The legacy module's phase 2, as a hook. `on_assemble` asks every member its own briefing
question concurrently and holds the barrier — `asyncio.gather` waits for all of them, so the
lead's evidence never depends on scheduling. `on_request` then prepends the rendered brief to
the request the lead is asked, which is the delivery the whole phase exists for: the barrier
is only worth holding if what it waited for reaches the lead's model context (the historical
`render_brief` bug — a phase once *recorded* briefings the lead never read, and
`.erpaval/solutions/ai-functions-runtime/orchestrator-state-lifetimes-and-tool-races.md`
carries the lesson: a delivery claim needs a wire).

A member that raises becomes a rendered `BRIEFING_ERROR` string rather than a run-ending
fault — a four-member team with one dead thread is still a team worth asking, and the lead
can see in its own prompt that one source is missing. A cast whose *every* member failed is
refused before the lead spends anything: the lead holds no evidence of its own (that is why
there is a team), so it would rule from the request alone and the result would look graded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from ..core import Workspace
from ..members import Recruit

__all__ = ["BRIEFING_ERROR", "Briefing"]

BRIEFING_ERROR = "error: "
"""The prefix a failed member's briefing is rendered with, single-sourced.

Two places read it — the rendering in `on_assemble` and the all-failed refusal — and they
have to agree, because the refusal's whole job is to notice that every string in the mapping
is one of these. The tradeoff is the legacy one, kept deliberately: a member whose
*successful* answer begins with the prefix would be miscounted, and every member would have
to do it at once for the refusal to fire.
"""

DEFAULT_QUESTION = (
    "Report what you alone know that bears on this request, including anything it cannot "
    "settle without another member's evidence."
)


class Briefing:
    """Ask every member first; put what they said into the lead's own prompt.

    Args:
        question_fn: What each member is asked, per member — the interesting teams are the
            asymmetric ones, where a member holding a private view needs to be told what to
            do with *that* view. `None` asks everyone the same generic question. The run's
            request is appended to whatever this returns, so one string drives the team.
        forward_request: Whether the run's request rides along with each member's question.
            `True` — the default — is what lets one string drive the whole team. `False` is
            the war-room shape: a specialist answers for its own evidence and is not told
            what the lead was asked, because one that read the question would be reasoning
            about the answer, which is the lead's job and the asymmetry the team exists for.
    """

    def __init__(
        self,
        question_fn: Callable[[Recruit], str] | None = None,
        *,
        forward_request: bool = True,
    ) -> None:
        self.question_fn = question_fn
        self.forward_request = forward_request

    def question(self, member: Recruit) -> str:
        return self.question_fn(member) if self.question_fn is not None else DEFAULT_QUESTION

    async def on_assemble(self, work: Workspace) -> None:
        """The barrier: every member answers before this returns, failures rendered.

        `gather` starts every coroutine before any completes and returns exceptions
        positionally (`return_exceptions=True`), so the pairing below is sound and one dead
        member cannot take the run down. The mapping is keyed by name, which the core's
        duplicate-name guard is what makes lossless.
        """
        members = list(work.members)
        if not members:
            work.data["briefing"] = {}
            return
        suffix = f"\n\n{work.request}" if self.forward_request else ""
        answers = await asyncio.gather(
            *(member.ask(f"{self.question(member)}{suffix}".strip()) for member in members),
            return_exceptions=True,
        )
        briefings = {
            member.name: (
                f"{BRIEFING_ERROR}{answer!r}" if isinstance(answer, BaseException) else str(answer)
            )
            for member, answer in zip(members, answers, strict=True)
        }
        self._check_some_briefing_survived(briefings)
        work.data["briefing"] = briefings

    def on_request(self, work: Workspace, request: str) -> str:
        """The delivery: the brief lands in the text the lead is actually asked.

        One text block rather than tools or turns, because the lead's first parameter is
        the only channel every lead shape shares — `lead_handle.run(text)` binds there for
        a `STRUCTURED` lead as much as for a `STR_PROMPT` one. An empty cast prepends
        nothing: a team that declares no members has lost none, and its lead's prompt must
        not grow a stray empty heading.
        """
        briefings: Mapping[str, str] = work.data.get("briefing") or {}
        if not briefings:
            return request
        lines = "\n".join(f"{name}: {text}" for name, text in briefings.items())
        return f"{request}\n\nWhat your team reported:\n{lines}".strip()

    def _check_some_briefing_survived(self, briefings: Mapping[str, str]) -> None:
        """Refuse to let the lead run when every member failed. Raised, not rendered.

        The one member failure that is not recoverable, and the asymmetry with the
        `return_exceptions=True` above is the whole argument: three planes of four is still
        a team; zero is a lead reasoning from the request alone while the run looks staffed.
        Raised as a `RuntimeError` because there is no model in this failure to hand a
        string to — a dead cast is a coordinator or wiring fault at the level above the
        lead, and the caller is the party that can act. Raising here, inside `on_assemble`,
        is before the lead's first cycle by the core's pipeline order, so the refusal spends
        nothing the guard protects.
        """
        if not briefings or not all(text.startswith(BRIEFING_ERROR) for text in briefings.values()):
            return
        detail = "; ".join(f"{name}: {text}" for name, text in briefings.items())
        raise RuntimeError(
            f"briefing: every one of the {len(briefings)} member(s) failed its briefing, so "
            f"the lead would have no evidence at all and would rule from the request alone — "
            f"an answer that looks staffed and was not. Refused before the lead runs. The "
            f"failures were — {detail}"
        )
