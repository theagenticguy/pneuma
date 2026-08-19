/-
Formal model of the routing policy (spec R2-R4, .erpaval/specs/001-routing-thrash).

Mirrors `src/sdlc_blackboard/domain/common.py` (ActorKind, PRODUCER_KINDS,
REVIEWER_KINDS) and `src/sdlc_blackboard/domain/events.py` (RoutingClass), plus
the `domain/routing.py` policy this model certifies. The Python implementation
must stay in lockstep; `tests/contract/` pins the mapping table against this file's
`routingPolicy` via the enumeration in the module docstring of domain/routing.py.
-/

namespace Blackboard

/-- `domain/common.py` ActorKind — 18 bounded contexts. -/
inductive ActorKind
  | human | lead | system
  | analyst | architect | implementation | data | documentation | ux
  | quality | security | compliance | release | platform
  | operations | finops | support | visual
  deriving DecidableEq, Repr

/-- `domain/events.py` RoutingClass — Bedrock routing provenance. -/
inductive RoutingClass
  | globalInferenceProfile
  | geoInferenceProfile
  | inRegionRuntime
  | regionalMantle
  deriving DecidableEq, Repr

open ActorKind RoutingClass

/-- PRODUCER_KINDS (domain/common.py). -/
def isProducer : ActorKind → Bool
  | analyst | architect | implementation | data | documentation | ux => true
  | _ => false

/-- REVIEWER_KINDS (domain/common.py). Note architect is BOTH producer and reviewer. -/
def isReviewer : ActorKind → Bool
  | quality | security | compliance | release | platform
  | operations | finops | support | visual | architect => true
  | _ => false

/-- Cost tier of a routing class: the economic ordering the policy steers.
    global (frontier cross-region) ≥ geo ≥ in-region ≥ mantle (cheapest). -/
def costTier : RoutingClass → Nat
  | globalInferenceProfile => 3
  | geoInferenceProfile => 2
  | inRegionRuntime => 1
  | regionalMantle => 0

/-- The routing policy (spec R2/R3): default routing class per actor kind when
    a start_runtime_run carries none.

    Design intent (Cursor swarm-economics glean): planning/design-heavy contexts
    get the expensive tier; mechanical review and system work get cheap tiers.
    - lead/architect/analyst: frontier planning → global profile
    - producing contexts: geo profile (regional frontier)
    - reviewing/governing contexts: in-region runtime (cheap, decorrelated)
    - system/human: mantle (cheapest; human runs are interactive, not model-billed)
-/
def routingPolicy : ActorKind → RoutingClass
  | lead | architect | analyst => globalInferenceProfile
  | implementation | data | documentation | ux => geoInferenceProfile
  | quality | security | compliance | release | platform
  | operations | finops | support | visual => inRegionRuntime
  | human | system => regionalMantle

/-- R3: totality — every actor kind maps to exactly one routing class.
    In Lean this is definitional (routingPolicy is a total function on an
    18-constructor inductive); we state existence + uniqueness explicitly so the
    certificate names the requirement without Mathlib's ∃! notation. -/
theorem routing_total :
    ∀ k : ActorKind, ∃ c : RoutingClass,
      routingPolicy k = c ∧ ∀ c' : RoutingClass, routingPolicy k = c' → c' = c :=
  fun k => ⟨routingPolicy k, rfl, fun _ h => h.symm⟩

/-- The producing-context tier named by R4: the maximum tier the policy assigns
    to any producer. -/
def producerTierBound : Nat := 3

theorem producer_tier_bound_correct :
    ∀ k : ActorKind, isProducer k → costTier (routingPolicy k) ≤ producerTierBound := by
  intro k _; cases k <;> simp [routingPolicy, costTier, producerTierBound]

/-- R4: cost monotonicity — every PURE reviewer (reviewer that is not also a
    producer, i.e. excluding architect) routes at a tier ≤ every producer's tier.
    Architect is excluded because it is a producing context first (PRODUCER_KINDS
    membership wins); the spec's "reviewing or governing context" pre-condition
    is read against the actor's routing role. -/
theorem reviewer_cheaper_than_producers :
    ∀ r p : ActorKind,
      isReviewer r → ¬ isProducer r → isProducer p →
      costTier (routingPolicy r) ≤ costTier (routingPolicy p) := by
  intro r p hr hnp hp
  cases r <;> cases p <;> simp_all [isReviewer, isProducer, routingPolicy, costTier]

/-- R2 corollary: the derived default never selects a tier above the frontier
    ceiling — the policy cannot silently invent a more expensive class than the
    most expensive explicit option. -/
theorem policy_bounded : ∀ k : ActorKind, costTier (routingPolicy k) ≤ 3 := by
  intro k; cases k <;> simp [routingPolicy, costTier]

end Blackboard
