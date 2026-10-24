"""A worker for the action fixture's runs, in its own process (dashboard E2E and reliability tests).

    python tests/dashboard_worker.py <root> <kind> [--lease S] [--heartbeat S] [--poll S] [--hold-after-effect]

The test has set up the fixture flow under ``<root>`` (tests/decision_support.py) in the PostgreSQL test
schema named by ``DASHBOARD_WORKER_SCHEMA``. This process runs one real ``Worker`` loop until it is
killed: it claims the flow's run, consumes decisions (made in the dashboard), dispatches the notice
through the ActionExecution ledger, and releases the run, exactly as a production worker would.

``--hold-after-effect`` makes the notice gateway slow to acknowledge: it sends the notice (the effect,
counted in the oracle file), writes ``<oracle>/holding``, and waits for ``<oracle>/release`` before
returning. A test kills the worker while it holds, to prove a takeover never sends the notice twice.

Trusted test code only: never a hostile fixture. Events are printed as JSON lines.
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
from decision_support import Flow  # noqa: E402

from agent_factory.domain.claims import LeasePolicy  # noqa: E402
from agent_factory.persistence.config import TEST_PREFIX, DatabaseConfig  # noqa: E402
from agent_factory.persistence.postgres import open_postgres_store  # noqa: E402
from agent_factory.worker import Worker, WorkerEvents, WorkerIdentity, WorkerSettings  # noqa: E402


def hold_after_effect() -> None:
    real = fx.SendNoticeTool.run

    def run(self, *args, **kwargs):
        result = real(self, *args, **kwargs)  # the notice is out
        (self.oracle / "holding").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 300
        while not (self.oracle / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return result

    fx.SendNoticeTool.run = run  # type: ignore[method-assign]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("kind", choices=["notice", "unknown-notice"])
    parser.add_argument("--lease", type=float, default=10.0)
    parser.add_argument("--heartbeat", type=float, default=3.0)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--hold-after-effect", action="store_true")
    args = parser.parse_args()
    config = DatabaseConfig.from_environment(prefix=TEST_PREFIX).with_schema(os.environ["DASHBOARD_WORKER_SCHEMA"])
    store = open_postgres_store(config)
    flow = Flow.postgres(Path(args.root), config, store, args.kind, enroll=False)
    if args.hold_after_effect:
        hold_after_effect()
    identity = WorkerIdentity.new()
    events = WorkerEvents(identity.worker_id, sink=lambda record: print(json.dumps(record, sort_keys=True), flush=True))
    settings = WorkerSettings(
        policy=LeasePolicy(lease=timedelta(seconds=args.lease), heartbeat_interval=timedelta(seconds=args.heartbeat)),
        poll_interval=args.poll, max_backoff=max(1.0, args.poll * 5), max_runs=None, idle_exit=None,
    )  # fmt: skip
    worker = Worker(identity, settings, open_claims=flow.open_claims, source=flow, events=events)
    print(json.dumps({"event": "fixture_worker_ready", "worker_id": identity.worker_id}), flush=True)
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
