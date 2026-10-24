"""Model usage in PostgreSQL (migration 0008; docs/model-providers.md).

A real worker advances a run on a session fenced by its claim; the checkpoint's usage summary is stored in
the same sync transaction. Re-syncing, or a new connection after a restart, never counts a call twice.
"""

import pytest

pytestmark = pytest.mark.postgres


def test_usage_is_stored_with_the_checkpoint_and_never_counted_twice(tmp_path, pg_store, pg_open):
    from agent_factory.demo import scenario_provider
    from agent_factory.factory import build_executor
    from agent_factory.templates import CALCULATOR_TEMPLATE

    workspace = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    executor = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), workspace, product_store=pg_store)
    state = executor.start(CALCULATOR_TEMPLATE.task, run_id="run-1")
    usage = pg_store.model_usage.get(workspace.project_id, "run-1")
    assert state.status.value == "completed" and usage is not None
    assert (usage.calls, usage.failed_calls, [m.provider for m in usage.by_model]) == (4, 0, ["fake"])
    assert usage.calls == state.metadata["model_usage"]["calls"] == executor.usage_ledger("run-1").totals().calls
    restarted = pg_open()  # a new connection, as after a restart
    again_executor = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), workspace, product_store=restarted)
    for _ in range(3):
        again_executor.recorder.sync(state)
    again = restarted.model_usage.get(workspace.project_id, "run-1")
    assert again.same_counts(usage) and again.calls == 4

def test_the_database_refuses_inconsistent_usage(pg_schema, pg_store):
    import psycopg

    from agent_factory.persistence.postgres import connect
    from dashboard_support import seed_project

    project = seed_project(pg_store, "Usage")
    conn = connect(pg_schema)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO run_model_usage VALUES (%s, 'run-1', 1, 2, 0, 0, 0, NULL, '[]'::jsonb, now())", (project.id,)
            )  # more failed calls than calls
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO run_model_usage VALUES (%s, 'run-9', 1, 0, 0, 0, 0, NULL, '[]'::jsonb, now())", (project.id,))
    finally:
        conn.close()
