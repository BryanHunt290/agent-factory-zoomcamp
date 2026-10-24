"""A run's model usage in the API (openapi.yaml ModelUsage; docs/model-providers.md).

A real executor run (fake provider) records its usage ledger; the recorder stores the checkpoint's summary;
``getRun`` and ``listRuns`` show it, validated against the checked-in contract. Counts only.
"""

from api_support import ContractClient, application
from auth_support import ensure_owner

from agent_factory.api.stores import InMemoryStoreProvider
from agent_factory.demo import scenario_provider
from agent_factory.factory import build_executor
from agent_factory.templates import CALCULATOR_TEMPLATE


def test_a_runs_model_usage_is_shown_by_the_api(tmp_path):
    stores = InMemoryStoreProvider()
    ensure_owner(stores.store)
    workspace = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    executor = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), workspace, product_store=stores.store)
    state = executor.start(CALCULATOR_TEMPLATE.task, run_id="run-1")
    project_id = workspace.project_id
    with ContractClient(application(stores)) as client:
        run = client.get(f"/api/v1/projects/{project_id}/runs/run-1").json()
        listed = client.get(f"/api/v1/projects/{project_id}/runs").json()["items"]
    assert state.status.value == "completed"
    assert run["model_usage"] == {
        "calls": 4, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "estimated_cost_usd": None,
        "by_model": [{"provider": "fake", "model": "fake-deterministic-1", "calls": 4, "input_tokens": 0,
                      "output_tokens": 0, "cached_tokens": 0}],
    }  # fmt: skip
    assert listed[0]["model_usage"] == run["model_usage"]


def test_a_run_without_model_calls_shows_zero_usage(tmp_path):
    from api_support import add_project, add_run

    stores = InMemoryStoreProvider()
    ensure_owner(stores.store)
    project = add_project(stores.store)
    add_run(stores.store, project)
    with ContractClient(application(stores)) as client:
        usage = client.get(f"/api/v1/projects/{project.id}/runs/run-1").json()["model_usage"]
    assert usage == {"calls": 0, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                     "estimated_cost_usd": None, "by_model": []}  # fmt: skip
