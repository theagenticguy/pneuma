---
name: evaluate-gate
description: Evaluate the release gate, resolve missing or stale reviews and blockers, collect human approval, and authorize completion.
user-invocable: false
---

# Evaluate Gate

1. Call `get_gate_status`.
2. Dispatch missing reviews.
3. Replace stale reviews.
4. Create remediation for blockers.
5. Ask the human when approval is missing.
6. Record the human approval with `record_human_approval`.
7. Call `authorize_goal_completion` only when the gate is satisfied.
