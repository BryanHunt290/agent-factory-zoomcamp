"""A worker in a child process that dies for real while consuming a human decision (E5 crash tests).

    python tests/decision_child.py <root> <kind> <fault>

The parent (tests/test_postgres_decisions.py) has set up the fixture flow
under ``<root>`` (workspace ``ws``, oracle ``oracle``) in the PostgreSQL test
schema named by ``DECISION_CHILD_SCHEMA``, and recorded a decision. This
process runs one worker (one claim at most) and ends with ``os._exit(137)``
at ``fault``, skipping every ``finally`` block and flush, like ``kill -9``:

* ``in_claim``: inside the claim transaction, before it commits (rolled back by the server);
* ``before_apply``: claimed and checked, before the decision is applied to the engine;
* ``after_apply_before_checkpoint``: the decision applied and logged, before the checkpoint write;
* ``before_dispatch_record``: the checkpoint written, before the write-ahead dispatch record;
* ``after_tool_before_outcome``: the side effect done, before its outcome is recorded;
* ``after_outcome``: the outcome recorded, before the node's checkpoint;
* ``after_reconcile_before_checkpoint``: a reconciliation applied to the ledger, before the checkpoint;
* ``before_retry_dispatch``: a retry authorized (applied), before the retry's dispatch record;
* ``after_retry_tool_before_outcome``: the retry's side effect done, before its outcome is recorded.

It prints ``claimed`` once it holds the claim. Trusted test code only: never a hostile fixture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import action_fixtures as fx  # noqa: E402
from decision_support import Flow  # noqa: E402

from agent_factory.domain.executions import ExecutionLedger  # noqa: E402
from agent_factory.engine.actions import ActionGuard  # noqa: E402
from agent_factory.engine.checkpoint import CheckpointStore  # noqa: E402
from agent_factory.engine.executor import Executor  # noqa: E402
from agent_factory.persistence.config import TEST_PREFIX, DatabaseConfig  # noqa: E402

DIE = 137


def die() -> None:
    sys.stdout.flush()
    os._exit(DIE)


def install(fault: str) -> None:
    """Make this process end at ``fault``. Patches only this child process."""
    real_begin, real_finish = ExecutionLedger.begin, ExecutionLedger.finish
    real_reconcile, real_notice = ActionGuard.reconcile, fx.SendNoticeTool.run

    if fault == "in_claim":
        from agent_factory.persistence import claims as pg_claims

        def claim_next(self, worker_id, policy):
            with self.db.transaction():
                self.db.query(pg_claims.CLAIM_NEXT, {"worker_id": worker_id, "lease": policy.lease,
                                                     "finished": pg_claims.FINISHED_STATUSES})  # fmt: skip
                die()  # never committed: the server rolls the claim back

        pg_claims.PostgresClaimRepository.claim_next = claim_next  # type: ignore[method-assign]
    elif fault == "before_apply":
        Executor._apply_decision = lambda self, *a, **k: die()  # type: ignore[method-assign]
    elif fault == "after_apply_before_checkpoint":
        CheckpointStore.save = lambda self, state: die()  # type: ignore[method-assign]  # the first write here applies it
    elif fault == "before_dispatch_record":
        ExecutionLedger.begin = lambda self, *a, **k: die()  # type: ignore[method-assign]
    elif fault == "after_tool_before_outcome":

        def run(self, *a, **k):
            real_notice(self, *a, **k)
            die()

        fx.SendNoticeTool.run = run  # type: ignore[method-assign]
    elif fault == "after_outcome":

        def finish(self, *a, **k):
            real_finish(self, *a, **k)
            die()

        ExecutionLedger.finish = finish  # type: ignore[method-assign]
    elif fault == "after_reconcile_before_checkpoint":

        def reconcile(self, *a, **k):
            real_reconcile(self, *a, **k)
            die()

        ActionGuard.reconcile = reconcile  # type: ignore[method-assign]
    elif fault == "before_retry_dispatch":

        def begin(self, execution, **k):
            if execution.authorization_seq > 1:
                die()
            return real_begin(self, execution, **k)

        ExecutionLedger.begin = begin  # type: ignore[method-assign]
    elif fault == "after_retry_tool_before_outcome":

        def run(self, *a, **k):
            retry = bool(fx.read_lines(self.oracle / "notices.jsonl"))
            result = real_notice(self, *a, **k)
            if retry:
                die()
            return result

        fx.SendNoticeTool.run = run  # type: ignore[method-assign]
    else:
        raise SystemExit(f"unknown fault {fault!r}")


def main() -> int:
    root, kind, fault = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    config = DatabaseConfig.from_environment(prefix=TEST_PREFIX).with_schema(os.environ["DECISION_CHILD_SCHEMA"])
    from agent_factory.persistence.postgres import open_postgres_store

    store = open_postgres_store(config)
    flow = Flow.postgres(root, config, store, kind, enroll=False)
    install(fault)
    events = flow.work(idle_exit=5.0)
    print("finished without dying: " + ",".join(e["event"] for e in events), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
