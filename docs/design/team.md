# `team.py` — design rationale

Why a team's phases are ordinary `asyncio` rather than a prompt, why members join a lead as typed
tools rather than as chat peers, why the hiring catalog is a mapping the caller supplies rather
than a registry the library owns, and why a mandate goes through a factory rather than onto an
attribute. The module docstring states the shape; this file carries the arguments, the
measurements, and the alternatives that lost.

## What was lifted, and what stayed behind

`demo/warroom.py` is a `Spawnable` that stands up four telemetry specialists, briefs them behind a
barrier, runs an incident lead against a post-condition oracle with a hiring hook, rolls up the
subtree's tokens, and retires everybody in a `finally`. Read it with the incident removed and what
is left is a general shape: fan out, barrier, gated lead, budget, rollup, unconditional unwind.

What stayed behind is everything that made it *that* run. `demo/incident.py`'s planted root cause
and its `MECHANISMS` vocabulary, the four `PLANES`, `Verdict` and `Finding`, the `IncidentLead`'s
system prompt, and `demo/agent.ROSTER` — the module-level registry of hireable roles that
`__init_subclass__` populates (`agent.py:53-56`). None of that generalises, and one piece of it
actively resists generalisation: a `ROSTER` is global, so two teams in one process would share
one pool of hireable roles whether or not either wanted the other's.

So the split is between the skeleton and the cast, which is `gated.py`'s split — skeleton versus
judgment — restated at team scale. `Team` owns the phases, the barrier, the oracle attach, the
budgeted hiring seam, the rollup and the unwind; `members()`, `briefing()`, `lead_function()`,
`oracle()`, `catalog()` and `grade()` are the subclass's. The measurable form of the split is the
same as `gated.py`'s: `tests/library/test_team.py` builds a whole team out of two `MethodAgent`s
and a `Spy` and needs nothing from `demo/`, and `test_boundary.py` now names `team` on the library
side.

## Why the phases are plain Python and not a prompt

`warroom.py:1-8` makes the argument this module inherits: "the fan-out order and the barrier are
ordinary `asyncio`, so they are reproducible in a way a prompt-driven orchestrator is not." It is
worth stating why that is a design claim rather than a preference.

### Rejected: an LLM-driven orchestrator

The obvious alternative is a lead agent holding `spawn`, `ask` and `retire` as tools, deciding for
itself who to convene and in what order. It is more flexible, it needs no `Spawnable` at all, and
it is what most agent frameworks ship.

It cannot be measured. A run's phase order becomes a sample from the model, so two runs of the
same team on the same data differ in who was asked, when, and whether anybody was asked twice —
and every experimental claim about the *team* is then confounded with a claim about the
orchestrator's mood. The barrier in particular is unenforceable: a lead that decided to start
reasoning after two of four reports is doing something no oracle can distinguish from a lead that
waited, until the verdict is wrong. Determinism here is what makes the *interesting* part —
whether disjoint evidence converges on the truth — the only variable.

It also costs turns for nothing. Convening a fixed cast is not a decision; it is four spawns. Each
one routed through a model is a turn spent producing a tool call the code could have written, and
`process/agent.py` made the same argument for deterministic corridors: asking a model to choose
from one option buys nothing.

The flexibility that is genuinely wanted survives anyway, in the place where it is a real choice:
the lead decides who to *interrogate*, what to ask, and whom to hire, and hiring is exactly the
seam where an unbounded decision is given a budget rather than removed.

### Rejected: a `Team` that owns its members

A shorter design has `Team.__init__` build the cast — pass the plane names, get the specialists.
`WarRoom` is written that way (`warroom.py:68`), and for one incident it is right.

As a library base it forecloses the case the library exists for. A subclass that cannot swap the
cast cannot supply `MethodAgent` members where the base assumed `Agent`s, cannot inject scripted
members for an offline test without monkeypatching a constructor, and cannot build members per run
— which matters because `members()` is called inside `execute`, so a scripted model bound onto an
agent *after* the `Team` was constructed still reaches it. That is the same reasoning
`ProcessAgent.decider` records for compiling inside `work()` rather than in `__init__`
(`agent.py:143-148`): anything captured at wiring time silently bypasses the instance binding a
test uses, and the failure mode is the worst available in an offline suite — a test that reaches
the network instead of failing.

## Why members are typed tools and `send_message` is the demo's deliberate exception

The runtime injects two peer tools into every thread, and `send_message` refuses any target whose
`input_shape` is not `STR_PROMPT`:

```python
if peer_info.input_shape != InputShape.STR_PROMPT:
    return (
        f"error: thread {thread_id} has input_shape={peer_info.input_shape!s}; "
        "send_message requires a str_prompt peer."
    )
```

`ai_thread/tools.py:172-176`, and `continue_then_receive` additionally requires the *sender* to be
`STR_PROMPT` (`:225`). So being addressable by the message bus is not free: it requires compiling
an agent down to one `str` parameter, which is exactly the price `method.py`'s header itemises —
the typed contract, the docstring-as-template, and every learnable parameter, all three lost at
once. `demo/agent.py:132-133` pays it on purpose and raises if it ever stops paying, because the
demo's *subject* is a room of peers that can only reach each other through the runtime.

A library has no reason to pay it. A `MethodAgent` compiles to `STRUCTURED` and joins a lead
through `agents()`, which hands the lead one typed tool per capability under its qualified name —
composition by Python typing, checkable at the call site, where a chat box is not. `notify()` is
the inbound side channel for the cases that still want one: it appends to a thread's log without
starting a cycle, so the next `run` sees it as context (`test_method.py:341-351`).

This is the module's central claim, so it is a test and not a docstring
(`test_the_lead_holds_each_member_as_a_typed_tool_and_no_member_is_reachable_by_chat`), and it is
asserted in both directions: every member's capability is in the lead's `config.tools` under its
qualified name, *and* every member's `input_shape` is `STRUCTURED` — so no member is reachable by
the bus at all. A team that quietly compiled its members down to one `str` to make them chattable
would pass the first half and fail the second.

Measured during this build and worth recording, because it nearly falsified the test that was
meant to prove the claim: `_infer_input_shape` (`ai_function.py:71-95`) classifies **exactly one
positional parameter resolving to `str`** as `STR_PROMPT`. So a `@ai_method` whose only parameter
is `focus: str` compiles to `STR_PROMPT` and *is* chat-addressable. The typed shape is the one with
a real signature — two or more parameters, or one that is not a `str` — which is the ordinary shape
for anything worth calling as a tool, but not something to assume. The fixture cast was corrected
to have real signatures. Separately measured: a `STRUCTURED` lead is still drivable by
`handle.run("one string")`, because the positional binds to its first parameter — which is why
`execute`'s `lead_handle.run(request)` is correct for typed leads and not only for `STR_PROMPT`
ones.

`team.py` therefore does not mention `send_message` anywhere, and the demo's `STR_PROMPT` cast
keeps working because it subclasses this skeleton and supplies its own members — not because the
skeleton knows what a plane is.

## Why the barrier

Phase 2 gathers every member's briefing and waits for all of them before the lead is spawned. The
alternative — let the lead start while the slower members are still reading — is faster and
wrong.

A lead that begins interrogating a half-formed team produces a verdict whose evidence depends on
scheduling: the same team, the same data, and a different answer depending on which member's model
happened to return first. That is not a small nondeterminism, because the members hold *disjoint*
evidence by design; a verdict formed from two of four planes is a verdict formed from different
data, not merely an earlier one.

`return_exceptions=True` is the other half. A four-member team in which one member's thread died
is still a team worth asking, and the lead can see that one source is missing because
`render_brief` puts every briefing — the surviving answers and the `"error: "` ones alike — into
the text the lead is asked. Letting the exception out would lose the three members that worked and
turn one dead thread into a dead run. The delivery is the next section, and it was missing once.

Measured, because the pairing depends on it: `asyncio.gather` starts every coroutine before any
completes (`['start-a', 'start-b', 'start-c', 'end-b', 'end-c', 'end-a']`) and returns exceptions
positionally, so `zip(cast, answers, strict=True)` really does pair each member with its own
outcome. The barrier test asserts from an interleaved journal the members and the lead's model both
write to, with the slowest member declared *first*, so that a `gather` replaced by a sequential
loop would still pass while an absent barrier would not — and separately asserts that the fast
member finished before the slow one, which a sequential loop over a slow-first cast cannot produce.

## Why the briefings reach the lead, and what an all-dead cast does

The barrier is only worth holding if the evidence it waits for arrives somewhere. It did not,
once: `brief` put the answers into the returned `TeamRun` and `execute` handed
`lead_handle.run(request)` the bare request, so the paragraph above — the lead "can see in its own
briefing text that one source is missing" — described a delivery that was not in the code. The
symptom was silent in the ordinary case, because a lead holding its members as typed tools can go
and ask them, and loud in exactly the case the barrier exists for: **measured on a two-member team
with both members raising, the lead ran, its model context mentioned no error at all, and the run
reported `correct=True`.** A verdict from a lead that read nothing, graded correct.

Two changes, one for each half.

### Delivery is a template method

`render_brief(request, briefings)` renders the request and then one line per member, and `execute`
drives the lead with its result. One text block rather than tools or extra turns, because that is
the only channel every lead shape shares — a lead is an `AIFunction` over a typed `prompt_fn` and
`handle.run(text)` binds to its first parameter for a `STRUCTURED` lead as much as for a
`STR_PROMPT` one (measured above). Anything richer would have to know the lead's signature, which
is the subclass's business.

It is a template method rather than inlined because composition is a judgment. A lead that reaches
its members another way wants them left out; a lead with a strict prompt format wants its own
headings; and an override returning `request` unchanged restores the pre-delivery behaviour
deliberately, which is the honest way to want it. The test asserts the override *won* — that the
base's rendering did not also run — because a delivery that appended both would make the
leave-them-out case unreachable.

Checked against the demo before it shipped, since `WarRoom` is the one real subclass and its
`investigation.json` is a published artifact. `WarRoom.brief` re-keys by plane, so `render_brief`
receives plane names and the lead's request grows a `What your team reported:` section over
`deploys`/`metrics`/`logs`/`traces`. Nothing pins the old shape — `WarRoom` is reached from
`demo/cli.py` and from no test in the suite — and the nine published keys of `Investigation`, in
order, are unchanged. So the default delivers and no override was needed, which is the outcome to
prefer: the demo's lead now reads the four planes it was always supposed to have been shown.

### An all-failed run is refused, not run

`_check_some_briefing_survived` raises when every briefing starts with `BRIEFING_ERROR`. The
asymmetry with `return_exceptions=True` is the whole argument: three planes of evidence with one
missing is a team, and the lead can be told which one is absent; *nothing* is not a team. The lead
holds no evidence of its own — that is the reason a team exists — so it would reason from the
request alone and produce a verdict shaped exactly like a real one, which `grade` has no way to
distinguish.

Raised rather than returned as text, which breaks the "every failure in the hiring seam is text"
rule for a reason that also explains the rule: text is for failures **a model can fix**, and there
is no model in this one. A dead cast is a coordinator, a network or a wiring failure at the level
above the lead, so the only honest report names the members and their errors to the *caller* — the
party that can act. An empty cast is a different thing and stays allowed: a team that declares no
members has not lost any, and `Toy(cast=[])` is the shape half the tests use to drive the hiring
seam alone.

`BRIEFING_ERROR` is a class attribute rather than two string literals because the rendering in
`brief` and the check here have to agree — the check's whole job is to notice that every string is
one of those. A subclass rendering failures differently moves both at once.

## The negotiation phase: optional, bounded, off by default

`negotiation_rounds: int = 0` on `Team` adds a phase between the briefing and the verdict: the
lead's first gated ruling is treated as a draft plan, `render_plan` renders it, and each round
fans that text to every member (`plan_request`, through the member's own `ask` — one cycle, same
barrier, same `return_exceptions=True` and `BRIEFING_ERROR` rendering as `brief`). A member
answers with objections or with the `APPROVAL` token; unanimity ends the negotiation early,
anything less goes back to the lead as one `run(render_objections(...))` — a full gated cycle, so
every revision faces the oracle exactly as the draft did. The transcript (plan, objections,
approvals, outcome, revision per round) lands on `TeamRun.negotiation`.

### The evidence for wanting it

AgentRadio (arXiv 2607.28430) measured a negotiation round as its single biggest layer: +67 net
rubrics, against +24 for passive awareness. Their MinIO case is the failure shape this phase
exists for, and it is *this* skeleton's failure shape too: the members hold disjoint evidence by
design — that is why there is a team — so a plan drafted from one-shot briefings can carry a flaw
any single member would catch on sight, and `brief` was a one-shot barrier: members answered
once, only the lead saw the answers, and the plan was never reviewed by the people holding the
evidence it was built from. Caveats carried honestly: their n=124, single run per task, LLM
judge, and the +29.8 headline bundles three layers — which is why the phase is off by default and
bounded rather than the new normal.

### Why zero is the default and what zero means

With the budget at zero, `negotiate` returns before touching anything and `execute` is the
pre-negotiation skeleton byte-for-byte: one member cycle each, one lead cycle, the same event
sequence. The compatibility claim extends to the artifact — `TeamRun`'s serializer drops the
`negotiation` key when the list is empty, so the demo's published `investigation.json` keeps its
nine keys without `demo/` changing at all (measured: same keys, same order, aliases intact,
round-trip equal). The test pins the *call counts* and the event sequence rather than the empty
list, because an empty list is also what a broken phase that ran and recorded nothing returns.

### Why the plan travels through `ask` and not `notify`

`Recruit` guarantees three verbs — spawn, ask, retire — and `notify` is not one of them; the
`Member` adapter deliberately does not wrap it. And an *answer* is wanted here: `notify` appends
to a thread's log without starting a cycle, so a notify-based fan-out would deliver the plan and
collect nothing until some later cycle that may never come. One `ask` per member per round is one
channel every member shape already supports, one model cycle, and a captured request a test can
read — which is the requirement the next paragraph makes load-bearing.

### The delivery lesson, applied twice

The briefings once never reached the lead (`render_brief`'s history above): the phase recorded
its data faithfully and the wire was missing, and only reading the model's actual context could
have said so. Negotiation has two such wires — plan → member, objections → lead — and both are
pinned from scripted-model contexts, not from the transcript: the plan text is asserted inside
each member's own model context, the objection text and the objector's name inside the lead's
revision context, and the round-2 fan-out is asserted to carry the *revision* and not the draft.
Measured with the wire deliberately severed (revision prompt replaced by a generic "your team
objected"): the transcript still recorded every objection and only the context assertions failed
— the render_brief bug's exact shape, reproduced on purpose to prove the tests can catch it. The
negative half needed scoping: a thread's history is cumulative, so "the draft did not fan out
again" is a claim about round 2's *own request*, not about a context that legitimately carries
round 1 above it.

### Approval is containment, and the tradeoff is `BRIEFING_ERROR`'s

`approves` checks that the answer contains `APPROVAL` and does not start with `BRIEFING_ERROR`.
Containment rather than equality because a typed member answers with a pydantic model whose
`str()` embeds the token inside a field's repr — an equality check would silently veto every
typed member and every negotiation would run to its cap, with nothing raised. The cost is the
same one the `BRIEFING_ERROR` prefix carries: an objection that *quotes* the token is miscounted.
Both the instruction (`plan_request`) and the check read the one class attribute, so a subclass
with a stricter vocabulary moves them together — a drifted pair would make unanimity unreachable
and every negotiation silently cap out.

### The edges, refused rather than smoothed

A member that raises mid-review is a briefing failure's twin: rendered under `BRIEFING_ERROR`,
never fatal, never counted as approving — it blocks unanimity (the lead revises knowing one
reviewer died) and the cap bounds what that blocking can cost. A cap reached without unanimity
marks its last round `cap_reached` rather than `revised`, and the run proceeds with the last
gated revision — the transcript says the team never agreed rather than implying it did. An empty
cast never negotiates: `all([])` is true, so without the guard a `Toy(cast=[])` at rounds>0 would
record a unanimous round no member ever gave. And a negative budget is refused at construction —
`range(1, 0)` is empty, so "negotiate backwards" would silently mean "never negotiate", which is
the fail-soft this kernel keeps refusing.

What this phase deliberately is not: a member↔member channel. Objections flow member → lead and
the revision flows lead → members; no member sees another's objection except as the lead's
revision reflects it. A lateral channel is a different design with its own determinism argument
to make.

## Why a duplicate member name is refused at wiring time

`brief` keys its mapping by `member.name`, because a name is the only identity `Recruit`
guarantees. So a cast holding two members called `plane` produces a mapping with **one** entry:
measured on a two-member cast answering `FIRST` and `SECOND`, the report carried
`{'plane': 'SECOND'}` and the run was graded correct. The earlier briefing is gone from
`TeamRun.briefings` and from whatever the lead was shown, and nothing raises.

The half that costs most is the one no report can show. A reader comparing `len(briefings)` against
`len(members())` is the only person who could notice, and no reader does that. So
`_check_no_duplicate_members` sits with the other pre-spawn guards, where it costs nothing —
`Counting([])` and the spies' empty event lists pin that the refusal precedes both the model call
and the spawn, which is `_check_no_oracle_collision`'s placement lesson applied a second time. The
fix is the caller's either way: name them apart, or override `brief` and key by something else,
which is what `WarRoom` does when it keys by plane.

## Why the oracle is a post-condition, and how it composes

The argument is `gated.py`'s and holds unchanged: a check the caller runs afterwards is a check the
caller can forget, and the loops that forget it are the ones under pressure. A post-condition
cannot be skipped — the runtime runs every validator before the cycle returns and turns any
exception into the text of a `[VALIDATION ERROR]` user turn the *next* attempt reads — so refusal
is the default and the oracle's own words are the re-ask feedback. `oracle` is one of the four
required overrides for a reason that follows directly: the only possible default is "raise
nothing", i.e. grade every verdict correct while reporting that grading happened.

The attach is the part that needed measuring. `AIFunction.replace` merges through
`dataclasses.replace` (`ai_function.py:407`, `_merge_config`:32-49), so a field named in the call
**overwrites**:

| call on a lead carrying `post_conditions=(existing,)` | result |
| --- | --- |
| `replace(post_conditions=[added])` | `['added']` |
| `replace(post_conditions=[*fn.config.post_conditions, added])` | `['existing', 'added']` |

So the naive `replace(post_conditions=[self.oracle])` silently deletes every post-condition the
subclass's lead already carried, and the failure mode is the worst available: the checks are gone,
nothing raises, and the run reports a gated verdict. `_gated_lead` therefore reads the lead's own
conditions off its config and prepends the oracle, which is `gated.gated()`'s composition for the
same reason. Fields the call does not name are untouched — `max_attempts`, `system_prompt` and
`tools` are asserted to survive, and `max_attempts` is the one that matters, because it bounds how
many times a refused verdict is re-asked and what a never-admitted run costs.

`config_hook` cannot compose the same way, because the runtime calls exactly one hook per cycle
(`ai_thread.py:548-553`). A lead arriving with its own hook and a team with a non-empty catalog is
a genuine conflict, and `_gated_lead` refuses it loudly rather than resolving it by precedence:
either silent outcome is invisible — the lead loses its cycle-local tools, or the team loses its
hiring — and the message names both ways out (compose them into one hook in `lead_function`, or
return an empty `catalog()`). A lead with a hook and *no* catalog keeps its hook untouched, because
the conflict is between a hook and a catalog and not between a hook and a team.

### The collision guard, and why it is reachable here

`ai_thread` passes the result positionally and then injects, by keyword, every bound argument whose
name appears in the validator's signature. Those two rules are useful together and fatal for the
*first* parameter, which already holds the result: the same slot filled twice raises `TypeError:
got multiple values for argument`, which the runtime catches and reports to the model as a
validation failure. The oracle appears to refuse every verdict, the message makes no sense, and
the fix is a one-word rename nothing points at. `gated._check_no_collision` refuses exactly this
for a propose method, and it is reachable for a lead too — a lead is an `AIFunction` over a typed
`prompt_fn`, so `decide(question, rigour)` is the ordinary shape and an oracle whose first
parameter is named `question` is one careless rename away.

`_check_no_oracle_collision` therefore runs in `_gated_lead`, over *every* attached condition
rather than only the oracle — the trap is a property of the runtime's kwarg injection and an extra
condition hits it identically. Only the first parameter is checked, in `gated.py`'s spirit:
forbidding the rest would forbid the injection the runtime documents.

Measured with the guard removed, at `max_attempts=3`: **4 model calls burnt**, and the run dies as
`AIFunctionError: Result not satisfied after 4 attempt(s)` — a `TypeError` about Python calling
mechanics, laundered into a report that the model failed to satisfy a requirement.

### Where `_gated_lead` is called, which was wrong once

`_gated_lead` is where both wiring guards live, so *when* `execute` calls it decides what a
refusal costs. The first version composed the lead where it is used — just before the spawn, after
`assemble` and `brief` — which reads naturally and is wrong for the reason the hooks-and-budgets
lesson names: a guard that fires after the barrier has already spent what it protects. Measured on
a two-member team with a colliding oracle: the refusal was correct, arrived as a `RuntimeError`,
and cost two spawns and two *real* briefing cycles to reach.

So the wiring phase is now `members()`, then `_gated_lead()`, then `assemble`. The order between
the first two also matters, and in the other direction: `members()` runs first because a subclass
may build its cast there and hand those same objects to `lead_function()` as tools — which is the
shape a typed team has, since `agents()` is called on the objects `members()` returns. Both
orderings are pinned by a test that records the three calls, and both assertions were confirmed to
fail when the composition is moved back after the barrier.

This is also why `members()` is called inside `execute` at all rather than in `__post_init__`: the
guard's cheapness comes from being before the first *spawn*, not from being at construction, and
building the cast per run is what keeps a scripted model bound after construction reachable.

### Why `oracle` is not fault-wrapped, unlike `gated.admits`

`GatedProposer.admits` wraps its gate and re-raises an internal failure as a message that says it
is internal, because the runtime cannot tell a bug from a refusal and a bug that reads as a
refusal burns every retry. The same trap exists here — measured: a validator raising
`AttributeError` under `max_attempts=2` is called **three times** and the cycle raises
`AIFunctionError: Result not satisfied after 3 attempt(s)`, with the original type gone — and the
wrap is still not applied, for two reasons.

`admits` has something to wrap *around*: a gate that is a separate injected callable, a
`candidate_of` extractor, and a `Verdict` object whose `ok` and `report_text()` reads can each fail
independently. Those are four user-supplied surfaces on one path, which is why `gated.py` needed a
vocabulary for "this was a fault, not a verdict". `Team.oracle` is one method the subclass writes
directly, with no extractor and no verdict object; there is no seam at which the base could tell a
deliberate `AssertionError` from an accidental one, so a wrap would either catch everything —
including the refusals the oracle exists to raise — or catch nothing.

And the subclass already has the tool: an oracle that wants the distinction makes it itself, in
its own words, exactly as `HarnessProposer` does. What the library owes on this path is the part it
can guarantee, and `test_an_oracle_that_is_itself_broken_burns_the_retries_and_the_run_still_unwinds`
pins it: a broken oracle costs turns, and not leaked threads.

## Why `grade` is defaulted and `oracle` is not

Both judge a verdict, so the asymmetry needs an argument. By the time `grade` runs, the oracle has
already gated: a verdict that reached it either satisfied the oracle or the cycle exhausted its
attempts and raised. So `(True, [])` is a *true* default — a team whose oracle is its whole
standard leaves `grade` alone and the report says correct, honestly. An `oracle` default would be
"raise nothing", which reports a grading that did not happen.

They stay two hooks because they answer different questions. An oracle is checked per attempt and
its text is written for a model that must revise; a grade is computed once, for a reader, and may
apply a standard it would be wrong to re-ask against — a stricter check the model was never told
about, or one too expensive to run on every attempt. `warroom.py:116-118` calls `incident.verify` a
second time for exactly that reason, and the test that makes the split observable has an oracle
admit a verdict that `grade` then refuses, which is only visible because `correct` and
`oracle_failures` are separate fields.

## Why the catalog is a mapping and the mandate goes through the factory

`demo/staffing.py` reads its roles from `ROSTER`, a module-level dict `__init_subclass__`
populates. Two problems make it unliftable. It is global, so every team in one process shares one
pool of hireable roles; and it is a registry of `Agent` subclasses, which is the application's base
class. `hiring_tools` therefore takes `catalog: Mapping[str, Callable[[str], Recruit]]` — a plain
mapping the caller supplies, called as `factory(name)`. What a team may hire is a property of that
team.

The mandate is the sharper change. `staffing.py:109` writes

```python
sub.mandate = mandate  # type: ignore[attr-defined]
```

which works only because every roster class happens to declare the attribute (`cast.py:133`,
`:159`, `:188`). A library cannot assume it: the `Recruit` protocol says nothing about a mandate,
so the injection would either fail on a `__slots__` recruit or silently create a field nothing
reads — and the `type: ignore` is the marker that the type system already knew. So the mandate
reaches the tool as an argument, is recorded on the roster's log, and is handed to the *factory*,
which is a closure or a `partial` over whatever the recruit's constructor actually takes. Wave 2
keeps the demo's behaviour inside its own factories, where the attribute is real.

## Why every hiring failure is text and never an exception

Five things can go wrong in the hiring seam, and a model can fix all five: an unknown role, a
duplicate name, a headcount cap, delegating to someone unhired, dismissing a stranger. All five
return a string beginning `error: `. The test of the rule is what it excludes, and there are two:
a `spawn` that raises and a `retire` that raises are *not* on this list, because neither is a
mistake the lead made and neither is anything it could do differently. They surface as faults, and
what the seam owes on those paths is a roster that still describes reality — the two ordering
sections above.

Measured, because the behaviour is what makes this correct rather than merely tidy: a tool
returning `"error: ..."` reaches the model as a **successful** tool result whose content is that
string —

```
{'toolUseId': 'scripted-...', 'status': 'success', 'content': [{'text': 'error: nope, pick another name'}]}
```

— and the cycle continues. So the model reads the problem and fixes it in the next turn, which is
what the tests assert: after each refusal the scripted lead makes its remaining calls and the run
completes. An exception would instead surface as a tool fault in the middle of a cycle the lead was
going to finish.

The cap is checked *before* the recruit is constructed, and that ordering is load-bearing rather
than stylistic — a cap that fires after the spawn is a cap that still spent the thread it was
refusing. The test asserts both the log and a construction spy, which is what separates the two
failures: with the cap moved after the spawn the log assertion still passes and the spy assertion
is the one that fails.

## The roster's lifetime is one run, and `execute` is where that is enforced

A `Team` instance outlives a single `handle.run`: the field's default is a construction-time object,
and a handle runs as many times as it is called. Every promise attached to that roster is a promise
about **one** run — the headcount cap, the duplicate-name refusal, the hiring log a report
publishes — so a roster carried into a second run makes all three quietly false there. Measured on
one instance and one handle, run twice:

| what run 2 saw | why |
| --- | --- |
| its report opened with run 1's hiring log | `hiring_log=self.roster.log`, never cleared |
| its names were already taken | `if name in roster.hires` still held run 1's hires |
| `max_hires` was short by run 1's headcount | the cap counts `len(hires)` |
| `delegate` reached a retired thread | run 1's `finally` retired them and left them registered |

The last one is the sharpest: `delegate_failed` against a thread the previous run tore down, which
reads in the audit like a subagent that broke rather than a run that inherited a corpse.

So `execute` stands up a fresh roster before anything is spawned. It is `type(self.roster)()` and
not `Roster()`, which is not stylistic: `WarRoom` narrows the field to a `Staff` whose `record` is
what lands a hire's mandate on the agent it hired, so a base resetting to the library's own class
would leave run 1 correct and brief every later run's hires on nothing — with the `type: ignore`
that marks the attribute injection nowhere in sight. The test asserts the *subclass's* `record` ran,
not merely that an `isinstance` held.

Within a run the roster persists exactly as before, which is what makes the hiring seam usable at
all: a lead hires on one turn and delegates on a later one. That is also the finest granularity
worth testing — measured, the `config_hook` fires **once per cycle** and a `handle.run` is one
cycle, so relocating the reset into the hook is indistinguishable from leaving it in `execute`.

The bullet below still says "no persistent roster", and it is now true by construction rather than
by convention.

## Why a hire reserves before it awaits

`hire` runs three refusals — unknown role, duplicate name, headcount cap — and then spawns. The
first version registered the recruit *after* the spawn, which is the natural order and wrong,
because the runtime's default tool executor is `ConcurrentToolExecutor`
(`strands/agent/agent.py:462`): every tool call in one assistant turn is its own task, and
`recruit.spawn` awaits. Two `hire`s in one turn therefore both read the same pre-hire roster.

Measured, both scenarios:

| one turn, two hires | result before the fix |
| --- | --- |
| names `a` and `b`, `max_hires=1` | both hired, `headcount == 2` — the cap enforced by nobody |
| both named `dup` | two spawned, one registered, retirement counts `[0, 1]` |

The second is worse than a miscounted cap. The second write to `roster.hires[name]` overwrites the
first, so the first recruit is **live and unregistered** — and `dismiss`, `execute`'s `finally` and
`teardown` all walk `roster.hires` to find who to retire. A leaked thread, unreachable by all three
unwind paths, with nothing raised anywhere.

The fix is a reservation: the three checks *and* the write into `roster.hires` run in one
synchronous stretch, and only then is the spawn awaited, rolled back if it raises. A `Lock` was the
alternative and is worse for this shape — the checks and the write already happen inside one
event-loop step, so there is nothing to serialise and a lock would add a contention surface to
guard a critical section that is already atomic. The rollback earns its own test: without it the fix
trades a race for a phantom entry holding a name the model can never use and a slot against a cap
it never filled.

The fixture is the load-bearing part. `SlowSpawnSpy` awaits in `spawn`, because a spawn that never
suspends cannot interleave — a plain `Spy` would let a broken reservation pass, which is the kind of
test that reads correct and proves nothing.

## Why `dismiss` retires before it unregisters

`dismiss` used to `pop` and then `await recruit.retire()`, which hands the recruit to a local
variable and drops the roster's only reference to it. A `retire` that raises — a coordinator hiccup,
a `ThreadNotFoundError` from something else's teardown, the failure `return_exceptions=True` exists
for elsewhere in this module — then leaves the recruit **unregistered and alive**, and every unwind
path looks for it in `roster.hires`. Measured with a recruit whose first `retire` raises: one retire
call total, and the thread still live after the `finally` *and* an explicit `teardown`.

So the order is inverted — retire first, `del` only on success. The raise then leaves the recruit
registered, where the `finally` and `teardown` both retry it, and `retire` being idempotent per
`MethodThread`'s contract makes the retry free. This is the same reasoning as the reservation above
read backwards: put the registry write on the side of the `await` where a fault leaves the registry
describing reality. The dismissal is not recorded in the log, which is correct — a dismissal that
did not complete must not appear in the audit as one.

## Why the unwind is in a `finally` and `teardown()` exists as well

Three ways a run ends, and the two mechanisms cover different ones. Measured against the worker:
an exception out of `execute` propagates to the caller of `handle.run` and the worker does **not**
call `teardown()` on that path; `terminate_now` **does** call it, with no cycle in flight. So the
`finally` inside `execute` covers the normal and the faulting run — including the lead's own thread
— and `teardown()` covers external termination, where `execute` may never have been entered.
Neither is redundant, and `retire` being idempotent per `MethodThread`'s contract makes the overlap
harmless.

`return_exceptions=True` on the unwind is the `MethodThread.retire` lesson restated: a recruit
something else already tore down raises `ThreadNotFoundError`, which is a `KeyError` and sails past
the handlers callers write. Without it the first failure aborts the loop and leaves every later
member alive — the exact failure an unwind loop exists to prevent.

Measured with the `finally` broken (unwind moved to an `else`): a failed run leaves **two threads
still registered** on the coordinator.

## Why `fork()` raises

`protocols.py:235` names `NotImplementedError` as an honest answer, and a team is the case it was
written for. A fork copies the event log, which is the whole of an `AIThread`'s state and nowhere
near the whole of a team's: the members are live threads this instance owns and the roster is a
mutable registry. Two "independent" branches would retire each other's members and hire into one
dict, so the fork would be a shared-state bug wearing a branch's clothes.

## `TeamRun.verdict` is `Any`, and `run_type()` is the price

`protocols.py` states `deserialize_result(serialize_result(x)) == x` as an `Ensures` on the pair.
`TeamRun.verdict` has to be `Any` because the lead's output type is the subclass's choice, and a
pydantic field typed `Any` does not round-trip a `BaseModel`: measured, `{"verdict":{"answer":"x"}}`
validates back to a plain **dict**, so the base round-trips *shape* and not equality.

Rather than leave the protocol's guarantee quietly false, `run_type()` is the seam: a subclass
declares `verdict: Verdict` on a `TeamRun` subclass and names it there, and the round-trip holds.
`demo.warroom.Investigation` is already exactly that shape, so Wave 2's rebase is one method. The
test asserts both halves — the narrowed class round-trips equal and the base widens the verdict to
a dict — so the seam is a decision rather than an omission.

## The progress markers

`CustomEvent`s named `team.assembled`, `team.briefings_in`, `team.lead_running`, `team.hired` and
`team.graded`, mirroring `warroom`'s. They are the only observation of a phase available to a
reader outside the process, and they are what a live tape subscribes to (`demo/live.py:55-63`,
which reads `event.kind` and `event.payload` and stamps the thread itself, since `CustomEvent`
carries no `thread_name`). Kinds stay namespaced under `team.*` so a subscriber can filter one
team's progress out of a shared log. `last_event_id` skips them for want of an id
(`runtime/usage.py:46-49`), which is why the usage baseline is captured from it and not from a
count.

## What this module deliberately does not do

- **No learning loop.** No optimizer, no memory, no `Recall` binding. A team run produces a
  verdict and a bill; turning a series of runs into a gradient is a separate concern with its own
  traps, and `gated.py` records why "admissible" and "better" must not be the same mechanism.
- **No persistent roster.** The `Roster` lives for one run and is the evidence that run was
  budgeted, and `execute` is what makes that true rather than a convention — see the lifetime
  section above, where the four things a carried-over roster broke are measured. A roster that
  outlived a run would make the headcount cap meaningless across runs and would be shared mutable
  state between two teams that never agreed to share anything.
- **No cross-team messaging.** No team knows another exists. The runtime already has a peer
  channel for threads that want one, and a second one at team scope would be a message bus the
  determinism argument above exists to avoid.
- **No forkable teams.** See `fork()`.
- **No `send_message` anywhere.** The typed join is the library's path; the bus is the demo's
  deliberate exception, and the gate that forces the choice is cited above.
- **No `Process` coupling.** A `ProcessAgent` can be a team's lead or a team's member — it is a
  `MethodAgent` — but the skeleton knows nothing about states or transitions, and a team is not a
  verified process. Composing them is the caller's to do and the caller's to justify.
