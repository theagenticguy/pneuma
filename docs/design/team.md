# `team/` — design rationale

Why the team layer is a small core plus a hook library rather than one class with phases and
flags, why members join a lead as typed tools rather than as chat peers, why review is opt-in
members rather than a built-in oracle, and why each hook carries the design it does. The module
docstrings state the shapes; this file carries the arguments, the measurements, and the
alternatives that lost.

## From a monolith to a core and hooks

The first `team.py` grew the way orchestrators grow. It started as the war room's skeleton
lifted into the library: fan out, barrier, gated lead, budget, rollup, unconditional unwind.
Then every new capability became another field on the class: a rounds counter for negotiation,
a flag for the worklog, a flag for dynamic hiring, a mandatory `oracle` override, a `grade`
hook. At 1,655 lines the class answered every question about teams at once, and a caller who
wanted none of it still paid for all of it: the simplest possible team required four subclass
overrides before it would run.

The rebuild inverts the shape. `core.Team` owns exactly what every team needs and nothing else:
spawn the members, run the lead with the members as typed tools, retire everybody. Everything
else arrives as a `TeamHook`, an object implementing whichever of six optional methods it
needs (`on_assemble`, `on_request`, `tools_for_lead`, `tools_for_member`, `on_answer`,
`on_teardown`). The bare team is the whole API for the common case:

```python
from pneuma.team import Member, Team

team = Team(
    lead=chair.compiled("decide"),
    members=[Member(left, "read"), Member(right, "read")],
)
run = await team.run("who is right")
```

The old class's capabilities did not disappear; they moved. The briefing phase is the
`Briefing` hook, negotiation is `Negotiation`, the worklog is `Worklog`, the hiring seam is
`Hiring`, and two things the old class never had, review (`Critic`, `Council`) and learning
(`Learning` + `train`), joined as ordinary hooks because the seam existed for them. What
disappeared on purpose is the mandatory oracle: the old `Team` refused to run without a
subclass-supplied `oracle`, and the new core grades nothing. That change has its own section
below.

Hook methods are discovered with `getattr(hook, name, None)`, never `isinstance`, so a hook
implements the two methods it needs and nothing else, and a debugger's `hasattr` probe cannot
detonate a guard. `tools_for_lead` and `tools_for_member` are synchronous because the runtime
calls the thread's one `config_hook` synchronously inside `_run_cycle` and documents that it
must not block (`config.py:186-188`); the four lifecycle methods may be sync or async and the
core awaits whichever it finds.

## The core pipeline and its contract

One `run` is: spawn the lead's thread (registered, not yet running), spawn every member as its
child, call every hook's `on_assemble`, fold the request through every hook's `on_request` in
order, run the lead, drive the answer loop, and tear down unconditionally. Three parts of that
carry the design weight.

### One composed `config_hook`, because the runtime honours exactly one

The runtime resolves exactly one `config_hook` per cycle, and its `tools` patch replaces the
compiled tools rather than stacking on them (`ai_thread.py:548-553`, `config.py:166-185`, both
re-verified against the installed package). So tool composition happens in one place or not at
all. The core owns the single hook on each thread it manages and folds every contribution into
it. For the lead, per cycle: the lead's own hook runs first and its full patch is honoured
(its `tools`, when set, stand in for the compiled `tools=`, which is the replace semantics the
lead's author already wrote against); then the members-as-tools; then each hook's
`tools_for_lead`, rebuilt against that cycle's context. For a member, `Member.equip` installs
one composed hook that recomposes the member's own `tools=` override ahead of whatever hooks
contribute, so a member that carried tools cannot lose them to a hook it never asked about.

The refusals around this constraint are deliberate about when they fire. A member constructed
with its own `config_hook=` is refused only when a hook actually needs the slot (some hook
implements `tools_for_member`); the bare path must not police what it does not use. A lead
arriving with its own hook loses nothing, because the core recomposes it rather than refusing
it; the old class refused that case, and the recomposition is what let `demo/warroom.py` keep
its `staffing_tools` hook on the lead while riding the library's core.

Member tools have one more wire fact behind them. Strands validates tool names against
`^[a-zA-Z0-9_\-]{1,}$` and drops a dotted name from the registry with only a warning logged
(`strands/tools/tools.py:66-78`, measured), so the lead would silently lose the member.
`Member` names are `{owner}.{method}` by construction, so the core maps the dot to an
underscore on the wire and keeps the real name on the transcript. The duplicate-name guard
checks the mapped name, so `a.b` and `a_b` are refused together: two tools sharing a wire name
shadow silently, and the lead would reach one member believing it reached either.

### The Accept/Revise loop, bounded by the verdict

Every hook with `on_answer` reviews the lead's answer in hook order. `Accept` moves to the next
hook; `Revise(feedback, cap)` re-runs the lead with the feedback as a new request. The cap
rides on the verdict rather than on the hook because the hook is the party that knows how much
revision a particular finding is worth, and the core reads the cap off the latest verdict, so a
hook may lower it mid-loop. A hook that returns `Revise` forever must still terminate:
exhaustion is not an error, the last answer passes on, and the transcript records `revise_cap`
so a reader can tell that the budget, not a clean review, ended the loop. Each reviewing hook
gets its own rounds budget, and a verdict that is neither `Accept` nor `Revise` raises naming
the hook, because a `None` silently treated as accept would grade nothing while looking like a
review happened.

### Unconditional teardown

Teardown hooks run even on a mid-run fault, and the retire runs even when a teardown hook
raises: the registry must be empty on every path. Each hook's `on_teardown` is guarded so one
hook's raise cannot silence another's cleanup, and the first collected error resurfaces only
when nothing else is already propagating. The members and the lead are retired with
`return_exceptions=True`, which is the `MethodThread.retire` lesson restated: a recruit
something else already tore down raises `ThreadNotFoundError`, which is a `KeyError` and sails
past the handlers callers write; without the flag the first failure aborts the loop and leaves
every later member alive, the exact failure an unwind loop exists to prevent.

`Recruit.retire` being idempotent is what makes overlapping unwind paths harmless. The `Hiring`
hook's `on_teardown` retires whatever the lead never dismissed, the core's `finally` retires
the cast and the lead, and a dismissal that already completed costs a retry nothing.

## Why members are typed tools and `send_message` is the demo's deliberate exception

The runtime injects two peer tools into every thread, and `send_message` refuses any target
whose `input_shape` is not `STR_PROMPT`:

```python
if peer_info.input_shape != InputShape.STR_PROMPT:
    return (
        f"error: thread {thread_id} has input_shape={peer_info.input_shape!s}; "
        "send_message requires a str_prompt peer."
    )
```

`ai_thread/tools.py:172-176`. So being addressable by the message bus is not free: it requires
compiling an agent down to one `str` parameter, which is exactly the price `method.py`'s header
itemises. The typed contract, the docstring-as-template, and every learnable parameter are all
lost at once. `demo/agent.py` pays it on purpose and raises if it ever stops paying, because
the demo's subject is a room of peers that can only reach each other through the runtime.

A library has no reason to pay it. A `MethodAgent` compiles to `STRUCTURED` and joins a lead as
a typed tool, checkable at the call site, where a chat box is not. `notify()` is the inbound
side channel for the cases that still want one: it appends to a thread's log without starting a
cycle, so the next model call sees it as context. The `Member` adapter deliberately does not
wrap `notify`; its `thread` property exposes the live `MethodThread` for the runtime operations
the adapter does not cover, and the `Worklog` hook reads `notify` off that handle.

One measured trap survives from the first build and still governs `DynamicAgent`'s shape:
`_infer_input_shape` classifies exactly one positional parameter resolving to `str` as
`STR_PROMPT`, so a synthesized agent whose only parameter was `request: str` would be the one
member shape addressable by every peer's free-text `send_message`. `DynamicAgent.answer`
carries a second typed `context` parameter deliberately, keeping the compiled shape
`STRUCTURED`, so a dynamic hire sits behind exactly the boundary a catalog hire does.
Separately measured and load-bearing for the core: a `STRUCTURED` lead is still drivable by
`handle.run("one string")`, because the positional binds to its first parameter, which is why
the core's `lead_handle.run(request)` is correct for typed leads and not only for `STR_PROMPT`
ones.

## The `Recruit` protocol and the `Member` adapter

`Recruit` is three verbs and a name: `spawn`, `ask`, `retire`. Three because that is the whole
of what the core does to a member; anything richer would be a contract the library cannot
honour for every member shape it wants to accept. It is a protocol rather than a base class
because the members worth having already exist and already differ: the demo's `STR_PROMPT`
`Agent` satisfies it as written, and `Member` adapts a `MethodAgent` capability by naming which
keyword the request arrives as. `spawn` returns `Any` and the core reads only `.id` off it;
demanding a `ThreadHandle` would exclude `MethodThread`, the library's own first-class member.

Optional capabilities go through `getattr` probes, never through the protocol. A recruit
without `equip` is skipped by the tool fold, and a handle without `notify` gets no worklog
channel; a mixed cast (scripted spies beside typed members) is half the test suite's shape and
must keep working. A member that cannot take tools is a fact, not a fault.

## Where the old grading went: no oracle in the core, review as opt-in members

The old `Team` required an `oracle` override and attached it as a post-condition on the lead;
`grade` ran once more for the reader. The new core carries no grading vocabulary at all, and
the bare team's test pins the sharpest consequence: an answer with `admitted=False` returns
as-is, which no oracle-bearing skeleton would allow.

Two forces drove the deletion. First, the mandatory override was the wrong default: the only
possible library-supplied oracle is "raise nothing", which grades every verdict correct while
reporting that grading happened, so the old class made every caller write one even when the
caller had no standard to encode. Second, the post-condition seam already exists on the lead
itself. A lead that wants a hard gate attaches its own `post_conditions`, and the runtime turns
a refusal into re-ask feedback with no help from the team layer; `demo/warroom.py` does exactly
that, prepending its incident check to the lead's own conditions. What the team layer owed was
a place for review that involves the team, and that is what `Critic` and `Council` are:
ordinary members' work riding the same Accept/Revise loop every hook shares, with no special
phase and no privileged vocabulary.

### The review-integrity rule

An errored, empty, or never-spawned reviewer must never settle `Accept`. Positive evidence is
the only thing that may wave an answer through: a reviewer whose thread died reviewed nothing,
so its failure counts against the answer (a `Revise` for `Critic`, an objection for `Council`),
never for it. The same asymmetry governs `detect`'s truncated sweeps: absence of findings under
failure settles nothing. Concretely, an error is rendered under the `error: ` prefix and
checked before the approval token, so an error that merely quotes `NO-FINDINGS` or `APPROVED`
still reads as an error; an empty answer is rendered as an error rather than passed through,
because the empty string contains no token and would silently read as findings with nothing for
the lead to act on. `Council` keeps its denominator at the full panel size, so an errored
panelist lowers the approval fraction and cannot shrink the quorum. An empty `Council` panel is
refused at construction: `0/0` compares vacuously against any threshold, and a review by nobody
settling `Accept` is the silent-accept defect verbatim.

### Reviewers are not tools on the lead's wire

A member becomes a tool the lead can call, and a lead that can consult, and lobby, its own
adversarial reviewer mid-draft defeats the framing. So each review hook spawns the reviewers it
was given as private threads in `on_assemble` and retires them in `on_teardown`; a reviewer
that is already in the cast (checked by identity) is left to the core's lifecycle entirely, so
nothing spawns or retires twice.

### `advisory` changes what the verdict does, never what the record says

Both hooks take `advisory=True`, under which findings and errors are recorded but the verdict
is always `Accept`: review as annotation, not gate. The record still distinguishes clean from
findings from error, which is what lets `compose_feedback` (the learning path) read real
review outcomes off an advisory run.

## The `Briefing` hook: barrier, delivery, and the all-dead refusal

`on_assemble` asks every member its own briefing question concurrently and holds the barrier;
`on_request` prepends the rendered brief to the request the lead is asked. The barrier argument
is the old one and still true: members hold disjoint evidence by design, so a lead that begins
reasoning after two of four reports produces a verdict formed from different data, not merely
an earlier one, and no reviewer can distinguish it from a lead that waited. `asyncio.gather`
starts every coroutine before any completes and returns exceptions positionally
(`return_exceptions=True`), so the name-keyed pairing is sound and one dead member cannot take
the run down.

Delivery earns its own emphasis because it was once missing. An early version recorded
briefings faithfully on the run report while the lead's prompt carried only the bare request;
measured on a two-member team with both members raising, the lead ran, its model context
mentioned no error at all, and the run graded itself correct. A delivery claim needs a wire.
The hook therefore delivers through `on_request`, the seam every hook shares, and one text
block is the right channel because the lead's first parameter is the only channel every lead
shape has (the `STRUCTURED` positional-bind fact above). The tests assert the brief from the
lead model's own captured context, never from the returned report.

A member that raises becomes a rendered `error: ` string rather than a run-ending fault: a
four-member team with one dead thread is still a team worth asking, and the lead can see in its
own prompt that one source is missing. A cast whose every member failed is refused before the
lead spends anything, and raised rather than rendered, which breaks the "failures are text"
rule for the reason that also explains the rule: text is for failures a model can fix, and
there is no model in this one. A dead cast is a coordinator or wiring fault at the level above
the lead, so the only honest report names the members and their errors to the caller, the party
that can act. Raising inside `on_assemble` is before the lead's first cycle by the pipeline
order, so the refusal costs nothing it protects.

`question_fn` exists because the interesting teams are asymmetric: a member holding a private
view needs to be told what to do with that view. `forward_request=False` is the war-room shape,
where a specialist answers for its own evidence and is not told what the lead was asked,
because a specialist that read the question would be reasoning about the answer, which is the
lead's job and the asymmetry the team exists for.

## The `Negotiation` hook: bounded objections on the core's loop

`on_answer` renders the lead's answer as a plan, fans it to every member concurrently (the
briefing barrier's twin: same `gather`, same error rendering), and either every member approves
or the objections go back as `Revise(feedback, cap=rounds)`. The old class owned this loop
itself; the hook rides the core's, and the per-round transcript in
`hooks_data["negotiation"]` distinguishes a round whose revision ran (`revised`) from the one
the cap refused (`cap_reached`), so a capped-out run says the team never agreed rather than
implying it did.

The evidence for the phase existing at all: AgentRadio (arXiv 2607.28430) measured a
negotiation round as its single biggest layer, +67 net rubrics against +24 for passive
awareness. Their MinIO case is this layer's failure shape too: members hold disjoint evidence
by design, so a plan drafted from one-shot briefings can carry a flaw any one member would
catch on sight, and a briefing is exactly a one-shot barrier. Caveats carried honestly: their
n=124, single run per task, LLM judge, and their +29.8 headline bundles three layers, which is
why this is an opt-in hook rather than the default.

The plan travels through `ask` and not `notify` because an answer is wanted: `notify` appends
without starting a cycle, so a notify-based fan-out would deliver the plan and collect nothing
until some later cycle that may never come. The worklog makes the opposite choice for the
opposite reason, and the pair of arguments is the clearest statement of what each channel is
for.

Approval is containment (`APPROVAL in answer`), not equality, because a typed member answers
with a pydantic model and `str(model)` embeds the token inside a field's repr; an equality
check would silently veto every typed member and every negotiation would run to its cap with
nothing raised. The cost, kept deliberately, is that an objection that quotes the token is
miscounted. A rendered error can never approve (checked by prefix before the token), so a
member whose thread died blocks unanimity rather than faking it, and the lead revises knowing
one reviewer is gone. `render_objections` names the approvers alongside the objections, because
a revision that undoes what the approvers approved is a worse plan wearing a fix's clothes. An
empty cast accepts immediately without recording a round: an empty round is vacuously unanimous,
and a transcript entry would record a consensus no member ever gave.

Both delivery wires (plan into each member's context, objections into the lead's revision
context) are pinned from scripted-model contexts. Measured with the wire deliberately severed
(revision prompt replaced by a generic "your team objected"): the transcript still recorded
every objection and only the context assertions failed, which is the briefing-delivery bug's
exact shape, reproduced on purpose to prove the tests can catch it.

## The `Worklog` hook: typed lateral awareness at step boundaries

`tools_for_member` gives every member a `post_discovery` tool whose `source` is wired to the
member's name, so attribution is something the model cannot spoof. A post appends to the
durable log (`hooks_data["worklog"]`) and fans the rendered text to every other registered
channel through `notify`.

The evidence: AgentRadio measured passive awareness alone at +10.5 points net, concentrated on
cross-cutting tasks, and a team's members hold disjoint evidence by design, so one member's
dead end is precisely the thing another member is about to spend a cycle re-exploring. What is
deliberately not granted bounds the grant: no member can address another (no member-to-member
`ask`, no reply channel), a discovery is a broadcast, and the vocabulary is closed. Four kinds
(`bears-on-teammate`, `contradicts-plan`, `obstacle`, `dead-end`); a kind the model invents is
refused as text, so the model reads the refusal and posts again with a real one. Typed payloads
over free text is the library's standing bet, applied to the one lateral channel it allows.

### `notify` this time, because step-boundary delivery is the feature

No answer is wanted, and forcing one is the failure mode: a fan-out through `ask` would
interrupt a member mid-briefing to acknowledge a note it cannot yet use, one full model cycle
per discovery per member. `notify` appends to a thread's log without starting a cycle (the
runtime buffers it and drains at the next model-call boundary), so a teammate reads the
discovery at its own next step, as context. The rendered text says so explicitly: awareness,
not an instruction.

### Reserve before await, and one dead channel never stops the rest

The tool executor is concurrent (`strands/agent/agent.py:462`), so two posts in one assistant
turn interleave at the first genuine suspension. The entry is appended to the log in the same
synchronous stretch that builds it, and only then is any delivery awaited; an append on the far
side of an await could drop one of two concurrent posts with nothing raised (measured on the
hiring seam, the same discipline). Each delivery is awaited under its own handler: a retired
thread raises out of `notify`, the failure lands on the entry as `failed[name]`, and the loop
continues.

### Registration replays, which is what makes ordering not matter

A channel opened late receives every prior entry on registration. The lead's channel opens in
`on_assemble`, before the lead's first cycle (the `Workspace.lead` contract exists for exactly
this), so a discovery posted during another hook's assembly reaches the lead's first model
context. A hire's channel opens through `on_hire` when the `Hiring` hook announces it, so a
helper hired because of an obstacle is not the one teammate who never heard of it. The entry's
`delivered`/`failed` record does not distinguish replay from live delivery, because both answer
the same question: who saw this.

### Per-run state is per run

The entries list lives on the run's own `Workspace.data`, and the channel map resets whenever
the hook sees a new workspace (compared by identity, because the workspace is the run). Without
the reset, one hook instance on a `Team` that runs twice would fan run 2's first post into run
1's retired threads and record their predictable failures on run 2's log.

Every delivery claim in the worklog tests is pinned from a scripted model's own captured
context. Measured with the wire severed (`_deliver` recording success without sending): the
log still recorded every entry as delivered and only the context assertions failed.

## The `Artifacts` hook: a versioned artifact plane, landed only by the lead

The `Worklog` gives members a way to *tell* each other something; `Artifacts` gives them a way
to *change* something together. `tools_for_member` grants `read_artifact` and `propose_change`
with the author bound by the wire (the worklog's attribution rule applied to writes);
`tools_for_lead` grants read, `list_proposals`, `commit_change` and `merge_change`. Proposals
land on the proposing member's own branch and change nothing anyone else reads; `main` moves
only when the lead fast-forwards it or lands a proven non-overlapping three-way merge, and an
overlapping edit always surfaces as conflict text rather than as a silent overwrite. Conflicts
are rows, so a collision is queryable a run later. `split_brain` is a three-valued probe over
the plane, in `detect/discrimination.py`'s style: two branches settling one design question
differently, none observed, or nothing recorded to compare.

Why the lead alone commits, why commit is fast-forward-or-conflict rather than auto-merge, why
overlap can never be resolved by rule, why the store outlives the run while the report does
not, and what the plane deliberately does not build (no megafile decomposer, no ossification
licensing, no cross-team store): [artifacts.md](artifacts.md).

## The `Hiring` hook: budgeted synthesis of the cast, always unwound

Two layers, deliberately. `hiring_tools(roster, catalog, ...)` is a functional seam that builds
a `config_hook` granting `hire`/`delegate`/`dismiss` over a `Roster`; it composes outside any
team, and `demo/staffing.py` binds it straight onto a lead's own hook. The `Hiring` class is
that seam as a `TeamHook`: `tools_for_lead` rebuilds the tools each cycle, the roster lives for
one run, every hire is equipped with the sibling hooks' member tools before it spawns and
announced to them after, and `on_teardown` retires whatever the lead never dismissed.

### Why the catalog is a mapping and the mandate goes through the factory

The demo's original roster was a module-level registry populated by `__init_subclass__`, which
is global (two teams in one process would share one pool of hireable roles) and typed on the
application's own base class. `hiring_tools` therefore takes
`catalog: Mapping[str, Callable[[str], Recruit]]`, a plain mapping the caller supplies, called
as `factory(name)`: what a team may hire is a property of that team. The mandate reaches the
tool as an argument and is recorded on the roster's log; it is never injected onto the recruit
as an attribute, because the `Recruit` protocol says nothing about a mandate, so injection
would either fail on a `__slots__` recruit or silently create a field nothing reads. A factory
that wants the mandate on its agent closes over its own constructor, where the attribute is
real.

### A hire reserves its name and its headcount before it awaits anything

The refusals and the registration into `roster.hires` run in one synchronous stretch, and only
then is the spawn awaited, rolled back if it raises. The measured reason: the concurrent tool
executor lets two `hire` calls in one assistant turn interleave at the first suspension, and
with the registration on the far side of the await, both passed the cap (measured:
`headcount == 2` under `max_hires=1`) and a duplicate name left the first recruit live and
unregistered, unreachable by every unwind path, with nothing raised anywhere. A `Lock` was the
rejected alternative: the checks and the write already happen inside one event-loop step, so
there is nothing to serialise. The cap is checked before the recruit is constructed, because a
cap that fires after the spawn already spent the thread it was refusing.

`dismiss` inverts the order: retire first, unregister only on success. A `pop` before the await
drops the roster's only reference, so a retire that raises would leave the recruit unregistered
and alive; left registered, the raise is retried by teardown, and retire being idempotent makes
the retry free. It is the reservation argument read backwards: put the registry write on the
side of the await where a fault leaves the registry describing reality.

### Every hire-side failure is text

An unknown role, a duplicate name, a headcount cap, delegating to someone unhired, dismissing a
stranger: all five are mistakes the model made and can fix, and all five return a string
beginning `error: `. Measured, because the behaviour is what makes this correct rather than
tidy: a tool returning that string reaches the model as a successful tool result whose content
is the string, the model reads the problem, and the cycle continues. An exception would surface
as a tool fault the model cannot act on. The exclusions define the rule: a `spawn` or `retire`
that raises is not the lead's mistake and surfaces as a fault, and what the seam owes on those
paths is a roster that still describes reality (the two ordering rules above).

### The catalog-vs-synthesis boundary

`hire` chooses from a catalog someone reviewed; `hire_dynamic` lets the lead write a new
subagent's instructions itself, mid-run. The evidence for admitting synthesis at all is
Shepherd (arXiv 2605.10913), which measured runtime agent synthesis as a layer worth having:
some work has no pre-declared role because the decomposition is only discoverable mid-run, and
a factory for "whatever the lead just realised it needs" is not a factory anyone can review in
advance.

What is admitted is deliberately less than a prompt-driven orchestrator. Only the instructions
are dynamic: the signature, the output type, the adapter, the lifecycle, the budget, and the
tool surface are all fixed in `DynamicAgent` at review time, so a synthesized agent is an
ordinary `MethodAgent` whose per-instance state happens to have been written by a model. It
satisfies `Recruit`, joins the roster under the shared `max_hires` (one cap for both kinds,
because two caps would let a lead run twice the intended team behind an innocent flag), gets
the sibling equip, and is reached and released through the same `delegate` and `dismiss` as a
catalog hire. Two contract details are pinned by tests because each is one careless
simplification from breaking the boundary: `ai_methods()` walks the MRO, so `DynamicAgent`'s
published tool set is asserted to be exactly `["answer"]`; and the `context` parameter keeps
the compiled shape `STRUCTURED` (the `send_message` boundary above).

The audit trail is the safety story. A catalog role was reviewed once, by a person, before any
run; a synthesized agent's instructions were reviewed by nobody, and that cost cannot be
checked away without deleting the feature. The mitigation is attribution: the roster records
`hire_dynamic` with the instructions verbatim, so the log answers "which of these agents did a
human review, and what exactly was the unreviewed one told to be" for every run, after the
fact, without trusting the model's own account. A truncated record would be an audit of a
different agent. `hire_dynamic` is a separate tool rather than a sentinel catalog role, so a
team that never opts in keeps the exact three-tool wire it always had, and both hire kinds
share one reservation discipline through one helper, because two copies of reserve-before-await
is how they drift apart. `dynamic=False` is the default, and the tool's own description tells
the lead to prefer catalog roles even when it is on: a reviewed role that fits is strictly
better than a synthesized one, because it carries knowledge the lead did not have to write and
a review the lead cannot give.

### The roster's lifetime is one run

Every promise on the roster (the cap, the name reservation, the published log) is a promise
about one run. Measured on the old class with one instance run twice: run 2's report opened
with run 1's hiring log, its names were already taken, its cap was short by run 1's headcount,
and `delegate` reached a thread run 1's teardown had retired, which reads in the audit like a
subagent that broke rather than a run that inherited a corpse. The hook therefore stands up a
fresh roster per workspace, keyed by identity, and `on_assemble` creates it early so
`hooks_data["hiring"]` exists even on a run whose lead never hires.

### Sibling coordination is a hook-library convention, not core surface

A hire should carry the same member tools a cast member does and join the same worklog fan-out,
but the core knows nothing about hiring or worklogs. So the `Hiring` hook folds every sibling's
`tools_for_member` into the hire's own equip before spawn (the same fold the core does for the
cast), and announces each hire to every sibling carrying `on_hire` and each dismissal to every
`on_dismiss`. Those two names are a convention between hooks; the core's `TeamHook` protocol
does not mention them, and a hook library that grows a new cross-cutting concern can grow a new
convention without touching the core.

## The `Learning` hook: guidance as a gradient target

`casestudy/learning.py` proved the loop (run, observe, phrase feedback, let `TextGradOptimizer`
rewrite the guidance) for a single navigator. `Learning` lifts the same shape onto a team with
the smallest possible surface: the hook recalls one prose parameter from a memory backend and
folds its rendered text into the request; `traced_result` turns one finished `TeamRun` into the
`Result` graph the optimizer consumes; `train(team, cases)` drives a batch and takes one step.
It is the paved road, not a framework: one prose parameter, one step per batch, nothing
configurable the case study did not prove necessary. Measured live against Bedrock: one real
traced run and one real TextGrad step changed the stored guidance text.

### Guidance is advice, never structure, and never code

Only a prose parameter is learnable. A `Procedural`-marked field is reusable code with sandbox
semantics and is refused at construction: code is not advice, and structural or executable
behaviour stays in reviewed code where the optimizer cannot reach it. A `Frozen` field cannot
receive a gradient, so accepting it would produce a training loop that reports rounds and
learns nothing; it is refused in the same place. Anything structural about the team (the cast,
the tools, the caps) lives in code.

### How the gradient edge survives a run the core never traced

`AIFunction.trace` cannot be used here: the core runs the lead through its live thread handle,
and a handle run does not scan arguments for dataflow handles. So the hook reproduces what
`trace` does, split across the run boundary. `on_assemble` recalls the guidance under
`no_thread_scope()`, explicitly, because when `team.run` is itself called from inside a live
cycle the ambient scope would emit the recall event against the caller's thread and the edge
would silently die. The recall leaves a live `ParameterView` in `hooks_data["learning"]`, and
`traced_result` later scans the staged inputs by identity and emits the recall event against
the lead thread's surviving log (the event log outlives the thread, measured). Interpolate the
view into a string instead of keeping the object and the edge is already dead with every
offline test still green, which is why `traced_result` raises on a viewless record rather than
returning a `Result` the optimizer would walk and silently update nothing. Every run recalls a
fresh view (a view is emitted once, so a reused one yields a parameter node on the first traced
run and none after), so `train` may keep whichever trace it likes; it keeps the last, which saw
the newest guidance.

### Feedback comes from what the run actually recorded

`compose_feedback` reads review entries that found something or errored and the core's own
`revise`/`revise_cap` transcript entries: the feedback that was really put to the lead, plus
the fact that a budget rather than a clean review ended a loop. When nothing objected it says
so and asks for no additions, because an instruction to improve anyway teaches the consolidator
to grow the parameter without a measured reason. `train` refuses a team with zero `Learning`
hooks (nothing to train) or two (picking one silently), and refuses an empty batch, because a
step over no trace is a no-op wearing a training run's name.

## The progress markers

`CustomEvent`s named `team.hired`, `team.hired_dynamic`, and `team.discovery`, emitted by the
hooks that own those moments. The old class's phase markers (`team.assembled`,
`team.briefings_in`, `team.lead_running`, `team.graded`) went with the phases: the core's
pipeline is short enough that the thread events themselves (spawns, cycles, retires) are the
observation, and a hook that wants a marker emits its own through the `ThreadContext` it
already holds. Kinds stay namespaced under `team.*` so a subscriber can filter one team's
progress out of a shared log.

## What this layer deliberately does not do

- **No grading in the core.** The bare team returns the lead's answer as produced. Review is
  the `Critic`/`Council` hooks; a hard gate is the lead's own `post_conditions`. A built-in
  default oracle could only be "raise nothing", which reports a grading that did not happen.
- **No learning in the core.** The `Learning` hook and `train` are the paved road, and they
  ride seams (`on_request`, `hooks_data`) every hook has. `gated.py` records why "admissible"
  and "better" must not be the same mechanism.
- **No cross-team messaging.** No team knows another exists. The one lateral channel inside a
  team is the `Worklog` hook: typed, broadcast-only, step-boundary, closed vocabulary, opt-in.
  The one lateral *write* is the `Artifacts` hook, and it is asymmetric on purpose: members
  propose, only the lead lands, and no team shares a plane with another
  ([artifacts.md](artifacts.md)).
- **No `send_message` anywhere.** The typed join is the library's path; the bus is the demo's
  deliberate exception, and the gate that forces the choice is cited above.
- **No `Process` coupling.** A `ProcessAgent` can be a team's lead or a team's member; the
  core knows nothing about states or transitions, and a team is not a verified process.
  Composing them is the caller's to do and the caller's to justify.
