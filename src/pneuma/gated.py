"""An agent that proposes what it is then judged by — the gate as a post-condition.

`casestudy/harnesslearn.py` grew a shape worth keeping and worth separating from the domain
it grew in. `HarnessProposer` proposes the objective weight that its own score is then
computed with, and the detectors that decide whether a weight is pathological are wired as
that method's *post-conditions* rather than as a check the caller runs afterwards. That
placement is the whole idea, and this module is it with the polars pulled out. Rationale:
`docs/design/gated.md`.

**A gate is not a convention.** A manual check after the call is a check the loop can forget,
and the loops that forget it are the ones under time pressure. A post-condition cannot be
skipped: `ai_thread` runs every validator against the result before the cycle returns, and any
exception a validator raises becomes the message text of a `[VALIDATION ERROR]` user turn that
the *next* attempt reads (`ai_thread.py:640-664`). So refusal is the default and the gate's own
report is the re-ask feedback — the model that has to fix the proposal is handed the reason,
in the reason's own words, without the caller writing any glue.

**A rejection and a bug are not the same event, and the runtime cannot tell them apart.**
Every exception out of a validator is reported to the model as a validation failure, so a
`KeyError` in the gate is indistinguishable from a considered refusal and burns every retry on
something the model cannot fix. `admits` therefore wraps the gate call and re-raises an
internal failure as a message that says it is internal, and it does *not* record that
non-verdict in `rejected`.

**Two paths, deliberately different.** The single-thread path retries until admitted, because
that is what a post-condition is for. The beam path (`propose_k`) forks k branches, takes one
proposal from each, and filters with the gate directly — no post-condition on the branches, or
each branch's internal retries would blur what k measures.

    class Picker(GatedProposer):
        def __init__(self, floor: int) -> None:
            super().__init__(lambda pick: Threshold(pick.value >= floor, floor))

        @ai_method(Pick, description="Propose a value, judged by the gate")
        def propose(self, hint: str) -> Pick:
            '''Propose a value given {hint}.'''

`Picker(5).gated()` is an `AIFunction` whose post-condition is the gate, so awaiting it either
returns an admitted `Pick` or exhausts its retries trying; `await picker.propose_k(3, coord,
hint="go")` returns the admitted `(candidate, verdict)` pairs out of three one-shot branches.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Protocol, cast, runtime_checkable

from ai_functions import AIFunction, Coordinator

from .method import MethodAgent, MethodThread

__all__ = ["Gate", "GatedProposer", "Verdict"]


@runtime_checkable
class Verdict(Protocol):
    """What a gate returns: a decision, and a report a model can act on.

    Two members because a gate needs to serve two readers. `ok` is for the loop, which
    branches on it. `report_text()` is for the model, which has to *change* something, and a
    bare `False` tells it nothing to change — the report is the feedback channel that makes
    the re-ask worth spending an attempt on.

    Deliberately not a base class. The verdicts this generalises over already exist and
    already differ: `casestudy.harnesslearn.Admission` is a dataclass whose `ok` is a
    conjunction over two independent detector readings and which also carries `quality`,
    `threshold`, and `baseline_rules`; `detect.Probe` derives `ok` from its refusals. A
    protocol lets both satisfy this without either being edited or losing what it knows.
    """

    @property
    def ok(self) -> bool:
        """Whether the candidate may be used."""
        ...

    def report_text(self) -> str:
        """The verdict as prose the model can act on. Reaches the model verbatim."""
        ...


@runtime_checkable
class Gate(Protocol):
    """Judges one candidate. Sync or async; `admits` awaits a coroutine.

    A callable rather than a method to override, which is the load-bearing choice in this
    module. The gates worth having are made of the things this package's library half must not
    import — `harnesslearn.admit` needs polars, a process miner, and a reachability sweep — so
    a `GatedProposer` that reached its gate through `self.gate(...)` as an abstract method
    would be a library class whose only real implementation lives in the application. Taking
    the gate as a value keeps the boundary honest and makes the protocol testable with three
    lines of a trivial gate, which is why the tests here need no fixture at all.

    A subclass may still assign `self.gate` in its own `__init__` — `HarnessProposer` binds a
    closure over its event log — and that is injection too, just spelled from the inside.
    """

    def __call__(self, candidate: Any) -> Verdict | Awaitable[Verdict]: ...


class GatedProposer(MethodAgent):
    """An agent whose proposal is judged by the gate it is graded against.

    The subclass supplies the propose `@ai_method` and the gate; this base owns the skeleton:
    the post-condition, the `rejected` ledger, the wiring guard, and the beam search.

    **Where state goes is not a style question here.** The gate arrives on `self` and the
    candidate arrives as a call argument, and each has to be where it is. A validator needs
    the gate on every attempt, including attempts it has not seen yet, so the gate cannot be a
    parameter the model fills in. The learnable value must be a call argument or it is
    invisible to the optimizer: `collect_nodes` walks `(args, kwargs)`, so anything on `self`
    can never be a gradient target (see `method.py`'s header and `docs/design/method.md`).
    That is the same split `HarnessProposer` documents, stated once here so subclasses inherit
    it rather than rediscover it.

    **What this class deliberately does not do.** No score, no optimizer, no memory. A gate
    answers *admissible or not*; turning admitted candidates into a scalar an optimizer climbs
    is a separate concern with its own trap (`Admission.quality` exists because the
    objective's own peak is maximised at the pathological end), and it stays in the
    application. `propose_k` accordingly returns every admitted pair and imposes no ordering:
    "best" is the caller's to define, and quality shapes vary.
    """

    REASK = "Propose a different candidate."
    """Appended after `report_text()` on a rejection, so the model is told what to *do*.

    A class attribute rather than a constant because the instruction is domain wording: the
    harness proposer says which direction on its weight axis causes the pathology it just
    refused. Subclasses override it; the mechanism does not change.
    """

    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        self.rejected: list[Verdict] = []
        """Every candidate the gate turned away, in order. The evidence the gate has teeth.

        Kept because a loop that silently re-asked and then succeeded looks, from the outside,
        exactly like a loop whose gate never fired. A gate fault is *not* recorded here — see
        `admits` — so this list stays readable as refusals and nothing else.
        """

    # ── The candidate ──

    def candidate_of(self, response: Any) -> Any:
        """Pull the thing to be judged out of the model's response. Sync, and dumb.

        The base cannot know which field carries the candidate: a proposal usually arrives
        alongside the reasoning that produced it (`HarnessProposal` is `coverage_weight` *and*
        `evidence`, and the evidence is the auditable artifact, not the subject of the gate).
        The default judges the whole response, which is right when the response *is* the
        candidate; `HarnessProposer` overrides it to `response.coverage_weight`.

        Kept synchronous and free of side effects on purpose. It runs inside a validator on
        every attempt, and an override that fetched or computed anything would put work the
        gate is supposed to own behind a hook named for an accessor.
        """
        return response

    # ── The post-condition ──

    def admits(self, response: Any) -> None:
        """Post-condition: the gate must admit the proposed candidate.

        Raising is how a post-condition fails; `ai_thread` catches any exception from a
        validator and reports its text to the model as a validation failure. That is also the
        trap this method is written around: an unexpected exception is indistinguishable from a
        rejection and would burn every retry on a bug. So the gate call is wrapped and an
        internal failure is re-raised as a message that says it is internal, rather than being
        allowed to masquerade as a verdict about the proposal.

        The parameter is named `response` rather than `proposal` on purpose, and the name is
        not merely a convention here — `_check_no_collision` enforces it at wiring time. A
        post-condition whose *first* parameter shares a name with a propose parameter is handed
        the result positionally and the bound argument by keyword, which raises `TypeError: got
        multiple values for argument` and is then swallowed as a validation failure: a silent
        bug wearing a verdict's clothes.

        Synchronous, so it can also be called directly in a test — which is how the sharp
        properties are asserted, since what needs proving is that it raises with a usable
        message. An async gate is only reachable through the async path; a coroutine returned
        here is refused loudly rather than being truth-tested as an object, because every
        coroutine is truthy and `not verdict.ok` on one would silently admit everything.
        """
        try:
            candidate = self.candidate_of(response)
        except Exception as error:  # noqa: BLE001 — an extractor bug must not read as a verdict either
            text = self._fault_text(response, error, part="candidate extractor")
            raise AssertionError(text) from error
        try:
            verdict = self.gate(candidate)
        except Exception as error:  # noqa: BLE001 — see the docstring: a bug must not read as a verdict
            raise AssertionError(self._fault_text(candidate, error)) from error
        if inspect.isawaitable(verdict):
            # Close it before raising. An abandoned coroutine is a `RuntimeWarning` at
            # collection time and a warning is not where this belongs — the error below is.
            close = getattr(verdict, "close", None)
            if callable(close):
                close()
            raise AssertionError(
                f"{self._label()}: the gate returned an awaitable for candidate={candidate!r}, "
                "which is a fault in the wiring rather than a verdict about your proposal: an "
                "async gate needs the async path, because a coroutine is truthy and would be "
                "admitted without ever being judged"
            )
        self._record(candidate, verdict)

    async def judge(self, candidate: Any) -> Verdict:
        """Run the gate on one candidate, awaiting it if it is async, and record a rejection.

        The direct path `propose_k` uses, and the only place an async gate is honoured. Raises
        the same gate-fault `AssertionError` `admits` does, so a bug in the gate reads the same
        way whichever path found it; unlike `admits` it *returns* a rejection rather than
        raising it, because the beam path filters where the single-thread path re-asks.
        """
        try:
            returned = self.gate(candidate)
            # `await`ing the union directly widens the result to `object` for a type checker,
            # because `Awaitable`'s type argument is lost across the `isawaitable` narrowing.
            # Casting the awaitable rather than the result keeps `Verdict` on the value.
            verdict = (
                await cast("Awaitable[Verdict]", returned)
                if inspect.isawaitable(returned)
                else returned
            )
        except Exception as error:  # noqa: BLE001 — a bug must not read as a verdict
            raise AssertionError(self._fault_text(candidate, error)) from error
        try:
            ok = bool(verdict.ok)
            # Render the report now, not when the ledger is read: a rejection whose
            # `report_text` crashes would otherwise detonate far from the gate that
            # produced it, in whatever loop renders `rejected` for a summary.
            if not ok:
                verdict.report_text()
        except Exception as error:  # noqa: BLE001 — a broken verdict is a fault, not a verdict
            raise AssertionError(self._fault_text(candidate, error)) from error
        if not ok:
            self.rejected.append(verdict)
        return verdict

    def _record(self, candidate: Any, verdict: Verdict) -> None:
        """Append a rejection and raise it as the re-ask, or return quietly.

        The verdict itself is untrusted here: reading `ok` or rendering `report_text`
        can crash when a gate returned something malformed (a gate that swallowed a
        bad candidate and built a verdict around it, say). That crash is a fault, not
        a rejection — it must neither land in `rejected` nor reach the model dressed
        as a validation message it cannot act on.
        """
        try:
            ok = bool(verdict.ok)
            report = None if ok else verdict.report_text()
        except Exception as error:  # noqa: BLE001 — a broken verdict is a fault, not a verdict
            raise AssertionError(self._fault_text(candidate, error)) from error
        if not ok:
            self.rejected.append(verdict)
            raise AssertionError(f"{report}\n\n{self.REASK}")

    def _fault_text(self, candidate: Any, error: Exception, *, part: str = "gate") -> str:
        """One wording for "the gate broke", shared by both paths so both read alike."""
        return (
            f"{self._label()}: the {part} could not be evaluated for candidate={candidate!r}, "
            f"which is a fault in the {part} rather than a verdict about your proposal: "
            f"{type(error).__name__}: {error}"
        )

    def _label(self) -> str:
        return getattr(self, "name", None) or type(self).__name__.lower()

    # ── Wiring ──

    def gated(self, method: str = "propose", **overrides: Any) -> AIFunction[..., Any]:
        """Compile one propose method with the gate attached as a post-condition.

        Any `post_conditions` passed in are kept and `admits` is prepended, so a subclass can
        add checks without losing the gate — and cannot accidentally replace it, which is the
        failure mode of a plain `compiled(..., post_conditions=[...])` call written by hand.
        """
        extra = tuple(overrides.pop("post_conditions", ()))
        # Every attached condition gets the guard, not just admits: the collision trap is a
        # property of the runtime's kwarg injection, and an extra condition hits it the same
        # way the gate would.
        self._check_no_collision(method, *extra)
        return self.compiled(method, post_conditions=(self.admits, *extra), **overrides)

    def _check_no_collision(self, method: str, *conditions: Any) -> None:
        """Refuse at wiring time if a result parameter shares a propose parameter's name.

        `ai_thread` passes the result positionally and then injects, by keyword, every bound
        argument whose name appears in the validator's signature
        (`ai_thread.py:1016-1018`). Those two rules are useful together — a validator that
        wants the call's `window` just names it — and fatal for the *first* parameter, which
        already holds the result: the same slot filled twice raises `TypeError: got multiple
        values for argument`, which the runtime then catches and reports to the model as a
        validation failure. The gate appears to reject everything, the message makes no sense,
        and the fix is a one-word rename nothing points at.

        So this is checked here rather than left to a convention, because the convention's
        failure mode is silent and its violation is one careless rename away. Only the first
        parameter is checked, deliberately: forbidding the rest would forbid the injection the
        runtime documents and this class has no reason to prevent.

        `tests/app/test_harnesslearn.py:611` asserts this property for `HarnessProposer` by
        comparing signatures in the test. This turns that assertion into a guarantee of the
        library, which is the point of lifting the skeleton at all: the next subclass gets it
        without knowing it exists.
        """
        proposed = set(inspect.signature(getattr(self, method)).parameters)
        for condition in (self.admits, *conditions):
            validator = self._first_parameter(condition)
            if validator is None:
                continue
            if validator in proposed:
                label = getattr(condition, "__name__", repr(condition))
                raise RuntimeError(
                    f"{self._label()}: the post-condition {label!r}'s result parameter is "
                    f"named {validator!r}, which is also a parameter of {method!r}; the "
                    f"runtime would pass the result positionally and {validator!r} by "
                    f"keyword, and `TypeError: got multiple values for argument "
                    f"{validator!r}` is then reported to the model as a validation failure. "
                    f"Rename one of them."
                )

    @staticmethod
    def _first_parameter(validator: Any) -> str | None:
        """The name of the slot the result lands in, or None if the validator takes no args."""
        positional = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        parameters = [
            name
            for name, parameter in inspect.signature(validator).parameters.items()
            if parameter.kind in positional
        ]
        return parameters[0] if parameters else None

    # ── Beam search ──

    async def propose_k(
        self,
        k: int,
        coordinator: Coordinator,
        *run_args: Any,
        method: str = "propose",
        seed: Sequence[Mapping[str, Any]] = (),
        **run_kwargs: Any,
    ) -> list[tuple[Any, Verdict]]:
        """Take k independent proposals from one shared context and return the admitted ones.

        One thread is spawned for `method` and run once per entry in `seed`; then it is forked
        `k - 1` times, so the original is branch 0 and every branch starts byte-identical to
        the seeded context and diverges from there. Each branch runs exactly one propose cycle
        with the same arguments, and each result is judged by the gate *directly*.

        **Why the gate is not attached as a post-condition here.** The retries a post-condition
        drives are the right behaviour on the single-thread path and the wrong behaviour on
        this one: a branch that quietly re-asked until it was admitted would make k a count of
        branches-that-eventually-succeeded, and a k of 3 could mean twelve model calls and
        three admitted candidates that tell you nothing about the width of the search. One shot
        per branch, then filter, so k is what it says it is and `rejected` measures how much of
        the beam the gate turned away.

        **Why the seed is a sequence of cycles rather than a `notify`.** A pending `notify` is
        worker-side inject state, not log state, and `fork` copies the log — measured: a forked
        branch does not see it, while a seed *cycle* run before the fork appears in every
        branch's first model call. Seeding therefore means running the method, which is also
        the honest shape: what a branch inherits is a real turn it could have produced.
        Each seed entry is one cycle's keyword arguments, because that is `MethodThread.run`'s
        contract and a thread hosts exactly one signature.

        Returns:
            `(candidate, verdict)` for every admitted branch, in branch order, and nothing for
            the rejected ones — those are in `rejected`. No ordering is imposed and no scalar
            is computed: which admitted candidate is best depends on a quality shape this class
            has no opinion about, and inventing one here would make every subclass live with it.

        Raises:
            ValueError: `k` is not at least 1.
            AssertionError: The gate itself failed, with the same wording `admits` uses. The
                exception propagates, and every thread is still retired — the unwind is in a
                `finally`, and `retire` is idempotent against the runtime, so unwinding
                threads one of which something else already tore down cannot crash mid-loop
                and leave the rest alive.
        """
        if k < 1:
            raise ValueError(f"propose_k needs at least one branch, got k={k}")
        self._check_no_collision(method)

        threads: list[MethodThread] = []
        try:
            root = await self.spawn(method, coordinator)
            threads.append(root)
            for cycle in seed:
                await root.run(**cycle)
            for _ in range(k - 1):
                threads.append(await root.fork())

            admitted: list[tuple[Any, Verdict]] = []
            for thread in threads:
                response = await thread.run(*run_args, **run_kwargs)
                try:
                    candidate = self.candidate_of(response)
                except Exception as error:  # noqa: BLE001 — an extractor bug must not read as a verdict
                    raise AssertionError(
                        self._fault_text(response, error, part="candidate extractor")
                    ) from error
                verdict = await self.judge(candidate)
                if verdict.ok:
                    admitted.append((candidate, verdict))
            return admitted
        finally:
            for thread in threads:
                await thread.retire()
