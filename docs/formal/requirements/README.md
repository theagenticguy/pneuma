# pneuma's three coordination planes as EARS requirements, checked by symspec/Z3

87 requirements restating what `src/pneuma/team/` and `docs/design/org-plane.md` actually say,
authored in EARS shapes and run through `symspec check` (v1.2.0) to look for cross-plane
contradictions the prose could hide. The check found **two proven contradictions**, and both
are real design tensions rather than authoring slips. They are the finding, and they are
written up below rather than reworded away.

## What "clean" would and would not have meant

symspec's own scope statement is the frame to read this under, and it is stronger than the
usual disclaimer. Three sentences from `symspec manifest`, restated:

- **Sound for conflicts, silent about consistency.** Every reported contradiction is a genuine
  logical conflict of the requirements *as atomized*. But paraphrases become distinct atoms, so
  a real conflict can be missed. Silence is not a consistency certificate.
- **`verified` is a coverage claim, not a verdict.** It says "I compared enough to certify",
  not "the spec is correct". A document with a proven contradiction can report
  `verified: true` and exit 1 — the exit code is what keeps the two apart.
- **The reachability proof is about the model you declared, not the text.** The `classify`
  expressions *are* the model, so a mis-declared effect yields a sound proof of the wrong
  thing.

So the honest claim this directory supports is: **no conflict was proven beyond the two below,
over the 49 candidate pairs that shared an atom.** That is weaker than "the three planes are
consistent", and 42 of the 87 requirements were never cross-compared at all (see
[Uncompared](#what-was-never-compared)).

## Verdict

```
verified: false          # demoted by 42 uncovered requirements + 1 frame hypothesis
counts:  error 2, warn 0, info 2
waived:  244 findings suppressed, out of 248 raised
pairs:   49 candidate pairs compared
exit:    1               # an error-severity finding is present
```

The full envelope is committed as `check-result.json`.

## Corpus organization

| Prefix | Plane | Count | Source of truth |
|---|---|---|---|
| `CON` | Content — `ArtifactStore` + `Artifacts` hook | 30 | `src/pneuma/team/artifacts.py`, `src/pneuma/team/hooks/artifacts.py`, `docs/design/artifacts.md` |
| `EXE` | Execution — `Team` core, `Trajectory`, `Worklog` | 30 | `src/pneuma/team/core.py`, `hooks/trajectory.py`, `hooks/worklog.py` |
| `ORG` | Organization — the blackboard kernel and the release gate | 16 | `docs/design/org-plane.md` plus the supplied transition matrix |
| `XPL` | Cross-plane — the seams where a contradiction would hide | 11 | both, joined |

By EARS pattern: 41 event-driven, 25 unwanted-behavior, 13 state-driven, 8 ubiquitous. 69
`refines` edges follow the code's own containment — a guard's special case refines the general
rule it narrows.

Every requirement carries a `verificationNote` naming the file and line range, or the
design-doc section, it was read off. Where the code and a design doc disagree, the code wins
in the requirement and the drift is stated — see CON-25/CON-26 below.

The corpus is stored as two replayable op streams rather than only as the built document, so a
reader can see what was authored and why:

- `corpus.jsonl` — the 87 `add` records and the 69 `refine` edges.
- `vocabulary.jsonl` — terms, antonyms, the org-plane state model, and the 13 waivers.
- `requirements.json` — the built symspec document (the thing `check` reads).
- `check-result.json` — the committed verdict envelope.

## Findings and their dispositions

### 1. REAL TENSION — the release gate and a withheld split-brain verdict (XPL-1 vs XPL-2)

```
XPL-1  When the split-brain probe withholds the divergence verdict,
       the release gate shall admit the release.
XPL-2  If the split-brain probe withholds the divergence verdict,
       then the release gate shall not admit the release.
```

`FND_CONTRADICTION`, error severity. Both requirements are faithful restatements of two rules
this codebase already holds, and the two rules disagree about the same reachable state.

`docs/design/org-plane.md:101-105` specifies the widened gate as accepted reviews AND no
unresolved `Conflict` rows AND **`split_brain` not CONFIRMED**. Read literally, that is a
two-valued test over a three-valued probe. `split_brain` returns `True` / `False` / `None`
(`artifacts.py:1125-1137`), and `None` — "nothing carried a `decides`, so the question could
not be posed" — is *not* CONFIRMED. So XPL-1 follows from the gate wording.

But `hooks/review.py:9-16` states the opposite rule for the same shape of evidence, and
states it as a standing discipline: *an errored, empty, or never-spawned reviewer must never
settle `Accept`. Positive evidence is the only thing that may wave an answer through.* The
same discipline is cited from `detect/`'s truncated sweeps
(`.erpaval/solutions/verification/truncation-must-dominate-positive-evidence.md`). Under that
rule a withheld verdict blocks, which is XPL-2.

**This is the finding, and it is not hypothetical.** The gate-evidence step in
`org-plane.md:103-105` is specified as publishing the probe's three-valued verdict as a
blocking review. A team where nobody used `decides` produces `withheld`, which is the *most
likely* state for a first run — and the design doc's wording lets it through while the
library's own review-integrity rule says it must not. The abstention case is exactly the one
the three-valued verdict exists to keep separate from agreement (`artifacts.py:1077-1098`),
and the gate's two-valued phrasing collapses it back.

**Disposition: documented, not resolved.** Resolving it means a decision nobody in the repo
has taken yet: either the gate condition becomes `split_brain is False` (abstention blocks,
matching review-integrity, at the cost of a gate no team passes until someone declares a
`decides`), or the gate stays `not CONFIRMED` and the design doc states explicitly that
content-plane abstention is treated as clean. Nothing in `src/` implements this gate today —
`org-plane.md:3` says "design sketch, nothing built" — so the tension is cheap to fix now and
expensive after Phase 4 ships.

### 2. REAL TENSION — seeding advances `main` without the lead (CON-25 vs CON-26)

```
CON-25  When the Artifacts hook assembles the run and the seeded path holds no revision,
        the artifact plane shall advance main to the seeded revision.
CON-26  If the Artifacts hook assembles the run and the seeded path holds no revision,
        then the artifact plane shall not advance main to the seeded revision.
```

`FND_CONTRADICTION`, error severity. CON-25 is the code. CON-26 is the design claim, stated
verbatim so the drift would be *proven* rather than argued.

`hooks/artifacts.py:126-139` — `on_assemble` calls `store.propose(...)` on an
`f"{origin}-seed"` branch and then calls `store.commit(...)` itself, before the lead's first
cycle. Nothing about that path involves the lead.

`hooks/artifacts.py:13-16` states the invariant the module exists to express: *the lead holds
sole commit authority* — `main` moves only when the lead commits (fast-forward) or merges. The
same claim is repeated at `docs/design/artifacts.md:76-79`, and it is enforced on the wire by
`tools_for_member` not offering `commit_change` (CON-21).

So the plane has exactly one writer of `main` that is not the lead: the hook itself, during
assembly. The escape hatch is real and narrow — the seed lands only when the path has no
revision at all (`hooks/artifacts.py:126-127`), so a file-backed store's second run cannot be
overwritten (XPL-3), and the seeded author is `origin` rather than a member name precisely so a
reader can tell "this is where the document started" from "a member wrote this"
(`hooks/artifacts.py:70-72`).

**Disposition: documented as a scope drift in the stated invariant, not a code defect.** The
code is right; the sentence is over-broad. "The lead holds sole commit authority" should read
"the lead holds sole commit authority over member proposals; the hook seeds an empty path
during assembly". The reason this is worth the ink rather than a shrug: CON-9's fast-forward
argument is that *every version of `main` is a document some author actually read in full*
(`docs/design/artifacts.md:92-97`), and a seeded `main` is a document no agent has read at all.
That is fine — it is the document as the team received it — but it is a different claim, and
anything later reasoning "every `main` revision was read by its author" would be wrong at
revision one.

### 3. PROVED UNDER HYPOTHESES — one live run per task (ORG-11)

`FND_REACHABILITY_UNDER_HYPOTHESES`, info severity, and it demotes `verified`.

The org plane's transition matrix is committed as a real state model: `task_state` as a
12-member enum initialised to `DRAFT`, `live_runs` as a bounded integer, nine transitions
classified as effects (ORG-1 … ORG-9), and ORG-11's one-live-run rule classified as the
constraint `live_runs <= 1`. Z3 Spacer proved it holds in every reachable state — with no
bound on path length, and the proof independently re-checked by three plain-SMT obligations.

But it proved it **only under the frame hypothesis**: that `task_state` and `live_runs` change
only where a requirement changes them. With nothing assumed, the constraint is violable —
because nothing in the document forbids some other actor incrementing `live_runs`. symspec
names the exact variables and the requirements that write them, and refuses to render the
result as proven unconditionally.

**Disposition: accepted as the honest ceiling, not waived.** The hypothesis is genuinely
unstated: fencing and optimistic concurrency live in the kernel's Postgres
(`org-plane.md:14, 111-114`), so the requirements that would justify the frame are outside this
corpus by design. Discharging it would mean either importing the kernel's fencing requirements
(a second organizational brain in a pneuma doc, which `org-plane.md:111-114` refuses) or
dropping the frame and accepting the weaker claim. Leaving the demotion in place is the
truthful third option, and it is why `verified` is `false`.

### 4. AUTHORING FIX (found by the tool) — EXE-4 and EXE-5 were one claim written twice

Not in the final envelope, because it was fixed. Worth recording because symspec found it and
a reader would not have.

Committing the `keep`/`refill` antonym — the two halves of budget monotonicity
(`core.py:299-302`) — let the solver decide the relation between EXE-4 and EXE-5 instead of
leaving them on separate atoms. It immediately returned `FND_REDUNDANCY`: as first written,
EXE-4 ("*While* the review walk restarts, the team core shall keep the rounds counter") was
**bi-implied** by EXE-5 ("If the review walk restarts, then the team core shall not refill the
rounds counter"). One claim, two sentences, and the corpus would have looked two requirements
richer than it was.

EXE-4 was re-pointed at the mechanism rather than the consequence — `core.py:311` builds
`rounds` with one `dict.fromkeys` *outside* the `while`, which is *why* the budget persists —
and the redundancy cleared. The antonym stays committed: it is what makes any future rewording
of either side provably comparable rather than silently uncompared.

### 5. WAIVED — 13 recorded waivers, 244 findings suppressed

Each waiver carries its reason in `vocabulary.jsonl` and in the document's `waivers` table.
Counts below are measured by re-running `check` with the `waivers` table emptied.

One waiver is recorded but **does not suppress its finding**: `FND_TERM_INCONSISTENT` still
appears in the envelope with or without it (244 is exactly the other twelve classes summed),
including when scoped with `--ref ORG-11`. Recorded anyway — the reasoned decision belongs in
the document whether or not the tool acts on it — but the finding below is the honest count, not
a leftover.

Summarised, with the two that are load-bearing first:

- **`GTWR_R16_NEGATION` (25)** — every hit is the `shall not` rendering of an
  unwanted-behavior requirement authored with `--negated`, which is symspec's *own* mechanism
  for putting a prohibition and its permission on one atom at opposite polarity. Obeying R16's
  "phrase it positively" would move each prohibition to a second atom and **silently destroy
  the contradiction detection this corpus exists to run**, including both findings above.
- **`FND_SIMILAR_SEMANTIC` (91) / `FND_SIMILAR_UNUNIFIED` (22) / `FND_DUPLICATE_CLUSTER` (3)**
  — reviewed class by class and declined. The largest cluster is the nine org-plane
  transitions, which are deliberately distinct rows of one matrix; collapsing "move to
  assigned" onto "move to running" would make the matrix unprovable and manufacture conflicts
  between legal transitions. `advance main to the proposal revision` and `... to the seeded
  revision` name genuinely different target revisions — merging them would prove a conflict
  the plane does not contain (and would have made finding 2 unreadable).
- **`FND_LEAF_UNVERIFIABLE` (18)** — every leaf carries a `verificationMethod` plus a note
  naming its source line range, which *is* the leaf-verifiability obligation. A `verifies`
  edge would mean inventing requirements that restate the notes.
- **`GTWR_R35_TEMPORAL` (19)** — "as" in "record X as Y" is the copular preposition, and the
  `before`/`after`/`latest` hits name a *definite* ordering that is the requirement
  (`worklog.py:126-133` appends before it awaits; `core.py:325` reads the cap off the latest
  verdict). R35 targets "eventually"/"until"; neither appears.
- **`GTWR_R24_PRONOUN` (16)** — every remaining hit is the cardinal determiner "one", verified
  span by span. The five genuine `it` / `that` / `its` hits (CON-29, EXE-10, EXE-22, XPL-4,
  XPL-5) were **rewritten, not waived**.
- **`GTWR_R19_COMBINATOR` (5) / `GTWR_R15_LOGICAL_EXPR` (8)** — each is one compound *object*
  or one genuinely multi-clause guard. ORG-14's gate really is three conditions ANDed
  (`org-plane.md:101-105`); adopting the bracketed `[X AND Y]` convention would change what
  the atomizer compares without changing what the gate means.
- **`GTWR_R5_INDEFINITE_ARTICLE` (4), `FND_MISSING_TRACE_LINK` (26),
  `FND_AMBIGUITY_NEEDS_JUDGMENT` (7)** — see the reasons in `vocabulary.jsonl`.
- **`FND_TERM_INCONSISTENT` (1)** — the waiver that does not take. Info severity and it pushes
  no coverage demotion, so it cannot move `verified` or the exit code. ORG-10 and ORG-11 share
  the term "task contract" across contexts at cosine 0.59, which is two *properties* of one
  object (legality, concurrency) rather than one word for two things.

## Glossary decisions

Small on purpose. 6 term groups / 7 aliases, 2 antonyms, 0 response-phrase glossary entries.

**Terms committed** (nouns, substituted inside every body): `revision identifier` ←
`revision id`, `content address`; `conflict row` ← `conflicts row`; `task contract` ←
`blackboard task`; `design question` ← `decides field`; `divergence verdict` ←
`split-brain verdict`; `rounds counter` ← `revise budget`.

**Antonyms committed**: `insert`/`delete` (proposed by the tier for CON-12/CON-14 — genuinely
polar operations on one row), and `keep`/`refill` (committed on the authoring pass, and the one
that found finding 4).

**Glossary left empty, deliberately.** `propose-glossary` offered a large merge class that
would have folded most of the artifact plane's responses onto one atom. Declined: a glossary
entry replaces a whole slot phrasing, so committing those merges would have collapsed "advance
main to the proposal revision", "advance main to the seeded revision", and "store the whole
document text" into one thing — over-unification, which is the one *false-positive* risk the
formal tier has, and it would have manufactured conflicts between CON-9 and CON-25 that the
code does not contain. The declines are recorded as the `FND_SIMILAR_SEMANTIC` waiver.

`propose-glossary` also failed one op: `three-way merge` ← `three way merge` was refused with
"cannot be a term alias of itself", because the two normalize to the same key. The entry was
dropped rather than forced.

## What was never compared

42 of 87 requirements never participated in a cross-comparison, and 112 atoms are owned by
exactly one requirement. This is the largest gap in the result and it is not a bug in the
corpus — symspec's own note says a singleton with no same-context peer is not a coverage gap.
But it *is* the reason `verified` is `false`, and the reason "clean" here means less than it
looks.

Uncompared, by key:

```
CON-1  CON-2  CON-3  CON-17 CON-21 CON-22 CON-27 CON-28 CON-29 CON-30
EXE-1  EXE-4  EXE-5  EXE-6  EXE-10 EXE-11 EXE-14 EXE-15 EXE-16 EXE-17
EXE-22 EXE-23 EXE-24 EXE-29 EXE-30
ORG-2  ORG-3  ORG-4  ORG-5  ORG-6  ORG-7  ORG-10 ORG-11 ORG-12 ORG-13 ORG-16
XPL-3  XPL-6  XPL-7  XPL-9  XPL-10 XPL-11
```

Three shapes, and they need different remedies:

1. **The nine org transitions** (ORG-2 … ORG-7 and friends) are singletons in the
   *propositional* tier by construction — each has a distinct guard and a distinct response,
   which is what a transition matrix is. They are not unchecked: they are the nine effects the
   **reachability tier** encoded and proved ORG-11 against. That is the stronger check, and it
   ran.
2. **Genuine singletons** — CON-1, CON-2, EXE-16, EXE-17, ORG-13, XPL-9 state properties no
   other requirement in this corpus touches. Comparing them would need a peer that does not
   exist yet, not a rewording.
3. **Pairs the atomizer split** — a handful (e.g. CON-28/CON-29's confirm-vs-withhold) are
   arguably relatable and were left on separate atoms because unifying them needs a glossary
   entry, and the entry would risk over-unification. Recorded here rather than forced.

The two demotion reasons in `check-result.json` are exactly `uncovered-requirement` (42) and
`reachability-frame-relied-upon` (1). Neither can be discharged without either widening the
corpus or making a claim the source does not.

## Re-running

```bash
cd /path/to/pneuma
export SYMSPEC_DOC=docs/formal/requirements/requirements.json

# Rebuild the document from the two op streams (order matters: corpus, then vocabulary).
symspec init "$SYMSPEC_DOC" --force
symspec apply --ops docs/formal/requirements/corpus.jsonl
symspec apply --ops docs/formal/requirements/vocabulary.jsonl

# Re-check and re-commit the envelope.
symspec check > docs/formal/requirements/check-result.json   # exit 1: two proven contradictions

# Read it yourself.
symspec check --pretty --findings-only
symspec explain FND_CONTRADICTION
symspec explain FND_REACHABILITY_UNDER_HYPOTHESES

# The coverage gate, as a work list rather than a pass/fail.
symspec check --strict          # lists the discharging op for each demotion
```

`--strict` on this corpus exits **1**, not 3: exit 3 is reserved for a tripped coverage gate on
a run with *no* error-severity finding, and the two proven contradictions take precedence. The
43 demotions are still listed in `data.coverage.demotions` either way.

`symspec check` is byte-reproducible given the document, the committed tables, and the pinned
embedding model. If the model is not cached the run **fails closed** with
`ERR_EMBED_MODEL_MISSING` rather than skipping the semantic tier — run `symspec download-model`
once (~110MB, sha256-pinned).

Exit codes: `0` clean, `1` an error-severity finding is present (this corpus), `2` an
operational error, `3` a strict coverage gate tripped.

## Deviations from the brief

- **`symspec init` has no workspace concept** — it creates one JSON document at a path. So the
  "workspace" here is the directory: two op streams, the built document, the envelope, and
  this README.
- **The `apply` op stream uses different verbs than the CLI.** An edge is not
  `{"op":"link","relation":"refines"}` but `{"op":"refine"}` — `link` is not a legal `op`
  value at all, and the four relations are the verbs. Likewise `state-initial` (hyphenated) is
  the op, `state --domain` takes a JSON array rather than a comma string, and `--min`/`--max`
  take numbers rather than strings. Found by probing; recorded here because the manifest
  documents the CLI flags, not the op-record schema.
- **`state-initial` was not used.** Per-variable `initial` predicates (`task_state = DRAFT`,
  `live_runs = 0`) were sufficient, and a model-wide predicate would only have narrowed the
  initial states further without adding a claim. The vacuous-initial trap
  (`FND_REACHABILITY_VACUOUS_INITIAL`) did not fire: `reachability.violated` is 0 and
  `provedUnderHypotheses` is 1, so the model has a satisfiable initial state and the proof is
  not vacuous.
- **CON-26 and XPL-1 are deliberately "wrong" requirements.** Both restate a design claim the
  code contradicts, verbatim and on purpose, so the contradiction would be *proven by the
  solver* rather than asserted in prose. They carry `status: draft` and their notes say so.
  Deleting either would make the check clean and the document dishonest.
- **`src/` is untouched.** `uv run pytest tests/library/test_boundary.py -q` passes 144 tests,
  and `git status src/` is empty.
