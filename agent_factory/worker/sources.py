"""Where a worker finds its claimed runs: the project workspaces on this host, and PostgreSQL.

A run's checkpoint, execution log, and project files are in its workspace,
on the local disk of the host that created it (docs/workers.md,
"checkpoints"). A worker therefore works only on runs whose workspace it
finds under its workspace base, and never creates one for a run that exists:
that would fork the run. A claimed run whose workspace is missing is offered
again later, so a worker started with the wrong ``--workspace-base`` delays
runs but never takes them away from the workers that can run them.
"""

from __future__ import annotations

from pathlib import Path

from ..demo import SCENARIOS, scenario_provider
from ..domain.claims import Claim
from ..domain.repositories import ProductStore
from ..engine.checkpoint import CheckpointStore
from ..engine.executor import Executor
from ..engine.ownership import RunOwnership
from ..factory import build_executor
from ..persistence.config import DatabaseConfig
from ..templates import TEMPLATES
from ..workspace import MARKER, META_DIR, Workspace, WorkspaceError, workspace_root_for
from .loop import RunUnavailable


def executor_for_claimed_run(
    workspace_base: Path | str, project_id: str, run_id: str, *, store: ProductStore, ownership: RunOwnership
) -> Executor:
    """Rebuild the executor of an existing run from its workspace on this host. Creates nothing."""
    root = workspace_root_for(workspace_base, project_id)
    if not (root / META_DIR / MARKER).is_file():
        raise RunUnavailable(f"project {project_id} has no workspace on this host", reason="workspace_unavailable", requeue=True)
    try:
        workspace = Workspace.open(root)
    except WorkspaceError as exc:
        raise RunUnavailable(f"the workspace of project {project_id} cannot be opened: {exc}", reason="unsupported_run", requeue=False) from None
    if workspace.project_id != project_id:
        raise RunUnavailable(f"the workspace at the path of project {project_id} belongs to another project", reason="unsupported_run", requeue=False)
    template = TEMPLATES.get(workspace.info.get("template") or "")
    if template is None:
        raise RunUnavailable(f"project {project_id} uses a template this worker does not have", reason="unsupported_run", requeue=False)
    checkpoints = CheckpointStore(workspace.runs_dir)
    if not checkpoints.exists(run_id):
        raise RunUnavailable(f"run {run_id} has no checkpoint on this host", reason="workspace_unavailable", requeue=True)
    metadata = checkpoints.load(run_id).metadata
    if metadata.get("provider") != "fake" or metadata.get("scenario") not in SCENARIOS:
        raise RunUnavailable("this worker runs only runs that use the deterministic fake provider", reason="unsupported_run", requeue=False)
    return build_executor(template, scenario_provider(metadata["scenario"]), workspace, product_store=store, ownership=ownership)


class WorkspaceRun:
    """A claimed run: its fenced product store, and its executor from the workspace."""

    def __init__(self, store: ProductStore, workspace_base: Path, claim: Claim) -> None:
        self.store = store
        self.workspace_base = workspace_base
        self.claim = claim

    def prove(self) -> None:
        self.store.unit_of_work.prove_ownership()  # type: ignore[attr-defined]

    def executor(self, ownership: RunOwnership) -> Executor:
        fence = self.claim.fence
        return executor_for_claimed_run(self.workspace_base, fence.project_id, fence.run_id, store=self.store, ownership=ownership)

    def close(self) -> None:
        self.store.close()


class WorkspaceRunSource:
    """Claimed runs in PostgreSQL whose workspaces are under ``workspace_base`` on this host."""

    def __init__(self, config: DatabaseConfig, workspace_base: Path | str) -> None:
        self.config = config
        self.workspace_base = Path(workspace_base)

    def open(self, claim: Claim) -> WorkspaceRun:
        from ..persistence.claims import open_fenced_store

        return WorkspaceRun(open_fenced_store(self.config, claim.fence), self.workspace_base, claim)


__all__ = ["WorkspaceRun", "WorkspaceRunSource", "executor_for_claimed_run"]
