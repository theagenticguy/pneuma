"""The hook library: every capability the core does not own, as a `TeamHook` each."""

from .briefing import BRIEFING_ERROR, Briefing
from .hiring import Hiring, Roster, hiring_tools
from .negotiation import Negotiation
from .review import Council, Critic
from .worklog import DISCOVERY_KINDS, Worklog

__all__ = [
    "BRIEFING_ERROR",
    "Briefing",
    "Council",
    "Critic",
    "DISCOVERY_KINDS",
    "Hiring",
    "Negotiation",
    "Roster",
    "Worklog",
    "hiring_tools",
]
