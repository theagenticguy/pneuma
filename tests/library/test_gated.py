"""Offline tests for `gated.py`: the gate as a post-condition, and the beam that filters.

Two claims, and they are checked differently on purpose.

The *sharp* properties of the post-condition — that a rejection raises with a usable message,
that a gate fault does not read as a verdict, that a rejection is recorded and a fault is not —
are asserted by calling `admits` directly, because what needs proving is what it raises. The
*wiring* is then asserted through a real cycle: that the message actually reaches the model as
a retry prompt is a claim about `ai_thread`, not about this code, and only a real thread with a
recording model can settle it.

Retirement is asserted from the coordinator's own registry rather than from
`MethodThread.live`. A wrapper's local liveness flag can desync from the runtime, so the flag
alone would prove the object thinks it is retired — which is the weaker of the two claims and
the one that survives a broken `finally`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any

import pytest
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.gated import Gate, GatedProposer, Verdict
from pneuma.method import ai_method

# ── Fixtures ──
#
# Module level, all of them: `compile_ai_method` resolves annotations with
# `typing.get_type_hints` against module globals, so a function-local output type cannot be
# resolved at compile time.


class Pick(BaseModel):
    """The proposal shape: the candidate, and the reasoning that produced it."""

    value: int = Field(description="The proposed value, which is what the gate judges.")
    evidence: str = Field(description="What led to this value. Not the subject of the gate.")


@dataclass(frozen=True)
class Threshold:
    """A trivial `Verdict`: admitted above a floor, and a report that names the floor."""

    value: int
    floor: int

    @property
    def ok(self) -> bool:
        return self.value >= self.floor

    def report_text(self) -> str:
        return f"value {self.value} is below the floor of {self.floor}"


def floor_gate(floor: int) -> Gate:
    """A gate over the whole `Pick`. Three lines, which is the point of injecting it."""

    def gate(candidate: Pick) -> Threshold:
        return Threshold(candidate.value, floor)

    return gate


def value_gate(floor: int) -> Gate:
    """The same gate over an already-extracted `int`, for the `candidate_of` override."""

    def gate(candidate: int) -> Threshold:
        return Threshold(candidate, floor)

    return gate


class Picker(GatedProposer):
    """The default shape: the response *is* the candidate, so `candidate_of` is untouched."""

    name = "picker"

    @ai_method(Pick, description="Propose a value, judged by the gate", max_attempts=3)
    def propose(self, hint: str) -> Pick:
        """Propose a value given {hint}."""


class ValuePicker(Picker):
    """Overrides `candidate_of` to hand the gate the field, not the whole response."""

    name = "value-picker"

    def candidate_of(self, response: Pick) -> int:
        return response.value


class CollidingPicker(GatedProposer):
    """A proposer whose propose parameter is named `response`, like `admits`'s first one.

    The collision the guard exists to catch: the runtime would fill that slot twice.
    """

    name = "colliding"

    @ai_method(Pick, description="Propose a value", max_attempts=1)
    def propose(self, response: str) -> Pick:
        """Propose a value given {response}."""


class InjectingPicker(GatedProposer):
    """A validator naming a propose parameter *second* — the injection the runtime documents.

    The guard must allow this, or it forbids the one pattern `postcondition.py`'s own docstring
    describes ("if any argument names in the signature of the callable match keys in
    bound_args, the callable also receives those values as keyword arguments").
    """

    name = "injecting"

    def __init__(self, gate: Gate) -> None:
        super().__init__(gate)
        self.hints: list[str] = []

    def admits(self, response: Pick, hint: str) -> None:
        self.hints.append(hint)
        super().admits(response)

    @ai_method(Pick, description="Propose a value", max_attempts=1)
    def propose(self, hint: str) -> Pick:
        """Propose a value given {hint}."""


class Capturing(Model):
    """A `ScriptedModel` that also records the messages each call received.

    `ScriptedModel` is `@final` and its `stream` ignores `messages`, so there is nothing to
    subclass and no history to read back off it. Composition instead, exactly as
    `test_method.py` does it: record what the model was handed, then delegate the response.
    """

    def __init__(self, values: list[int]) -> None:
        super().__init__()
        self._inner = ScriptedModel(
            [Turn(tool_calls=(("Pick", {"value": v, "evidence": f"because {v}"}),)) for v in values]
        )
        self.contexts: list[list[Any]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {"calls": len(self.contexts)}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("scripted turns only")

    def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        self.contexts.append(list(messages))
        return self._inner.stream(messages, *args, **kwargs)

    def prompts(self, call: int) -> list[str]:
        """Every text block the model saw on its `call`-th invocation."""
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


async def live_threads(harness: RuntimeHarness) -> list[str]:
    """Thread ids the *coordinator* still has registered — the runtime's own answer.

    Measured: deregistration is complete by the time `retire()` returns, so no polling is
    needed. `asyncio.sleep(0)` is a yield, not a wait, and is here so this reads as a
    settled-state question rather than a race won by luck.
    """
    await asyncio.sleep(0)
    return [info.thread_id for info in await harness.coordinator.list_threads()]


# ── The protocols ──


def test_the_trivial_verdict_and_gate_satisfy_the_protocols() -> None:
    """Guard the guard: if `Threshold` were not a `Verdict`, every test below would be
    exercising the skeleton against a shape no real gate has to match."""
    assert isinstance(Threshold(1, 5), Verdict)
    assert isinstance(floor_gate(5), Gate)


# ── The post-condition path: refusal is the default, the report is the feedback ──


def test_an_admitted_candidate_passes_quietly_and_records_nothing() -> None:
    """The negative half of the ledger claim: `rejected` only grows on a rejection."""
    picker = Picker(floor_gate(5))
    assert picker.admits(Pick(value=7, evidence="above the floor")) is None
    assert picker.rejected == []


def test_a_rejected_candidate_raises_with_the_report_and_the_reask() -> None:
    """The gate's own report is the message, so the model is told the reason and the action."""
    picker = Picker(floor_gate(5))

    with pytest.raises(AssertionError) as raised:
        picker.admits(Pick(value=1, evidence="below the floor"))

    message = str(raised.value)
    assert "value 1 is below the floor of 5" in message
    assert "Propose a different candidate." in message
    assert len(picker.rejected) == 1
    assert picker.rejected[0].value == 1


def test_every_rejection_lands_in_the_ledger_in_order() -> None:
    """The evidence the gate has teeth: a silently-re-asked loop that later succeeded looks
    identical, from the outside, to a loop whose gate never fired."""
    picker = Picker(floor_gate(5))
    for value in (1, 3, 2):
        with pytest.raises(AssertionError):
            picker.admits(Pick(value=value, evidence="low"))

    assert [verdict.value for verdict in picker.rejected] == [1, 3, 2]


def test_the_reask_suffix_is_overridable_without_touching_the_mechanism() -> None:
    """Domain wording is the subclass's; the placement is the base's."""

    class Loud(Picker):
        REASK = "Propose a different value. Going below the floor removes the only term."

    with pytest.raises(AssertionError, match=r"removes the only term"):
        Loud(floor_gate(5)).admits(Pick(value=0, evidence="x"))


async def test_the_rejection_reaches_the_model_as_the_next_attempts_prompt() -> None:
    """The wiring, asserted from what the *model* received.

    `admits` raising is this module's claim; the message becoming a retry prompt is
    `ai_thread`'s, and only a real cycle settles it. The scripted model proposes a rejected
    value and then an admitted one, so a working post-condition means exactly two calls and
    the second one carrying the first one's report.
    """
    async with RuntimeHarness():
        picker = Picker(floor_gate(5))
        model = Capturing([1, 9])

        result = await picker.gated(model=model)(hint="go")

        assert result.value == 9
        assert len(model.contexts) == 2

        retry = model.prompts(1)
        assert any("[VALIDATION ERROR]" in prompt for prompt in retry)
        assert any("value 1 is below the floor of 5" in prompt for prompt in retry)
        assert any("Propose a different candidate." in prompt for prompt in retry)

        assert [verdict.value for verdict in picker.rejected] == [1]


async def test_gated_keeps_the_gate_first_and_the_callers_own_conditions() -> None:
    """A hand-written `compiled(..., post_conditions=[mine])` drops the gate silently. This
    cannot: the gate is prepended, so an added check is additive rather than a replacement."""

    def also(response: Pick) -> None:
        if not response.evidence:
            raise AssertionError("evidence is required")

    picker = Picker(floor_gate(5))
    conditions = picker.gated(post_conditions=[also]).config.post_conditions

    assert list(conditions) == [picker.admits, also]


# ── The unwanted behaviour: a bug must not wear a verdict's clothes ──


def test_a_fault_in_the_gate_does_not_masquerade_as_a_rejection() -> None:
    """Any exception inside a post-condition is reported to the model as a validation failure.

    So a `KeyError` in the gate looks identical to a refused proposal and burns every retry on
    a bug the model cannot fix. The gate call is wrapped and an internal failure re-raised as
    a message that says it is internal — and, just as load-bearing, it is not recorded as
    evidence against a proposal it never judged.
    """

    def broken(candidate: Pick) -> Threshold:
        raise KeyError("floor")

    picker = Picker(broken)

    with pytest.raises(AssertionError) as raised:
        picker.admits(Pick(value=7, evidence="fine"))

    message = str(raised.value)
    assert "fault in the gate rather than a verdict" in message
    assert "KeyError" in message
    assert picker.rejected == [], "a gate fault is not evidence against the proposal"


def test_a_malformed_verdict_is_a_fault_not_a_rejection() -> None:
    """A gate can return without raising and still hand back garbage — a verdict whose
    `ok` or `report_text` crashes (built around a candidate the gate should have refused,
    say). Reading it is part of judging: the crash must surface as a gate fault, not land
    in `rejected` and not reach the model dressed as a validation message.
    """

    class Broken:
        @property
        def ok(self) -> bool:
            raise TypeError("unsupported format string")

        def report_text(self) -> str:  # pragma: no cover — ok raises first
            return "never reached"

    picker = Picker(lambda candidate: Broken())  # type: ignore[arg-type, return-value]

    with pytest.raises(AssertionError) as raised:
        picker.admits(Pick(value=7, evidence="fine"))

    message = str(raised.value)
    assert "fault in the gate rather than a verdict" in message
    assert "TypeError" in message
    assert picker.rejected == [], "a verdict nobody could read is not evidence"


async def test_a_malformed_verdict_is_a_fault_on_the_async_path_too() -> None:
    """`judge` reads `ok` as well; the same garbage must read the same way there."""

    class Broken:
        @property
        def ok(self) -> bool:
            raise TypeError("unsupported format string")

        def report_text(self) -> str:  # pragma: no cover
            return "never reached"

    picker = Picker(lambda candidate: Broken())  # type: ignore[arg-type, return-value]

    with pytest.raises(AssertionError, match=r"fault in the gate"):
        await picker.judge(Pick(value=7, evidence="fine"))
    assert picker.rejected == []


def test_a_broken_candidate_extractor_is_a_fault_not_a_verdict() -> None:
    """`candidate_of` is a subclass hook, so a typo in an override is exactly as likely as a
    bug in the gate — and without the wrap it would surface as a raw AttributeError that the
    runtime reports to the model as a validation failure, burning every retry."""

    class Typoed(GatedProposer):
        name = "typoed"

        @ai_method(Pick, description="propose")
        def propose(self, hint: str) -> Pick:
            """Pick, given {hint}."""

        def candidate_of(self, response: Pick) -> int:
            return response.weight  # type: ignore[attr-defined] — the typo under test

    picker = Typoed(lambda candidate: Threshold(candidate, 5))

    with pytest.raises(AssertionError) as raised:
        picker.admits(Pick(value=7, evidence="fine"))

    message = str(raised.value)
    assert "fault in the candidate extractor" in message
    assert "AttributeError" in message
    assert picker.rejected == []


def test_an_extra_post_condition_gets_the_collision_guard_too() -> None:
    """`gated()` advertises extras as the safe wiring path; an extra whose first parameter
    shadows a propose parameter hits the same double-fill TypeError the admits guard exists
    for, so the guard must cover it."""

    def extra(hint: object) -> None:  # first param collides with propose's `hint`
        return None

    picker = Picker(lambda candidate: Threshold(candidate.value, 5))

    with pytest.raises(RuntimeError, match=r"'extra'.*'hint'|'hint'.*'extra'"):
        picker.gated("propose", post_conditions=[extra])


async def test_judge_refuses_a_rejection_whose_report_cannot_be_rendered() -> None:
    """The beam path must not admit an unreadable rejection into the ledger: rendering it
    later (a training summary, say) would crash far from the gate that produced it."""

    class HalfBroken:
        ok = False

        def report_text(self) -> str:
            raise TypeError("unsupported format string")

    picker = Picker(lambda candidate: HalfBroken())  # type: ignore[arg-type, return-value]

    with pytest.raises(AssertionError, match=r"fault in the gate"):
        await picker.judge(Pick(value=1, evidence="x"))
    assert picker.rejected == [], "an unreadable rejection is not evidence"


def test_an_async_gate_on_the_sync_path_is_refused_rather_than_silently_admitted() -> None:
    """A coroutine is truthy, so `not verdict.ok` on one would admit everything the gate was
    supposed to judge — a gate that appears to be wired and refuses nothing."""

    async def slow(candidate: Pick) -> Threshold:
        return Threshold(candidate.value, 5)

    with pytest.raises(AssertionError, match=r"fault in the wiring"):
        Picker(slow).admits(Pick(value=0, evidence="x"))


async def test_an_async_gate_works_on_the_async_path() -> None:
    """The other half: `judge` is where an async gate is honoured."""

    async def slow(candidate: Pick) -> Threshold:
        await asyncio.sleep(0)
        return Threshold(candidate.value, 5)

    picker = Picker(slow)
    assert (await picker.judge(Pick(value=9, evidence="x"))).ok is True
    assert (await picker.judge(Pick(value=1, evidence="x"))).ok is False
    assert [verdict.value for verdict in picker.rejected] == [1]


async def test_a_fault_in_an_async_gate_reads_the_same_way() -> None:
    """One wording for "the gate broke", whichever path found it."""

    async def broken(candidate: Pick) -> Threshold:
        raise KeyError("floor")

    picker = Picker(broken)
    with pytest.raises(AssertionError, match=r"fault in the gate rather than a verdict"):
        await picker.judge(Pick(value=7, evidence="x"))
    assert picker.rejected == []


# ── The collision guard: a convention turned into a library guarantee ──


def test_a_colliding_propose_parameter_is_refused_at_wiring_time() -> None:
    """The runtime would pass the result positionally and `response` by keyword, raise
    `TypeError: got multiple values`, and report *that* to the model as a validation failure —
    so the gate appears to reject everything and the message names nothing a model can fix.

    Refused at wiring time instead, naming both sides, because the alternative is a silent
    failure whose fix is a one-word rename that nothing points at.
    """
    picker = CollidingPicker(floor_gate(5))

    with pytest.raises(RuntimeError) as raised:
        picker.gated()

    message = str(raised.value)
    assert "'response'" in message
    assert "'propose'" in message


async def test_the_collision_guard_also_covers_the_beam_path() -> None:
    """Both wiring entry points, or the guarantee is one call away from not applying."""
    async with RuntimeHarness() as harness:
        picker = CollidingPicker(floor_gate(5))
        with pytest.raises(RuntimeError, match=r"'response'"):
            await picker.propose_k(2, harness.coordinator, response="go")
        assert await live_threads(harness) == []


async def test_a_later_validator_parameter_is_injected_not_forbidden() -> None:
    """The guard checks the first parameter only, and that narrowness is deliberate.

    A validator naming a propose parameter after the result is handed that bound argument by
    keyword — the runtime documents it and it is useful. Forbidding every name would outlaw
    the one pattern `postcondition.py` describes, so this asserts the injection really happens
    rather than merely that the guard stayed quiet.
    """
    async with RuntimeHarness():
        picker = InjectingPicker(floor_gate(5))
        result = await picker.gated(model=Capturing([9]))(hint="the injected hint")

        assert result.value == 9
        assert picker.hints == ["the injected hint"]


# ── candidate_of: the base cannot know which field is the candidate ──


def test_the_default_candidate_is_the_whole_response() -> None:
    picker = Picker(floor_gate(5))
    response = Pick(value=7, evidence="x")
    assert picker.candidate_of(response) is response


def test_an_override_hands_the_gate_the_field_instead() -> None:
    """`HarnessProposer`'s shape: the proposal carries its own evidence, and the evidence is
    the auditable artifact rather than the subject of the gate."""
    picker = ValuePicker(value_gate(5))
    assert picker.candidate_of(Pick(value=7, evidence="x")) == 7

    assert picker.admits(Pick(value=7, evidence="x")) is None
    with pytest.raises(AssertionError, match=r"value 1 is below the floor"):
        picker.admits(Pick(value=1, evidence="x"))
    assert [verdict.value for verdict in picker.rejected] == [1]


async def test_the_override_reaches_the_beam_path_too() -> None:
    """Both paths extract through the same hook, so a subclass overrides it once."""
    async with RuntimeHarness() as harness:
        picker = ValuePicker(value_gate(5))
        picker.compiled = _scripting(picker, Capturing([9, 9]))  # type: ignore[method-assign]

        admitted = await picker.propose_k(2, harness.coordinator, hint="go")

        assert [candidate for candidate, _ in admitted] == [9, 9]
        assert await live_threads(harness) == []


# ── The beam path: k one-shot branches, then filter ──


def _scripting(agent: GatedProposer, model: Model) -> Any:
    """Bind a scripted model by replacing `compiled` on the *instance*.

    `spawn` compiles through `self.compiled` precisely so this reaches live threads; going to
    `compile_ai_method` directly would silently bypass the binding and reach a real model.
    """
    original = type(agent).compiled

    def compiled(name: str, **overrides: Any) -> Any:
        overrides.setdefault("model", model)
        return original(agent, name, **overrides)

    return compiled


async def test_propose_k_returns_every_admitted_branch_and_records_the_rest() -> None:
    """k branches, one proposal each, filtered by the gate — and all threads retired.

    The three scripted values are one rejection and two admissions, so a working beam returns
    two pairs and leaves exactly one verdict in `rejected`. Retirement is asserted from the
    coordinator's registry, which is the runtime's answer rather than this code's.
    """
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        model = Capturing([1, 7, 9])
        picker.compiled = _scripting(picker, model)  # type: ignore[method-assign]

        admitted = await picker.propose_k(3, harness.coordinator, hint="go")

        assert [candidate.value for candidate, _ in admitted] == [7, 9]
        assert all(verdict.ok for _, verdict in admitted)
        assert [verdict.value for verdict in picker.rejected] == [1]
        assert len(model.contexts) == 3, "one shot per branch, no post-condition retries"
        assert await live_threads(harness) == []


async def test_propose_k_attaches_no_post_condition_so_k_stays_a_count_of_branches() -> None:
    """The contrast with the single-thread path, asserted rather than documented.

    Every branch proposes a rejected value. A post-condition on the branches would retry each
    one up to `max_attempts` and the model would be called far more than k times; one shot per
    branch means exactly k calls and an empty result.
    """
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        model = Capturing([1, 2, 3])
        picker.compiled = _scripting(picker, model)  # type: ignore[method-assign]

        admitted = await picker.propose_k(3, harness.coordinator, hint="go")

        assert admitted == []
        assert len(model.contexts) == 3
        assert [verdict.value for verdict in picker.rejected] == [1, 2, 3]
        assert await live_threads(harness) == []


async def test_every_branch_inherits_the_seed_cycle_and_only_its_own_turn() -> None:
    """Why the seed is a cycle and not a `notify`.

    A pending `notify` is worker-side inject state and `fork` copies the log, so a branch never
    sees it — measured, and the reason this signature takes cycles. A seed *cycle* run before
    the fork reaches every branch's first model call, and each branch's own turn reaches only
    that branch. Four calls: one seed, three branches.
    """
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        model = Capturing([9, 9, 9, 9])
        picker.compiled = _scripting(picker, model)  # type: ignore[method-assign]

        admitted = await picker.propose_k(
            3, harness.coordinator, hint="BRANCH", seed=[{"hint": "SEEDEVIDENCE"}]
        )

        assert len(admitted) == 3
        assert len(model.contexts) == 4

        assert not any("BRANCH" in prompt for prompt in model.prompts(0))
        for call in (1, 2, 3):
            prompts = model.prompts(call)
            assert any("SEEDEVIDENCE" in prompt for prompt in prompts), call
            assert any("BRANCH" in prompt for prompt in prompts), call

        assert await live_threads(harness) == []


async def test_propose_k_of_one_runs_the_original_thread_and_forks_nothing() -> None:
    """Branch 0 is the spawned thread, so k=1 is a single one-shot proposal."""
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        model = Capturing([9])
        picker.compiled = _scripting(picker, model)  # type: ignore[method-assign]

        admitted = await picker.propose_k(1, harness.coordinator, hint="go")

        assert [candidate.value for candidate, _ in admitted] == [9]
        assert len(model.contexts) == 1
        assert await live_threads(harness) == []


async def test_propose_k_refuses_a_beam_narrower_than_one_thread() -> None:
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        with pytest.raises(ValueError, match=r"at least one branch"):
            await picker.propose_k(0, harness.coordinator, hint="go")
        assert await live_threads(harness) == []


async def test_propose_k_retires_every_thread_when_the_gate_faults_mid_beam() -> None:
    """The `finally`, and the reason it is one.

    The gate breaks on the second branch, after three threads exist. The documented gate-fault
    error propagates — a bug does not become a verdict just because it happened inside a beam —
    and every thread is still gone, asserted through the coordinator. A leak here would be
    invisible in the return value and would only surface later as threads nothing owns.
    """
    calls: list[int] = []

    def breaks_on_the_second(candidate: Pick) -> Threshold:
        calls.append(candidate.value)
        if len(calls) == 2:
            raise KeyError("floor")
        return Threshold(candidate.value, 5)

    async with RuntimeHarness() as harness:
        picker = Picker(breaks_on_the_second)
        picker.compiled = _scripting(picker, Capturing([9, 9, 9]))  # type: ignore[method-assign]

        with pytest.raises(AssertionError, match=r"fault in the gate rather than a verdict"):
            await picker.propose_k(3, harness.coordinator, hint="go")

        assert len(calls) == 2, "the beam stopped at the fault rather than continuing"
        assert picker.rejected == []
        assert await live_threads(harness) == []


async def test_propose_k_retires_every_thread_when_a_branch_cycle_fails() -> None:
    """The other unwind: the failure is in the runtime, not the gate.

    The model has fewer turns than there are branches, so the third branch's cycle raises.
    Same requirement — nothing left running — and it exercises the `finally` from a path that
    never reaches the gate at all.
    """
    async with RuntimeHarness() as harness:
        picker = Picker(floor_gate(5))
        picker.compiled = _scripting(picker, Capturing([9, 9]))  # type: ignore[method-assign]

        with pytest.raises(Exception):  # noqa: B017,PT011 — the runtime's own error, whatever it is
            await picker.propose_k(3, harness.coordinator, hint="go")

        assert await live_threads(harness) == []


async def test_the_beam_shares_a_coordinator_with_its_caller() -> None:
    """`AIFunction.spawn()` with no coordinator builds a private one per call, which would put
    the beam outside the caller's registry — invisible to peers, and invisible to the
    retirement assertions every test above depends on. So the threads must be observable on
    the harness's coordinator *while the beam runs*, not only absent afterwards."""
    seen: list[int] = []

    async def counting(candidate: Pick) -> Threshold:
        seen.append(len(await harness.coordinator.list_threads()))
        return Threshold(candidate.value, 5)

    async with RuntimeHarness() as harness:
        picker = Picker(counting)
        picker.compiled = _scripting(picker, Capturing([9, 9]))  # type: ignore[method-assign]

        await picker.propose_k(2, harness.coordinator, hint="go")

        assert seen == [2, 2], "the beam's threads are not on the caller's coordinator"
        assert await live_threads(harness) == []
