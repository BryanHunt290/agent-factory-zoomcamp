"""Project templates: how a concrete project plugs into Agent Factory.

A template bundles everything project-specific; the orchestration core
(graph executor, state, safety, logging) stays generic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..agents import Agent, default_agents
from ..domain.milestones import MilestonePolicy, standard_milestone_policy
from ..graph import GraphDefinition, build_software_dev_graph
from ..safety import PermissionPolicy
from ..sandbox.types import CodeTrust, ResourceLimits
from ..state import TaskSpec
from ..tools import UNITTEST_COMMAND
from ..workspace import Workspace


@dataclass(frozen=True)
class ProjectTemplate:
    name: str
    description: str
    task: TaskSpec
    #: Files written into ``project/`` when the workspace is created.
    scaffold: Mapping[str, str] = field(default_factory=dict)
    #: Paths agents may read but never write or delete (e.g. acceptance tests).
    protected_paths: tuple[str, ...] = ()
    #: Fixed, backend-neutral test command (``python ...``). Never chosen by a model.
    test_command: tuple[str, ...] = UNITTEST_COMMAND
    #: Resource limits for each test execution in the sandbox.
    sandbox_limits: ResourceLimits = field(default_factory=ResourceLimits)
    graph_factory: Callable[[], GraphDefinition] = build_software_dev_graph
    agent_factory: Callable[[], Mapping[str, Agent]] = default_agents
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    #: What "meaningful progress" means for this kind of project: its milestone
    #: types and the workflow outcomes that reach them. Projects define their own
    #: milestones here; Agent Factory core needs no changes.
    milestone_policy: MilestonePolicy = field(default_factory=standard_milestone_policy)

    def create_workspace(self, root: Path | str, *, git: bool = False, project_id: str | None = None) -> Workspace:
        """Create a workspace from this template; ``project_id`` binds it to an existing project."""
        return Workspace.create(
            root,
            scaffold=self.scaffold,
            protected=self.protected_paths,
            git=git,
            info={"template": self.name},
            # The template scaffold is maintainer-authored. The executor downgrades
            # this to UNTRUSTED as soon as an untrusted provider is involved.
            trust=CodeTrust.TRUSTED_FIXTURE,
            project_id=project_id,
        )
