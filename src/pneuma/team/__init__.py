"""The hooks-first team: a minimal core, member shapes, and a hook library.

`core.Team` owns spawn → hooks → lead → answer loop → teardown and nothing else; every
capability the old monolith carried as a phase or a flag — briefing, negotiation, worklog,
hiring, review — is a `TeamHook` under `hooks/`. `members` carries the shapes a cast may
be made of.
"""

from .core import Accept, Revise, Team, TeamHook, TeamRun, Workspace
from .members import DynamicAgent, Member, Recruit

__all__ = [
    "Accept",
    "DynamicAgent",
    "Member",
    "Recruit",
    "Revise",
    "Team",
    "TeamHook",
    "TeamRun",
    "Workspace",
]
