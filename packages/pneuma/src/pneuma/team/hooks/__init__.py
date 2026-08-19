"""The hook library: every capability the core does not own, as a `TeamHook` each."""

from .artifacts import Artifacts
from .briefing import BRIEFING_ERROR, Briefing
from .hiring import Hiring, Roster, hiring_tools
from .learning import Learning, TrainingRound, compose_feedback, traced_result, train
from .negotiation import Negotiation
from .review import Council, Critic, verdict_token_present
from .trajectory import Trajectory, read_trajectories
from .worklog import DISCOVERY_KINDS, Worklog

__all__ = [
    "BRIEFING_ERROR",
    "Artifacts",
    "Briefing",
    "Council",
    "Critic",
    "DISCOVERY_KINDS",
    "Hiring",
    "Learning",
    "Negotiation",
    "Roster",
    "TrainingRound",
    "Trajectory",
    "Worklog",
    "compose_feedback",
    "hiring_tools",
    "read_trajectories",
    "traced_result",
    "train",
    "verdict_token_present",
]
