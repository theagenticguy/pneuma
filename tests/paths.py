"""Fixture and source-tree paths, plus availability markers, resolved once from the repo root.

Resolving from this file rather than from each test module's own depth is what keeps the
suite's directory layout free to change: a marker built on the caller's `parents[1]` starts
reporting "needs data/receipt.xes" on a repo that has it as soon as the file moves down a
level, and a skip reads like a pass.

`SRC` carries the same hazard in a form no marker guards. A test that globs a source
directory to build its parametrisation collects zero cases from a path that resolves
nowhere, and an empty parametrisation is reported as nothing at all rather than as a
failure — so a module deriving `SRC` itself would go quiet on the same move.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = ROOT / "src"

PERMITS = DATA / "receipt.xes"
FINES = DATA / "roadfines.xes"
FLEET = DATA / "transcripts_fleet.json"
SAMPLE = DATA / "transcripts_sample.json"

needs_permits = pytest.mark.skipif(not PERMITS.is_file(), reason="needs data/receipt.xes")
needs_fines = pytest.mark.skipif(not FINES.is_file(), reason="needs data/roadfines.xes")
needs_fleet = pytest.mark.skipif(not FLEET.is_file(), reason="needs data/transcripts_fleet.json")
needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason="needs data/transcripts_sample.json")
