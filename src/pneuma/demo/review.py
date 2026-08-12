"""`pneuma-review`: a live-Bedrock team code review of a git diff, timeline attached.

The everyday shape: you have a diff, you want it reviewed before you push. A lead
reviewer holds the verdict; two specialists each hold the SAME diff privately (on
`self`, never in the lead's context) and answer only what the lead asks them —
correctness and maintainability respectively. A worklog lets a specialist flag a
discovery the other should see; a critic gets one shot at refuting the lead's
verdict before it ships. The timeline renders the whole run live, so you watch the
lead consult, the discoveries fan out, and any revision round happen.

This is the live sibling of `timeline_demo` (scripted, offline). Every model here is
`model.opus5(...)` — real Bedrock calls against `global.anthropic.claude-opus-5`,
needing AWS credentials. Cost control: `--effort low` by default and one critic
round; a mid-size diff reviews for a few cents.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

from ai_functions import InMemoryCoordinator, LocalWorker
from pydantic import BaseModel, Field
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from pneuma.method import MethodAgent, ai_method
from pneuma.model import Effort, opus5
from pneuma.team import Member, Team
from pneuma.team.hooks import Critic, Worklog

from .timeline import Timeline

DIFF_LIMIT = 24_000
"""Characters of diff each agent holds; beyond this the tail is truncated with a note."""


class Review(BaseModel):
    verdict: str = Field(description="One of: approve, approve-with-nits, request-changes")
    summary: str = Field(description="Two or three sentences: what the change does and the risk")
    blocking: list[str] = Field(
        default_factory=list, description="Issues that must be fixed before merge, each cited"
    )
    nits: list[str] = Field(default_factory=list, description="Non-blocking improvements")


class Specialist(MethodAgent):
    """One reviewer, one lens, the diff held privately on `self`."""

    def __init__(self, lens: str, charter: str, diff: str) -> None:
        self.name = f"{lens}-reviewer"
        self.lens = lens
        self.charter = charter
        self.diff = diff

    @ai_method(str, description="Answer one question about the diff through your lens")
    def examine(self, question: str) -> str:
        """You review code through one lens: {self.charter}

        Answer the lead reviewer's question below from the diff you hold, and only
        through your lens. Cite file paths and hunks. If you find something the other
        reviewer or the lead must know, post it with post_discovery. If your lens has
        nothing to say on the question, say so plainly.

        Question: {question}

        The diff you hold:
        {self.diff}
        """


class LeadReviewer(MethodAgent):
    name = "lead"

    @ai_method(Review, description="Deliver the team's review verdict on the diff")
    def review(self, request: str) -> Review:
        """You lead a code review. {request}

        You do NOT hold the diff. Your specialists do — each is a tool on your wire.
        Consult BOTH (correctness and maintainability) with pointed questions before
        deciding; a verdict citing neither is unfounded. Weigh their answers, then
        deliver the verdict: blocking issues only for defects that break behaviour or
        will hurt whoever touches this code next; everything else is a nit.
        """


class Skeptic(MethodAgent):
    name = "skeptic"

    @ai_method(str, description="Refute one review verdict, or concede NO-FINDINGS")
    def refute(self, brief: str) -> str:
        """{brief}

        You are the review's adversary. The verdict above ships unless you refute it:
        look for a blocking issue waved through as a nit, a claim citing nothing, or a
        verdict the cited evidence does not support. If after genuinely trying you
        find nothing wrong, answer with the single word NO-FINDINGS on its own line.
        """


def build_team(diff: str, effort: Effort) -> Team:
    def model():  # one fresh model handle per agent; they share nothing
        return opus5(effort, max_tokens=8_000, show_thinking=False)

    correctness = Specialist(
        "correctness",
        "defects, broken invariants, edge cases, error paths, concurrency races.",
        diff,
    )
    maintainability = Specialist(
        "maintainability",
        "readability, naming, duplication, doc drift, test coverage of the change.",
        diff,
    )
    members = [
        Member(correctness, "examine", model=model()),
        Member(maintainability, "examine", model=model()),
    ]
    critic = Member(Skeptic(), "refute", model=model())
    lead = LeadReviewer().compiled("review", model=model())
    return Team(lead, members, hooks=[Worklog(), Critic(critic, rounds=1)])


def read_diff(diff_cmd: str) -> str:
    """Piped stdin wins when it carries content; otherwise `diff_cmd` runs.

    An empty non-TTY stdin (headless shells, CI) falls THROUGH to the command
    rather than erroring — the first cut treated no-TTY as "stdin owns the
    diff" and made `--diff-cmd` unreachable anywhere without a terminal.
    """
    diff = "" if sys.stdin.isatty() else sys.stdin.read()
    if not diff.strip():
        diff = subprocess.run(  # noqa: S602 — the user's own shell command, their machine
            diff_cmd, shell=True, capture_output=True, text=True, check=True
        ).stdout
    if not diff.strip():
        raise SystemExit(f"no diff: stdin was empty and {diff_cmd!r} produced nothing")
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + f"\n... [truncated at {DIFF_LIMIT} chars]"
    return diff


async def run_review(diff: str, effort: Effort, console: Console) -> None:
    team = build_team(diff, effort)
    timeline = Timeline(console=console, cast=[member.name for member in team.members])
    coordinator = InMemoryCoordinator()
    worker = LocalWorker(coordinator)
    await worker.register()
    subscription = timeline.attach(coordinator)
    try:
        with Live(timeline.render_live(), console=console, auto_refresh=False) as live:
            timeline.follow(live)
            run = await team.run("Review this diff and deliver the team's verdict.", coordinator)
    finally:
        subscription.unsubscribe()
        await worker.close()
    console.print(timeline.render_static())
    review = run.answer
    body = (
        f"[bold]{review.verdict}[/] — {review.summary}\n\n"
        + ("\n".join(f"[red]•[/] {b}" for b in review.blocking) or "[green]no blockers[/]")
        + ("\n" + "\n".join(f"[dim]·[/] {n}" for n in review.nits) if review.nits else "")
    )
    console.print(Panel(body, title="team verdict", border_style="green"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Team code review of a git diff on live Bedrock, timeline attached"
    )
    parser.add_argument(
        "--diff-cmd",
        default="git diff HEAD~1 HEAD",
        help="shell command producing the diff (ignored when stdin is piped)",
    )
    parser.add_argument(
        "--effort",
        default="low",
        choices=["low", "medium", "high", "xhigh"],
        help="adaptive-thinking effort for every agent (cost lever)",
    )
    args = parser.parse_args()

    diff = read_diff(args.diff_cmd)
    console = Console()
    console.print(
        Panel(
            f"Reviewing {len(diff):,} chars of diff on live Bedrock "
            f"(claude-opus-5, effort={args.effort}).\n"
            "A lead consults a correctness and a maintainability reviewer, "
            "a skeptic gets one round to refute the verdict.",
            title="pneuma team review",
            border_style="cyan",
        )
    )
    asyncio.run(run_review(diff, args.effort, console))


if __name__ == "__main__":
    main()
