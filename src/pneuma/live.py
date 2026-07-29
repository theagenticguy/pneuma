"""Live console rendering of a team's event stream.

The coordinator's event log is the single place where every thread's activity
shows up, including threads an agent hired mid-run that no line of our code named.
`coordinator.on(...)` takes a synchronous callback, so this module keeps the work
per event trivial and lets Rich do the printing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_functions import Coordinator
from ai_functions.types import (
    CustomEvent,
    Event,
    EventKind,
    FailedEvent,
    ThreadSpawnedEvent,
    TokenUsageEvent,
    ToolCallEvent,
)
from rich.console import Console

WATCHED = [
    EventKind.STARTED,
    EventKind.COMPLETED,
    EventKind.FAILED,
    EventKind.TOOL_CALL,
    EventKind.THREAD_SPAWNED,
    EventKind.TOKEN_USAGE,
]

STYLE = {
    "started": "cyan",
    "completed": "green",
    "failed": "bold red",
    "tool_call": "yellow",
    "thread_spawned": "bold magenta",
}


@dataclass
class Tape:
    """Records the run for the write-up while printing it as it happens."""

    console: Console
    lines: list[str] = field(default_factory=list)
    tool_calls: dict[str, int] = field(default_factory=dict)
    spawns: list[tuple[str, str]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def __call__(self, event: Event) -> None:
        # CustomEvent declares only `kind`/`payload`; the worker stamps thread_id
        # onto it but thread_name is never present, so both need getattr.
        tid = getattr(event, "thread_id", None)
        who = getattr(event, "thread_name", None) or (str(tid)[:14] if tid else "-")
        text = self._describe(event)
        if text is None:
            return
        self.lines.append(f"{who}\t{event.kind}\t{text}")
        self.console.print(f"[dim]{who:>18}[/dim] [{STYLE.get(str(event.kind), 'white')}]{text}[/]")

    def _describe(self, event: Event) -> str | None:
        match event:
            case ToolCallEvent():
                self.tool_calls[event.tool_name] = self.tool_calls.get(event.tool_name, 0) + 1
                detail = _summarize_args(event.arguments)
                return f"calls {event.tool_name}({detail})"
            case ThreadSpawnedEvent():
                self.spawns.append((str(event.thread_id), str(event.child_thread_id)))
                return f"spawned child {str(event.child_thread_id)[:14]}"
            case CustomEvent():
                return f"{event.kind}: {event.payload}"
            case TokenUsageEvent():
                self.input_tokens += event.token_usage.input_tokens
                self.output_tokens += event.token_usage.output_tokens
                return None
            case FailedEvent():
                return f"failed: {event.error[:160]}"
            case CustomEvent():
                return f"{event.kind}: {event.payload}"
            case _:
                return {"started": "thinking", "completed": "done"}.get(str(event.kind))

    def watch(self, coordinator: Coordinator) -> object:
        return coordinator.on(self, kinds=WATCHED)


def _summarize_args(arguments: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in arguments.items():
        shown = str(value)
        if len(shown) > 60:
            shown = shown[:57] + "..."
        parts.append(f"{key}={shown}")
    return ", ".join(parts)[:180]
