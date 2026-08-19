"""Offline tests for the `Trajectory` hook: one durable row per run, on every path.

The load-bearing claims: a completed run persists a row whose fields match what the run
actually was (request, cast, final answer); a FAULTED run still persists — the
write-on-every-path contract, proven by breaking the run deliberately, because a guard
that never fires proves nothing; one hook instance across two sequential runs writes two
rows with no state bleed; and the answer persisted alongside a revising reviewer is the
FINAL revised answer, not the draft. Rows are read back through `read_trajectories` (the
consumer seam) and through fresh raw JSON parses, so "valid JSON" is asserted, not assumed.

`Counting` composes a `ScriptedModel` (which is `@final`); output types are module level
because `compile_ai_method` resolves annotations against module globals (`method.py:146`).
Databases are tmp_path files — never `:memory:` — per the repo convention the hook itself
enforces.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from ai_functions import AIFunction
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member, Team
from pneuma.team.hooks.review import Critic
from pneuma.team.hooks.trajectory import Trajectory, read_trajectories

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Sequence
    from pathlib import Path

# ── Output types, module level for get_type_hints ──


class Ruling(BaseModel):
    admitted: bool = Field(description="Whether this ruling is ready")
    cites: list[str] = Field(default_factory=list, description="What it relies on")


# ── The cast ──


class Chair(MethodAgent):
    name = "chair"

    @ai_method(Ruling, description="Rule on what the team reported")
    def decide(self, question: str, rigour: str = "normal") -> Ruling:
        """Rule on {question}, with {rigour} rigour."""


class RedTeam(MethodAgent):
    """A reviewer as a typed member: `@ai_method(str)` answers via `FinalAnswer`."""

    name = "red-team"

    @ai_method(str, description="Refute one answer, or say you cannot")
    def review(self, brief: str, style: str = "harsh") -> str:
        """Review this brief with {style} rigour: {brief}"""


class Counting(Model):
    """Composes a `ScriptedModel` and records what each call carried on the wire."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
        self.contexts: list[list[Any]] = []
        self.tool_specs: list[list[str]] = []

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
        self.tool_specs.append([spec["name"] for spec in (tool_specs or [])])
        return self._inner.stream(messages, tool_specs, *args, **kwargs)


class Spy:
    """A model-less `Recruit`: a fixed answer and a lifecycle journal, nothing else."""

    def __init__(self, name: str, *, answer: str = "ok") -> None:
        self.name = name
        self.answer = answer
        self.asked: list[str] = []
        self.retirements = 0

    async def spawn(self, coordinator: Any, *, parent_id: Any = None) -> Any:
        return _FakeHandle(f"tid-{self.name}")

    async def ask(self, request: str) -> Any:
        self.asked.append(request)
        return self.answer

    async def retire(self) -> None:
        self.retirements += 1


class _FakeHandle:
    def __init__(self, ident: str) -> None:
        self.id = ident


def ruling(*, admitted: bool = True, cites: Sequence[str] = ()) -> Turn:
    return Turn(tool_calls=(("Ruling", {"admitted": admitted, "cites": list(cites)}),))


def review_says(text: str) -> Turn:
    """One reviewer turn: `@ai_method(str)` wraps `str` in a generated `FinalAnswer`."""
    return Turn(tool_calls=(("FinalAnswer", {"answer": text}),))


def scripted_lead(turns: list[Turn]) -> tuple[AIFunction[..., Any], Counting]:
    model = Counting(turns)
    return Chair().compiled("decide", model=model), model


def scripted_reviewer(turns: list[Turn]) -> tuple[Member, Counting]:
    model = Counting(turns)
    return Member(RedTeam(), "review", model=model), model


# ── 1. The completed row ──


async def test_a_completed_run_persists_one_row_whose_fields_match_the_run(
    tmp_path: Path,
) -> None:
    """The whole plane, closed: one row, `completed`, no fault, the request verbatim, the
    cast's names, the run's own answer, and transcript/hooks_data that parse as JSON."""
    async with RuntimeHarness() as h:
        db = tmp_path / "trajectories.db"
        lead, _ = scripted_lead([ruling(cites=["EX-1"])])
        member = Spy("archivist")
        run = await Team(lead, [member], hooks=[Trajectory(db)]).run(
            "who is right", h.worker.coordinator
        )

    rows = read_trajectories(db)
    assert len(rows) == 1, "one run, one row"
    (row,) = rows
    assert row["outcome"] == "completed"
    assert row["fault"] is None
    assert row["request"] == "who is right"
    assert row["answer"] == str(run.answer), "the persisted answer is the run's answer"
    assert "EX-1" in row["answer"]
    assert json.loads(row["members"]) == ["archivist"]
    assert isinstance(json.loads(row["transcript"]), list), "transcript is valid JSON"
    assert isinstance(json.loads(row["hooks_data"]), dict), "hooks_data is valid JSON"
    assert row["started_at"] <= row["finished_at"], "ISO-8601 UTC stamps order lexically"


# ── 2. The faulted row: write-on-every-path, proven by firing it ──


async def test_a_faulted_run_still_persists_a_row_with_the_fault_on_it(
    tmp_path: Path,
) -> None:
    """Guard-must-fire: the lead's script is empty, so its model raises on the first call
    and `team.run` raises — and the row is there anyway, `faulted`, fault non-null, answer
    NULL, because `on_teardown` runs in the core's `finally` on every path."""
    async with RuntimeHarness() as h:
        db = tmp_path / "trajectories.db"
        lead, _ = scripted_lead([])  # ScriptExhausted on the first lead cycle
        with pytest.raises(Exception, match="ScriptExhausted|script has only"):
            await Team(lead, [Spy("bystander")], hooks=[Trajectory(db)]).run(
                "doomed question", h.worker.coordinator
            )

    (row,) = read_trajectories(db)
    assert row["outcome"] == "faulted"
    assert row["fault"] is not None and "Script" in row["fault"]
    assert row["answer"] is None, "no answer ever existed, so none is invented"
    assert row["request"] == "doomed question"
    assert json.loads(row["members"]) == ["bystander"]


# ── 3. Sequential reuse: per-run state resets ──


async def test_two_sequential_runs_of_one_team_and_hook_write_two_clean_rows(
    tmp_path: Path,
) -> None:
    """One `Team`, one `Trajectory` instance, two runs: two rows, each with its own
    request and answer, and run 2's transcript free of run 1's member calls — the
    identity-keyed reset, observed from the durable side."""
    async with RuntimeHarness() as h:
        db = tmp_path / "trajectories.db"
        hook = Trajectory(db)
        first_member = Spy("first-witness", answer="FIRST-EVIDENCE")

        lead1, _ = scripted_lead(
            [
                Turn(tool_calls=(("first-witness", {"request": "report"}),)),
                ruling(cites=["one"]),
            ]
        )
        team1 = Team(lead1, [first_member], hooks=[hook])
        await team1.run("first question", h.worker.coordinator)

        lead2, _ = scripted_lead([ruling(cites=["two"])])
        team2 = Team(lead2, [], hooks=[hook])
        await team2.run("second question", h.worker.coordinator)

    first, second = read_trajectories(db)
    assert first["request"] == "first question" and "one" in first["answer"]
    assert second["request"] == "second question" and "two" in second["answer"]
    assert "FIRST-EVIDENCE" in first["transcript"], "run 1's member call is on run 1's row"
    assert "FIRST-EVIDENCE" not in second["transcript"], "…and never bleeds into run 2's"
    assert json.loads(second["members"]) == [], "run 2's cast, not run 1's"


# ── 4. With a revising Critic: the FINAL answer is the one persisted ──


async def test_the_persisted_answer_is_the_final_revised_answer_not_the_draft(
    tmp_path: Path,
) -> None:
    """A `Critic` that revises once: the row carries the answer the review loop settled,
    and the review record itself rides in as `hooks_data` JSON — the trajectory plane sees
    what the learning loop will want to learn from."""
    async with RuntimeHarness() as h:
        db = tmp_path / "trajectories.db"
        reviewer, _ = scripted_reviewer(
            [review_says("the citation DRAFT-1 is fabricated"), review_says("NO-FINDINGS")]
        )
        lead, _ = scripted_lead([ruling(cites=["DRAFT-1"]), ruling(cites=["FIXED-2"])])
        run = await Team(lead, [], hooks=[Critic(reviewer), Trajectory(db)]).run(
            "who is right", h.worker.coordinator
        )

    assert run.answer.cites == ["FIXED-2"], "precondition: the run really did revise"
    (row,) = read_trajectories(db)
    assert "FIXED-2" in row["answer"], "the FINAL answer is the one persisted"
    assert "DRAFT-1" not in row["answer"], "…never the refuted draft"
    hooks_data = json.loads(row["hooks_data"])
    assert [e["outcome"] for e in hooks_data["review"]] == ["findings", "clean"], (
        "the critic's record rides into the row"
    )
    transcript = json.loads(row["transcript"])
    assert any(e["kind"] == "revise" for e in transcript), "the revision is on the record"


# ── 5. The reader, and the constructor guard ──


async def test_read_trajectories_returns_rows_written_on_the_same_path(
    tmp_path: Path,
) -> None:
    """The consumer seam over the same file a run just wrote — the cursor-closed read
    path exercised against a real WAL database, plus `[]` from a never-written path."""
    async with RuntimeHarness() as h:
        db = tmp_path / "trajectories.db"
        assert read_trajectories(db) == [], "an unwritten plane reads as zero rows, not a fault"
        lead, _ = scripted_lead([ruling()])
        await Team(lead, [], hooks=[Trajectory(db)]).run("go", h.worker.coordinator)

    rows = read_trajectories(db)
    assert len(rows) == 1 and rows[0]["outcome"] == "completed"
    assert rows[0]["id"] == 1
    assert read_trajectories(str(db)) == rows, "str paths read the same file"


def test_an_in_memory_path_is_refused_at_construction() -> None:
    """The repo convention as a guard: a trajectory plane that dies with the process is
    the durability defect verbatim, refused where the wirer is looking."""
    with pytest.raises(ValueError, match="in-memory"):
        Trajectory(":memory:")
