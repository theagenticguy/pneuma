"""The hooks-first team: a minimal core, member shapes, and (Wave 2) a hook library.

`core.Team` owns spawn → hooks → lead → answer loop → teardown and nothing else; every
capability the old monolith carried as a phase or a flag — briefing, negotiation, worklog,
hiring — returns as a `TeamHook` under `hooks/`. `members` carries the shapes a cast may be
made of. The old module survives as `pneuma._team_legacy` until Wave 2 finishes porting.
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
