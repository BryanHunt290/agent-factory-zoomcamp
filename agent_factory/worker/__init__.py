"""Workers: processes that claim runs from PostgreSQL and advance them (docs/workers.md).

Several workers may share one database. A worker may change a run only while
it holds the run's claim (``domain/claims.py``): one owner at a time, a lease
renewed by heartbeats, and a fencing token checked by every write. A stale
worker cannot change the run. Workers are internal infrastructure: the HTTP
API neither exposes nor authenticates them.

* ``identity``: who a worker process is (a random ``wkr-`` ID);
* ``lease``: heartbeats, the local deadline, and the executor's ownership;
* ``loop``: claim, advance, release; graceful shutdown;
* ``events``: safe, structured worker events;
* ``sources``: claimed runs from this host's workspaces and PostgreSQL.
"""

from .events import EVENTS, WorkerEvents
from .identity import WorkerIdentity
from .lease import ClaimOwnership, LeaseKeeper
from .loop import ClaimedRun, RunSource, RunUnavailable, Worker, WorkerReport, WorkerSettings

__all__ = [
    "EVENTS",
    "ClaimOwnership",
    "ClaimedRun",
    "LeaseKeeper",
    "RunSource",
    "RunUnavailable",
    "Worker",
    "WorkerEvents",
    "WorkerIdentity",
    "WorkerReport",
    "WorkerSettings",
]
