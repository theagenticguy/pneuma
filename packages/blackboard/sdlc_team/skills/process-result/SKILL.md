---
name: process-result
description: Read a completed child's authoritative blackboard result and decide the next organizational action (review, remediation, retry).
user-invocable: false
---

# Process Result

1. Read the child inbox message.
2. Read the authoritative goal snapshot.
3. Confirm the expected task state changed.
4. Ignore claims not persisted in the blackboard.
5. If SUBMITTED, dispatch required reviews.
6. If AWAITING_INPUT, answer from accepted artifacts or ask a human.
7. If FAILED, retry through a new run or replan.
8. If STALE_ASSIGNMENT, reject the result.
