# SDLC Team Roster — bounded contexts, not personas

Each specialist is a **bounded context**: a slice of organizational authority (who may
produce artifacts, who may open blocking findings, who governs the release gate).
Behavior is driven by the **task contract** the lead writes, never by the label
(HANDOFF §29: "a function name is an organizational convenience; the task contract
drives behavior"). The kernel treats every reviewing context uniformly — adding one to
the roster and giving the implementation task a blocking review requirement for it makes
it a gate condition automatically, with no kernel change (HANDOFF §28).

The lead composes the roster per goal. A small feature may engage 3 contexts; a
regulated launch may engage all 17.

## Producing contexts (author + submit artifacts)

| Specialist | `ActorKind` | Harness / model | Produces |
|---|---|---|---|
| `analyst` | analyst | claude-sdk / Opus 4.8 | requirements, acceptance criteria, open decisions |
| `architect` | architect | claude-sdk / Opus 4.8 | architecture-decision record, interface contracts |
| `implementation_claude_opus` | implementation | claude-sdk / Opus 4.8 | source revision + verification evidence |
| `implementation_claude_fable` | implementation | claude-sdk / Fable 5 | source revision (long-horizon profile) |
| `implementation_codex_sol` | implementation | codex-native / GPT-5.6 Sol | source revision (cross-model replication) |
| `data_engineer` | data | claude-sdk / Opus 4.8 | migrations, schema/data contracts, backfill plans |
| `documentation` | documentation | claude-sdk / Fable 5 | docs, API reference, operator runbook |
| `ux` | ux | claude-sdk / Fable 5 | contract-shape + error-taxonomy guidance |

## Reviewing / governing contexts (validate a revision, may open findings, gate release)

| Specialist | `ActorKind` | Governs (blocking finding domain) |
|---|---|---|
| `quality` | quality | acceptance-criteria conformance |
| `security` | security | vulnerabilities, injection, secrets, authz |
| `security_adversarial` | security | same domain as `security`, attacker-mindset red-team strategy: attack trees, abuse cases, injection/authz-bypass attempts (`review_type: security_adversarial`) |
| `security_property` | security | same domain as `security`, property-based / invariant strategy: state security invariants and check the revision upholds them (`review_type: security_property`) |
| `compliance` | compliance | privacy/PII, retention, licensing, audit trail |
| `release_engineer` | release | rollback, migration ordering, versioning, kill switch |
| `platform_sre` | platform | SLO impact, resource footprint, failure modes |
| `operations` | operations | observability, alertability, runbook usability |
| `finops` | finops | unit economics, unbounded-cost paths, budget |
| `support` | support | supportability, customer-facing failure clarity |
| `visual` | visual | reference fidelity of rendered artifacts: palette, frame coverage, composition, requested motifs |

Reviewing contexts run under the `read_only_os` policy (they may not mutate the
repository) and hold only the `open_finding` / `submit_review` command tools plus reads.
`architect` both produces (design) and reviews (design conformance), so it appears in
both roles.

Multiple reviewing personas may share one `ActorKind` but run different *strategies*,
each a distinct free-text `review_type` — `security_adversarial` and `security_property`
both map to the `security` kind (mirroring the N:1 `implementation_*` pattern). This is
composition-axis only: no new authority slice, no kernel change. Declaring a blocking
`ReviewRequirement{reviewer_kind: security, review_type: <strategy>}` on the
implementation task makes `<strategy>` a gate condition automatically — the goal stays
`UNSATISFIED` until each strategy lands a non-stale APPROVED, revision-bound review
(SELF-COMPOSITION.md Feature 1).

## How authority is enforced (kernel, not prompt)

- **Producing** requires the task's `required_actor_kind ∈ PRODUCER_KINDS` and drives
  `submit_task_result`.
- **A blocking finding** requires `required_actor_kind ∈ REVIEWER_KINDS` AND the task
  contract's `may_create_blocking_finding = true` — the lead grants it per review task.
- **The release gate** requires a non-stale APPROVED review for every blocking review
  type declared across the goal's task contracts, no open blocking findings, and a
  human release approval bound to the exact current revision.

These are `domain/common.py` (`PRODUCER_KINDS`, `REVIEWER_KINDS`) and the application
services — the prompts describe the bounded context, but the kernel is what enforces it.
