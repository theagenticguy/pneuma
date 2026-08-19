"""The original incident war-room demo: a self-staffing agent hierarchy over synthetic evidence.

This is a case study, not part of the reusable core. Nothing under `detect/`, `memory/`,
`process/`, `casestudy/`, or `method.py` imports anything from here, and the dependency runs
one way only: `demo` reaches up to `..model` and `..method`, never the reverse. The shipping
console script (`pneuma = pneuma.demo.cli:main`) is the only entry point into it.
"""
