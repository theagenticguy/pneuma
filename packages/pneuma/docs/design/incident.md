# `demo/incident.py` — design rationale

The synthetic four-plane incident dataset, and why its information asymmetry is a machine-checked
property rather than a claim. The module docstring states the scenario and the planted truth; this
file carries the per-plane accounting and the decoy inventory.

## Scenario

All timestamps 2026-07-28, UTC, fixed literals. A five-service fleet — edge-gateway ->
checkout-api -> {pricing-svc, cart-store}, with auth-svc on the side — suffers a latency and error
incident. Symptom onset is 14:20Z.

**The planted truth, and there is exactly one.** checkout-api change `chg-4417` (v2.31.0, rolled
5% at 14:12Z, 25% at 14:16Z, 100% at 14:20Z) flipped its outbound HTTP client to
`max_retries: 2 -> 5`, `backoff: exp_jitter -> fixed_50ms`, and `retry_budget_pct: 10 -> disabled`.
Mechanism = `retry_amplification`: every checkout request now re-issues the same logical downstream
call up to 5x with no jitter and no budget, multiplying pricing-svc and cart-store inbound load
~4.8x while *user* traffic at the edge stays flat. Rolled back by `chg-4423` at 14:44Z; recovery by
14:48Z.

## Information asymmetry, by construction

Each plane alone is consistent with >= 2 mechanisms in `MECHANISMS`. Only the intersection of >= 3
planes collapses to one. The honest accounting of what each plane cannot rule out:

**DEPLOYS alone -> {retry_amplification, unbounded_concurrency, connection_pool_exhaustion,
cache_key_collision}.** `chg-4417`'s diff deliberately bundles FOUR suspicious knobs: the retry
trio, `max_inflight: 32 -> unlimited` (unbounded_concurrency), and `pool.max_idle: 8 -> 16` (pool).
Nothing in this plane says which knob mattered, or even that any change caused symptoms — there are
no symptoms here. The decoy `chg-4420` (auth-svc, `jwt.cache.key: sub -> sub+aud`) reads like a
textbook cache_key_collision, and a deploy-only investigator has no onset time to exclude it.

**METRICS alone -> {retry_amplification, n_plus_one_fanout, cache_key_collision}.** checkout-api's
`rps_out` / `rps_in` ratio jumps 1.19 -> 5.8 while edge rps and checkout `rps_in` stay flat: extra
calls are manufactured inside checkout-api, but this plane cannot tell repeated-identical calls
(retry) from many-distinct calls (N+1 fanout). cart-store's cache_hit_ratio 0.94 -> 0.70 reads as a
key-space problem that could itself be generating the extra calls. This is the ONE plane that
demotes connection_pool_exhaustion: pricing-svc's pool gauge tracks its own `rps_in` bucket for
bucket (16/48 at 1130 rps, 44/48 at 2604 rps, 48/48 at 4970 rps) and never saturates before load
rises, so the pool is a follower, not a driver. It still names no change id, so it can never
identify a culprit change alone.

**LOGS alone -> {retry_amplification, n_plus_one_fanout, connection_pool_exhaustion,
cache_key_collision}.** Logs show checkout "retrying upstream" WARNs, pricing-svc `PoolTimeout`
ERRORs, and cart-store duplicate-ish keys, but log lines carry no parent/child structure and no
rates, so "5 retries of one call" and "one request making 5 different calls" are
indistinguishable, and the pool timeouts are equally readable as pricing-svc's own defect.

**TRACES alone -> {retry_amplification, n_plus_one_fanout, clock_skew,
connection_pool_exhaustion}.** Trace tr-9d41 shows 5 sibling `pricing.quote` spans with the SAME
idempotency key 50ms apart (retry-shaped), but trace tr-7b02 shows a pre-existing legitimate
per-item fanout with DISTINCT keys, so a trace reader cannot tell which pattern is new. Spans
served by host `pricing-svc-7` carry a chronic -118ms clock offset (child starts before parent), a
real-looking clock_skew lead that traces alone cannot date. There are no deploy records and no
absolute rates here.

## Why the intersection is unique, and why no PAIR of planes suffices

The four candidate sets intersect in exactly `{retry_amplification}`, and every pair of planes
still leaves >= 2 live candidates. `self_check()` asserts both facts, so the asymmetry is
machine-checked rather than just claimed — a fixture whose central property is only asserted in
prose is a fixture that drifts.

Each plane contributes one elimination nobody else can make:

* **TRACES**: the extra pricing.quote spans share ONE idempotency key and run sequentially ~50ms
  apart -> repeats, not distinct per-item work, and not parallel. Kills n_plus_one_fanout and
  unbounded_concurrency.
* **METRICS**: pool_in_use rises only as rps_in rises, never ahead of it, and cart-store's
  hit-ratio moves after checkout's fanout does. Kills connection_pool_exhaustion.
* **DEPLOYS**: the only change whose staged rollout (14:12 / 14:16 / 14:20) matches the graded
  onset, and the only plane that names the retry knobs. Supplies `chg-4417` and kills the
  after-the-fact cache_key_collision decoy on timing.
* **LOGS + baseline TRACES**: the pricing-svc-7 -118ms offset is present at 13:52Z and 14:56Z with
  normal latency. Kills clock_skew.

Read together, that is why a correct answer requires composition rather than a single strong
plane, which is the whole point of the fixture: an investigator that reads one plane well and
stops is wrong here by construction, not by bad luck.

## Decoys: guilty-looking, innocent

1. `chg-4420`, auth-svc cache-key change at 14:31Z — 11 minutes AFTER onset; auth-svc metrics never
   degrade and its cache_hit_ratio *improves*. Timing does not fit.
2. `pricing-svc` — worst metrics in the fleet, pool pegged, but unchanged since `chg-4402` on
   2026-07-25. Pure downstream symptom.
3. `cart-store` cache_hit_ratio collapse, and `chg-4413` at 09:31Z, a max_entries bump — the
   hit-ratio drop is retry-driven duplicate traffic diluting the working set, five hours after that
   deploy.
4. `chg-4415`, edge-gateway at 14:02Z — coincident-ish, TLS-session-cache only, and edge-gateway's
   own numbers stay clean until its dependencies fail.

Each decoy is excluded by a different kind of evidence — timing, change history, causal direction,
and blast radius — so an investigator cannot clear the field with one heuristic.

## What this fixture cannot show

It is synthetic and every literal is fixed, so it measures whether an investigator composes
evidence, not whether it copes with real telemetry: no missing data, no clock drift outside the one
planted offset, no cardinality explosions, no partial ingestion. A method that scores well here has
cleared composition, and nothing else.
