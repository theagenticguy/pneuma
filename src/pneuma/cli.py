"""Run one war-room investigation and write the artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ai_functions import InMemoryCoordinator, LocalWorker
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import incident
from .live import Tape
from .warroom import PLANES, Investigation, WarRoom

QUESTION = (
    "Find the root cause of this incident. Name the culprit service, the change id, "
    "and the mechanism."
)


async def investigate(*, max_hires: int, out: Path, quiet: bool) -> Investigation:
    console = Console(record=True, width=120, no_color=quiet)
    tape = Tape(console=console)

    console.print(
        Panel(
            f"[bold]{len(PLANES)} specialists[/], one lead holding no evidence, "
            f"up to {max_hires} self-hired subagents.\n"
            f"Ground truth is planted and checked by a post-condition; "
            f"{len(incident.MECHANISMS)} mechanisms are allowed.",
            title="pneuma war room",
            border_style="cyan",
        )
    )

    out.mkdir(parents=True, exist_ok=True)
    coordinator = InMemoryCoordinator()
    worker = await LocalWorker(coordinator).register()
    subscription = tape.watch(coordinator)

    room = WarRoom(question=QUESTION, max_hires=max_hires)
    handle = await coordinator.spawn(room, thread_name=room.name)

    # A full xhigh run takes tens of minutes; flush the tape as it grows so an
    # interrupted run still leaves a usable transcript behind.
    flusher = asyncio.create_task(_flush_periodically(tape, out))
    try:
        result: Investigation = await handle.run("")
        # Persist before teardown: half an hour of xhigh reasoning should not be
        # lost to a failure on the shutdown path.
        (out / "investigation.json").write_text(result.model_dump_json(indent=2))
    finally:
        flusher.cancel()
        subscription.unsubscribe()
        _write_tape(tape, out)
        await handle.terminate_now()
        await worker.close()
        await asyncio.sleep(0.1)

    _report(console, result, tape)
    _write_tape(tape, out)
    (out / "console.txt").write_text(console.export_text())
    return result


def _write_tape(tape: Tape, out: Path) -> None:
    (out / "transcript.txt").write_text("\n".join(tape.lines))


async def _flush_periodically(tape: Tape, out: Path, every: float = 20.0) -> None:
    while True:
        await asyncio.sleep(every)
        _write_tape(tape, out)


def _report(console: Console, result: Investigation, tape: Tape) -> None:
    v = result.verdict
    verdict_table = Table(show_header=False, box=None)
    verdict_table.add_row("culprit", f"{v.culprit_service} / {v.culprit_change_id}")
    verdict_table.add_row("mechanism", v.mechanism)
    verdict_table.add_row("confidence", f"{v.confidence:.2f}")
    verdict_table.add_row("oracle", "[green]correct[/]" if result.correct else "[red]wrong[/]")
    if result.oracle_failures:
        verdict_table.add_row("failures", "; ".join(result.oracle_failures))
    console.print(
        Panel(verdict_table, title="verdict", border_style="green" if result.correct else "red")
    )

    console.print("[bold]causal chain[/]")
    for i, step in enumerate(v.causal_chain, 1):
        console.print(f"  {i}. {step}")

    console.print("\n[bold]decoys dismissed[/]")
    for item in v.ruled_out:
        console.print(f"  - {item}")

    if result.staffing_log:
        console.print("\n[bold]subagents the lead hired for itself[/]")
        for entry in result.staffing_log:
            if entry["action"] == "hire":
                console.print(f"  {entry['name']} ({entry['role']}): {entry['mandate']}")

    stats = Table(show_header=False, box=None)
    stats.add_row("tool calls", ", ".join(f"{k}x{n}" for k, n in sorted(tape.tool_calls.items())))
    stats.add_row("threads spawned", str(len(tape.spawns)))
    stats.add_row("tokens", f"{result.input_tokens:,} in / {result.output_tokens:,} out")
    stats.add_row("turns", str(result.turns))
    stats.add_row("wall clock", f"{result.wall_seconds}s")
    console.print(Panel(stats, title="run", border_style="blue"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="pneuma", description=__doc__)
    parser.add_argument("--max-hires", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    parser.add_argument("--quiet", action="store_true", help="disable color in captured output")
    parser.add_argument("--truth", action="store_true", help="print ground truth and exit")
    args = parser.parse_args()

    if args.truth:
        truth = incident.GROUND_TRUTH
        print(
            json.dumps(
                {
                    "culprit_service": truth.culprit_service,
                    "culprit_change_id": truth.culprit_change_id,
                    "mechanism": truth.mechanism,
                    "onset": truth.onset_ts,
                    "decoys": list(truth.decoy_change_ids),
                    "single_plane_ambiguity": {
                        k: list(v) for k, v in incident.single_plane_ambiguity().items()
                    },
                },
                indent=2,
            )
        )
        return 0

    result = asyncio.run(investigate(max_hires=args.max_hires, out=args.out, quiet=args.quiet))
    return 0 if result.correct else 1


if __name__ == "__main__":
    sys.exit(main())
