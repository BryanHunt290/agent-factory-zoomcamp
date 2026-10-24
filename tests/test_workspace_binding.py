"""Workers create a workspace under an existing product identity without clobbering files."""

import os

import pytest

from agent_factory.demo import scenario_provider
from agent_factory.domain import Project, in_memory_store
from agent_factory.factory import build_executor, workspace_for_project
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE
from agent_factory.workspace import Workspace, WorkspaceError, workspace_root_for
from test_domain_repositories import P1, P2, ts


def product(store, *, template=CALCULATOR_TEMPLATE.name):
    project = Project(
        id=P1, name="../A label, never a path", created_at=ts(0), template=template,
        milestone_policy=CALCULATOR_TEMPLATE.milestone_policy,
    )
    store.projects.add(project)
    return project


def test_existing_project_workspace_preserves_identity_and_binds_the_run(tmp_path):
    store = in_memory_store()
    project = product(store)
    template, workspace = workspace_for_project(store, project.id, tmp_path)
    assert workspace.root == tmp_path / project.id
    assert workspace.project_id == project.id
    workspace.write_text("owner.txt", "preserve")
    _, reopened = workspace_for_project(store, project.id, tmp_path)
    assert reopened.read_text("owner.txt") == "preserve"
    result = build_executor(template, scenario_provider("repair"), reopened, product_store=store).start(
        template.task, run_id="bound-run",
    )
    assert result.status is RunStatus.COMPLETED
    assert store.projects.list() == [project]
    assert store.runs.get(project.id, result.run_id) is not None


@pytest.mark.parametrize("project_id", ["../other", "C:/outside", "prj-" + "0" * 20 + "\n", None])
def test_worker_workspace_path_rejects_invalid_ids(tmp_path, project_id):
    with pytest.raises(WorkspaceError, match="invalid project id"):
        workspace_root_for(tmp_path, project_id)
    assert list(tmp_path.iterdir()) == []


def test_worker_refuses_another_projects_workspace_and_preserves_its_marker(tmp_path):
    store = in_memory_store()
    product(store)
    existing = CALCULATOR_TEMPLATE.create_workspace(tmp_path / P1, project_id=P2)
    with pytest.raises(WorkspaceError, match="different project"):
        workspace_for_project(store, P1, tmp_path)
    assert existing.project_id == P2


def test_worker_refuses_a_workspace_from_another_template(tmp_path):
    store = in_memory_store()
    product(store)
    Workspace.create(tmp_path / P1, project_id=P1, info={"template": "different"})
    with pytest.raises(WorkspaceError, match="different project template"):
        workspace_for_project(store, P1, tmp_path)


def test_worker_refuses_occupied_non_workspace_directory(tmp_path):
    store = in_memory_store()
    product(store)
    root = tmp_path / P1
    root.mkdir()
    (root / "owner.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="occupied directory"):
        workspace_for_project(store, P1, tmp_path)
    assert [p.name for p in root.iterdir()] == ["owner.txt"]
    assert (root / "owner.txt").read_text(encoding="utf-8") == "preserve"


def test_worker_refuses_redirected_workspace_root(tmp_path):
    store = in_memory_store()
    product(store)
    target = tmp_path / "other-project"
    target.mkdir()
    base = tmp_path / "workspaces"
    base.mkdir()
    link = base / P1
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symlink or junction"):
        workspace_for_project(store, P1, base)
    assert list(target.iterdir()) == []


def test_missing_project_or_template_never_creates_a_workspace(tmp_path):
    store = in_memory_store()
    with pytest.raises(WorkspaceError, match="does not exist"):
        workspace_for_project(store, P1, tmp_path)
    product(store, template="missing")
    with pytest.raises(WorkspaceError, match="not available"):
        workspace_for_project(store, P1, tmp_path)
    assert list(tmp_path.iterdir()) == []
