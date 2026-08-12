"""Executable contract pins for the installed `ai_functions` package (git rev e47dc94).

The `pneuma.team` modules pin load-bearing upstream behaviours by file:line in their
docstrings. Those pins are prose; an upstream bump that changes the semantics would break
tool composition silently. Each test here is the executable version of one pin, asserted
on the wire (what a recording model actually received) or against the runtime object
itself, so a semantic change fails THIS file loudly.

What is pinned, and which pneuma docstring relies on it:

- `ai_thread/ai_thread.py:548-554` — exactly one `config_hook` resolved per cycle, its
  patch applied via `dataclasses.replace` so a `tools` key REPLACES compiled tools
  wholesale (no merge), and the `config_hook` key itself is popped from the patch.
  Relied on by `team/members.py` (`Member.equip`), `team/core.py` (module docstring and
  the composer), and `team/hooks/hiring.py`.
- `ai_thread/ai_thread.py:583-589` — when `coordinator_tools_enabled`, the runtime
  APPENDS `list_threads`/`send_message` AFTER the hook's replacement, so a replacing
  hook cannot cost a thread its runtime tools. Relied on implicitly by every hook that
  patches `tools`.
- `handle.py:120-132` — `ThreadHandle.notify` appends to the inject buffer and starts
  NO cycle; the next run observes it as context. Relied on by `method.py`
  (`MethodThread.notify`) and the `Member` docstring's side-channel claim.
- `ai_thread/config.py:72-93` — `ThreadKwargs` declares the keys the team module
  passes as overrides and hook patches.
- `memory/base.py:120-140` — `MemoryBackend._resolve_field` (slash-path resolution,
  `KeyError` on a missing field) and `_is_procedural`/`_is_frozen` marker detection.
  Relied on by `team/hooks/learning.py` (`Learning.__init__` guards).
- `runtime/worker.py:857-896` — `LocalWorker.close()` is idempotent. Relied on by
  every teardown path that may close a worker it did not open.
- `team/members.py:133-154` (`Member.equip`) — a member constructed with its own
  `tools=` keeps them after `equip` composes a hook, because `spawn` recomposes them
  ahead of whatever the hook adds.

The wire-recording `Counting` model composes `ScriptedModel` (which is `@final`), the
same pattern as `tests/library/test_team_core.py`; fixture output types are module
level because `compile_ai_method` resolves annotations against module globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import pytest
from ai_functions import Frozen, InMemoryCoordinator, LocalWorker, Procedural, ai_function
from ai_functions.ai_thread.config import ThreadKwargs
from ai_functions.memory.base import MemoryBackend
from ai_functions.testing import RuntimeHarness, ScriptedModel, Turn
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo
from strands.models import Model
from strands.tools.decorator import tool as strands_tool

from pneuma.method import MethodAgent, ai_method
from pneuma.team import Member

# ── Output types, module level for get_type_hints ──


class Sighting(BaseModel):
    target: str = Field(description="What was looked at")


class Scout(MethodAgent):
    name = "scout"

    @ai_method(Sighting, description="Look at one target and report it")
    def look(self, target: str) -> Sighting:
        """Look at {target}."""


# ── Recording model: contexts AND tool_specs, both wire facts ──


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

    def prompts(self, call: int) -> list[str]:
        return [
            block["text"]
            for message in self.contexts[call]
            for block in message.get("content", [])
            if "text" in block
        ]


@strands_tool(name="tool_a", description="the tool the compiled config carries")
def tool_a(x: str) -> str:
    return x


@strands_tool(name="tool_b", description="the tool the hook's patch carries")
def tool_b(x: str) -> str:
    return x


# ── 1+2. config_hook: replacement semantics, one call per cycle, key popped ──


def sighting() -> Turn:
    return Turn(tool_calls=(("Sighting", {"target": "seen"}),))


async def test_a_hooks_tools_patch_replaces_compiled_tools_coordinator_tools_append() -> None:
    """`ai_thread.py:548-554`: the hook's `tools` REPLACE the compiled tools wholesale —
    tool_b is on the wire and tool_a is NOT. And `ai_thread.py:583-589`: the runtime
    appends `list_threads`/`send_message` AFTER that replacement, so they survive it."""
    model = Counting([sighting()])

    def hook(ctx: Any) -> ThreadKwargs:
        return {"tools": [tool_b]}

    @ai_function[Sighting](model=model, tools=[tool_a], config_hook=hook)
    def probe(question: str) -> str:
        """Answer {question}."""

    async with RuntimeHarness() as h:
        handle = await h.spawn(probe)
        await handle.run("anything")

    specs = model.tool_specs[0]
    assert "tool_b" in specs, f"the hook's tool never reached the wire: {specs}"
    assert "tool_a" not in specs, f"REPLACE became merge — compiled tools survived: {specs}"
    for runtime_tool in ("list_threads", "send_message"):
        assert runtime_tool in specs, (
            f"{runtime_tool} missing: coordinator tools no longer append after the hook"
        )


async def test_the_config_hook_is_called_exactly_once_per_cycle_and_cannot_replace_itself() -> None:
    """`ai_thread.py:548-554`: one hook resolution per cycle — two `run()` cycles on one
    thread are exactly two calls. And line 553: a `config_hook` key in the patch is
    popped, so a hook smuggling in a replacement hook changes nothing — the decoy is
    never called and the original still fires on the next cycle."""
    calls = {"hook": 0, "decoy": 0}

    def decoy(ctx: Any) -> ThreadKwargs:
        calls["decoy"] += 1
        return {}

    def hook(ctx: Any) -> ThreadKwargs:
        calls["hook"] += 1
        return {"config_hook": decoy}  # popped by the runtime, a no-op

    model = Counting([sighting(), sighting()])

    @ai_function[Sighting](model=model, config_hook=hook)
    def probe(question: str) -> str:
        """Answer {question}."""

    async with RuntimeHarness() as h:
        handle = await h.spawn(probe)
        await handle.run("first")
        await handle.run("second")

    assert calls["hook"] == 2, f"expected one hook call per cycle, got {calls['hook']}"
    assert calls["decoy"] == 0, "the patch's config_hook key was applied instead of popped"
    assert len(model.contexts) == 2


# ── 3. notify: buffered, never a cycle ──


async def test_notify_starts_no_cycle_and_the_text_arrives_as_the_next_runs_context() -> None:
    """`handle.py:120-132`: `notify` appends to the inject buffer and starts nothing —
    zero model calls after it. The next `run` carries the text in its context."""
    model = Counting([sighting()])

    @ai_function[Sighting](model=model)
    def probe(question: str) -> str:
        """Answer {question}."""

    async with RuntimeHarness() as h:
        handle = await h.spawn(probe)
        await handle.notify("the deadline moved to Friday")
        assert model.contexts == [], "notify started a cycle"

        await handle.run("reschedule")
        assert any("the deadline moved to Friday" in p for p in model.prompts(0)), (
            "the notified text never reached the next cycle's context"
        )


# ── 4. ThreadKwargs surface ──


def test_thread_kwargs_declares_every_key_the_team_module_passes() -> None:
    """`config.py:72-93`: the keys `pneuma.team` uses as overrides and hook patches all
    exist on `ThreadKwargs` — a renamed or dropped key breaks composition silently
    because TypedDict keys are not checked at runtime."""
    declared = ThreadKwargs.__required_keys__ | ThreadKwargs.__optional_keys__
    assert {"model", "tools", "config_hook", "system_prompt", "thread_name"} <= declared


# ── 5. MemoryBackend schema introspection ──


class Notes(BaseModel):
    guidance: str = Field(default="", description="prose advice")
    recipe: Procedural
    identity: Frozen[str] = "fixed"


class _ProbeBackend(MemoryBackend):
    """Minimal concrete backend: the test uses only the base-class introspection."""

    def close(self) -> None:
        pass

    def _save(self, name: str, value: Any) -> None:
        pass

    def _recall(self, name: str) -> Any:
        raise NotImplementedError

    def _query(self, name: str, query: str) -> Any:
        raise NotImplementedError

    def _search(self, name: str, query: str, k: int = 5, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _consolidate(self, name: str, feedback: Any, retrieved: Any = None, **kwargs: Any) -> None:
        pass

    def _delete(self, name: str) -> None:
        pass


def test_memory_backend_field_resolution_and_marker_detection_hold() -> None:
    """`memory/base.py:120-140`: `_resolve_field` returns a `FieldInfo`, raises
    `KeyError` on a missing name (the exception `Learning.__init__` catches), and
    `_is_procedural`/`_is_frozen` answer False for a plain prose field and True for
    marked ones — the exact guards `team/hooks/learning.py:98-117` runs."""
    backend = _ProbeBackend(Notes, "probe")

    assert isinstance(backend._resolve_field("guidance"), FieldInfo)
    with pytest.raises(KeyError):
        backend._resolve_field("no_such_field")

    assert backend._is_procedural("guidance") is False
    assert backend._is_frozen("guidance") is False
    assert backend._is_procedural("recipe") is True
    assert backend._is_frozen("identity") is True


# ── 6. LocalWorker.close idempotence ──


async def test_local_worker_close_is_idempotent() -> None:
    """`runtime/worker.py:857-896`: closing an already-closed worker is a no-op, so an
    unwind path that closes a worker somebody else already closed does not crash."""
    coordinator = InMemoryCoordinator()
    worker = LocalWorker(coordinator)
    await worker.register()
    await worker.close()
    await worker.close()  # must not raise


# ── 7. Member.equip keeps the member's own tools ──


async def test_an_equipped_member_keeps_its_own_tools_alongside_the_hooks() -> None:
    """`team/members.py:133-154`: `equip` composes, `spawn` recomposes the member's own
    `tools=` ahead of the hook's — both tools on the member's wire. Distinct from
    `test_team_core.py`'s recompose test, which drives the same claim through `Team`'s
    composer; this one calls `Member.equip` directly, so the members.py seam is pinned
    even for callers that never build a `Team`."""

    @strands_tool(name="members_own", description="the member carried this itself")
    def members_own(x: str) -> str:
        return x

    @strands_tool(name="equipped_gave", description="the equipped hook contributed this")
    def equipped_gave(x: str) -> str:
        return x

    model = Counting([sighting()])
    member = Member(Scout(), "look", model=model, tools=[members_own])
    member.equip(lambda ctx: {"tools": [equipped_gave]})

    async with RuntimeHarness() as h:
        try:
            await member.spawn(h.worker.coordinator)
            result = await member.ask("the horizon")
        finally:
            await member.retire()

    assert result.target == "seen"
    specs = model.tool_specs[0]
    assert "members_own" in specs, f"the member's own tool was lost to the hook: {specs}"
    assert "equipped_gave" in specs, f"the equipped hook's tool never arrived: {specs}"
