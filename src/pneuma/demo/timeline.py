"""A swimlane timeline of one team run, rendered live on the terminal.

`live.Tape` prints the event stream as a flat scroll; this module renders the same
stream as a *timeline*: one lane per thread (the lead first, members in spawn order,
hires appended as they arrive), one row per event, each row carrying the elapsed
seconds, the lane it belongs to, and a glyph that says what kind of thing happened —
so the shape of a run (lead consults members, a discovery fans out, a critic sends
the lead back) is readable at a glance rather than reconstructed from prose.

The subscription is `coordinator.on(self, kinds=WATCHED)`, the `Tape` pattern: the
coordinator calls the callback synchronously inline, so the per-event work here is
appending one record and (when attached to a `rich.live.Live`) asking for one
refresh. Nothing blocks, nothing awaits.

Lanes are keyed by thread id and labelled by `thread_name` when the runtime carries
one (`CustomEvent` declares only kind/payload — the worker stamps `thread_id` but
never `thread_name`, so both ride through `getattr`, exactly as `live.py` documents).
A lead consulting a member arrives as a TOOL_CALL whose tool name is the member's
wire name (dots mapped to underscores by the core's tool composer); the renderer
resolves those back to lanes and draws them as arrows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ai_functions import Coordinator
from ai_functions.types import (
    CustomEvent,
    Event,
    EventKind,
    FailedEvent,
    MessageUserEvent,
    ThreadSpawnedEvent,
    ToolCallEvent,
)
from rich.console import Console
from rich.live import Live
from rich.table import Table

__all__ = ["Timeline", "TimelineRecord"]

WATCHED = [
    EventKind.STARTED,
    EventKind.COMPLETED,
    EventKind.FAILED,
    EventKind.TOOL_CALL,
    EventKind.THREAD_SPAWNED,
    EventKind.MESSAGE_USER,
    # Custom kinds ride the same filter as plain strings (`Coordinator.on` accepts
    # EventKind members or str); without these the hooks' events never reach us.
    "team.discovery",
    "team.hired",
    "team.hired_dynamic",
]

REVISION_MARK = "Your answer was reviewed"
"""How the core's answer loop phrases a re-run (`core.py`, `_answer_loop`).

Matched by containment, not prefix: the feedback rides through the lead's typed
method, whose prompt template wraps it ("Rule on Your answer was reviewed...")."""

# glyph, style — one visual vocabulary, shared by live and static rendering
GLYPHS = {
    "spawn": ("▶", "magenta"),
    "consult": ("→", "yellow"),
    "tool": ("·", "dim yellow"),
    "discovery": ("★", "cyan"),
    "hire": ("✚", "green"),
    "revise": ("↻", "dark_orange"),
    "done": ("✓", "green"),
    "failed": ("✗", "bold red"),
    "start": ("○", "dim"),
}


@dataclass(frozen=True)
class TimelineRecord:
    """One rendered event: everything a test asserts without a terminal.

    `lane_key` is the thread id; the display label resolves at *render* time
    against the lanes map, because a thread's first events (its own spawn
    announcements) arrive before any named event teaches us its label.
    """

    elapsed: float
    lane_key: str
    kind: str  # a GLYPHS key
    text: str


@dataclass
class Timeline:
    """Collects a run's events into lanes and renders them as a live table.

    `attach(coordinator)` subscribes and returns the runtime's `Subscription`
    (call `.unsubscribe()` to detach — further events then add nothing). Pass a
    `rich.live.Live` via `follow` to have every event refresh the display;
    without one the records still accrue and `render_static()` produces the
    full table for a post-run print, so the demo leaves a durable artifact on
    stdout even without a TTY.
    """

    console: Console
    max_rows: int = 40
    cast: list[str] = field(default_factory=list)
    """Member names known up front (e.g. `[m.name for m in team.members]`).

    A lead's consult of a member arrives as a TOOL_CALL *before* the member's own
    lane has emitted a named event, so name-resolution against `lanes` alone would
    misread the first consult as a generic tool. Passing the cast makes every
    consult an arrow from the first event on."""
    records: list[TimelineRecord] = field(default_factory=list)
    lanes: dict[str, str] = field(default_factory=dict)  # thread_id -> label
    parents: dict[str, str] = field(default_factory=dict)  # child id -> parent id
    _live: Live | None = None
    _started: float = field(default_factory=time.monotonic)

    def attach(self, coordinator: Coordinator) -> Any:
        self._started = time.monotonic()
        return coordinator.on(self, kinds=WATCHED)

    def follow(self, live: Live) -> None:
        """Refresh `live` on every event; the Live should run with auto_refresh=False."""
        self._live = live

    # ── The callback the coordinator drives ──

    def __call__(self, event: Event) -> None:
        record = self._describe(event)
        if record is None:
            return
        self.records.append(record)
        if self._live is not None:
            self._live.update(self.render_live())
            self._live.refresh()

    def _lane(self, event: Event) -> str:
        tid = getattr(event, "thread_id", None)
        key = str(tid) if tid is not None else "-"
        name = getattr(event, "thread_name", None)
        if name:
            self.lanes.setdefault(key, str(name))
        return key

    def label(self, lane_key: str) -> str:
        return self.lanes.get(lane_key, lane_key[:14])

    def _describe(self, event: Event) -> TimelineRecord | None:
        lane = self._lane(event)
        elapsed = time.monotonic() - self._started
        match event:
            case ThreadSpawnedEvent():
                child = str(event.child_thread_id)
                parent = str(event.thread_id)
                self.parents[child] = parent
                return TimelineRecord(elapsed, lane, "spawn", f"spawned {child[:14]}")
            case ToolCallEvent():
                kind, text = self._tool_call(event)
                return TimelineRecord(elapsed, lane, kind, text)
            case CustomEvent() if event.kind == "team.discovery":
                source = event.payload.get("source", "?")
                reached = ", ".join(event.payload.get("delivered", [])) or "nobody"
                what = event.payload.get("discovery", "?")
                return TimelineRecord(
                    elapsed, lane, "discovery", f"{source} flags {what} → {reached}"
                )
            case CustomEvent() if event.kind in ("team.hired", "team.hired_dynamic"):
                who = event.payload.get("name", "?")
                return TimelineRecord(elapsed, lane, "hire", f"hired {who}")
            case CustomEvent():
                return None
            case MessageUserEvent() if REVISION_MARK in event.text:
                gist = event.text.split("Feedback:", 1)[-1].strip()
                return TimelineRecord(elapsed, lane, "revise", f"revision: {_clip(gist)}")
            case MessageUserEvent():
                return TimelineRecord(elapsed, lane, "start", f"asked: {_clip(event.text)}")
            case FailedEvent():
                return TimelineRecord(elapsed, lane, "failed", f"failed: {_clip(event.error)}")
            case _ if event.kind == EventKind.COMPLETED:
                return TimelineRecord(elapsed, lane, "done", "done")
            case _:
                return None  # STARTED and anything unrecognised stay off the timeline

    def _tool_call(self, event: ToolCallEvent) -> tuple[str, str]:
        """A tool call is a consult when its name resolves to a cast member or a lane.

        The core names member tools after the member with the characters strands
        rejects mapped to underscores (`core.py`, `_tool_name`), so both the declared
        cast and the observed lane labels are checked in that mapping.
        """
        tool = event.tool_name
        for label in (*self.cast, *self.lanes.values()):
            if tool == label or tool == label.replace(".", "_"):
                request = str(event.arguments.get("request", ""))
                return "consult", f"consults {label}: {_clip(request, 40)}"
        args = ", ".join(f"{k}={_clip(str(v), 40)}" for k, v in event.arguments.items())
        return "tool", f"{tool}({args})"

    # ── Rendering ──

    def render_live(self) -> Table:
        return self._table(self.records[-self.max_rows :], title="team timeline (live)")

    def render_static(self) -> Table:
        return self._table(self.records, title="team timeline")

    def _table(self, records: list[TimelineRecord], *, title: str) -> Table:
        table = Table(title=title, expand=False, pad_edge=False)
        table.add_column("t+s", justify="right", style="dim", width=7)
        table.add_column("lane", style="bold", min_width=12)
        table.add_column("event", overflow="fold")
        for record in records:
            glyph, style = GLYPHS[record.kind]
            table.add_row(
                f"{record.elapsed:6.2f}",
                self.label(record.lane_key),
                f"[{style}]{glyph} {record.text}[/]",
            )
        return table


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."
