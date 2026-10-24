"""Who a worker process is."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from ..domain.claims import WORKER_ID_RE, new_worker_id
from ..domain.serialization import check_pattern
from ..util import Clock, timestamp, utc_now


@dataclass(frozen=True)
class WorkerIdentity:
    """One worker process: a random ID that no other process, here or on another host, ever has.

    The ID is what claims, fences, and execution attempts record. The host
    and process ID are shown in the worker's start-up event to help a person
    find the process; nothing decides anything by them.
    """

    worker_id: str
    host: str
    pid: int
    started_at: str  # this host's wall clock: for display only

    def __post_init__(self) -> None:
        check_pattern(self.worker_id, WORKER_ID_RE, "worker_id")

    @classmethod
    def new(cls, clock: Clock = utc_now) -> WorkerIdentity:
        return cls(worker_id=new_worker_id(), host=socket.gethostname()[:100], pid=os.getpid(), started_at=timestamp(clock))


__all__ = ["WorkerIdentity"]
