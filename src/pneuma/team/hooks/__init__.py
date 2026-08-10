"""The hook library: every capability the core does not own, as a `TeamHook` each."""

from .briefing import BRIEFING_ERROR, Briefing
from .hiring import Hiring, Roster, hiring_tools
from .learning import Learning, TrainingRound, compose_feedback, traced_result, train
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
    "Learning",
    "Negotiation",
    "Roster",
    "TrainingRound",
    "Worklog",
    "compose_feedback",
    "hiring_tools",
    "traced_result",
    "train",
]
