"""`Expedition` — the code-owned bounded outer loop: run a team round by round until dry.

This is the third loop in the multi-loop story. The inner loop is each member's own
act/observe thread — a `Member`'s live `MethodThread` deciding its next tool call. The
middle loop is the team's answer loop — `core.Team._answer_loop`'s bounded Accept/Revise
restart chain over one run. The outer loop is this module: a plain-code driver that asks
the same team a question round after round, folds what earlier rounds found into the next
round's request, and stops when the findings run dry or the budget runs out.

**The determinism rule.** No LLM decides when this loop ends. Budgets — the round cap, the
dry-streak threshold, the external halt — are conventional code, checked by conventional
comparisons. This is the shape deep-research systems converge on (LangChain's "loop
engineering" writing calls it tier 3: the orchestrating loop is authored code, the model
works *inside* the rounds): a model asked "are you done?" says no forever or yes too early,
and either answer is unauditable; a counter is neither. The model's only influence on
termination is indirect — by producing an answer the novelty check grades as stale.

**Loop-until-dry is the stop shape.** Each round's answer is graded fresh or stale against
the rounds before it; a run of consecutive stale rounds (`dry_after` of them) means the well
is dry and the loop stops with what it has. The default novelty check is deliberately dumb —
normalized text equality against prior answers — and is exactly the seam a real deployment
overrides (embedding distance, a findings-set diff, a citation-count delta) by passing its
own `novelty=` callable. Same for `next_request=`: the default digest is a plain bullet list
of prior answers plus "go deeper", and a real deployment substitutes its own compression.

**Composition, not knowledge.** An Expedition holds a `Team`; that team's members may
themselves be `Squad`s wrapping whole inner teams. Neither class knows about the other —
the Expedition sees only `team.run`, the Squad sees only `Recruit` — so the loops nest by
construction, without a registry or a protocol between them.

**One team, sequential rounds.** The same `Team` instance runs every round. That is safe
because the rounds are strictly sequential and `Team.run` builds a fresh `Workspace` per
run (hooks key their state to the workspace, members spawn and retire inside the run's own
`finally`), so no state leaks between rounds except what `next_request` deliberately carries.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .core import Team, TeamRun

__all__ = ["Expedition", "ExpeditionResult", "Round"]


@dataclass(frozen=True)
class Round:
    """One round of the expedition, kept whole for the audit trail.

    `answer` is `str(run.answer)` — the loop grades and digests text, whatever richer type
    the lead produced — and `run` is the full `TeamRun`, so a caller wanting the round's
    `hooks_data` or transcript has it without re-running anything.
    """

    index: int  # 1-based
    request: str  # what the team was asked this round
    answer: str  # str(run.answer)
    fresh: bool  # the novelty verdict: did this round add anything new
    run: TeamRun  # the full run, for hooks_data / transcript access


@dataclass(frozen=True)
class ExpeditionResult:
    """Every round the expedition ran, and why it stopped.

    `stopped` is one of `'dry'` (the dry-streak threshold fired), `'max_rounds'` (the hard
    cap fired), or `'halted'` (the external `should_continue` seam said stop).
    """

    rounds: list[Round]
    stopped: str

    @property
    def answer(self) -> str:
        """The last round's answer — the expedition's deepest finding."""
        if not self.rounds:
            raise ValueError("ExpeditionResult has no rounds; answer is undefined.")
        return self.rounds[-1].answer


def _default_novelty(answer: str, rounds: list[Round]) -> bool:
    """Fresh unless the answer's normalized text equals a prior round's.

    Deliberately dumb: strip, casefold, compare. It catches only the degenerate loop — the
    team repeating itself verbatim — and nothing subtler. This is the seam a real deployment
    overrides with `novelty=` (embedding distance, a findings-set diff); the default exists
    so the dry stop works out of the box without any model in the judgment.
    """
    normalized = answer.strip().casefold()
    return all(normalized != prior.answer.strip().casefold() for prior in rounds)


def _default_next_request(base: str, rounds: list[Round]) -> str:
    """The base request plus a plain digest of prior answers and a push to go deeper."""
    digest = "\n".join(f"- {prior.answer}" for prior in rounds)
    return (
        f"{base}\n\n"
        f"Prior rounds found:\n{digest}\n\n"
        f"Go deeper on what is still unknown. Do not repeat what prior rounds already found."
    )


class Expedition:
    """Run a team round by round, accumulating findings, until dry or out of budget.

    Args:
        team: The team every round runs. The same instance is reused — safe because rounds
            are strictly sequential and each `Team.run` is self-contained (fresh workspace,
            members spawned and retired per run).
        max_rounds: The hard cap; the loop never runs more rounds than this. Must be >= 1.
        dry_after: Stop after this many *consecutive* rounds graded stale (`fresh=False`).
            A single stale round between fresh ones resets the streak and does not stop the
            loop. Must be >= 1.
        novelty: `(answer, prior_rounds) -> bool` — is this answer fresh relative to what
            earlier rounds found? Sync or async; the default is normalized-text equality
            (see `_default_novelty` for why it is deliberately dumb).
        next_request: `(base_request, prior_rounds) -> str` — round N+1's request. Sync or
            async; the default appends a bullet digest of prior answers and an instruction
            to go deeper (see `_default_next_request`). Round 1 is always the base request
            verbatim.
        should_continue: `(rounds) -> bool`, consulted AFTER each round; `False` stops the
            loop with `stopped='halted'`. This is the external-budget seam — token ledgers,
            wall clocks, kill switches live outside this class, behind this callable. Sync
            or async.
    """

    def __init__(
        self,
        team: Team,
        *,
        max_rounds: int,
        dry_after: int = 2,
        novelty: Callable[[str, list[Round]], Any] | None = None,
        next_request: Callable[[str, list[Round]], Any] | None = None,
        should_continue: Callable[[list[Round]], Any] | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError(
                f"Expedition(max_rounds={max_rounds}) could never run a round — "
                f"pass a cap of at least 1."
            )
        if dry_after < 1:
            raise ValueError(
                f"Expedition(dry_after={dry_after}) would grade the well dry before any "
                f"round could be stale — pass a threshold of at least 1."
            )
        self.team = team
        self.max_rounds = max_rounds
        self.dry_after = dry_after
        self.novelty = novelty if novelty is not None else _default_novelty
        self.next_request = next_request if next_request is not None else _default_next_request
        self.should_continue = should_continue

    async def run(
        self,
        request: str,
        coordinator: Any = None,
        *,
        parent_id: Any = None,
    ) -> ExpeditionResult:
        """The whole expedition: rounds until dry, capped, externally haltable.

        Args:
            request: The base request. Round 1 asks it verbatim; later rounds ask what
                `next_request` builds from it and the history.
            coordinator: Passed through to every `Team.run`. `None` works — each round then
                stands up and tears down its own private runtime via the team's convenience
                path — but passing one coordinator is the efficient path: one runtime, one
                registry, one event log for the whole expedition.
            parent_id: Passed through to every `Team.run`, so each round's subtree hangs
                off the same parent thread.
        """
        rounds: list[Round] = []
        stale_streak = 0
        while True:
            if rounds:
                round_request = str(await _maybe_await(self.next_request(request, list(rounds))))
            else:
                round_request = request
            run = await self.team.run(round_request, coordinator, parent_id=parent_id)
            answer = str(run.answer)
            fresh = bool(await _maybe_await(self.novelty(answer, list(rounds))))
            rounds.append(
                Round(
                    index=len(rounds) + 1,
                    request=round_request,
                    answer=answer,
                    fresh=fresh,
                    run=run,
                )
            )
            stale_streak = 0 if fresh else stale_streak + 1
            if stale_streak >= self.dry_after:
                return ExpeditionResult(rounds=rounds, stopped="dry")
            if len(rounds) >= self.max_rounds:
                return ExpeditionResult(rounds=rounds, stopped="max_rounds")
            if self.should_continue is not None and not bool(
                await _maybe_await(self.should_continue(list(rounds)))
            ):
                return ExpeditionResult(rounds=rounds, stopped="halted")

    def __repr__(self) -> str:
        return (
            f"<Expedition team={self.team!r} max_rounds={self.max_rounds} "
            f"dry_after={self.dry_after}>"
        )


async def _maybe_await(value: Any) -> Any:
    """Await `value` when a callable chose to be async; pass it through otherwise.

    A copy of `core._maybe_await` rather than an import — the name is private to `core`
    and the helper is three lines; duplicating it keeps this module free of a dependency
    on a sibling's internals.
    """
    if inspect.isawaitable(value):
        return await value
    return value
