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
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..method import MethodAgent, ai_method
from . import interpreter
from .ir import Process, Transition


class Choice(BaseModel):
    """One transition, and why."""

    transition: str = Field(description="Exactly one transition name from the offered list")
    reason: str = Field(description="One sentence, citing the condition that applies")


class Navigator(MethodAgent):
    """Walks a process, choosing a transition at each branch."""

    def __init__(self, process: Process, *, context: str = "") -> None:
        self.process = process
        self.context = context
        self.name = f"{process.name.lower()}-navigator"

    @ai_method(
        Choice, description="Choose the next transition in a business process", max_attempts=2
    )
    def choose(self, state: str, options: str, facts: str) -> Choice:
        """You are executing the `{self.process.name}` process.

        {self.process.description}

        Case facts:
        {facts}

        {options}

        Pick the transition the process rules require for these facts. Name the
        condition you relied on. Choosing an option not on the list wastes a turn:
        the runtime rejects it and asks again.
        """

    def decider(self, facts: str) -> interpreter.Decide:
        """Adapt `choose` into the callable `interpreter.run` expects."""
        compiled = self.compiled("choose")

        async def decide(
            state: str, enabled: list[Transition], variables: dict[str, int | str]
        ) -> str:
            choice = await compiled(state, interpreter.offer(state, enabled, variables), facts)
            return choice.transition

        return decide
