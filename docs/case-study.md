# Case study: automating a building-permit process, safely

**Data:** 1,434 real building-permit applications from a Dutch municipality.
8,577 recorded steps, 27 activities, 48 staff, 478 days of operation. A public XES
event log (`data/receipt.xes`), used unmodified.

**Question:** we want AI agents to run this process. How do we know they will not
break it?

**Answer in one line:** mine the process from what actually happened, state the
compliance rules in one sentence each, prove the process obeys them before any
agent touches it, and make the runtime refuse the paths that break them.

---

## 1. What the data says before anyone builds anything

| Measure | Value |
| --- | --- |
| Median case duration | **0.8 hours** |
| 95th percentile | **572 hours** (24 days) |
| Slowest case | 6,620 hours (276 days) |
| Cases over 30 days | 48 (3.3%) |
| Total elapsed queue time | **175,499 hours** ≈ 7,312 days |

The median says this process is fast. The 95th percentile says one in twenty
applicants waits over three weeks. **A 700× spread between the two.** Any dashboard
reporting the average describes a process nobody experiences.

The queue is concentrated. One handoff — *print and send confirmation* → *determine
necessity of stop advice* — carries **58,131 hours** of accumulated waiting across
791 transfers. Its median wait is 0.01 hours. Its 95th percentile is 338 hours.
That gap is the definition of a queue: usually instant, occasionally two weeks, and
invisible to any measure of central tendency.

**Rework, measured rather than estimated:** 59 cases redid *determine necessity of
stop advice* (107 extra touches), 35 redid the *check confirmation* step (52 extra
touches).

## 2. The model, mined from behaviour and not from a policy binder

We derived the process from the log itself: count every activity-to-next-activity
pair, keep the edges walked by at least 25 distinct cases, drop the rest.

**Result: 11 activities, 20 handoffs, replays 89.8% of all real cases.**

That last number is the one to insist on. It is a testable claim, not a drawing.
The 6.3% of traffic we dropped is stated openly rather than hidden — it is the
long tail of one-off exceptions, and a model that included all of it would be a
photograph of the log rather than a description of the process.

The threshold of 25 is the single number to defend. Lower it and you explain more
cases with a model nobody can read; raise it and you get a clean diagram that
describes fewer real cases. We verified both directions behave as expected.

## 3. The finding

**The mined process is structurally perfect and provably non-compliant.**

We ran an exhaustive model-checker (TLA+/TLC) against the mined model. It confirmed:
no deadlock, no unreachable activity, every value in range. On the structure alone,
this process is sound.

Then we added one rule, in the language a compliance officer would use:

> *A confirmation of receipt may not be determined before it has been checked.*

The same model **failed immediately**, and the checker named the exact three-step
path that breaks it:

```
Confirmation of receipt  →  T06 Determine necessity of stop advice
                         →  T04 Determine confirmation of receipt
```

The verification step is skipped entirely.

**This is not hypothetical.** We then measured it in the raw log: **118 of 1,434
cases (8.2%) never perform the check at all.** By channel: Desk 11.0%, Internet
8.2%, Post 5.7%, e-mail 4.8%.

A model-checker found a control gap in a live government process from its own data,
in seconds, before a single agent was deployed.

## 4. Two independent verifiers, one conclusion

We ran a second, entirely different check: property-based testing (Hypothesis)
driving the actual execution engine with adversarial inputs. It found the **same
violation** by sampling real code paths, where the model-checker found it by
exhausting an abstraction.

Both were needed, and here is the uncomfortable reason. During development, **both
verifiers initially passed while the process was broken.** A variable pinned to one
starting value made the whole branch unreachable, so each tool cheerfully confirmed
a rule about a case it never visited. Same blind spot, two tools, independently.

The lesson for governance: *a green check is a claim about what was examined.* Ask
what the verification actually visited, not whether it passed.

## 5. The guardrail at runtime

Verification happens before deployment. The agent runs after. So the same rules are
enforced live.

The agent is treated as an untrusted advisor. It **proposes** the next step; the
engine decides whether that step is permitted from the current state. We tested
this by replaying the model-checker's own non-compliant path through the live
engine with an agent driving. It was refused, with the reason in plain English:

> `NoDetermineWithoutCheck violated — a confirmation of receipt may not be
> determined before it has been checked`

The refusal is written to the audit database alongside everything else. **A blocked
run is evidence, not an error to be swallowed.**

Cost control falls out of the same design: when only one step is legally available,
the engine takes it without consulting the model. In a three-step permit run, one
step is an actual decision. **Two-thirds of the AI calls disappear**, and the
process gets faster and cheaper because the rules are explicit.

## 6. What is on disk afterwards

One libSQL file (Turso engine, WAL mode) holds the complete chain of evidence:

| Table | Contents |
| --- | --- |
| `events` | All 8,577 real events |
| `mined_models` | The model, timestamped, with its full definition |
| `verifications` | Each checker's verdict and detail |
| `runs` | Every execution, including refusals and rejected proposals |

Verified: written, closed, reopened, and read back intact. An auditor can ask *which
model was live on this date, what proved it safe, and what did it refuse to do* — and
get an answer from one file.

---

## What this means for a rollout

**Do not ask an AI to follow the process.** Extract the process, verify it, and make
the runtime enforce it. The agent decides only what is genuinely a judgment call,
and only within a boundary that has been proven.

Three things to expect:

1. **Mining will find control gaps before automation does.** 8.2% here. Fix them
   first — automating an unverified process industrialises its defects.
2. **The tail is the business case, not the average.** 3.3% of cases exceed 30 days
   and 175,499 hours sit in queues. That is where the money is, and it is where the
   median tells you nothing.
3. **Verification is not a checkbox.** Both our verifiers passed on a broken process
   until we corrected what they examined. Demand to know the scope of any green
   result.

**What we have not done:** no live LLM was used in these runs (all agent behaviour
was scripted, so results are deterministic and repeatable), the compliance rule was
written by a human rather than discovered, and the model-checker verifies the
process skeleton rather than the quality of the work inside each step. The
guardrails are real and tested; the intelligence inside them still needs evaluating
separately.

---

*Reproduce: `uv run pytest tests/test_casestudy.py` (17 tests over the real log).
Requires `java` and `tools/tla2tools.jar` for the model-checking step.*
