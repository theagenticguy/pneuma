# A truncated sweep must withhold, never settle "works"; asymmetry rule for probe verdicts

**Category**: verification
**Tags**: detect, probes, three-valued-verdict, truncation, gaming, gate-fitting
**Modules**: src/pneuma/detect/gaming.py, src/pneuma/detect/discrimination.py
**Session**: session-b84f7e (2026-08-09)

## Lessons (verified by execution)

1. **Positive evidence gathered under a budget cut is not evidence — the exploit may sit in
   the unexamined tail.** First cut of `probe_gate_fitting` let contained candidates settle
   `works` even when the sweep was truncated; the could-not-tell test caught it. Fix shape:
   `separating = 0 if self.withheld else self.contained` (gaming.py:154) — containment counts
   only over a COMPLETED sweep. The asymmetry is the rule: a witnessed violation (an Exploit)
   settles `decoration` even under truncation, because truncation cannot fabricate a positive
   witness; the absence of violations under truncation settles nothing. Same rule as
   vacuity's witnessed-violation precedent — apply it to every new probe.

2. **Write the could-not-tell test first when adding a probe.** It is the verdict most likely
   to be silently wrong (fallback paths and early returns bias toward True/False), and here
   it was the test that found the real bug, not the works/decoration pair.
