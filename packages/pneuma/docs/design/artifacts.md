# `team/artifacts.py` and the `Artifacts` hook — design rationale

Why versioning the conversation was not enough, why every member gets a branch and only the
lead gets a commit, why a merge that could overwrite an author refuses instead, and why the
split-brain check is a probe rather than a gate. The module docstrings state the shapes; this
file carries the arguments, the measurements, and the alternatives that lost.

## Context versioning and artifact versioning are different problems

pneuma already versions a lot. A `MethodThread` forks; the event log replays; `Recall` carries
what an agent should remember into the next prompt; `Trajectory` writes one durable row per
run. Every one of those versions a *conversation* — who said what, in which order, on which
branch of a beam search.

None of it versions a **document**. That gap has a shape, and the shape is the team layer's
own topology read back as a defect. Members hold disjoint evidence by design
([team.md](team.md), the briefing barrier's argument), members cannot address each other, and
the one lateral channel — the `Worklog` — is broadcast-only, closed-vocabulary, and carries
*text about* work rather than the work. So every artifact a team produces has to pass through
the lead's own answer, and the lead is the single integration point for everything: whatever
it fails to fold in is not merely late, it is gone. Two members improving the same design were
invisible to each other until one answer contradicted the other, and the plane held no record
that there had ever been two.

The fix is not more channels. It is one shared mutable thing with a version history, which is
the same answer version control gave human teams and for the same reason: the collision is not
avoidable, so make it *visible* and give someone the authority to resolve it. Cursor's
agent-swarm work states the failure this prevents in one sentence — agents working the same
files without a VCS either overwrite each other or abandon the work — and both halves of that
sentence are design constraints here. Overwriting is refused by the commit rule; abandoning is
what a conflict report with no available next move produces, so `Conflict.__str__` always ends
in the two moves that exist.

What this borrows from Cursor is the *idea* (agents need a version-controlled artifact plane,
not just a shared filesystem). What it does not borrow is the mechanism: this is not git, there
are no worktrees, there is no filesystem, and there is no daemon. It is expressed in pneuma's
own idioms — a `TeamHook`, typed tools whose attribution is bound by the wire, per-run state
keyed by `Workspace` identity, three-valued verdicts, and stdlib persistence with
`hooks/trajectory.py`'s connection discipline.

## Three phases, and what each one adds

**Phase 0 — the store and the hook.** `ArtifactStore` holds content-addressed immutable
revisions. `Artifacts` gives members `read_artifact` + `propose_change` and the lead
`read_artifact` + `list_proposals` + `commit_change`. Proposals land on the proposer's branch;
commit fast-forwards `main` when the proposal's parent *is* main's head. This alone changes the
team's shape: a member's contribution is now a durable, attributed, reviewable object rather
than a paragraph in an answer the lead may or may not have used.

**Phase 1 — branches and merge at the lead.** When a sibling committed first, the second
commit is not a failure and not an overwrite: it is a typed `Conflict` carrying both revisions,
their common ancestor, a bounded three-way diff, and whether a merge is available.
`merge_change` lands clean non-overlapping merges; overlapping hunks always surface as
conflict text. Conflicts are rows in a `conflicts` table, so a collision is queryable a run
later.

**Phase 2 — the split-brain probe.** A proposal may carry `decides: str | None`, the design
question it settles. `split_brain(store)` reports, three-valued, whether two branches settled
one question differently. It refuses nothing and grades nothing.

## Why the lead alone commits

The rejected alternative is the obvious one: let every member commit to `main`, and let the
store's last write win. It was rejected for three reasons that compound.

The plane would have no integration point. `main` under last-writer-wins is not "the team's
agreed version", it is "whoever ran most recently", and no reader can tell those apart from the
outside. Second, there would be nowhere for a conflict to be *seen*: a collision detected at
the moment of writing has no party whose job is to resolve it, so the only available behaviours
are the two Cursor measured. Third, it would contradict the layer it lives in. `team/core.py`
already makes the lead the one agent with the whole picture — members join as typed tools, they
cannot address each other, and the answer is the lead's. A plane where any member can move
`main` grants laterally exactly the authority the team layer withholds, and does it through a
side door nobody reviewing the team's shape would look at.

So members propose and the lead lands. The asymmetry is enforced on the wire, not in the store
alone: `tools_for_member` simply does not include `commit_change`, and the test asserting that
reads the member model's own offered tools, because a member offered the tool could call it
whatever the store's rules said afterwards.

The mirror-image restriction is also deliberate: the lead gets no `propose_change`. A lead that
could propose would have a branch of its own to commit from, which is `main` moving on one
party's decision through two doors, and the record would no longer distinguish what the team
contributed from what the lead wrote.

## Fast-forward-or-conflict, not auto-merge-everything

`commit` moves `main` only when the proposal's parent is main's current head. The rejected
alternative was to have `commit` merge whenever it could — three-way merge on every commit,
conflict only on textual overlap.

It loses on a property worth more than the convenience: under fast-forward-only, **every
version of `main` is a document some author actually read in full**. The content of a
fast-forwarded `main` is exactly what one member wrote, against the exact base it read. Under
auto-merge-on-commit, `main` routinely becomes text no agent has ever seen — spliced from two
proposals, each written without knowledge of the other, and plausible enough that nobody
notices until it contradicts itself two commits later.

Merging still needs to exist, because refusing it would make every sibling proposal a manual
rewrite and the plane would cost more than it saves. So it exists as a *separate, named,
lead-initiated act*: `merge_change`. The lead has already seen the conflict report before it
merges, which means the one document nobody authored is a document somebody chose.

## Overlap always surfaces; the asymmetry is deliberate

`three_way_merge` returns merged text only when the two sides' hunks claim disjoint base
regions. Otherwise it returns the overlapping regions and no text. Two decisions inside that
are worth stating.

**A pure insertion is widened to one line.** An insertion covers zero base lines, and a
zero-width interval overlaps nothing — so two members inserting different paragraphs at the
same point would both read as non-overlapping and the merge would land them in whichever order
the sort produced. That is a silent, arbitrary composition of two authors' work, so `_Hunk.span`
widens an insertion by a line and the case becomes a conflict. There is a test for exactly this,
because it is the one overlap case an interval check gets wrong while looking right.

**Two byte-identical hunks are agreement, not collision.** Both members made the same edit;
there is nothing for the lead to choose between, and a conflict here would be unactionable
noise.

Everything else that touches refuses. The asymmetry is the module's whole safety argument: a
wrongly refused merge costs the lead one turn, and a wrongly accepted one deletes an author's
work with nothing raised. The author whose edit disappears is precisely the one who knew why it
was there, which is why no rule may pick for them.

`difflib` rather than a merge library, and not only to avoid a dependency: the judgement a real
merge engine adds over `SequenceMatcher` opcodes *is* heuristics for resolving overlap, which is
exactly the judgement this plane refuses to make.

## A conflict is a row, never a lost write

`conflicts` is a table, and `hooks_data["artifacts"]` records a `conflict` entry with the same
fields. Both, because they answer different questions: the run report says what happened in this
run, and the table says what has ever happened on this plane. A conflict that lived only as a
tool's return value would be indistinguishable, one run later, from a change nobody proposed —
which is the same defect `hooks/trajectory.py` refuses when it insists a missing row is a bug
rather than an expected state.

The row is *updated* rather than deleted when the proposal later lands (`resolution` becomes
`committed` or `merged`). Deleting would make the plane's history a story where the collision
never happened, and the collision is the most interesting thing the plane records.

## Content-addressed revisions, and what the address includes

`revision_id` is a digest over `(path, parent, content, author, branch, rationale)`. Not content
alone: two members proposing identical text would collapse into one revision and the second
author's attribution and reason would vanish. Not a counter: then an idempotent retry — a model
repeating a tool call, which happens — would double the plane's history with two ids for one
document. `created_at` is deliberately outside the address, because including it would make the
address a timestamp with extra steps.

`digest` (content alone) is carried *alongside* the id, and it is what `split_brain` compares:
"did two branches say the same thing" is a question about content, and the id answers a
different one.

## Attribution is bound by the wire

`propose_change` takes no author parameter. The name on every revision is the one the hook bound
from `member.name`, which is `hooks/worklog.py`'s rule for `post_discovery`'s `source` applied to
writes — where it matters more. A worklog entry with the wrong source misleads a reader; a
revision with the wrong author sends the lead to the wrong agent to resolve a collision, and
sends the audit trail somewhere it cannot be corrected from.

## Storage: stdlib `sqlite3`, and why `:memory:` is allowed here

`hooks/trajectory.py` persists through `turso` because `memory/turso_backend.py` already did.
The artifact plane needs no vector search and no remote replica, so it uses the interpreter's own
`sqlite3` and adds zero dependencies — which also keeps it inside the library boundary
`tests/library/test_boundary.py` enforces (no `polars`, no `libsql`, no `pm4py`; this module
imports `difflib`, `hashlib`, `json`, `sqlite3` and nothing else outside the stdlib).

The *discipline* is trajectory's verbatim: WAL + `synchronous=NORMAL` per connection, a
module-level `SCHEMA` of `CREATE TABLE IF NOT EXISTS` split on `;`, idempotent init, cursors
closed in `try/finally` (`memory/embedding.py` measured a GC'd unfinalized SELECT cursor
silently discarding pending writes on its connection, with the symptom surfacing statements away
from the cause).

One thing differs, and it is a difference in the *argument* rather than a relaxation of it.
`Trajectory` refuses `:memory:` at construction because its access pattern is
open-write-commit-close per run, and under that pattern an in-memory database is a different
empty database every time — the refusal protects a caller from a plane that silently persists
nothing. That argument is about the pattern, not about memory. So `ArtifactStore` takes
`:memory:` as its *default* and keeps one connection alive for the store's whole lifetime, which
makes an in-memory plane fully functional; a file path gets the open-per-operation pattern, so
nothing is held between runs and two teams sharing one file coordinate through WAL alone. The
default is `:memory:` because the common caller is a test or a single-run script, and a plane
that demands a filesystem before it will hold a document is a plane most callers will not wire.

## Failures: which are text and which are loud

`hooks/hiring.py`'s rule, applied with the boundary drawn explicitly. `ArtifactError` is a
mistake in what was asked — an unknown path, a blank rationale, a stale or ambiguous revision id,
a proposal aimed at `main` — and the hook catches exactly that class and returns its message as
`"error: ..."` text, which reaches the model as a *successful* tool result whose content is the
problem. The model reads it and retries correctly.

A `sqlite3.Error` is not the model's mistake and never becomes text. An artifact plane that
renders a corrupt or full disk as advice has silently stopped persisting while every consumer
reads "no proposals" as "nobody proposed" — `hooks/trajectory.py`'s argument for why a failed
write must be loud. The one bug this discipline guards against is a bare `except Exception` at
the tool boundary, so `ArtifactError` exists as its own class purely to make that boundary
expressible, and there is a test that corrupts the database file under a live store and asserts
the two error types are disjoint. (Measured on the way: `DROP TABLE` does *not* fire this guard
— every operation runs `_init_schema` first, so `CREATE TABLE IF NOT EXISTS` politely recreates
what the test removed. Overwriting the file's bytes is a fault no schema init can repair.)

A `Conflict` is neither. It comes back to the lead as its own rendered text rather than as
`"error: ..."`, deliberately: it is not the lead's mistake, it is a fact about the plane the lead
now has to decide about, and an error prefix invites the model to retry the same commit rather
than read the diff.

## Two lifetimes, and confusing them is the defect

`hooks_data["artifacts"]` is per run, reset by `Workspace` identity — the
`worklog.py`/`hiring.py`/`trajectory.py` pattern, and for their reason: the workspace *is* the
run, and a report carrying the previous run's proposals attributes work to the wrong run.

The **store is not** per run, and that is the entire point of versioning the artifact rather than
the conversation. A file-backed plane outlives the run; run 2 reads what run 1 landed and parents
its proposals at run 1's head. A store reset per run would make every commit a fast-forward over
an empty document, which is the plane doing nothing while looking busy. One test asserts both
lifetimes at once, because keeping them straight is the whole claim, and reads the durable half
back through a *second* `ArtifactStore` on the same path — so "persisted" means a different
object found it rather than the first one remembering it.

Seeding follows from the same split: `seed=` writes a starting document in `on_assemble` only
when the path has no revision yet, so a hook instance running twice against a file-backed store
does not restore the original over what the first run agreed to.

## The split-brain probe: three-valued, and a probe

`decides` is free text a model wrote. A closed vocabulary was considered and rejected: the
`Worklog`'s four kinds work because "obstacle" and "dead-end" are shapes of event, and the design
questions a team circles are exactly the thing a vocabulary fixed in advance cannot name. This
module explicitly does not touch the `Worklog`'s vocabulary — a discovery and a decision are
different objects on different channels.

The cost of free text is that "which store backs the plane" and "Which store backs the plane?"
are one question typed twice. Keying on the raw string made every real divergence read as two
uncontested questions — measured, it was the first split-brain test's failure — so `_question_key`
folds case, collapses whitespace, and drops trailing punctuation. Nothing more: a normaliser that
stemmed or dropped stopwords would begin merging questions a team genuinely holds apart, and a
false *merge* here manufactures a divergence that is not there.

The verdict is three-valued in `detect/discrimination.py`'s style and for its reason. Under a
boolean, "the plane recorded decisions and none diverged" and "the plane recorded no decisions at
all" collapse into one `False`, so a team nobody asked to declare its decisions would read as a
team that agreed — a check reporting a question it never posed as a pass. `withheld` is a tuple of
named reasons rather than a flag, so a reader can tell those apart.

    True   at least one question is settled differently on two branches. The finding.
    False  every recorded decision was examined and none diverged.
    None   the measurement could not be posed: nothing carried a decision.

A question decided on only one branch is an examined question that was never in a position to
diverge, and `contested` reports how many were. That emptiness belongs to the subject rather than
to the probe's own bound, so it reports `False` — `detect/discrimination.py` states the rule and
names the one case that inverts it.

It reuses the *shape* of `detect.discrimination.Discrimination` without reusing the class, for
`memory.turso_backend.Discrimination`'s reason: the verdict's shape generalises, the measurement
does not. There, `separating > 0` means the check works; here it means the team is split, so every
helper on that class (`idle`, `discriminates`) would read backwards. Reusing it would also make
`team/` the first library package to import `detect/`, coupling two packages that today share
nothing.

And it is a **probe, not enforcement**. Nothing refuses a proposal, closes a branch, or grades a
run on it. A team may legitimately hold two answers to one question for a while; the value is that
the lead can see it before an answer ships built on both. That includes a divergence the lead has
already merged: `merge` lands two *line-disjoint* edits, which resolves the document and says
nothing about the question, so two members who answered "which store" in different paragraphs
produce a clean merge whose text now asserts both answers — precisely the state worth surfacing.
Only the merge revision's own `decides` is dropped, so `main` is never counted as a third voice on
a question it merely integrated.

## What this deliberately does not build

- **No megafile decomposer.** Nothing here splits a large document into agent-sized units,
  proposes a decomposition, or reasons about structure. The plane versions whatever text it is
  given, and a splitter would need a model, a notion of "unit" per document type, and a story for
  re-joining — three decisions with no measurement behind any of them yet.
- **No ossification licensing.** No revision can be frozen, locked, marked stable, or made to
  require extra approval. A "this part is settled, propose against it at your own risk" mechanism
  is a policy layer, and a policy layer over a plane nobody has run in anger yet would be a
  vocabulary invented before the cases it names.
- **No cross-team store.** A store is passed to a hook by one caller; no team discovers another's
  plane, and no registry maps teams to artifacts. This is [team.md](team.md)'s "no cross-team
  messaging" applied to writes, and for the stronger version of that reason: a shared plane
  between teams that cannot see each other's conversations is collisions with no party able to
  resolve them.
- **No per-line ownership, blame, or review assignment.** `author` is on the revision; nothing
  derives who owns which line, and nothing routes a conflict to a member automatically. The lead
  is the router, which is the same answer the rest of the team layer gives.
- **No deletion, rename, or move.** An artifact's path is its identity. Renaming is a new path,
  and there is no rewrite of history: a plane whose promise is "no lost writes" does not get a
  verb for losing them.
