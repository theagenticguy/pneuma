"""The process-less caseworker's gate: refusal, introspection safety, and the wired path.

`Caseworker(case)` with no process is a legitimate object — it does its activities as typed
tools — but it cannot walk anything, and the gate on `process` is what says so. New behavior
from the ProcessAgent rebase, pinned here because a guard nobody has watched fire is a
comment, and this one has three ways to regress: the refusal itself, its exception type (an
`AttributeError`, so `hasattr`/`getattr(..., default)`/`inspect.getmembers` get their answer
instead of a message in place of whatever they were reporting), and the `__repr__` that must
keep working without a process.
"""

from __future__ import annotations

import inspect

import pytest

from pneuma.casestudy.handlers import CaseFile, Caseworker


def _case() -> CaseFile:
    return CaseFile(reference="G-1", facts="a gate probe")


def test_a_processless_caseworker_refuses_the_walk_naming_the_fix() -> None:
    worker = Caseworker(_case())
    with pytest.raises(AttributeError, match=r"built without a process"):
        _ = worker.process


def test_the_gate_is_invisible_to_introspection() -> None:
    """`hasattr` answers, `getattr` defaults, `getmembers` completes — none of them raise.

    The gate is an `AttributeError` precisely so capability probes and debugger variable
    panes get "no process" instead of the gate's message arriving in place of the real
    failure. A refactor to `RuntimeError` breaks all three, silently for the suite and
    loudly for whoever is debugging.
    """
    worker = Caseworker(_case())
    assert hasattr(worker, "process") is False
    assert getattr(worker, "process", None) is None
    names = [name for name, _ in inspect.getmembers(worker)]
    assert "case" in names  # getmembers completed without raising


def test_a_processless_caseworker_still_reprs() -> None:
    text = repr(Caseworker(_case()))
    assert "caseworker-G-1" in text
    assert "no process" in text


def test_a_wired_caseworker_hands_back_the_process_it_was_given() -> None:
    from pneuma.process.ir import Process, State, Transition

    process = Process(
        name="Tiny",
        description="One step",
        initial_state="A",
        states=[State(name="A"), State(name="B", terminal=True)],
        transitions=[Transition(name="AtoB", source="A", target="B")],
    )
    worker = Caseworker(_case(), process=process)
    assert worker.process is process
