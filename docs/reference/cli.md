# pneuma · CLI

The `pneuma` CLI is a single-command entry point with no subcommands, declared as `pneuma = "pneuma.demo.cli:main"` in `pyproject.toml:21-22`.

## pneuma

```
usage: pneuma [-h] [--max-hires MAX_HIRES] [--out OUT] [--quiet] [--truth]

Run one war-room investigation and write the artifacts.

options:
  -h, --help            show this help message and exit
  --max-hires MAX_HIRES
  --out OUT
  --quiet               disable color in captured output
  --truth               print ground truth and exit
```

Runs one war-room incident investigation to completion and writes its artifacts, exiting `0` when the oracle accepts the verdict and `1` when it rejects it.

`src/pneuma/demo/cli.py:117-145`

Flags:

- `-h`, `--help` — show the help message and exit; supplied by `argparse.ArgumentParser`, which is constructed with `prog="pneuma"` and `description=__doc__`. `src/pneuma/demo/cli.py:118`.
- `--max-hires` — cap on the subagents the lead may hire for itself; `type=int`, `default=3`, passed through to `WarRoom(question=..., max_hires=...)`. `src/pneuma/demo/cli.py:119`.
- `--out` — directory the run writes into; `type=Path`, `default=Path("artifacts")`, created with `out.mkdir(parents=True, exist_ok=True)` before the run starts. `src/pneuma/demo/cli.py:120`.
- `--quiet` — disable color in captured output; `action="store_true"`, forwarded as `no_color` to the recording `rich` `Console`. `src/pneuma/demo/cli.py:121`.
- `--truth` — print the demo's planted ground truth as JSON and exit `0` without running the investigation; `action="store_true"`. `src/pneuma/demo/cli.py:122`.

**Artifacts written.** Three files are written into the `--out` directory on a normal run:

- `investigation.json` — the `Investigation` result serialized with `model_dump_json(indent=2)`, written before teardown so a failure on the shutdown path cannot lose the run. `src/pneuma/demo/cli.py:56`.
- `transcript.txt` — the joined `Tape` lines, flushed every 20 seconds during the run and again after teardown so an interrupted run still leaves a usable transcript. `src/pneuma/demo/cli.py:71-78`.
- `console.txt` — the recorded console output via `console.export_text()`. `src/pneuma/demo/cli.py:67`.

**Exit codes.**

- `0` — `--truth` completed, or the run finished and the oracle accepted the verdict. `src/pneuma/demo/cli.py:142`, `src/pneuma/demo/cli.py:145`.
- `1` — the run finished and the oracle rejected the verdict (`result.correct` is false). `src/pneuma/demo/cli.py:145`.

**`--truth` output shape.** `--truth` prints a JSON object with the keys `culprit_service`, `culprit_change_id`, `mechanism`, `onset`, `decoys`, and `single_plane_ambiguity`, read from `incident.GROUND_TRUTH` and `incident.single_plane_ambiguity()`. `src/pneuma/demo/cli.py:125-142`.

## See also

- [Data flow][data-flow] — 2 shared source files
- [Module map][module-map] — 2 shared source files
- [System overview][system-overview] — 2 shared source files
- [Processes][processes] — 2 shared source files
- [Dependency graph][dependency-graph] — 2 shared source files

[data-flow]: ../architecture/data-flow.md
[module-map]: ../architecture/module-map.md
[system-overview]: ../architecture/system-overview.md
[processes]: ../behavior/processes.md
[dependency-graph]: ../diagrams/structural/dependency-graph.md
