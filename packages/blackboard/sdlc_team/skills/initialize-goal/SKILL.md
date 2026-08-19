---
name: initialize-goal
description: Convert a human objective into a blackboard goal with measurable success criteria and an initial work graph.
user-invocable: false
---

# Initialize Goal

1. Convert the human objective into measurable success criteria and constraints.
2. Call `create_goal`.
3. Create an analysis task unless requirements are already explicit.
4. Do not create implementation work until required analysis artifacts exist.
5. Return the goal ID and work graph.
