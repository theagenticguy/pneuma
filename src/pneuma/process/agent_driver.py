"""An `@ai_method` as the interpreter's decision-maker.

This is where the two halves meet. `interpreter.run` needs a function that picks a
transition; `Navigator.choose` is an AI function that does it, with the process
context in its docstring template and the legal moves rendered by
`interpreter.offer`.

The typed return is the useful part. `Choice.transition` is a plain string, so the
model can return anything at all — and the interpreter rejects whatever is not
legal, re-offering the same choice. That is the boundary from the ladder: the model
sits on the untrusted side, and its output is validated where it touches the
verified skeleton.

`Choice`, the `choose` capability, and the `decider` adapter now live in
`process/agent.py`, because choosing turned out to be half of what an agent bound to a
process does — the other half is the work inside each state it enters. `Navigator` is
what remains once that skeleton is lifted: a `ProcessAgent` that decides and does no
per-state work, which is exactly the shape `casestudy/live.py` runs its experiment
with. Nothing about its published behaviour changed, and
`tests/library/test_process.py:765-800,949-953` is the oracle that says so.
"""

from __future__ import annotations

from .agent import Choice, ProcessAgent
from .ir import Process

__all__ = ["Choice", "Navigator"]


class Navigator(ProcessAgent):
    """Walks a process, choosing a transition at each branch.

    Everything it does is `ProcessAgent`'s, and what it does *not* do is the point: no
    handlers are declared, so `handler_for` resolves every state to None and a run costs
    exactly the branch decisions it always did. A mined process whose states all carry
    `agent_method='handle'` is therefore navigated at unchanged cost — the placeholder
    names no method on this class either.
    """

    def __init__(self, process: Process, *, context: str = "") -> None:
        super().__init__(process, context=context)
        # The published name, which is also the compiled tool's prefix and every error
        # message's subject (`method._owner_name`). Kept as it was: `live.py` writes its
        # decision log against a live experiment, and renaming the agent mid-study would
        # split one arm's rows across two names.
        self.name = f"{process.name.lower()}-navigator"
