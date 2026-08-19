# omnigent-blackboard-poc · Ownership

"Share" here is commit share over the full available history: for each folder, the fraction of commits touching that folder attributed to its top contributor, computed with `git log --pretty=format:%an -- <folder>`. The window is the entire repository history, 2026-07-18 to 2026-07-20, 27 commits total (`git rev-list --count HEAD`). Ownership is still concentrated: the repository has three in-scope contributors — `Bonk`, `bgagent`, and `impl-1` — which is below the five-contributor line at which ownership analysis stops being trivial. The two principals are `Bonk` and `bgagent`, tied at 9 in-scope commits each; `impl-1` has 1. Note that `bgagent` used to touch only `demo_app/` (excluded) and now authors the most recent kernel changeset — routing/thrash, the `formal/` Lean project, the new migration, operator scripts, and `.erpaval/` specs — so ownership of the newest code has shifted to it. Every author of a shared folder still traces to at most two people, so bus-factor risk remains high. A share above 70% signals a bus-factor of one for that path: either a single author holds it outright, or one author dominates and only one other has ever touched it. No `CODEOWNERS` file exists in the repository, so the git-derived owner is the only ownership signal available.

| Folder | Top owner | Share | Total contributors |
|---|---|---|---|
| `tests/property` | Bonk | 100% | 1 |
| `formal` | bgagent | 100% | 1 |
| `.erpaval` | bgagent | 100% | 1 |
| `sdlc_team/skills` | impl-1 | 100% | 1 |
| `src/sdlc_blackboard/interfaces` | bgagent | 71% | 2 |
| `tests/unit` | bgagent | 67% | 2 |
| `tests/contract` | bgagent | 67% | 2 |
| `scripts` | bgagent | 67% | 2 |
| `src/sdlc_blackboard/infrastructure` | bgagent | 60% | 2 |
| `tests/integration` | Bonk | 58% | 2 |
| `src/sdlc_blackboard/application` | bgagent | 54% | 2 |
| `src/sdlc_blackboard/domain` | Bonk / bgagent | 50% | 2 |
| `tests/e2e` | Bonk / bgagent | 50% | 2 |
| `tests/acceptance` | Bonk / bgagent | 50% | 2 |
| `migrations` | Bonk / bgagent | 50% | 2 |
| `sdlc_team/agents` | bgagent / impl-1 | 50% | 2 |

## Single points of failure

Five paths cross the 70% bus-factor threshold: four folders authored by a single contributor, and `src/sdlc_blackboard/interfaces`, where one author dominates and only one other has ever committed. The remaining folders sit at or below 67% shared between two authors, so they clear the threshold — but with only three in-scope contributors overall, none has a genuinely healthy bus factor.

- `tests/property` — Bonk (100%). Document the property-based test invariants and pair on the next change so the Hypothesis strategies are understood by more than one person.
- `formal` — bgagent (100%). Run a knowledge-transfer session on the Lean 4 routing/thrash proofs so a second contributor can extend the specification without the original author.
- `.erpaval` — bgagent (100%). Add a second reviewer for the EARS requirement specs so the spec-first contract behind the feature is not gated on one author.
- `sdlc_team/skills` — impl-1 (100%). Cross-train a second contributor on the skill definitions so agent capabilities can be extended without the original author.
- `src/sdlc_blackboard/interfaces` — bgagent (71%). Add a formal reviewer for the MCP tools and CLI command surface so external contract changes get a second, consistent set of eyes rather than one dominant author.
