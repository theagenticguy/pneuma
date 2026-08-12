"""`pneuma-timeline`: a scripted team run rendered as a live terminal timeline.

Entirely offline — every model is a `ScriptedModel` playing fixed turns, so the demo
needs no credentials and finishes in seconds. The cast is small but exercises every
lane the timeline draws: a chair consulting two analysts (member-tool arrows), one
analyst flagging a discovery the worklog fans out (★), and a critic whose first
review objects so the answer loop sends the chair back for a revision (↻) before the
second review accepts (✓).

The scripted turns and the cast shapes are the test suite's own fixtures
(`tests/library/test_team_core.py`, `test_team_review.py`, `test_team_worklog.py`),
which is the point: what the tests assert on the wire is what the timeline shows on
the screen.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from ai_functions import InMemoryCoordinator, LocalWorker
from ai_functions.testing import ScriptedModel, Turn
from pydantic import BaseModel, Field
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Team, TeamRun
from pneuma.team.hooks import Critic, Worklog

from .timeline import Timeline

QUESTION = "Which plane carried the regression, and what is the mechanism?"

# ── Output types, module level for get_type_hints ──


class Reading(BaseModel):
    source: str = Field(description="Which evidence this reading came from")
    detail: str = Field(description="What it shows")


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="Which members were relied on")


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


class RedTeam(MethodAgent):
    name = "red-team"

    @ai_method(str, description="Refute one answer, or say you cannot")
    def review(self, brief: str, style: str = "harsh") -> str:
        """Review {brief} in a {style} style. Refute it or concede NO-FINDINGS."""


# ── Turn builders (the test suite's own) ──


def reading(detail: str, *, source: str) -> Turn:
    return Turn(tool_calls=(("Reading", {"source": source, "detail": detail}),))


def ruling(*cites: str, admitted: bool = True) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def call_member(name: str, request: str) -> Turn:
    return Turn(tool_calls=((name, {"request": request}),))


def posting(kind: str, body: str) -> Turn:
    return Turn(tool_calls=(("post_discovery", {"kind": kind, "body": body}),))


def review_says(text: str) -> Turn:
    return Turn(tool_calls=(("FinalAnswer", {"answer": text}),))


def build_team() -> Team:
    """The scripted cast: consult both analysts, one discovery, one revision round."""
    north_model = ScriptedModel(
        [
            posting("obstacle", "metrics plane lost 40% of its scrape targets at 14:02"),
            reading("scrape-target drop coincides with the rollout", source="north"),
        ]
    )
    south_model = ScriptedModel(
        [reading("config change 8842 landed at 14:01 on the south plane", source="south")]
    )
    lead_model = ScriptedModel(
        [
            call_member("north-analyst_read", "what does the north plane show"),
            call_member("south-analyst_read", "what does the south plane show"),
            ruling("north-analyst.read", "south-analyst.read"),
            # the critic objects once; this turn is the revision cycle
            ruling("north-analyst.read", "south-analyst.read", "change 8842"),
        ]
    )
    critic_model = ScriptedModel(
        [
            review_says("the ruling names no change id; the mechanism is unsupported"),
            review_says("NO-FINDINGS"),
        ]
    )
    members = [
        Member(Analyst("north"), "read", model=north_model),
        Member(Analyst("south"), "read", model=south_model),
    ]
    reviewer = Member(RedTeam(), "review", model=critic_model)
    lead = Chair().compiled("decide", model=lead_model)
    return Team(lead, members, hooks=[Worklog(), Critic(reviewer, rounds=1)])


async def run_with_timeline(console: Console, *, live: bool = True) -> tuple[TeamRun, Timeline]:
    """One scripted run with a timeline attached; returns both for the caller to print."""
    team = build_team()
    timeline = Timeline(console=console, cast=[member.name for member in team.members])
    coordinator = InMemoryCoordinator()
    worker = LocalWorker(coordinator)
    await worker.register()
    subscription: Any = timeline.attach(coordinator)
    try:
        if live:
            with Live(timeline.render_live(), console=console, auto_refresh=False) as live_display:
                timeline.follow(live_display)
                run = await team.run(QUESTION, coordinator)
        else:
            run = await team.run(QUESTION, coordinator)
        return run, timeline
    finally:
        subscription.unsubscribe()
        await worker.close()


LEGEND = (
    "[magenta]▶[/] spawn   [yellow]→[/] lead consults member   [cyan]★[/] discovery fans out   "
    "[green]✚[/] hire   [dark_orange]↻[/] revision   [green]✓[/] done   [bold red]✗[/] failed"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted team run as a live terminal timeline")
    parser.add_argument("--quiet", action="store_true", help="no live display, plain output")
    args = parser.parse_args()

    console = Console(no_color=args.quiet)
    console.print(
        Panel(
            "One chair, two analysts, one adversarial critic — every model scripted, "
            "nothing live.\nWatch the lead consult its members, a discovery fan out, "
            "and the critic send the chair back once.",
            title="pneuma team timeline",
            border_style="cyan",
        )
    )
    run, timeline = asyncio.run(run_with_timeline(console, live=not args.quiet))
    console.print(timeline.render_static())
    console.print(LEGEND)
    console.print(
        Panel(str(run.answer), title="the team's answer (after one revision)", border_style="green")
    )


if __name__ == "__main__":
    main()
