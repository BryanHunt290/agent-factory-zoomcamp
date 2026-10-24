"""A worker in a child process, for the process-level coordination tests (tests/test_worker_processes.py).

    python tests/worker_child.py die-in-claim   <oracle>
    python tests/worker_child.py claim-then-die <oracle> <lease seconds>
    python tests/worker_child.py work <calculator|notice> <workspace> <oracle> [--hold] [--zombie]
                                      [--lease S] [--heartbeat S] [--idle-exit S]

* ``die-in-claim`` runs the claim statement inside an open transaction,
  writes ``<oracle>/claiming``, waits for ``<oracle>/die``, and ends with
  ``os._exit(137)`` without committing: a worker dying mid-claim.
* ``claim-then-die`` commits a claim, writes ``<oracle>/claimed`` (its worker
  ID and token as JSON), and ends with ``os._exit(137)``.
* ``work`` runs one worker (one claim at most) on the queued run. ``--hold``
  stops the run inside its first node (the calculator's planner, or the
  notice tool after its dispatch was recorded) until ``<oracle>/release``
  exists, writing ``<oracle>/holding`` first. ``--zombie`` makes the worker
  stall without knowing it: it never renews its lease and always believes it
  holds it. Events are printed as JSON lines; Ctrl+Break (Windows) or SIGTERM
  stops the worker gracefully, as in ``worker run``.

The database is the test schema named by ``WORKER_CHILD_SCHEMA`` in the test
database (``AGENT_FACTORY_TEST_DATABASE_URL_FILE``). Trusted test code only:
never a hostile fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import action_fixtures as fx  # noqa: E402
from worker_support import BeforeAgent, NoticeSource, WrappedSource, hold_until_released  # noqa: E402

from agent_factory.domain.claims import LeasePolicy, new_worker_id  # noqa: E402
from agent_factory.persistence.claims import CLAIM_NEXT, FINISHED_STATUSES, open_claim_repository  # noqa: E402
from agent_factory.persistence.config import TEST_PREFIX, DatabaseConfig  # noqa: E402
from agent_factory.worker import LeaseKeeper, Worker, WorkerEvents, WorkerIdentity, WorkerSettings  # noqa: E402
from agent_factory.worker.sources import WorkspaceRunSource  # noqa: E402

DIE = 137


def config() -> DatabaseConfig:
    return DatabaseConfig.from_environment(prefix=TEST_PREFIX).with_schema(os.environ["WORKER_CHILD_SCHEMA"])


def die_in_claim(oracle: Path) -> None:
    claims = open_claim_repository(config())
    db = claims.db
    with db.transaction():
        rows = db.query(CLAIM_NEXT, {"worker_id": new_worker_id(), "lease": timedelta(seconds=60), "finished": FINISHED_STATUSES})
        (oracle / "claiming").write_text(json.dumps({"run_id": rows[0]["run_id"] if rows else None}), encoding="utf-8")
        deadline = time.monotonic() + 30
        while not (oracle / "die").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os._exit(DIE)  # the transaction is never committed: the server rolls it back


def claim_then_die(oracle: Path, lease: float) -> None:
    claims = open_claim_repository(config())
    worker = new_worker_id()
    claim = claims.claim_next(worker, LeasePolicy(lease=timedelta(seconds=lease), heartbeat_interval=timedelta(seconds=lease / 3)))
    (oracle / "claimed").write_text(json.dumps({"worker_id": worker, "token": claim.fence.fencing_token}), encoding="utf-8")
    os._exit(DIE)  # committed, then gone without a release


class HeldNotice(fx.SendNoticeTool):
    """The notice tool, held after its dispatch was recorded: it sends only once released."""

    def run(self, workspace, *, to, text):
        hold_until_released(self.oracle)()
        return super().run(workspace, to=to, text=text)


def work(args: argparse.Namespace) -> int:
    from agent_factory.cli import _stop_on_signals

    cfg = config()
    oracle = Path(args.oracle)
    if args.zombie:
        # A stall the worker does not notice: no heartbeat ever reaches the database, yet the
        # worker keeps believing its lease is valid. Only fencing can stop it now.
        LeaseKeeper.beat = lambda self: True  # type: ignore[method-assign]
        LeaseKeeper.trusted = lambda self: True  # type: ignore[method-assign]
    if args.kind == "calculator":
        source = WorkspaceRunSource(cfg, args.workspace)
        if args.hold:
            def adjust(ex):
                ex.agents["planner"] = BeforeAgent(ex.agents["planner"], hold_until_released(oracle))

            source = WrappedSource(source, adjust)
    else:
        if args.hold:
            fx.tool_for = lambda kind, oracle_: HeldNotice(oracle_)  # this child process only
        source = NoticeSource(cfg, Path(args.workspace), oracle)
    identity = WorkerIdentity.new()
    events = WorkerEvents(identity.worker_id, sink=lambda record: print(json.dumps(record, sort_keys=True), flush=True))
    settings = WorkerSettings(
        policy=LeasePolicy(lease=timedelta(seconds=args.lease), heartbeat_interval=timedelta(seconds=args.heartbeat)),
        poll_interval=0.05, max_backoff=0.5, max_runs=1, idle_exit=args.idle_exit,
    )  # fmt: skip
    worker = Worker(identity, settings, open_claims=lambda: open_claim_repository(cfg), source=source, events=events)
    restore = _stop_on_signals(worker)
    try:
        report = worker.run()
    finally:
        restore()
    print("REPORT " + json.dumps(vars(report), sort_keys=True), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("die-in-claim")
    p.add_argument("oracle")
    p = sub.add_parser("claim-then-die")
    p.add_argument("oracle")
    p.add_argument("lease", type=float)
    p = sub.add_parser("work")
    p.add_argument("kind", choices=["calculator", "notice"])
    p.add_argument("workspace")
    p.add_argument("oracle")
    p.add_argument("--hold", action="store_true")
    p.add_argument("--zombie", action="store_true")
    p.add_argument("--lease", type=float, default=60.0)
    p.add_argument("--heartbeat", type=float, default=20.0)
    p.add_argument("--idle-exit", type=float, default=None)
    args = parser.parse_args()
    Path(args.oracle).mkdir(parents=True, exist_ok=True)
    if args.mode == "die-in-claim":
        die_in_claim(Path(args.oracle))
    elif args.mode == "claim-then-die":
        claim_then_die(Path(args.oracle), args.lease)
    return work(args)


if __name__ == "__main__":
    raise SystemExit(main())
