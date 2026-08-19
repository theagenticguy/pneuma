---
name: dispatch-task
description: Claim one READY task and dispatch the matching specialist child session, binding its conversation id.
user-invocable: false
---

# Dispatch Task

1. Read the goal snapshot.
2. Select one READY task.
3. Match `required_actor_kind` to a specialist.
4. Call `claim_task` with a stable command ID.
5. Dispatch through `sys_session_send`.
6. Include task ID and assignment epoch.
7. Bind the returned conversation ID with `bind_runtime_session`.
8. If dispatch fails, release or fail the assignment.
9. Do not claim multiple tasks unintentionally.

## Same-kind strategy fan-out

One `ActorKind` may have several personas running different review *strategies* (e.g.
`security`, `security_adversarial`, `security_property` all map to the `security` kind).
When a review task's `review_type` names a strategy variant, match it to the persona of
that strategy rather than the base kind. Each distinct `review_type` is its own gate
condition and its own review task (key `<producer>:review:<type>`), so fan out one
dispatch per declared strategy. `spawn_bounds` waves large fan-outs across turns.
