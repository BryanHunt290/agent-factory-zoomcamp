"""Shared fixtures for the worker coordination tests. Controlled fixtures only: nothing leaves the machine.

Used by tests/test_postgres_claims.py, tests/test_worker_processes.py,
tests/test_worker_restart_live.py, and the child process tests/worker_child.py.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_factory.agents.base import Agent, AgentContext, AgentResult
from agent_factory.demo import scenario_provider
from agent_factory.domain import Project
from agent_factory.domain.ids import new_project_id
from agent_factory.factory import build_executor, workspace_for_project
from agent_factory.templates import TEMPLATES
from agent_factory.util import timestamp, utc_now

CALCULATOR = TEMPLATES["calculator"]


def new_calculator_run(store, base: Path, run_id: str = "run-1", scenario: str = "repair"):
    """A project in the database, its workspace under ``base``, and one created (not yet run) run."""
    project = Project(
        id=new_project_id(), name="worker test", created_at=timestamp(utc_now),
        milestone_policy=CALCULATOR.milestone_policy, template=CALCULATOR.name,
    )  # fmt: skip
    store.projects.add(project)
    _, workspace = workspace_for_project(store, project.id, base, git=False)
    ex = build_executor(CALCULATOR, scenario_provider(scenario), workspace, product_store=store)
    ex.create(CALCULATOR.task, run_id=run_id, metadata={"template": CALCULATOR.name, "provider": "fake", "scenario": scenario})
    return workspace


def age_claim(conn, project_id: str, run_id: str, seconds: float = 3600) -> None:
    """As if the owner had stalled for ``seconds``: its claim, last heartbeat, and lease move into the past."""
    conn.execute(
        "UPDATE run_claims SET claimed_at = claimed_at - make_interval(secs => %(s)s), "
        "last_heartbeat_at = last_heartbeat_at - make_interval(secs => %(s)s), "
        "lease_expires_at = lease_expires_at - make_interval(secs => %(s)s) "
        "WHERE project_id = %(p)s AND run_id = %(r)s AND worker_id IS NOT NULL",
        {"s": seconds, "p": project_id, "r": run_id},
    )


def lease_expired(conn, project_id: str, run_id: str) -> bool:
    row = conn.execute(
        "SELECT lease_expires_at <= now() FROM run_claims WHERE project_id = %s AND run_id = %s", (project_id, run_id)
    ).fetchone()
    return bool(row and row[0])


def wait_until(condition: Callable[[], bool], timeout: float = 30.0, poll: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(poll)


def race(callables: list[Callable[[], Any]], timeout: float = 60.0) -> list[Any]:
    """Run the callables in threads released together by a barrier; return their results in order.

    An error in any thread is re-raised here, so a failure is never lost.
    """
    barrier = threading.Barrier(len(callables))
    results: list[Any] = [None] * len(callables)
    errors: list[BaseException] = []

    def runner(index: int, fn: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=timeout)
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, fn), daemon=True) for i, fn in enumerate(callables)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    if errors:
        raise errors[0]
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("a racing thread did not finish")
    return results


class BeforeAgent(Agent):
    """Runs ``before()`` (e.g. a takeover, or a wait for the parent) at the start of the node, then the real agent."""

    def __init__(self, inner: Agent, before: Callable[[], None]) -> None:
        self.inner = inner
        self.before = before
        self.name = inner.name  # type: ignore[misc]
        self.writes = inner.writes  # type: ignore[misc]

    def run(self, ctx: AgentContext) -> AgentResult:
        self.before()
        return self.inner.run(ctx)


def hold_until_released(oracle: Path, timeout: float = 60.0) -> Callable[[], None]:
    """A ``before`` that writes ``<oracle>/holding`` and waits for ``<oracle>/release``."""

    def hold() -> None:
        oracle.mkdir(parents=True, exist_ok=True)
        (oracle / "holding").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while not (oracle / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    return hold


def approved_notice_run(store, claims, root: Path, oracle: Path):
    """The action fixture's run (one approved class C notice): started, approved, and queued for a worker."""
    import action_fixtures as fx

    from agent_factory.state import TaskSpec

    workspace = fx.workspace(root)
    ex = fx.executor(workspace, "notice", oracle, recorder=fx.recorder_for(store, workspace))
    ex.start(TaskSpec(title="fixture", goal="one approved action"), run_id="run-1")
    fx.approve_pending(ex)
    claims.enqueue(workspace.project_id, "run-1")
    return workspace


class NoticeSource:
    """Claimed fixture runs: the store fenced by the claim, and the fixture executor bound to the claim.

    No process is ever started, so it also works in the live suites, whose guard allows the docker CLI only.
    """

    def __init__(self, config, workspace_root: Path, oracle: Path, adjust: Callable[[Any], None] | None = None) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root)
        self.oracle = Path(oracle)
        self.adjust = adjust

    def open(self, claim):
        from agent_factory.persistence.claims import open_fenced_store

        return _NoticeRun(open_fenced_store(self.config, claim.fence), self)


class _NoticeRun:
    def __init__(self, store, source: NoticeSource) -> None:
        self.store = store
        self.source = source

    def prove(self) -> None:
        self.store.unit_of_work.prove_ownership()

    def executor(self, ownership):
        import action_fixtures as fx

        workspace = fx.workspace(self.source.workspace_root)
        ex = fx.executor(workspace, "notice", self.source.oracle, recorder=fx.recorder_for(self.store, workspace))
        ex.ownership = ownership
        if self.source.adjust is not None:
            self.source.adjust(ex)
        return ex

    def close(self) -> None:
        self.store.close()


class WrappedSource:
    """A run source whose executors are adjusted by ``adjust`` before they run (agents replaced, tools held)."""

    def __init__(self, inner, adjust: Callable[[Any], None]) -> None:
        self.inner = inner
        self.adjust = adjust

    def open(self, claim):
        return _WrappedRun(self.inner.open(claim), self.adjust)


class _WrappedRun:
    def __init__(self, run, adjust) -> None:
        self.run = run
        self.adjust = adjust

    def prove(self) -> None:
        self.run.prove()

    def executor(self, ownership):
        ex = self.run.executor(ownership)
        self.adjust(ex)
        return ex

    def close(self) -> None:
        self.run.close()


__all__ = [
    "CALCULATOR",
    "BeforeAgent",
    "NoticeSource",
    "WrappedSource",
    "approved_notice_run",
    "age_claim",
    "hold_until_released",
    "lease_expired",
    "new_calculator_run",
    "race",
    "wait_until",
]
