"""Validate the SDLC team through the installed Omnigent parser.

Run under the PyPI-pinned interpreter so the proof is hermetic:

    uvx --from "omnigent==0.5.1" python scripts/validate_team.py sdlc_team
    # or: mise run team:validate

For every agent (the lead + each specialist) it parses the config through the real
``omnigent.spec.parse`` and prints the resolved ``os_env.sandbox.type``. This is the
guardrail behind ADR-0010: the team must parse with 0 errors and resolve only to LOCAL
sandbox backends (``none`` / ``linux_bwrap`` / ``darwin_seatbelt``) — never a managed
host or MicroVM. Exit code is non-zero if any config fails to parse or resolves to a
non-local backend.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Local, in-process sandbox backends. A managed-host / MicroVM run would surface a
# different backend here (or require host_type=managed, which this team never sets).
LOCAL_BACKENDS = {"none", "linux_bwrap", "darwin_seatbelt", "windows_jobobject"}


def main() -> int:
    try:
        from omnigent.spec import parse
        from omnigent.version import VERSION
    except Exception as exc:  # pragma: no cover - import guard
        print(f"cannot import omnigent: {exc}", file=sys.stderr)
        print("run me under the pinned interpreter: mise run team:validate", file=sys.stderr)
        return 2

    root = Path(sys.argv[1] if len(sys.argv) > 1 else "sdlc_team")
    if not root.is_dir():
        print(f"team dir not found: {root}", file=sys.stderr)
        return 2

    agents = [root, *sorted(p for p in (root / "agents").glob("*") if p.is_dir())]
    print(f"omnigent {VERSION}: parsing {len(agents)} configs under {root}/\n")

    failures = 0
    for agent in agents:
        try:
            spec = parse(agent)
        except Exception as exc:
            failures += 1
            print(f"FAIL  {agent.name:32s} {type(exc).__name__}: {exc}")
            continue

        os_env = getattr(spec, "os_env", None)
        sandbox = getattr(os_env, "sandbox", None) if os_env else None
        sandbox_type = getattr(sandbox, "type", None) if sandbox else "none"
        executor = getattr(getattr(spec, "executor", None), "type", "?")

        if sandbox_type not in LOCAL_BACKENDS:
            failures += 1
            marker = "FAIL"
        else:
            marker = "OK  "
        print(f"{marker}  {agent.name:32s} executor={executor!s:10s} sandbox.type={sandbox_type}")

    print(f"\n{len(agents) - failures}/{len(agents)} parsed and resolved to a local sandbox.")
    if failures:
        print(f"{failures} problem(s) — see FAIL rows above.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
