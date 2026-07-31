#!/usr/bin/env python
"""Render the write-up to PDF via headless Chromium.

Reads report.html plus artifacts/<run>/investigation.json, injects the real run
numbers into the placeholders, and prints to A4. Chromium comes from the local
Playwright cache; nothing is downloaded.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent


def chromium() -> str:
    candidates = sorted(
        glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    )
    if not candidates:
        raise SystemExit("no chromium found in the playwright cache")
    return candidates[-1]


def transcript_html(path: Path, limit: int) -> str:
    if not path.exists():
        return "<em>no transcript captured</em>"
    rows = [line.split("\t") for line in path.read_text().splitlines() if line.strip()]
    out: list[str] = []
    for row in rows[:limit]:
        who = html.escape(row[0])
        kind = row[1] if len(row) > 1 else ""
        body = html.escape(row[2]) if len(row) > 2 else ""
        cls = {
            "tool_call": "tool",
            "thread_spawned": "spawn",
            "completed": "ok",
        }.get(kind, "")
        span = f'<span class="{cls}">{body}</span>' if cls else body
        out.append(f'<span class="who">{who:>18}</span>  {span}')
    if len(rows) > limit:
        out.append(f'<span class="who">{"":>18}</span>  ... {len(rows) - limit} more events')
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="run1")
    ap.add_argument("--out", default="pneuma-writeup.pdf")
    ap.add_argument("--transcript-lines", type=int, default=46)
    args = ap.parse_args()

    art = ROOT / "artifacts" / args.run
    data = json.loads((art / "investigation.json").read_text())
    v = data["verdict"]

    template = (HERE / "report.html").read_text()
    css = (HERE / "style.css").read_text()

    hires = [e for e in data["staffing_log"] if e["action"] == "hire"]
    delegations = [e for e in data["staffing_log"] if e["action"] == "delegate"]

    subs: dict[str, str] = {
        "CSS": css,
        "CULPRIT": f"{v['culprit_service']} / {v['culprit_change_id']}",
        "MECHANISM": v["mechanism"],
        "CONFIDENCE": f"{v['confidence']:.2f}",
        "ORACLE": "satisfied" if data["correct"] else "failed",
        "TOKENS_IN": f"{data['input_tokens']:,}",
        "TOKENS_OUT": f"{data['output_tokens']:,}",
        "TURNS": str(data["turns"]),
        "WALL": _duration(data["wall_seconds"]),
        "HEADCOUNT": str(len(hires)),
        "DELEGATIONS": str(len(delegations)),
        "CAUSAL_CHAIN": "\n".join(
            f"<li>{html.escape(s)}</li>" for s in v["causal_chain"]
        ),
        "RULED_OUT": "\n".join(f"<li>{html.escape(s)}</li>" for s in v["ruled_out"]),
        "EVIDENCE_ROWS": "\n".join(
            f"<tr><td><code>{html.escape(k)}</code></td><td>{html.escape(str(x))}</td></tr>"
            for k, x in v["evidence_by_plane"].items()
        ),
        "HIRE_ROWS": "\n".join(
            f"<tr><td><code>{html.escape(e['name'])}</code></td>"
            f"<td><code>{html.escape(e['role'])}</code></td>"
            f"<td>{html.escape(e['mandate'])}</td></tr>"
            for e in hires
        )
        or '<tr><td colspan="3">The lead hired nobody on this run.</td></tr>',
        "TRANSCRIPT": transcript_html(art / "transcript.txt", args.transcript_lines),
        # The per-plane ambiguity is the dataset's own machine-checked property,
        # asserted by incident.self_check(), rather than anything the models said.
        "FINDINGS_ROWS": "\n".join(
            f"<tr><td><code>{html.escape(plane)}</code></td>"
            f"<td>{len(cands)}</td>"
            f"<td>{html.escape(', '.join(c.replace('_', ' ') for c in cands))}</td></tr>"
            for plane, cands in _ambiguity().items()
        ),
    }

    rendered = template
    for key, value in subs.items():
        rendered = rendered.replace("{{" + key + "}}", value)

    leftover = re.findall(r"\{\{(\w+)\}\}", rendered)
    if leftover:
        print(f"warning: unsubstituted placeholders {sorted(set(leftover))}", file=sys.stderr)

    tmp = HERE / ".rendered.html"
    tmp.write_text(rendered)
    out = ROOT / args.out

    subprocess.run(
        [
            chromium(),
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out}",
            tmp.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


def _duration(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest}s" if minutes else f"{rest}s"


def _ambiguity() -> dict[str, tuple[str, ...]]:
    """The planted per-plane ambiguity, read from the dataset itself."""
    sys.path.insert(0, str(ROOT / "src"))
    from pneuma.demo import incident

    return incident.single_plane_ambiguity()


if __name__ == "__main__":
    sys.exit(main())
