"""Routing-policy contract test — the Python half of the two-sided proof (spec R3/R4).

``formal/Blackboard/Routing.lean`` proves totality (routing_total) and cost monotonicity
(reviewer_cheaper_than_producers) over the Lean ``routingPolicy``; ``lake build`` is the
other half of the proof. This test pins the Python ``default_routing_class`` /
``ROUTING_POLICY`` against a table TRANSCRIBED BY HAND from that Lean file, so the two
cannot drift: change one without the other and a test goes red here or a proof fails
under ``mise run formal``.

Cheap, import-only, no Docker.
"""

from __future__ import annotations

import pytest

from sdlc_blackboard.domain.common import PRODUCER_KINDS, REVIEWER_KINDS, ActorKind
from sdlc_blackboard.domain.events import RoutingClass
from sdlc_blackboard.domain.routing import ROUTING_POLICY, default_routing_class

# --------------------------------------------------------------------------- #
# The 18-row expected table, transcribed constructor-for-constructor from       #
# formal/Blackboard/Routing.lean `routingPolicy`. DO NOT derive this from       #
# ROUTING_POLICY — it is an independent copy of the Lean source of truth, so a  #
# drift in the implementation table trips against the Lean-side transcription.  #
# --------------------------------------------------------------------------- #
_EXPECTED: dict[ActorKind, RoutingClass] = {
    # lead | architect | analyst => globalInferenceProfile
    ActorKind.LEAD: RoutingClass.GLOBAL_INFERENCE_PROFILE,
    ActorKind.ARCHITECT: RoutingClass.GLOBAL_INFERENCE_PROFILE,
    ActorKind.ANALYST: RoutingClass.GLOBAL_INFERENCE_PROFILE,
    # implementation | data | documentation | ux => geoInferenceProfile
    ActorKind.IMPLEMENTATION: RoutingClass.GEO_INFERENCE_PROFILE,
    ActorKind.DATA: RoutingClass.GEO_INFERENCE_PROFILE,
    ActorKind.DOCUMENTATION: RoutingClass.GEO_INFERENCE_PROFILE,
    ActorKind.UX: RoutingClass.GEO_INFERENCE_PROFILE,
    # quality | security | compliance | release | platform
    #   | operations | finops | support | visual => inRegionRuntime
    ActorKind.QUALITY: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.SECURITY: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.COMPLIANCE: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.RELEASE: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.PLATFORM: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.OPERATIONS: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.FINOPS: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.SUPPORT: RoutingClass.IN_REGION_RUNTIME,
    ActorKind.VISUAL: RoutingClass.IN_REGION_RUNTIME,
    # human | system => regionalMantle
    ActorKind.HUMAN: RoutingClass.REGIONAL_MANTLE,
    ActorKind.SYSTEM: RoutingClass.REGIONAL_MANTLE,
}

#: costTier from Routing.lean: global 3 >= geo 2 >= in-region 1 >= mantle 0.
_COST_TIER: dict[RoutingClass, int] = {
    RoutingClass.GLOBAL_INFERENCE_PROFILE: 3,
    RoutingClass.GEO_INFERENCE_PROFILE: 2,
    RoutingClass.IN_REGION_RUNTIME: 1,
    RoutingClass.REGIONAL_MANTLE: 0,
}


@pytest.mark.parametrize("kind", list(ActorKind))
def test_routing_policy_matches_lean_model(kind: ActorKind) -> None:
    """Every actor kind maps to the exact routing class the Lean model assigns."""
    assert default_routing_class(kind) == _EXPECTED[kind]


def test_routing_policy_is_total_over_actor_kind() -> None:
    """R3 totality: the policy is defined for every one of the eighteen actor kinds."""
    assert set(ROUTING_POLICY.keys()) == set(ActorKind)
    assert len(ActorKind) == 18
    # default_routing_class never raises for a valid enum member.
    for kind in ActorKind:
        assert isinstance(default_routing_class(kind), RoutingClass)


def test_expected_table_covers_every_actor_kind() -> None:
    """Guard the transcription itself: the hand-copied table is total, so a newly added
    ActorKind forces an explicit decision here (and in Routing.lean) rather than a gap."""
    assert set(_EXPECTED.keys()) == set(ActorKind)


def test_pure_reviewers_route_no_more_expensive_than_producers() -> None:
    """R4 cost monotonicity, replicated in Python (Lean: reviewer_cheaper_than_producers).

    Every PURE reviewer (reviewer kind that is not also a producer — i.e. excluding
    architect, which is a producing context first) routes at a cost tier <= every
    producer's tier."""
    pure_reviewers = REVIEWER_KINDS - PRODUCER_KINDS
    assert ActorKind.ARCHITECT not in pure_reviewers  # architect is both; excluded
    for reviewer in pure_reviewers:
        r_tier = _COST_TIER[default_routing_class(reviewer)]
        for producer in PRODUCER_KINDS:
            p_tier = _COST_TIER[default_routing_class(producer)]
            assert r_tier <= p_tier, f"{reviewer} tier {r_tier} > {producer} tier {p_tier}"
