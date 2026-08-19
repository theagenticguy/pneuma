"""The team-learning path: guidance as a gradient target, one hook and one `train`.

`casestudy/learning.py` proved the loop — run, observe, phrase feedback, let
`TextGradOptimizer` rewrite the guidance — for a single navigator. This module lifts the
same shape onto a team with the smallest possible surface: a `Learning` hook that recalls
the lead's guidance from a memory backend and folds it into the request, `traced_result`
to turn one finished `TeamRun` into the `Result` graph the optimizer consumes, and
`train(team, cases)` to drive a batch and take one optimizer step. It is the paved road,
not a framework: one prose parameter, one step per batch, nothing configurable that the
case study did not prove necessary.

**How the gradient edge survives a run the core never traced.** `AIFunction.trace` cannot
be used here — the core runs the lead through its live `ThreadHandle`, and a handle run
does not scan arguments for dataflow handles. So the hook reproduces exactly what `trace`
does, split across the run boundary (`ai_function.py:333-392` in the installed package):
`on_assemble` recalls the guidance under `no_thread_scope()` — explicitly, because when
`team.run` is itself called from inside a live cycle the ambient scope would emit the
recall event against the *caller's* thread and the edge would silently die
(`.erpaval/solutions/ai-functions-runtime/recall-injection-and-marker-traps.md`) — leaving
a `ParameterView` with `emitted=False`; `traced_result` later scans the kept inputs by
**identity** (`collect_nodes`) and emits the recall event against the lead thread's
surviving log (`emit_recall` works after teardown; the event log outlives the thread —
measured, T6). Store an f-string of the view instead of the view and `collect_nodes` finds
nothing, nothing is emitted, and `graph.parameters == []` with no error anywhere — which
is why the view object itself is what rides in `hooks_data`, and the *rendered text* only
ever appears in the prompt, where identity does not matter.

**Fresh recall per run, so the first-trace trap cannot happen.** A `ParameterView` is
emitted once — one logical recall, one event — so a view reused across a batch yields a
parameter node on the first traced run and none after (`casestudy/learning.py:302-304`).
Here every `on_assemble` recalls anew into per-run `Workspace.data`, so *every* run's
`Result` carries its own live edge and `train` may keep whichever it likes; it keeps the
last, which saw the newest guidance.

**Guidance is advice, never structure, and never code.** Only a prose parameter is
learnable: a `Procedural`-marked field is reusable *code* with sandbox semantics
(`memory/procedural.py`) and is refused at construction — the same refusal
`casestudy.Playbook` writes in prose; a `Frozen` field cannot receive a gradient, so
accepting it would produce a training loop that reports rounds and learns nothing, and it
is refused in the same place. Anything structural about the team — the cast, the tools,
the caps — lives in code the optimizer cannot reach.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_functions import TextGradOptimizer
from ai_functions.memory.base import MemoryBackend
from ai_functions.types import Result, no_thread_scope
from ai_functions.types.graph import ParameterView, collect_nodes

from ..core import Team, TeamRun, Workspace

__all__ = ["Learning", "TrainingRound", "compose_feedback", "traced_result", "train"]


def render_guidance(value: Any) -> str:
    """Render a recalled guidance value for the prompt, saying plainly when it is empty.

    The empty case matters: a "Guidance learned from previous runs:" heading over nothing
    reads to a model as a section to fill in, and an operator reading the transcript could
    not tell an empty parameter from a dropped one (`casestudy/learning.py`,
    `render_advice`).
    """
    if isinstance(value, (list, tuple)):
        rendered = "\n".join(f"- {entry}" for entry in value)
    else:
        rendered = str(value).strip()
    return rendered or "(no guidance learned yet)"


class Learning:
    """A hook that makes the lead's guidance a gradient target.

    On every run: recall `parameter` from `memory` (fresh, outside any thread scope),
    append its rendered text to the request the lead is asked, and leave the live
    `ParameterView` in `hooks_data["learning"]` so `traced_result` can finish the trace.
    The guidance rides the **request fold**, not a system frame: `on_request` is the seam
    every hook already uses, it needs no reach into the lead's compiled config, and the
    delivery is wire-testable from the lead model's own context — a system-frame variant
    would be invisible to the same assertion.

    A run never handed to `traced_result` pays nothing beyond the storage read: the recall
    emits no event (`emitted=False` until composition) and a `JSONMemoryBackend` fetch is
    model-free, so the hooked team's model-call count equals the bare team's.

    Args:
        memory: The backend holding the guidance. Its `backend_id` is what the optimizer
            matches recall events against, so `train` consolidates into this object.
        parameter: The schema field the guidance lives in. Must be prose — a
            `Procedural`-marked field is refused (code is not advice), a `Frozen` one is
            refused (a target that cannot learn makes the loop a lie).
    """

    def __init__(self, memory: MemoryBackend, parameter: str = "guidance") -> None:
        try:
            memory._resolve_field(parameter)  # noqa: SLF001 — schema introspection, ParameterHost surface
        except KeyError:
            raise RuntimeError(
                f"Learning: {parameter!r} is not a parameter of {memory.schema.__name__}"
                f" — the schema declares {memory._leaf_parameter_names()}"  # noqa: SLF001
            ) from None
        if memory._is_procedural(parameter):  # noqa: SLF001
            raise RuntimeError(
                f"Learning: {parameter!r} is Procedural — code, not advice. Guidance must be "
                f"a prose parameter; structural or executable behaviour stays in reviewed "
                f"code where the optimizer cannot reach it."
            )
        if memory._is_frozen(parameter):  # noqa: SLF001
            raise RuntimeError(
                f"Learning: {parameter!r} is Frozen, so no gradient can ever land on it — "
                f"training against it would report rounds and learn nothing. Unfreeze it or "
                f"pick the parameter that is meant to learn."
            )
        self.memory = memory
        self.parameter = parameter

    async def on_assemble(self, work: Workspace) -> None:
        """Recall the guidance fresh for this run and stage the trace ingredients.

        `no_thread_scope()` is load-bearing: inside a live cycle the ambient scope would
        emit the recall event against the caller's thread, mark the view `emitted=True`,
        and the deferred emission in `traced_result` would no-op — a dead edge with every
        offline test still green (the recall lesson, verified by probe here too).
        """
        with no_thread_scope():
            view = await self.memory.recall(self.parameter)
        work.data["learning"] = {
            "view": view,
            "coordinator": work.coordinator,
            "thread_id": work.lead.id,
            "parameter": self.parameter,
        }

    def on_request(self, work: Workspace, request: str) -> str:
        view: ParameterView[Any] = work.data["learning"]["view"]
        return f"{request}\n\nGuidance learned from previous runs:\n{render_guidance(view.value)}"

    def __repr__(self) -> str:
        return f"<Learning {self.parameter!r} on {self.memory.backend_id}>"


async def traced_result(run: TeamRun) -> Result[Any]:
    """Finish the trace for one `Learning`-hooked run: emit the edge, return the graph root.

    The tail of `AIFunction.trace`, performed after the fact: scan the staged inputs by
    identity, emit the recall event for every not-yet-emitted `ParameterView` against the
    lead thread's log (which outlives the thread), and wrap the answer in a `Result`
    carrying the same provenance a traced call would. `build_graph_from_result` — and so
    `TextGradOptimizer.step` — then reconstructs the lead's node with the guidance
    `ParameterNode` attached, exactly as if the lead had been run via `trace`.

    Raises when the run carries no learning record, because a `Result` with no parameter
    node is not an error anywhere downstream — the optimizer would walk an empty graph and
    consolidate nothing, silently.
    """
    staged = run.hooks_data.get("learning")
    if staged is None:
        raise RuntimeError(
            "traced_result: this run carries no hooks_data['learning'] — the team was not "
            "assembled with a Learning hook, so there is no gradient edge to finish."
        )
    inputs = collect_nodes(staged["view"])
    if not inputs:
        raise RuntimeError(
            "traced_result: hooks_data['learning']['view'] holds no ParameterView — the "
            "view's identity was lost (interpolated into a string?), so the gradient edge "
            "is already dead and the optimizer would silently update nothing."
        )
    for node in inputs:
        if isinstance(node, ParameterView):
            await node.backend.emit_recall(node, staged["coordinator"], staged["thread_id"])
    return Result(
        value=run.answer,
        coordinator=staged["coordinator"],
        thread_id=staged["thread_id"],
        inputs=inputs,
    )


@dataclass
class TrainingRound:
    """One `train` call: the runs, the feedback that was routed, and what changed."""

    runs: list[TeamRun] = field(default_factory=list)
    feedback: str = ""
    guidance_before: Any = None
    guidance_after: Any = None

    @property
    def changed(self) -> bool:
        """Whether the step actually rewrote the guidance — the loop's honest outcome."""
        return self.guidance_before != self.guidance_after


def compose_feedback(runs: Sequence[TeamRun]) -> str:
    """Turn a batch's review findings and revise-loop outcomes into behavioural feedback.

    Reads what the run actually recorded: review entries (`hooks_data["review"]`, the
    shape `Critic`/`Council` write) that found something or errored, and the core's own
    `revise` / `revise_cap` transcript entries — the feedback that was really put to the
    lead, plus the fact that a cap (not a clean review) ended a loop. When nothing
    objected, it says so and asks for no additions: an instruction to improve anyway
    teaches the consolidator to grow the parameter without a measured reason.
    """
    findings: list[str] = []
    for index, run in enumerate(runs):
        label = f"case {index + 1}"
        for entry in run.hooks_data.get("review", []):
            outcome = entry.get("outcome", "" if entry.get("accepted", True) else "findings")
            if outcome in ("findings", "error"):
                detail = entry.get("review") or ", ".join(
                    f"{name}: {text}"
                    for name, text in entry.get("reviews", {}).items()
                    if name not in entry.get("approved", [])
                )
                findings.append(f"{label}: review found problems — {detail}")
        for entry in run.transcript:
            if entry["kind"] == "revise":
                findings.append(f"{label}: the lead was asked to revise — {entry['feedback']}")
            elif entry["kind"] == "revise_cap":
                findings.append(
                    f"{label}: the revision cap ({entry['rounds']} rounds) ended review "
                    f"before the answer satisfied {entry['hook']} — the guidance did not "
                    f"prevent the problem, only the budget stopped the loop."
                )
    if not findings:
        return (
            "Every case passed review without revision. Keep the guidance that produced "
            "these answers; do not add new advice, since nothing was observed to be missing."
        )
    observed = "\n".join(f"- {finding}" for finding in findings)
    return (
        f"Across {len(runs)} case(s) the team's answers drew these objections:\n{observed}\n"
        "Sharpen the guidance so the lead avoids these specific problems on the first "
        "draft; update the advice that should have prevented them rather than adding "
        "near-duplicates beside it."
    )


async def train(
    team: Team,
    cases: Sequence[str],
    feedback_fn: Callable[[Sequence[TeamRun]], str] | None = None,
    *,
    coordinator: Any = None,
    optimizer: TextGradOptimizer | None = None,
) -> TrainingRound:
    """Run `cases` through the team and take one optimizer step over the kept trace.

    Each case is an ordinary `team.run` — hooks, members, the answer loop, all of it —
    and each run's gradient edge is finished by `traced_result`. The step lands on the
    **last** case's trace: every run recalls its own view (see `Learning.on_assemble`),
    so every trace carries a live parameter node and the last one saw the newest
    guidance. Feedback defaults to `compose_feedback` over the whole batch, so review
    findings from every case route through the one kept graph — the same shape
    `casestudy.train` proved (`learning.py:463-465`).

    Args:
        team: A team carrying exactly one `Learning` hook. Zero is refused (nothing to
            train); two is refused rather than picking one silently.
        cases: The batch of requests. Empty is refused — a step over no trace is not a
            small training run, it is a no-op wearing one's name.
        feedback_fn: Turns the batch's runs into the feedback string for the backward
            pass. `None` means `compose_feedback`.
        coordinator: One coordinator for the whole batch; `None` stands up a private
            harness pair for the call, mirroring `Team.run`'s convenience path. The
            traced graphs are read back from it, so it must be the one the runs used.
        optimizer: The stepper. `None` builds a real `TextGradOptimizer`; tests inject a
            scripted one because the backward model's target ids carry random suffixes
            (`optimizer/_formatting.py:14-16`) that no fixed script can name.
    """
    learners = [hook for hook in team.hooks if isinstance(hook, Learning)]
    if len(learners) != 1:
        raise RuntimeError(
            f"train: the team carries {len(learners)} Learning hooks; training needs "
            f"exactly one guidance parameter to route feedback into."
        )
    if not cases:
        raise RuntimeError("train: no cases — a step over no trace would learn nothing.")
    if coordinator is None:
        from ai_functions import InMemoryCoordinator, LocalWorker

        coordinator = InMemoryCoordinator()
        worker = LocalWorker(coordinator)
        await worker.register()
        try:
            return await train(
                team, cases, feedback_fn, coordinator=coordinator, optimizer=optimizer
            )
        finally:
            await worker.close()

    learning = learners[0]
    stepper = optimizer or TextGradOptimizer()
    round_result = TrainingRound(guidance_before=learning.memory.fetch(learning.parameter))

    kept: Result[Any] | None = None
    for case in cases:
        run = await team.run(case, coordinator)
        round_result.runs.append(run)
        kept = await traced_result(run)

    assert kept is not None  # cases is non-empty, so at least one run happened
    round_result.feedback = (feedback_fn or compose_feedback)(round_result.runs)
    await stepper.step(kept, round_result.feedback, backends=[learning.memory])
    round_result.guidance_after = learning.memory.fetch(learning.parameter)
    return round_result
