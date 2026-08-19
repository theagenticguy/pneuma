"""Default runtime-run routing policy (spec 001-routing-thrash R2/R3/R4).

Pure, total mapping from an actor kind to the Bedrock routing class a runtime run
defaults to when ``start_runtime_run`` carries no explicit ``routing_class``. This is
the Cursor swarm-economics cost lever: planning/design-heavy contexts get the
expensive frontier tier; mechanical review and system work get cheap tiers.

CERTIFIED BY THE LEAN MODEL. This module is the Python half of a two-sided proof: it
mirrors ``formal/Blackboard/Routing.lean`` ``routingPolicy`` EXACTLY, constructor for
constructor. The two MUST move in lockstep — a change here without the matching change
in Routing.lean (and ``lake build``) breaks the contract, and ``tests/contract/
test_routing_policy.py`` pins the 18-row table against the transcription of that file.
No I/O, no randomness (``domain/common.py`` §0.2 purity rule).
"""

from collections.abc import Mapping
from types import MappingProxyType

from sdlc_blackboard.domain.common import ActorKind
from sdlc_blackboard.domain.events import RoutingClass

#: The routing policy table (Routing.lean ``routingPolicy``): the default routing class
#: per actor kind. Transcribed constructor-for-constructor from the Lean model; the
#: contract test asserts this covers every ``ActorKind`` (R3 totality) and matches the
#: independently-hardcoded expected table.
#:
#: - lead/architect/analyst  -> global profile   (frontier planning, tier 3)
#: - implementation/data/documentation/ux -> geo profile (regional frontier, tier 2)
#: - quality/security/compliance/release/platform/operations/finops/support/visual
#:                           -> in-region runtime (cheap, decorrelated review, tier 1)
#: - human/system            -> regional mantle   (cheapest; human runs are interactive)
ROUTING_POLICY: Mapping[ActorKind, RoutingClass] = MappingProxyType(
    {
        ActorKind.LEAD: RoutingClass.GLOBAL_INFERENCE_PROFILE,
        ActorKind.ARCHITECT: RoutingClass.GLOBAL_INFERENCE_PROFILE,
        ActorKind.ANALYST: RoutingClass.GLOBAL_INFERENCE_PROFILE,
        ActorKind.IMPLEMENTATION: RoutingClass.GEO_INFERENCE_PROFILE,
        ActorKind.DATA: RoutingClass.GEO_INFERENCE_PROFILE,
        ActorKind.DOCUMENTATION: RoutingClass.GEO_INFERENCE_PROFILE,
        ActorKind.UX: RoutingClass.GEO_INFERENCE_PROFILE,
        ActorKind.QUALITY: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.SECURITY: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.COMPLIANCE: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.RELEASE: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.PLATFORM: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.OPERATIONS: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.FINOPS: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.SUPPORT: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.VISUAL: RoutingClass.IN_REGION_RUNTIME,
        ActorKind.HUMAN: RoutingClass.REGIONAL_MANTLE,
        ActorKind.SYSTEM: RoutingClass.REGIONAL_MANTLE,
    }
)


def default_routing_class(kind: ActorKind) -> RoutingClass:
    """Return the default routing class for ``kind`` (Routing.lean ``routingPolicy``).

    Total over ``ActorKind`` (R3): every one of the eighteen kinds is a key of
    ``ROUTING_POLICY``, so this never raises for a valid enum member.
    """
    return ROUTING_POLICY[kind]
