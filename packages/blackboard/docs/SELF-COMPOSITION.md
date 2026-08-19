# Self-composition — pointing the SDLC team at itself

This is the design for four capabilities the runtime can grow **for itself**: dynamic team
composition, just-in-time team members, different types of verifier members, and multi-team
composition. Each is framed as a **goal the SDLC team can run against this repository** — the
dogfood case study that exercises the repo-agnostic runbook (`docs/RUNBOOK.md`) with the target
set to this repo.

Nothing here is built yet. This document is the roadmap; the sections below say exactly which
axis each feature lives on, which files it touches, how it is exercised as a goal, how the
existing gate validates it, and its risks.

## The two axes (the spine of every decision)

Every composition question resolves to one of two axes:

- **Authority axis — `ActorKind`** (`src/sdlc_blackboard/domain/common.py:25`). A **closed**
  `StrEnum` of 18 bounded contexts, and it is **triple-locked**:
  1. the Python enum + the `PRODUCER_KINDS` / `REVIEWER_KINDS` frozensets (`common.py:58,71`);
  2. a Lean `inductive` with machine-checked totality and cost-monotonicity proofs
     (`formal/Blackboard/Routing.lean`, gated by `mise run formal`);
  3. a hand-transcribed contract test binding Python ↔ Lean (`tests/contract/test_routing_policy.py`).
  `ROUTING_POLICY` (`domain/routing.py:32`) must stay **total** over the enum. Adding a kind is a
  coordinated, atomic edit across all three, plus a routing-policy row. **No DB migration** — the
  `tasks.required_actor_kind` column is free `text` (enum discipline is enforced in Python only).

- **Composition axis — `sdlc_team/**` config + the lead's skills.** Which agents exist, how many,
  which personas, and when they are spawned. Behavior is driven by the **task contract, not the
  kind label** (the `ActorKind` docstring says so explicitly). Crucially, **N:1 persona→kind is
  already native**: the three `implementation_*` agents all map to the single `implementation`
  kind. Multiple personas per authority kind is the existing pattern, not a new capability.

**The rule of thumb:** stay on the composition axis. Only touch the authority axis when a
genuinely new *blocking-authority slice* is needed — a governing power the 18 kinds don't cover.
`VISUAL` (`common.py:81`) is the one precedent: it was "the axis the human carried by hand across
resort rev1–4," later promoted into the gated loop — the template for a real new kind.

## Why most of this is cheap: the gate is contract-derived

The release gate's required-review set is **not hardcoded**. `gate_service.required_review_types`
(`gate_service.py:134`) unions the `review_type` strings of every **blocking** `ReviewRequirement`
across the goal's task contracts. `review_type` is **free text** — it need not equal an
`ActorKind`. So:

> Declaring a blocking `ReviewRequirement{reviewer_kind: <existing kind>, review_type: "<any
> string>"}` makes `<any string>` a gate condition **automatically, with zero kernel change**.

The only closed pieces are (a) the `("quality","security")` fallback used when a goal declares no
blocking reviews, and (b) the `REVIEWER_KINDS` frozenset, which gates *blocking-finding creation*
authority (`open_finding` requires the task's `required_actor_kind ∈ REVIEWER_KINDS` **and**
`contract.may_create_blocking_finding`).

## JIT is a first-class Omnigent capability

Verified in the installed omnigent 0.5.1 runner:

- **`sys_session_send`** reaches only **pre-declared** sub-agents — its argument enum is the keys
  of the static `agents:` list. It cannot target an agent that isn't in the roster.
- **`sys_session_create`** has two modes (`runner/tool_dispatch.py`), exactly one required:
  - `agent_id=…` — launch an already-registered agent;
  - **`config_path=…` — upload a NEW agent from a config YAML / directory / `.tar.gz` the
    orchestrator authored on local disk (via `sys_os_write`), then spawn it as a child.**
  This is true JIT: the lead can synthesize a specialist at runtime and launch it with no static
  roster entry. Both modes force `parent_session_id = caller` (child-only).
- **`sys_agent_list`** surfaces built-ins, session-bound agents, and locally-authored config
  YAMLs (it scans the cwd); `sys_agent_get` / `sys_agent_download` inspect or fork an existing
  agent. `spawn_bounds` (`sdlc_team/config.yaml`) caps `max_dispatches_per_turn: 8` over
  `[sys_session_send, sys_session_create]`.

---

## Feature 1 — Different *types* of verifier members (sharing an existing kind)

**Axis:** composition-only. Zero kernel change.

Several verifiers share an existing `REVIEWER_KIND` (e.g. `security`, `quality`) but run different
*strategies*, each a distinct free-text `review_type`: adversarial, property-based, differential,
LLM-judge, fuzzing, etc.

**Files:**
- New reviewer configs, e.g. `sdlc_team/agents/security_adversarial/config.yaml`,
  `security_property/config.yaml`, `quality_differential/config.yaml`, `quality_llm_judge/config.yaml`
  — modeled on `sdlc_team/agents/quality/config.yaml` (read-only reviewer shape: `cwd: .`,
  `write_paths: ./artifacts/<role>` or `read_only_os` per ADR-0009; blackboard tool allowlist
  including `open_finding` + `submit_review`). Cheap routing is automatic
  (`default_routing_class(SECURITY) = IN_REGION_RUNTIME`).
- Register each in the lead's `agents:` list (`sdlc_team/config.yaml`); document in
  `sdlc_team/ROSTER.md`; optionally extend `sdlc_team/skills/dispatch-task/SKILL.md` with when to
  fan out strategies.
- **No** edit to `domain/common.py`, `routing.py`, the Lean model, or the contract test.

**Dogfood goal:** *"Harden this repo's own security review by adding adversarial and
property-based security verifiers, and prove they gate a real change."* Seed against this repo
(`--scope src/sdlc_blackboard`) with blocking reviews:
```
--review security:security --review security:security_adversarial --review security:security_property
```

**How the gate validates it:** `required_review_types` now includes `security_adversarial` and
`security_property`; the goal stays `UNSATISFIED` until each lands an `APPROVED`, non-stale,
revision-bound review. Mirrors `tests/integration/test_multi_context_gate.py`.

**Risks (low):** two verifiers of the same kind create two review *tasks* keyed
`<producer>:review:<type>` — the key includes `review_type`, so uniqueness holds (ADR-0007
reopen-not-recreate). `spawn_bounds` waves large fan-outs. The lead's dispatch match is
LLM-driven and ungated, but the *gate* enforces whatever `review_type`s the contract declares
regardless of who was dispatched — so the gate is the backstop.

---

## Feature 2 — Just-in-time team members

**Axis:** composition-only (uses Omnigent's native JIT). Kernel change only if the JIT member
needs a brand-new authority slice — then it degrades to Feature 4b.

**Files:**
- No new *committed* config is required by design — the lead authors a config YAML to disk via
  `sys_os_write`, then `sys_session_create(config_path=…)`. But add a target-agnostic skeleton,
  `sdlc_team/templates/jit_reviewer.config.yaml` (read-only reviewer, local sandbox, blackboard
  allowlist), so authored members are consistent and pass `team:validate`.
- Extend `sdlc_team/LEAD.md` / `sdlc_team/skills/dispatch-task/SKILL.md` with the JIT protocol:
  author config → `sys_session_create` → `bind_runtime_session` (the lead already binds
  conversation ids).
- Optionally extend `scripts/validate_team.py` to also scan a JIT-output directory, so authored
  configs are proven local-sandbox-only before dispatch.

**Dogfood goal:** *"When a goal needs a verifier the static roster lacks, author it just-in-time
and prove it participates in the gate."* Mid-run, the lead discovers a needed strategy (say a
`quality_differential` check), writes its config, spawns it via `sys_session_create`, and attaches
a blocking `ReviewRequirement{reviewer_kind: quality, review_type: quality_differential}`.

**How the gate validates it:** same mechanism as Feature 1 — the derived required-review set
forces the JIT member's review to land. Additionally run `scripts/validate_team.py` on the
authored config to prove `sandbox.type ∈ {none, linux_bwrap, …}` before dispatch.

**Risks:** JIT-authored configs are unvalidated until `team:validate` runs — a config resolving to
a managed host / MicroVM would violate ADR-0010, so **mandate validating authored configs before
dispatch**. `sys_session_send` cannot reach a JIT member (its enum is the static list) — the lead
**must** drive it via the handle returned by `sys_session_create`. Each create counts toward the
8-per-turn `spawn_bounds` cap. Authoring a member is an ungated LLM decision; the gate remains the
backstop.

---

## Feature 3 — Dynamic team composition

**Axis:** composition-only. This makes the lead's existing job ("engage only the contexts a goal
needs — a small fix may use 3, a regulated launch all 16", `sdlc_team/LEAD.md`) explicit and
testable. Zero kernel change.

**Files:**
- Strengthen the composition policy in `sdlc_team/skills/initialize-goal/SKILL.md` and
  `sdlc_team/skills/dispatch-task/SKILL.md`: a decision rubric mapping goal characteristics →
  which `ActorKind`s to engage and which blocking `ReviewRequirement`s to attach.
- `scripts/new_goal.py` is the mechanism for expressing a composition as data (repeatable
  `--review kind:type` flags → the derived gate).
- No kernel files touched.

**Dogfood goal:** *"Given a change to this repo's own gate logic (`gate_service.py`), dynamically
compose the minimal reviewing team the change's risk profile demands, and justify the inclusions
and exclusions in the analysis artifact."* The analyst/architect produce a composition-decision
artifact; the lead instantiates exactly those `ReviewRequirement`s.

**How the gate validates it:** the composed set of blocking `review_type`s *is* the gate. If the
lead under-composes (omits a needed reviewer), that's auditable — the intended set lives in the
analysis artifact and the human-release step, and divergence shows in `blackboard events` /
`snapshot`.

**Risks:** the composition decision is pure LLM judgment with **no kernel guardrail** — the kernel
enforces whatever contracts exist, not whether the *right* ones were chosen. Mitigation: make the
composition an explicit artifact so QA/architect can open a blocking finding on an inadequate
composition.

---

## Feature 4 — Multi-team composition

Two parts, on different axes.

### 4a — Nested teams (composition-only)

A sub-agent that is itself an orchestrator (`spawn: true`) with its own roster and `spawn_bounds`.

**Files:**
- A sub-orchestrator config, e.g. `sdlc_team/agents/subteam_lead/config.yaml`, modeled on
  `sdlc_team/config.yaml` (its own `agents:` list, its own `spawn_bounds`).
- Register `subteam_lead` in the top lead's `agents:` list.
- **Blackboard partitioning (design decision):** prefer **one goal, scope-prefix per sub-team**
  (`scope: ["src/.../subsystemA"]`) with each sub-lead owning its tasks under the shared goal, so
  the top-level gate stays a single derived union across all reviews. Cross-team sequencing uses
  `dependency_task_ids`. Separate-goal-per-team is the fallback when teams must gate
  independently.

**Dogfood goal:** *"Split this repo's build into a kernel sub-team and a team-runtime sub-team;
each sub-lead composes and drives its own reviewers; the top lead integrates and gates the
whole."*

**How the gate validates it:** the single goal's gate unions review types across both sub-teams'
tasks — no kernel change; validated via `mise run check` + the live gate loop.

**Risks:** `spawn_bounds` is per-orchestrator — nested spawning multiplies fan-out, so each
sub-lead needs its own cap. Watch for blackboard scope collisions if two sub-teams write the same
`logical_name`.

### 4b — A genuinely new blocking-authority slice (kernel lockstep)

Only when a new governing power is truly needed (e.g. a `privacy` context distinct from
`compliance`). This is the **highest-ceremony** change — a single atomic coordinated edit:

- `src/sdlc_blackboard/domain/common.py` — add the `ActorKind` member **and** add it to
  `PRODUCER_KINDS` or `REVIEWER_KINDS`.
- `src/sdlc_blackboard/domain/routing.py` — add the `ROUTING_POLICY` row (must stay total).
- `formal/Blackboard/Routing.lean` — add the constructor to `inductive ActorKind` and the
  `routingPolicy` case; `mise run formal` re-checks totality + cost monotonicity.
- `tests/contract/test_routing_policy.py` — add the row to `_EXPECTED` (count moves 18 → 19).
- A new reviewer agent config; register in the roster.
- **No DB migration** (the column is free text).

**Dogfood goal:** *"Introduce a new governing authority slice (e.g. `privacy`, distinct from
`compliance`) end to end — including the Lean proof and the contract test."*

**Risks:** the triple-locked enum. A Python-only edit without the matching Lean constructor +
`lake build` breaks the contract, and the 18-row contract-test pin fails. Because `mise run
formal` is **not** in `check.depends`, a 4b change could pass `mise run check` while the Lean
proof is stale — so `mise run formal` is **mandatory** for any enum change.

---

## Sequencing

1. **Feature 1 (verifier types)** — first. Pure composition, zero kernel risk, exercises the
   entire loop (compose → dispatch → review → findings → gate → human approval). The lowest-risk
   dogfood proof.
2. **Feature 3 (dynamic composition)** — second. Generalizes Feature 1 into a lead policy; reuses
   `scripts/new_goal.py`. No new mechanism.
3. **Feature 2 (JIT members)** — third. Composition-only but introduces a new runtime mechanism
   (`sys_session_create(config_path)` + authored-config validation discipline); sequence after the
   static-composition loop is proven.
4. **Feature 4** — last. 4a (nested teams) is composition and can slot alongside Feature 3; 4b
   (new authority slice) is the highest-ceremony item and should be done only when a genuinely new
   blocking slice is required, as one atomic coordinated change with `mise run formal` mandatory.

## Verification recipe

- **`mise run team:validate`** — every config (including new/JIT reviewers and any nested
  sub-lead) parses through real omnigent and resolves to a **local** sandbox; the count moves
  `18/18 → N/N` as agents are added (the ADR-0010 guardrail; covers Features 1, 2, 4a).
- **`mise run check`** (lint + typecheck + test) — the kernel; for 4b, the contract test's row
  count moves 18 → 19.
- **`mise run formal`** (`lake build`) — the Lean totality/cost proofs after any `ActorKind`
  change (4b). **Must be run explicitly** — it is not in `check.depends`.
- **`mise run demo`** — the deterministic, no-LLM gate + remediation loop; prove new
  `review_type`s gate before spending live tokens.
- **Live loop** — bootstrap → `mise run mcp` → health → seed with `scripts/new_goal.py` →
  `mise run team` → watch `uv run blackboard gate <goal-id>` go `UNSATISFIED` → `HUMAN_REQUIRED`
  → `SATISFIED` after `record_human_approval` + `authorize_goal_completion`. Per-feature
  acceptance: Features 1/3 — the gate blocks on the new/composed `review_type`s; Feature 2 — the
  authored config passes `team:validate` before dispatch and the JIT session participates via its
  `sys_session_create` handle; Feature 4a — the single goal's gate unions both sub-teams' reviews;
  Feature 4b — `mise run formal` green + contract test at 19 rows + `team:validate` shows the new
  reviewer.
