# `detect/adversary.py` — design rationale

Why the adversarial search sits beside the prober rather than inside it, and how
"obviously worthless" is adjudicated without a human. The module docstring states the
invariants; this file carries the arguments.

## Why a search at all

`objective.py` enumerates the degenerate inputs that follow from a declared `Structure`.
That is mechanical and it is strong where it applies: enumerating from the structure has
caught a defect that a hand-written list of degenerate inputs missed (see
[objective.md](objective.md), "Degenerate inputs are computed, not declared"). But it can
only find what the structure implies. An objective can be gamed in a way nobody put in the
declaration, and the only mechanism that can find those is a search.

## Why it lives in a separate module

`objective.py` imports nothing from pneuma, has no network dependency, and a suite with no
credentials runs every check it has. Putting a Bedrock call in that file would make the
arithmetic half of the prober untestable offline. `probe(..., search=None)` is the whole
prober minus this, and that is the seam.

## What an adversary sees

Everything the prober does, and one thing more. The `Brief` carries the axes, the swept
grid, the in-domain ceiling every candidate is measured against, the structure if there is
one, and — this is the part that matters — a tool that *calls the objective*. That is what
makes it a search rather than a guess. An adversary that could only read a description
would be reasoning about what the arithmetic probably does; one that can evaluate it
probes, gets a number back, and revises.

`Brief.source` carries the scoring code when the caller supplies it. Reading the source is
how an adversary finds a clamp that hides a mis-measurement, a guard tested with exact
float equality, a term that cancels. Sampling 21 points does not show any of that.

## How "obviously worthless" is adjudicated without a human

Two mechanisms, and neither of them is the proposer's own opinion.

**Arithmetic, in `probe`, which cannot be argued with.** A candidate becomes a
`degenerate-optimum` finding only if re-scoring it in `_check_degenerate` puts it at or
above the grid maximum. `Verdict.upheld` is not a vote, it is a comparison. So an adversary
that hallucinates a triumphant input produces nothing: the point is scored, it loses, and
the report says so. This is the half that makes a fabricated candidate harmless.

**A judge panel, for the half that is a judgment call.** Whether an input is *worthless* is
not decidable from its score — that is the entire premise, since a worthless input that
scored badly would not be a defect. So `judge` asks a panel, and the panel's ballot is
structured: a judge answers `worthless: bool` plus a reason, and `MIN_AGREEMENT` of them
must say yes. A single judge is the failure mode: one judge agreeing with one proposer is
two samples of the same prior.

The judges' guard against rubber-stamping is that they are asked to reject, with a concrete
example of what rejecting looks like, and the panel is measured: `Verdict` keeps every
ballot, so `Panel.rejection_rate` reports how often judges actually said no across a run. A
panel that has never rejected anything is a check that cannot fire, and the number is in the
report rather than assumed. This is a measurement, not a proof — see "What this cannot do".

## Why the adversaries get distinct angles

`ANGLES` is five prompts, not one prompt run five times. Diversity is doing the work here:
identical adversaries at temperature-equivalent sampling explore the same neighbourhood, and
the neighbourhood is chosen by the prior they share.

The five angles correspond to the five ways this project's own objective was observed to go
wrong, which is the only grounded basis available for picking them: emptiness, escape,
cancellation, clamp exploitation, and tie-seeking. `docs/case-study.md` section 10 is the
source, and the module cites it from code — that file must not be moved or renamed.

No silent caps. The fan-out is `len(angles)` adversaries, each proposing at most
`MAX_PER_ANGLE` candidates, judged by `PANEL_SIZE` judges needing `MIN_AGREEMENT` yes votes.
Every one of those is an argument to `adversarial_search` and every one lands in the
`Verdict` the caller can read.

## What this cannot do

The judge panel is an LLM panel, so "obviously worthless" is adjudicated by models sharing a
great deal of prior with the proposers. `rejection_rate` measures that the panel *does*
reject, which is weaker than proving it rejects the right things, and there is no mechanism
here that would notice a panel and a proposer being wrong in the same direction. The
arithmetic half has no such hole, which is why it is the half that decides whether a finding
is recorded.
