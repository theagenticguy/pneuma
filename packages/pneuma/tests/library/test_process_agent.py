"""Offline tests for `process/agent.py`: the interpreter hook, the dispatch, and the guards.

Three kinds of claim, checked three different ways on purpose.

The *hook* is a claim about `interpreter.run`, so it is asserted against `interpreter.run`
directly with a recording callable — not through `ProcessAgent`. Which states a run occupies
is the interpreter's business, and routing the assertion through the agent would let a
dispatch bug and a hook bug hide behind each other.

The *dispatch* is a claim about which states cost a model call, so every test that says
"this state is free" counts calls with a `Counting` model rather than trusting a return of
None. A dispatcher that resolved correctly and then called the model anyway would satisfy
every assertion about `on_result` and still spend the money the design exists to save.

The *guards* are checked by counting model calls too, never by matching a message. A guard
that raises after spending what it protects is half a guard, so the collision test scripts
`Counting([])`: had the refusal come late, the run would raise `ScriptExhausted` instead of
the error under test and the test would fail.

`Counting` composes rather than subclasses — `ScriptedModel` is `@final` — and every fixture
type is module level, because `compile_ai_method` resolves annotations with
`typing.get_type_hints` against module globals (`method.py:146`) and a function-local output
model cannot be resolved at compile time.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import pytest
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.method import ai_method
from pneuma.process import interpreter
from pneuma.process.agent import HandlerFailed, ProcessAgent
from pneuma.process.agent_driver import Navigator
from pneuma.process.ir import Effect, Guard, Invariant, Process, State, Transition, Variable

# ── The processes ──


def filing() -> Process:
    """A corridor, a branch, and three states that name handlers.

    Deliberately shaped so every claim in this file has a place to stand. `Intake → Checked`
    is deterministic, so it is where "the decider is not consulted" and "the corridor still
    gets its work" are both visible. `Checked` branches on `band`, which is where the
    decision and the rejection loop live. `Filed` names a handler *and* is entered from two
    different branches. `Logged` carries `agent_method="handle"` — the mined placeholder
    (`casestudy/miner.py:125`) that resolves to nothing on any agent — and `Refused` carries
    None, so the two ways a state can be free are both exercised.
    """
    return Process(
        name="Filing",
        description="A filing from intake to a decision",
        initial_state="Intake",
        states=[
            State(name="Intake", description="Read the filing", agent_method="check"),
            State(name="Checked", description="Route by band", agent_method=None),
            State(name="Filed", description="Record the decision", agent_method="record"),
            State(name="Logged", description="Mined placeholder", agent_method="handle"),
            State(name="Refused", terminal=True),
            State(name="Done", terminal=True),
        ],
        variables=[
            Variable(name="records", low=0, high=3, initial=0),
            # No `initial`: which band a filing falls in comes from the filing.
            Variable(name="band", values=["simple", "complex"]),
        ],
        transitions=[
            Transition(name="Read", source="Intake", target="Checked"),
            Transition(
                name="RouteSimple",
                source="Checked",
                target="Filed",
                guards=[
                    Guard(
                        variable="band",
                        op="eq",
                        value="simple",
                        stated_as="a simple filing is recorded directly",
                    )
                ],
            ),
            Transition(
                name="RouteComplex",
                source="Checked",
                target="Filed",
                guards=[
                    Guard(
                        variable="band",
                        op="eq",
                        value="complex",
                        stated_as="a complex filing is recorded with a note",
                    )
                ],
            ),
            # Unguarded, so `Checked` is a real branch under *either* band — otherwise the
            # simple corridor would have one enabled move there and the fast path would
            # silently swallow the decision every test in this file scripts.
            Transition(name="Reject", source="Checked", target="Refused"),
            Transition(
                name="Record",
                source="Filed",
                target="Logged",
                effects=[Effect(variable="records", increment=1)],
            ),
            Transition(name="Close", source="Logged", target="Done"),
        ],
    )


def corridor() -> Process:
    """No branch anywhere: every step is deterministic, so no decision is ever needed.

    The fixture that makes "the decider is consulted only at branches" falsifiable, and the
    one that shows the corridor's work still happens. `B` names a handler; `A` does not.
    """
    return Process(
        name="Corridor",
        description="Three states in a line",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="The only worked state", agent_method="check"),
            State(name="C", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoC", source="B", target="C"),
        ],
    )


def cycling() -> Process:
    """`A → B → C → B → D`, so a state can be occupied twice in one run.

    Copied in shape from `test_process.py:820`'s `revisiting()` on purpose: the history
    semantics it pins are the ones the hook has to agree with.
    """
    return Process(
        name="Cycle",
        description="A cycle the agent can be talked out of",
        initial_state="A",
        states=[
            State(name="A", description="Start"),
            State(name="B", description="Branch"),
            State(name="C", description="Detour"),
            State(name="D", terminal=True),
        ],
        transitions=[
            Transition(name="AtoB", source="A", target="B"),
            Transition(name="BtoC", source="B", target="C"),
            Transition(name="BtoD", source="B", target="D"),
            Transition(name="CtoB", source="C", target="B"),
        ],
    )


def colliding() -> Process:
    """A process whose state names the decider as its per-state handler."""
    return Process(
        name="Collide",
        description="A state that names the decider",
        initial_state="Start",
        states=[
            State(name="Start", description="Names the decider", agent_method="choose"),
            State(name="End", terminal=True),
        ],
        transitions=[Transition(name="Finish", source="Start", target="End")],
    )


def stuck() -> Process:
    """A guard that never holds, so the initial state deadlocks."""
    return Process(
        name="Stuck",
        description="Nowhere to go",
        initial_state="A",
        states=[State(name="A", agent_method="check"), State(name="B", terminal=True)],
        variables=[Variable(name="flag", low=0, high=1, initial=0)],
        transitions=[
            Transition(
                name="OnlyWhenSet",
                source="A",
                target="B",
                guards=[Guard(variable="flag", op="eq", value=1)],
            )
        ],
    )


def claims() -> Process:
    """`test_process.py`'s claims process, trimmed to what the Navigator tests need.

    A local copy rather than an import: `test_process.py` is the frozen oracle and importing
    a helper out of it would couple the two files, so that a change here could only be made
    by editing the file this one exists to leave alone.
    """
    return Process(
        name="ClaimsIntake",
        description="Insurance claim from intake to settlement",
        initial_state="Intake",
        states=[
            State(name="Intake", description="Read the claim", agent_method="extract"),
            State(name="Triage", description="Route by size", agent_method="triage"),
            State(name="Escalated", description="Senior reviews", agent_method="review"),
            State(name="Paid", terminal=True),
            State(name="Denied", terminal=True),
        ],
        variables=[
            Variable(name="approvals", low=0, high=3, initial=0),
            Variable(name="amount_band", values=["small", "large"]),
        ],
        transitions=[
            Transition(name="Extract", source="Intake", target="Triage"),
            Transition(
                name="RouteLarge",
                source="Triage",
                target="Escalated",
                guards=[
                    Guard(
                        variable="amount_band",
                        op="eq",
                        value="large",
                        stated_as="large claims need senior review",
                    )
                ],
            ),
            Transition(
                name="SeniorApprove",
                source="Escalated",
                target="Paid",
                effects=[Effect(variable="approvals", increment=2)],
            ),
            Transition(name="SeniorReject", source="Escalated", target="Denied"),
        ],
        invariants=[
            Invariant(
                name="LargeNeedsTwoApprovals",
                stated_as="a large claim is never paid on fewer than two approvals",
                forbidden_state="Paid",
                forbidden_when=[
                    Guard(variable="amount_band", op="eq", value="large"),
                    Guard(variable="approvals", op="lt", value=2),
                ],
            )
        ],
    )


# ── The agent, and the output models its handlers return ──
#
# Module level, all of them: `compile_ai_method` resolves annotations against module
# globals, so a function-local output type cannot be resolved at compile time.


class CheckFinding(BaseModel):
    """What checking a filing produced. Named for the tool call the scripted turns make."""

    verdict: str = Field(description="Whether the filing may proceed")
    note: str = Field(default="", description="One observation")


class Recorded(BaseModel):
    """What recording a decision produced. A second type, because one per capability."""

    reference: str = Field(description="The reference the decision was filed under")


class Clerk(ProcessAgent):
    """Two typed handlers and the default hooks, plus a recording `on_result`.

    `on_result` is `async` deliberately. A base that only called sync hooks would turn this
    override into a silent no-op — coroutine never awaited, paperwork never written, run
    still reporting a completed case — so the fixture is written in the shape that would
    expose it.
    """

    def __init__(self, process: Process, *, context: str = "") -> None:
        super().__init__(process, context=context)
        self.filed: list[tuple[str, Any]] = []
        """One `(state name, result)` per handler that ran, in path order."""

    @ai_method(CheckFinding, description="Check a filing for completeness", max_attempts=1)
    def check(self, focus: str = "completeness") -> CheckFinding:
        """Check this filing, focusing on {focus}.

        Context: {self.context}
        """

    @ai_method(Recorded, description="Record a decision against a filing", max_attempts=1)
    def record(self) -> Recorded:
        """Record the decision on this filing. Context: {self.context}"""

    async def on_result(self, state: State, result: Any) -> None:
        self.filed.append((state.name, result))


class ArgumentClerk(Clerk):
    """Overrides `arguments_for` only, inheriting the `agent_method` resolution."""

    def arguments_for(self, state: State) -> dict[str, Any]:
        if state.agent_method == "check":
            return {"focus": f"the {state.name} step"}
        return {}


class TableClerk(Clerk):
    """Overrides `handler_for` with a table keyed by state name — Wave 2's shape.

    `agent_method` is the opt-in gate and the table is the source of truth, which is exactly
    `casestudy/handlers.handler_for` (`handlers.py:155`). Here to prove the library needs to
    know nothing about tables for that override to work.
    """

    TABLE: dict[str, tuple[str, dict[str, Any]]] = {
        "Intake": ("check", {"focus": "the table said so"}),
        "Logged": ("record", {}),
    }

    def handler_for(self, state: State) -> tuple[str, dict[str, Any]] | None:
        if state.agent_method is None:
            return None
        return self.TABLE.get(state.name)


class SelfChoosingClerk(Clerk):
    """An `arguments_for` that happens to supply `choose`'s parameters.

    The silent half of the collision, and the only shape in which the guard is measurable.
    Under the default `arguments_for` a state naming `choose` dies on the signature bind
    with or without the guard; here the dispatch *succeeds*, spends a turn, hands
    `on_result` a `Choice` the interpreter never sees, and the run walks on.
    """

    def arguments_for(self, state: State) -> dict[str, Any]:
        if state.agent_method == "choose":
            return {"state": state.name, "options": "anything", "facts": "anything"}
        return {}


class BrokenClerk(Clerk):
    """A handler whose *body* raises, which is the fault path a scripted model cannot reach."""

    @ai_method(CheckFinding, description="A handler that cannot run", max_attempts=1)
    def check(self, focus: str = "completeness") -> CheckFinding:
        raise ZeroDivisionError("the handler body is wrong")


class BrokenRecorder(Clerk):
    """An `on_result` that raises. A fault in the paperwork is still a fault."""

    async def on_result(self, state: State, result: Any) -> None:
        raise KeyError("the case file is not open")


class BrokenResolver(Clerk):
    """A `handler_for` override that raises — the hook `gated.py` forgot to wrap."""

    def handler_for(self, state: State) -> tuple[str, dict[str, Any]] | None:
        raise AttributeError("typo in the override")


# ── The models ──


class Counting(Model):
    """A scripted model that reports how many times it was called and with what.

    `ScriptedModel` is `@final` and its `stream` ignores `messages`, so there is nothing to
    subclass — composition, exactly as `test_recall.py:244` and `test_gated.py` do it.
    `Counting([])` is the load-bearing case: a guard that raises before the model call leaves
    `contexts == []`, and one that raises after it raises `ScriptExhausted` instead of the
    error under test.
    """

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self._inner = ScriptedModel(turns)
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


def choice(transition: str) -> Turn:
    return Turn(tool_calls=(("Choice", {"transition": transition, "reason": "because"}),))


def finding(verdict: str = "complete") -> Turn:
    return Turn(tool_calls=(("CheckFinding", {"verdict": verdict, "note": "seen"}),))


def recorded(reference: str = "R-1") -> Turn:
    return Turn(tool_calls=(("Recorded", {"reference": reference}),))


def _scripting(agent: ProcessAgent, model: Model) -> Any:
    """Bind a scripted model by replacing `compiled` on the *instance*.

    The `tests/app` pattern (`test_casestudy.py:542-570`), and the reason nothing in
    `ProcessAgent` compiles at wiring time: a function compiled in `__init__` would silently
    bypass this binding and reach a real model.
    """
    original = type(agent).compiled

    def compiled(name: str, **overrides: Any) -> Any:
        overrides.setdefault("model", model)
        return original(agent, name, **overrides)

    return compiled


async def _always_first(
    _state: str, enabled: list[Transition], _variables: dict[str, int | str]
) -> str:
    return enabled[0].name


# ── The interpreter hook, asserted against the interpreter ──


async def test_on_enter_sees_every_state_the_run_occupies_including_the_initial_one() -> None:
    """The claim the whole design rests on: work reaches the deterministic corridor too.

    `A → B` is taken without consulting the decider (`interpreter.py:171-172`), so a hook
    fired from inside the decider would miss both `A` and `C`. Asserted here against
    `interpreter.run` rather than through the agent, so a dispatch bug cannot hide behind it.
    """
    entered: list[str] = []

    async def watch(state: str) -> None:
        entered.append(state)

    run = await interpreter.run(corridor(), _always_first, on_enter=watch)

    assert run.final_state == "C"
    assert entered == ["A", "B", "C"]


async def test_on_enter_fires_once_per_visit_so_a_cycle_works_twice() -> None:
    """A state re-entered is a state re-worked. Once per name would silently skip the rework
    loop that `test_process.py:820`'s `revisiting()` exists to model."""
    entered: list[str] = []
    calls = 0

    async def wander(state: str, enabled: list[Transition], _v: dict[str, int | str]) -> str:
        nonlocal calls
        calls += 1
        return "BtoC" if calls == 1 else "BtoD"

    async def watch(state: str) -> None:
        entered.append(state)

    run = await interpreter.run(cycling(), wander, max_steps=10, on_enter=watch)

    assert run.path == ["AtoB", "BtoC", "CtoB", "BtoD"]
    assert entered == ["A", "B", "C", "B", "D"]


async def test_on_enter_sees_the_same_history_a_decider_standing_there_would() -> None:
    """The hook runs inside the run's history scope, not beside it.

    Fired outside it, `history()` would read `[]` from a handler — so a handler prompt that
    wanted to say where the case has been would say nothing, with no error anywhere.
    """
    seen: list[list[str]] = []

    async def watch(_state: str) -> None:
        seen.append(interpreter.history())

    await interpreter.run(corridor(), _always_first, on_enter=watch)

    assert seen == [["A"], ["A", "B"], ["A", "B", "C"]]
    assert interpreter.history() == [], "the hook must not leak the history past the run"


async def test_on_enter_defaults_to_none_and_changes_nothing() -> None:
    """The additivity claim, restated locally. `tests/library/test_process.py` is the real
    oracle — 52 tests over this same interpreter, unmodified and green."""
    with_hook = await interpreter.run(corridor(), _always_first, on_enter=None)
    without = await interpreter.run(corridor(), _always_first)

    assert (with_hook.path, with_hook.final_state) == (without.path, without.final_state)
    assert with_hook.variables == without.variables


async def test_a_raising_on_enter_stops_the_run_and_propagates_unchanged() -> None:
    """The interpreter does not soften a hook fault, and does not keep walking past one.

    A run that swallowed this would return a `Run` reaching a terminal state with the work
    inside it missing — a report of a completed case that did not happen.
    """
    entered: list[str] = []

    async def watch(state: str) -> None:
        entered.append(state)
        if state == "B":
            raise ZeroDivisionError("the hook is wrong")

    with pytest.raises(ZeroDivisionError, match="the hook is wrong"):
        await interpreter.run(corridor(), _always_first, on_enter=watch)

    assert entered == ["A", "B"], "the run must not have reached C"
    assert interpreter.history() == []


# ── End to end: choose and work in one run ──


async def test_work_drives_the_process_and_dispatches_every_worked_state_in_path_order() -> None:
    """The whole loop, closed. One turn per model call, in the order the run makes them.

    `Filed` is worked, `Checked`/`Refused`/`Done` are not, and `Logged` names the mined
    `'handle'` placeholder, which resolves to nothing — so the script is exactly: check at
    `Intake`, one decision at `Checked`, record at `Filed`.

    **The interleaving is the assertion that rules out the post-hoc design.** Dispatching
    handlers afterwards over `Run.steps` produces this same `Run`, this same `filed` list, and
    this same call count — it differs only in that every decision was made before any of the
    work that was supposed to inform it. So the check handler must be call 0, the decision
    call 1, and the record handler call 2, and `ScriptedModel` playing back in order is what
    makes that observable: a post-hoc run would hand the `Choice` turn to the check handler
    and fail on the tool name.
    """
    async with RuntimeHarness():
        clerk = Clerk(filing(), context="the desk")
        model = Counting([finding("complete"), choice("RouteSimple"), recorded("R-7")])

        run = await clerk.work(
            facts="a routine filing", start={"records": 0, "band": "simple"}, model=model
        )

    assert run.final_state == "Done"
    assert run.path == ["Read", "RouteSimple", "Record", "Close"]
    assert [name for name, _ in clerk.filed] == ["Intake", "Filed"]
    assert clerk.filed[0][1].verdict == "complete"
    assert clerk.filed[1][1].reference == "R-7"
    assert len(model.contexts) == 3, "one call per handler plus one decision, and nothing else"
    assert any("Check this filing" in p for p in model.prompts(0)), "the check ran first"
    assert any("a routine filing" in p for p in model.prompts(1)), "then the decision"
    assert any("Record the decision" in p for p in model.prompts(2)), "then the record"


async def test_the_handler_prompt_carries_the_agents_own_context() -> None:
    """The `self`-closure half of the decorator paradigm: what varies per instance reaches
    the prompt without becoming a model-facing parameter."""
    async with RuntimeHarness():
        clerk = Clerk(corridor(), context="THE-DESK-CONTEXT")
        model = Counting([finding()])
        await clerk.work(facts="anything", model=model)

    assert len(model.contexts) == 1, "a corridor has no decisions, so this is the handler"
    assert any("THE-DESK-CONTEXT" in prompt for prompt in model.prompts(0))


async def test_the_corridor_is_worked_even_though_no_decision_is_ever_made() -> None:
    """The sharpest version of the design's central claim: a run with zero decisions still
    does its work. A dispatcher wired into the decider would do nothing at all here."""
    async with RuntimeHarness():
        clerk = Clerk(corridor())
        model = Counting([finding()])
        run = await clerk.work(facts="f", model=model)

    assert run.final_state == "C"
    assert [name for name, _ in clerk.filed] == ["B"]
    assert len(model.contexts) == 1


# ── Control points are free ──


async def test_a_state_naming_no_method_and_one_naming_nothing_both_cost_zero() -> None:
    """The mined placeholder is the case that matters. Every mined state carries
    `agent_method='handle'` (`casestudy/miner.py:125`), which resolves to no method on any
    agent, and `tests/app/test_casestudy.py:418` pins that as correctly `None`. A base that
    raised on an unrecognised name would make every mined process unrunnable.

    `Counting([])` is what makes this falsifiable: a dispatcher that resolved to None and
    then called the model anyway raises `ScriptExhausted` here instead of passing.
    """
    async with RuntimeHarness():
        clerk = Clerk(filing())
        model = Counting([])

        # A handler-free walk: `Checked` (None), `Logged` ('handle'), and the terminals.
        for name in ("Checked", "Logged", "Refused"):
            state = clerk.process.state_map[name]
            assert clerk.handler_for(state) is None
            assert await clerk.dispatch(state, model=model) is None

        assert model.contexts == [], "a control point must not reach the model"
        assert clerk.filed == [], "and must not reach on_result either"


async def test_handler_for_resolves_only_names_this_agent_actually_has() -> None:
    """The promise `ir.py:178` made: dispatch by the method name the field holds. Asserted
    on both sides, so the resolution is a lookup rather than a coincidence."""
    clerk = Clerk(filing())
    assert clerk.handler_for(clerk.process.state_map["Intake"]) == ("check", {})
    assert clerk.handler_for(clerk.process.state_map["Filed"]) == ("record", {})
    assert clerk.handler_for(State(name="Elsewhere", agent_method="check")) == ("check", {})
    assert clerk.handler_for(State(name="Elsewhere", agent_method="nonexistent")) is None


async def test_a_navigator_declares_no_handlers_so_a_worked_process_costs_only_decisions() -> None:
    """The lift must not have made the plain navigator more expensive. `Navigator` declares
    only `choose`, so every state in a process full of `agent_method` names resolves to
    None — and the model is called once, for the one branch."""
    async with RuntimeHarness():
        navigator = Navigator(filing())
        model = Counting([choice("RouteSimple")])

        run = await navigator.work(facts="f", start={"records": 0, "band": "simple"}, model=model)

    assert run.final_state == "Done"
    assert navigator.ai_methods() == ["choose"]
    assert len(model.contexts) == 1, "only the branch at Checked should have cost anything"


# ── The fast path, restated for work() ──


async def test_the_decider_is_consulted_only_at_a_branch() -> None:
    """`test_process.py:803-814` for the agent path. Cost control: asking a model to choose
    from one option buys nothing, and the interpreter owns that rule — this asserts `work`
    did not route around it."""
    async with RuntimeHarness():
        clerk = Clerk(filing())
        model = Counting([finding(), choice("RouteComplex"), recorded()])
        consulted: list[str] = []

        original = clerk.decider

        def decider(facts: str, **overrides: Any) -> interpreter.Decide:
            inner = original(facts, **overrides)

            async def decide(
                state: str, enabled: list[Transition], variables: dict[str, int | str]
            ) -> str:
                consulted.append(state)
                return await inner(state, enabled, variables)

            return decide

        clerk.decider = decider  # type: ignore[method-assign]
        await clerk.work(facts="f", start={"records": 0, "band": "complex"}, model=model)

    # Intake, Filed and Logged each have exactly one enabled move; only Checked branches.
    assert consulted == ["Checked"]


# ── The rejection loop still costs a turn and nothing more ──


async def test_an_illegal_proposal_is_rejected_and_re_offered_through_work() -> None:
    """The agent stays an untrusted oracle when it is also the worker.

    `Record` does not leave `Checked`; obeying it would skip the branch entirely. The
    interpreter rejects it, the run completes, and the wasted turn is on the record.
    """
    async with RuntimeHarness():
        clerk = Clerk(filing())
        model = Counting([finding(), choice("Record"), choice("RouteSimple"), recorded()])

        run = await clerk.work(facts="f", start={"records": 0, "band": "simple"}, model=model)

    assert run.rejections == 1
    assert "Record" in [r for step in run.steps for r in step.rejected]
    assert run.final_state == "Done"
    assert [name for name, _ in clerk.filed] == ["Intake", "Filed"]


# ── A handler fault stops the run, loudly, naming what broke ──


async def test_a_handler_that_raises_fails_the_run_naming_the_state_and_the_method() -> None:
    """A run that swallowed this would report a complete case that did not happen.

    The recording is the assertion that matters: `Filed`'s handler must never have run, so
    the failure stopped the walk rather than being noted and stepped over.
    """
    async with RuntimeHarness():
        clerk = BrokenClerk(filing())
        model = Counting([choice("RouteSimple"), recorded()])

        with pytest.raises(HandlerFailed) as raised:
            await clerk.work(facts="f", start={"records": 0, "band": "simple"}, model=model)

        message = str(raised.value)
        assert "'Intake'" in message, "the message must name the state"
        assert "'check'" in message, "and the method"
        assert "filing-agent" in message, "and the agent, as the tool schema names it"
        assert isinstance(raised.value.__cause__, ZeroDivisionError)
        assert clerk.filed == [], "no later handler may have fired"
        assert model.contexts == [], "the run must not have reached a decision"


async def test_a_failing_on_result_is_a_fault_too_and_names_itself_as_one() -> None:
    """The paperwork is on the same path as the work. A hook the base called and ignored
    would lose every record while the run reported success."""
    async with RuntimeHarness():
        clerk = BrokenRecorder(corridor())
        model = Counting([finding()])

        with pytest.raises(HandlerFailed) as raised:
            await clerk.work(facts="f", model=model)

        message = str(raised.value)
        assert "on_result hook" in message
        assert "'B'" in message
        assert isinstance(raised.value.__cause__, KeyError)


async def test_a_failing_handler_for_override_is_wrapped_with_the_states_name() -> None:
    """`gated.py`'s lesson: the hook added beside the wrapped one needs the wrap too.

    Unwrapped, a typoed override surfaces as a bare `AttributeError` with nothing saying
    which of a dozen states produced it. `Counting([])` proves the fault precedes the spend.
    """
    async with RuntimeHarness():
        clerk = BrokenResolver(corridor())
        model = Counting([])

        with pytest.raises(HandlerFailed) as raised:
            await clerk.work(facts="f", model=model)

        message = str(raised.value)
        assert "'A'" in message, "the first state entered is the one that failed"
        assert "handler_for" in message
        assert isinstance(raised.value.__cause__, AttributeError)
        assert model.contexts == []


async def test_a_handler_fault_is_not_catchable_as_a_process_refusal() -> None:
    """`casestudy/live.py:172-175` catches `ProcessError` and counts the case as `blocked`,
    which is an experimental result. A code bug must not arrive in that report as evidence
    about a guardrail."""
    async with RuntimeHarness():
        clerk = BrokenClerk(corridor())
        with pytest.raises(HandlerFailed):
            await clerk.work(facts="f", model=Counting([]))

    assert not issubclass(HandlerFailed, interpreter.ProcessError)


# ── The choose-as-handler collision, refused at wiring time ──


async def test_a_state_naming_the_decider_as_its_handler_is_refused_before_any_spend() -> None:
    """`choose` is an `@ai_method`, so the default resolution finds it and the state would
    dispatch the decision-maker as work.

    **The fixture supplies `choose`'s arguments deliberately, and that is what makes this
    test able to fail.** With the *default* `arguments_for` the collision is already loud
    without any guard — measured: `HandlerFailed ... TypeError: ProcessAgent.choose() missing
    3 required positional arguments`. A test written against that shape passes with the guard
    deleted, because the fallback message happens to name the same state and method and also
    spends nothing. It proves nothing.

    The case the guard exists for is the *silent* one, which `SelfChoosingClerk` is: a real
    turn spent producing a transition name that `on_result` receives and the interpreter never
    sees, at a state that then also gets a genuine decision. Measured with the guard removed —
    the model is called, the run walks on, and the waste is invisible.

    Two assertions carry the weight. `Counting([])` means any model call raises
    `ScriptExhausted` instead of this error; and the exception must be a plain `RuntimeError`
    about wiring, not the `HandlerFailed` a dispatch that actually ran would produce.
    """
    async with RuntimeHarness():
        clerk = SelfChoosingClerk(colliding())
        model = Counting([])

        with pytest.raises(RuntimeError) as raised:
            await clerk.work(facts="f", model=model)

        message = str(raised.value)
        assert type(raised.value) is RuntimeError, (
            f"the refusal must be a wiring error, not a report of something that ran: "
            f"{type(raised.value).__name__}: {message}"
        )
        assert "as their agent_method" in message, "the refusal's own wording, not a fallback's"
        assert "'Start'" in message, "the message must name the offending state"
        assert "'choose'" in message
        assert "collide-agent" in message
        assert model.contexts == [], "the guard must fire before any model call"
        assert clerk.filed == [], "and before on_result could record a phantom decision"


async def test_the_collision_is_refused_even_under_the_default_arguments() -> None:
    """The loud half of the same collision, which is loud with or without the guard.

    Kept as the negative control for the test above: it asserts the refusal *replaces* the
    signature-bind `TypeError`, so the guard covers both shapes rather than only the one it
    was written for.
    """
    async with RuntimeHarness():
        clerk = Clerk(colliding())
        with pytest.raises(RuntimeError) as raised:
            await clerk.work(facts="f", model=Counting([]))

        assert type(raised.value) is RuntimeError
        assert "as their agent_method" in str(raised.value)


def test_the_collision_guard_is_not_a_handler_fault() -> None:
    """A wiring mistake and a runtime fault are different events. The guard is a
    `RuntimeError` about how the process and the agent were put together, not a report that
    something ran and broke."""
    assert issubclass(HandlerFailed, RuntimeError)
    with pytest.raises(RuntimeError) as raised:
        Clerk(colliding())._check_no_decider_handler()
    assert not isinstance(raised.value, HandlerFailed)


# ── The interpreter's own failures pass straight through ──


async def test_deadlock_reaches_the_caller_unchanged_through_work() -> None:
    """The verified skeleton's refusals are not this class's to soften — and the state's own
    handler still ran first, because the deadlock is discovered from inside it."""
    async with RuntimeHarness():
        clerk = Clerk(stuck())
        model = Counting([finding()])

        with pytest.raises(interpreter.Deadlock, match="A has no enabled transition"):
            await clerk.work(facts="f", model=model)

        assert [name for name, _ in clerk.filed] == ["A"]


async def test_the_step_budget_still_raises_processerror_through_work() -> None:
    """`max_steps` is forwarded, and its failure keeps its type: `live.py` counts a
    `ProcessError` as a blocked case and has to keep seeing exactly what it saw."""
    async with RuntimeHarness():
        navigator = Navigator(cycling())
        model = Counting([choice("BtoC")] * 8)

        with pytest.raises(interpreter.ProcessError, match="exceeded 3 steps"):
            await navigator.work(facts="f", max_steps=3, model=model)


async def test_an_agent_that_never_proposes_legally_fails_loudly_through_work() -> None:
    """`max_rejections` is forwarded too, and a decider that never lands is a `ProcessError`
    naming what was legal."""
    async with RuntimeHarness():
        navigator = Navigator(filing())
        model = Counting([choice("NoSuchTransition")] * 3)

        with pytest.raises(interpreter.ProcessError, match="no legal transition"):
            await navigator.work(
                facts="f",
                start={"records": 0, "band": "simple"},
                max_rejections=1,
                model=model,
            )


async def test_a_runtime_invariant_violation_still_surfaces_from_work() -> None:
    """Defence in depth survives the lift: the bad path is stopped as it happens, with the
    invariant named, even though the run now also does per-state work."""
    process = claims()
    process.transitions.append(
        Transition(
            name="Expedite",
            source="Escalated",
            target="Paid",
            guards=[Guard(variable="amount_band", op="eq", value="large")],
            effects=[Effect(variable="approvals", increment=1)],
        )
    )

    async with RuntimeHarness():
        navigator = Navigator(process)
        model = Counting([choice("Expedite")])

        with pytest.raises(interpreter.InvariantViolated, match="LargeNeedsTwoApprovals"):
            await navigator.work(
                facts="f", start={"approvals": 0, "amount_band": "large"}, model=model
            )


# ── The scripting seam: overrides, and the instance rebinding ──


async def test_overrides_reach_both_the_decider_and_every_handler() -> None:
    """One keyword makes a whole process scriptable, decisions and work together — the seam
    `test_casestudy.py:542-570`'s monkeypatch had to work around.

    Two separate models, so the assertion cannot pass by one of them absorbing the other's
    calls: the handler model is asserted to have seen the handler prompts and the decider's
    turn is asserted to have come from the same script.
    """
    async with RuntimeHarness():
        clerk = Clerk(filing(), context="ctx")
        model = Counting([finding(), choice("RouteSimple"), recorded()])

        run = await clerk.work(
            facts="THE-FACTS", start={"records": 0, "band": "simple"}, model=model
        )

    assert run.final_state == "Done"
    assert len(model.contexts) == 3
    assert any("focusing on completeness" in p for p in model.prompts(0)), "the check handler"
    assert any("THE-FACTS" in p for p in model.prompts(1)), "the decision"
    assert any("Record the decision" in p for p in model.prompts(2)), "the record handler"


async def test_the_instance_rebinding_of_compiled_reaches_both_paths_too() -> None:
    """Nothing is compiled at wiring time, so the `tests/app` monkeypatch works here.

    A `decider` built in `__init__` would have captured the real model before the rebinding
    happened, and the failure mode is the worst available in an offline suite — a test that
    reaches the network instead of failing.
    """
    async with RuntimeHarness():
        clerk = Clerk(filing())
        model = Counting([finding(), choice("RouteSimple"), recorded()])
        clerk.compiled = _scripting(clerk, model)  # type: ignore[method-assign]

        run = await clerk.work(facts="f", start={"records": 0, "band": "simple"})

    assert run.final_state == "Done"
    assert len(model.contexts) == 3, "the instance binding did not reach every compilation"
    assert [name for name, _ in clerk.filed] == ["Intake", "Filed"]


# ── The two override points, separately ──


async def test_arguments_for_supplies_per_state_arguments_and_reaches_the_prompt() -> None:
    """The lighter override: the `agent_method` names are right, only the arguments vary.

    Asserted from the prompt rather than from the returned pair, because the claim is that
    the value *arrives* at the model and not merely that a dict was built.
    """
    async with RuntimeHarness():
        clerk = ArgumentClerk(corridor())
        model = Counting([finding()])

        assert clerk.handler_for(clerk.process.state_map["B"]) == (
            "check",
            {"focus": "the B step"},
        )
        await clerk.work(facts="f", model=model)

    assert any("focusing on the B step" in p for p in model.prompts(0))


async def test_handler_for_can_be_overridden_with_a_table_the_library_knows_nothing_about() -> None:
    """Wave 2's shape, proved from the library side.

    `casestudy.handlers.HANDLERS` keys by `state.name` with `agent_method` as the gate, and
    that override has to be expressible without `process/agent.py` importing anything from
    `casestudy` — which `tests/library/test_boundary.py` forbids outright. The table here
    also *disagrees* with `agent_method` on purpose: `Logged` carries the mined `'handle'`
    placeholder and the table maps it to `record`, so the override is doing the resolving.
    """
    async with RuntimeHarness():
        clerk = TableClerk(filing())
        model = Counting([finding(), choice("RouteSimple"), recorded()])

        run = await clerk.work(facts="f", start={"records": 0, "band": "simple"}, model=model)

    assert run.final_state == "Done"
    # `Filed` says `record` but is absent from the table, so it is free; `Logged` says
    # `handle` and the table gives it `record`. Neither follows from `agent_method` alone.
    assert [name for name, _ in clerk.filed] == ["Intake", "Logged"]
    assert any("the table said so" in p for p in model.prompts(0))
    assert len(model.contexts) == 3


async def test_a_sync_on_result_works_too() -> None:
    """The hook may be either. `Clerk`'s is `async` because that is the shape a silent
    no-op would hide in; the plain one must not have been broken to allow it."""

    class SyncClerk(Clerk):
        def on_result(self, state: State, result: Any) -> None:
            self.filed.append((state.name, result))

    async with RuntimeHarness():
        clerk = SyncClerk(corridor())
        await clerk.work(facts="f", model=Counting([finding()]))

    assert [name for name, _ in clerk.filed] == ["B"]


# ── Navigator is a ProcessAgent, and its published contract did not move ──


def test_navigator_still_exposes_a_typed_contract_and_keeps_its_name() -> None:
    """`test_process.py:949-953` is the oracle for the shape; this adds the name, which is
    what `live.py`'s decision log is written against and what every error message says."""
    navigator = Navigator(claims())
    assert navigator.compiled("choose").input_shape.value == "structured"
    assert navigator.name == "claimsintake-navigator"
    assert navigator.compiled("choose").config.name == "claimsintake-navigator.choose"
    assert isinstance(navigator, ProcessAgent)


def test_the_default_agent_name_is_the_process_and_a_subclass_may_rename_it() -> None:
    assert Clerk(filing()).name == "filing-agent"
    assert Navigator(filing()).name == "filing-navigator"


async def test_navigators_decider_still_drives_the_rejection_loop_by_itself() -> None:
    """`decider()` had zero callers when this landed, and it keeps working: `live.py` builds
    its own adapter today and this is the seam it migrates onto, so the contract has to hold
    outside `work()` as well as inside it."""
    async with RuntimeHarness():
        navigator = Navigator(claims(), context="claims desk")
        # `Extract` does not leave `Escalated`, so it is the illegal-then-legal pair. Both
        # of `Escalated`'s real moves are legal, which is why a rejection has to be scripted
        # as a transition from somewhere else entirely.
        model = Counting([choice("Extract"), choice("SeniorApprove")])

        run = await interpreter.run(
            claims(),
            navigator.decider("a claim of $80,000", model=model),
            start={"approvals": 0, "amount_band": "large"},
        )

    assert run.final_state == "Paid"
    assert run.variables["approvals"] == 2
    assert run.rejections == 1
    assert "Extract" in [r for step in run.steps for r in step.rejected]


def test_choice_is_reachable_from_where_it_always_was() -> None:
    """The type moved modules; the import path it was published under did not."""
    from pneuma.process.agent import Choice as Lifted
    from pneuma.process.agent_driver import Choice as Published

    assert Published is Lifted
    assert set(Published.model_fields) == {"transition", "reason"}


def test_repr_names_the_agent_and_its_process() -> None:
    assert repr(Clerk(filing())) == "<Clerk filing-agent over 'Filing'>"


# ── The critic's finding: terminal reached on the final budgeted step ──


async def test_a_run_that_lands_terminal_on_its_last_budgeted_step_has_completed() -> None:
    """Reaching the terminal state ON the `max_steps`-th transition is completion.

    The loop's terminal check sits at the top, so before the re-check after the loop a run
    landing terminal on its final budgeted step executed the step, worked the terminal
    state, and then raised `exceeded N steps` — a message that is factually false, attached
    to a case file carrying completed paperwork. `live.py` counts `ProcessError` as
    `blocked`, so at exactly the experiment's budget a completed case would corrupt the
    completed/blocked split. Measured with the fix removed: this test fails with
    `ProcessError: exceeded 2 steps` while `clerk.filed` already holds the terminal
    state's work.
    """
    process = corridor()
    process.states[-1].agent_method = "record"

    async with RuntimeHarness():
        clerk = Clerk(process)
        model = Counting([finding(), recorded()])

        run = await clerk.work(facts="f", max_steps=2, model=model)

    assert run.final_state == "C"
    assert [name for name, _ in clerk.filed] == ["B", "C"], "the terminal work was kept"
