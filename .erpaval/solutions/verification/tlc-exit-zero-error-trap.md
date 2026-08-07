# TLC can exit 0 after printing an Error: the success line is the gate

**Category**: verification
**Tags**: tla, tlc, exit-codes, parser, silent-failure
**Modules**: src/pneuma/process/tla.py
**Session**: session-5abb9e (2026-08-07)

## Lesson

An unevaluable temporal property (and other partial failures) makes TLC 2.19
exit **0** while printing an `Error:` line and *never* printing
`Model checking completed. No error has been found.` — a zero-exit run that
checked nothing. Web research (and TLC's own EC.java constants, e.g.
FAILURE_LIVENESS_EVAL=77) suggested a nonzero exit; direct probing showed 0.

Guard shape that catches it (already in `_parse`): a pass must survive every
gate at once — exit code in the property set, success line present, no
`failure` captured, nonzero states explored. Any single-signal check
(exit==0, or success-string grep alone) reads this as verified.

Pinned by the `_LIVENESS_EVAL_FAILED` canned-output test in
tests/library/test_process.py (real TLC output, real exit code — the file's
convention: test the parser against the checker's own vocabulary).

Meta-lesson (rhymes with the verify-before-asserting rule): even a
high-confidence research claim sourced from the tool's own source code was
wrong about runtime behavior; the implementer's cheap direct probe caught it.
Probe the actual binary before pinning a parser to a documented exit code.
