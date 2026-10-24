"""A child process that drives one fixture run and dies for real at a chosen boundary.

    python tests/crash_child.py <workspace> <oracle> <kind> <phase> <fault>

``phase`` is ``start`` or ``resume``; ``fault`` names the boundary where the
process ends with ``os._exit(137)``, which skips every ``finally`` block and
flush, like ``kill -9``. The OS releases the run lock. ``hold`` instead
resumes with a tool that waits for ``<oracle>/release`` and writes
``<oracle>/holding`` while it holds the run, so the parent can try to take it.

With ``AGENT_FACTORY_TEST_DATABASE_URL_FILE`` and ``CRASH_CHILD_SCHEMA`` set,
the run uses the PostgreSQL ledger in that schema instead of the checkpoint
ledger. Trusted test code only: never a hostile fixture.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import action_fixtures as fx  # noqa: E402

from agent_factory.domain.executions import Dispatch, ExecutionLedger  # noqa: E402
from agent_factory.engine.checkpoint import CheckpointStore  # noqa: E402
from agent_factory.engine.log import ExecutionLog  # noqa: E402

DIE = 137


def die() -> None:
    os._exit(DIE)


def install(fault: str, tool) -> None:
    """Make the process end at ``fault``. Patches only this child process."""
    state = {"effect_done": False}
    real_begin, real_finish = ExecutionLedger.begin, ExecutionLedger.finish
    real_save, real_append = CheckpointStore.save, ExecutionLog.append
    real_run = type(tool).run

    if fault == "before_dispatch_record":
        ExecutionLedger.begin = lambda self, *a, **k: die()  # type: ignore[method-assign]
    elif fault == "after_dispatch_record":

        def begin(self, *a, **k):
            result = real_begin(self, *a, **k)
            if isinstance(result, Dispatch):
                die()
            return result

        ExecutionLedger.begin = begin  # type: ignore[method-assign]
    elif fault == "before_tool":
        type(tool).run = lambda self, *a, **k: die()  # type: ignore[method-assign]
    elif fault == "after_tool_before_outcome":

        def run(self, *a, **k):
            real_run(self, *a, **k)
            die()

        type(tool).run = run  # type: ignore[method-assign]
    elif fault == "after_log_outcome":

        def append(self, event, **fields):
            entry = real_append(self, event, **fields)
            if event == "action_succeeded":
                die()
            return entry

        ExecutionLog.append = append  # type: ignore[method-assign]
    elif fault == "after_outcome":

        def finish(self, *a, **k):
            result = real_finish(self, *a, **k)
            die()
            return result

        ExecutionLedger.finish = finish  # type: ignore[method-assign]
    elif fault in ("before_node_checkpoint", "after_node_checkpoint"):

        def run(self, *a, **k):
            state["effect_done"] = True
            return real_run(self, *a, **k)

        def save(self, run_state):
            if state["effect_done"] and fault == "before_node_checkpoint":
                die()
            path = real_save(self, run_state)
            if state["effect_done"] and fault == "after_node_checkpoint":
                die()
            return path

        type(tool).run = run  # type: ignore[method-assign]
        CheckpointStore.save = save  # type: ignore[method-assign]
    elif fault == "hold":

        def run(self, *a, **k):
            oracle = Path(sys.argv[2])
            (oracle / "holding").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 60
            while not (oracle / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return real_run(self, *a, **k)

        type(tool).run = run  # type: ignore[method-assign]
    elif fault != "none":
        raise SystemExit(f"unknown fault {fault!r}")


def main() -> int:
    workspace_dir, oracle, kind, phase, fault = sys.argv[1:6]
    ws = fx.workspace(Path(workspace_dir))
    store = None
    schema = os.environ.get("CRASH_CHILD_SCHEMA")
    if schema:
        from agent_factory.persistence.config import TEST_PREFIX, DatabaseConfig
        from agent_factory.persistence.postgres import open_postgres_store

        store = open_postgres_store(DatabaseConfig.from_environment(prefix=TEST_PREFIX).with_schema(schema))
    recorder = fx.recorder_for(store, ws) if store is not None else None
    ex = fx.executor(ws, kind, Path(oracle), recorder=recorder)
    install(fault, ex.tools.get(fx.tool_for(kind, Path(oracle)).name))
    if phase == "start":
        state = ex.start(_task(), run_id="run-1")
    else:
        state = ex.resume("run-1")
    print(state.status.value)
    return 0


def _task():
    from agent_factory.state import TaskSpec

    return TaskSpec(title="fixture", goal="exercise one approved action")


if __name__ == "__main__":
    raise SystemExit(main())
