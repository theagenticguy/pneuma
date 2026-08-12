"""The timeline demo, asserted from its records rather than a terminal.

`demo.timeline.Timeline` collects `TimelineRecord`s as the coordinator drives its
callback; these tests run the demo's own scripted team (`timeline_demo.build_team` —
offline, every model a `ScriptedModel`) and assert the records carry the run's shape:
member consults, the discovery fan-out, the critic's revision, the lead's completion.
Rendering is asserted through `Console(record=True)` export, no TTY involved.
"""

from __future__ import annotations

from ai_functions import InMemoryCoordinator, LocalWorker
from rich.console import Console

from pneuma.demo.timeline import Timeline
from pneuma.demo.timeline_demo import QUESTION, build_team, run_with_timeline


async def test_the_records_carry_the_runs_shape() -> None:
    """Consults for both members, one discovery, one revision, and a completion."""
    console = Console(record=True, force_terminal=False, width=100)
    run, timeline = await run_with_timeline(console, live=False)

    kinds = [record.kind for record in timeline.records]
    consults = [r.text for r in timeline.records if r.kind == "consult"]
    assert any("north-analyst.read" in text for text in consults), consults
    assert any("south-analyst.read" in text for text in consults), consults

    discoveries = [r for r in timeline.records if r.kind == "discovery"]
    assert len(discoveries) == 1
    assert "north-analyst.read" in discoveries[0].text, "attributed to the poster"

    assert "revise" in kinds, "the critic's objection must appear as a revision row"
    assert "done" in kinds
    assert "failed" not in kinds
    assert "change 8842" in str(run.answer), "the revised ruling is the final answer"


async def test_the_static_render_names_the_lanes_and_draws_the_discovery() -> None:
    console = Console(record=True, force_terminal=False, width=140)
    _, timeline = await run_with_timeline(console, live=False)

    console.print(timeline.render_static())
    text = console.export_text()
    for lane in ("decide-lead", "north-analyst.read", "south-analyst.read", "red-team.review"):
        assert lane in text, f"lane {lane!r} missing from the rendered timeline"
    assert "★" in text, "the discovery glyph must appear"
    assert "↻" in text, "the revision glyph must appear"


async def test_unsubscribe_detaches_the_timeline() -> None:
    """After `.unsubscribe()`, further events add nothing — the subscription is the tap."""
    console = Console(record=True, force_terminal=False, width=100)
    timeline = Timeline(console=console)
    coordinator = InMemoryCoordinator()
    worker = LocalWorker(coordinator)
    await worker.register()
    try:
        subscription = timeline.attach(coordinator)
        subscription.unsubscribe()
        team = build_team()
        await team.run(QUESTION, coordinator)
    finally:
        await worker.close()

    assert timeline.records == [], "a detached timeline must record nothing"
