"""Wiring: template + provider + workspace -> Executor."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .capabilities import CapabilityRegistry, ToolPack
from .domain.recorder import ProgressRecorder
from .domain.repositories import ProductStore
from .engine import Executor
from .engine.ownership import RunOwnership
from .providers import ModelProvider
from .providers.budget import ModelBudget
from .sandbox.manager import SandboxManager
from .templates import TEMPLATES, ProjectTemplate
from .tools.packs import local_packs
from .util import Clock, utc_now
from .workspace import MARKER, META_DIR, Workspace, WorkspaceError, workspace_root_for


def workspace_for_project(
    product_store: ProductStore, project_id: str, base_dir: Path | str, *, git: bool = False
) -> tuple[ProjectTemplate, Workspace]:
    """The workspace of a project that already exists in the product store, created on first use.

    For projects created through the API. The stored project ID becomes the
    workspace's identity, so the runs recorded from this workspace attach to
    the same project: the database and the workspace can never disagree about
    which project this is. The directory is ``<base_dir>/<project_id>``; the
    project's name is a label and never becomes part of a path.
    """
    project = product_store.projects.get(project_id)
    if project is None:
        raise WorkspaceError(f"project {project_id!r} does not exist in the product store")
    template = TEMPLATES.get(project.template or "")
    if template is None:
        raise WorkspaceError(f"project {project_id} uses template {project.template!r}, which is not available")
    root = workspace_root_for(base_dir, project.id)
    if (root / META_DIR / MARKER).is_file():
        workspace = Workspace.open(root)
        if workspace.project_id != project.id:
            raise WorkspaceError(f"the workspace at {root} belongs to a different project")
        if workspace.info.get("template") != template.name:
            raise WorkspaceError(f"the workspace at {root} uses a different project template")
        return template, workspace
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise WorkspaceError(f"refusing to create a workspace in occupied directory {root}")
    return template, template.create_workspace(root, git=git, project_id=project.id)


def build_executor(
    template: ProjectTemplate,
    provider: ModelProvider,
    workspace: Workspace,
    *,
    clock: Clock = utc_now,
    sandbox: SandboxManager | None = None,
    product_store: ProductStore | None = None,
    ownership: RunOwnership | None = None,
    model_budget: ModelBudget | None = None,
    extra_packs: Sequence[ToolPack] = (),
) -> Executor:
    """Build an executor for ``template``.

    ``sandbox`` defaults to ``SandboxManager.default()``. Trusted fixtures run
    locally; untrusted code runs only in the approved Docker sandbox, or the
    run blocks.

    ``product_store``, if given, records the project, its runs, milestones,
    and approvals through the repository interfaces (see docs/product-domain.md).
    Without it the executor behaves exactly as in v0.2.

    ``ownership`` is for a worker holding the run's claim (docs/workers.md);
    ``product_store`` should then be the store fenced by the same claim.

    Tools come from tool packs (docs/capabilities.md): the local Core and Git packs always, plus
    any ``extra_packs`` the caller enables explicitly. Nothing is enabled by the environment, and a
    pack that is not enabled contributes no tool.
    """
    graph = template.graph_factory()
    recorder = None
    if product_store is not None:
        recorder = ProgressRecorder.for_workspace(
            product_store,
            workspace,
            name=template.task.title,
            policy=template.milestone_policy,
            template=template.name,
            description=template.description,
            node_agents={node.name: node.agent for node in graph.nodes if node.agent},
            clock=clock,
        )
    capabilities = CapabilityRegistry(
        (*local_packs(template.test_command, template.sandbox_limits, (sandbox or SandboxManager.default()).run),
         *extra_packs),
        clock=clock,
    )  # fmt: skip
    return Executor(
        graph=graph,
        agents=template.agent_factory(),
        provider=provider,
        workspace=workspace,
        tools=capabilities.tool_registry(),
        capabilities=capabilities,
        policy=template.policy,
        clock=clock,
        recorder=recorder,
        ownership=ownership,
        model_budget=model_budget,
    )
