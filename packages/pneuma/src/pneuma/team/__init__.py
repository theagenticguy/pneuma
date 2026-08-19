"""The hooks-first team: a minimal core, member shapes, and a hook library.

`core.Team` owns spawn → hooks → lead → answer loop → teardown and nothing else; every
capability the old monolith carried as a phase or a flag — briefing, negotiation, worklog,
hiring, review — is a `TeamHook` under `hooks/`. `members` carries the shapes a cast may
be made of. `squad` nests a whole team as one member; `expedition` loops a team round by
round under code-owned budgets — both compose teams from outside rather than extending the
core. `artifacts` is the versioned document plane the `Artifacts` hook writes through:
members propose on their own branches, the lead alone lands them, and `split_brain` probes
whether two branches settled one design question differently.
"""

from .artifacts import ArtifactError, ArtifactStore, Conflict, Revision, SplitBrain, split_brain
from .core import Accept, Revise, Team, TeamHook, TeamRun, Workspace
from .expedition import Expedition, ExpeditionResult, Round
from .members import DynamicAgent, Member, Recruit
from .squad import Squad

__all__ = [
    "Accept",
    "ArtifactError",
    "ArtifactStore",
    "Conflict",
    "DynamicAgent",
    "Expedition",
    "ExpeditionResult",
    "Member",
    "Recruit",
    "Revise",
    "Revision",
    "Round",
    "SplitBrain",
    "Squad",
    "Team",
    "TeamHook",
    "TeamRun",
    "Workspace",
    "split_brain",
]
