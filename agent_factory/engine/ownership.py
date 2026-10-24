"""Run ownership: what the executor asks of a worker that holds a run's claim.

A worker hands its executor a :class:`RunOwnership` (docs/workers.md). The
executor then proves ownership at every protected mutation of the run:

* before every **checkpoint write** (``verify``), because a file write cannot
  be made conditional on a database row;
* immediately before an approved action is **dispatched** (``verify``, after
  the write-ahead record): if ownership was lost, the action is not sent;
* every **database write** (product records, the execution ledger) is fenced
  by the store itself, in the same transaction as the write.

If ownership cannot be proved, ``OwnershipLost`` propagates out of the
executor at once and nothing more is written. It is a ``BaseException``, so
neither the executor's own failure handling nor an agent can turn it into a
failed run.

The executor also hands the run back at a node boundary, after the node's
checkpoint, when ``should_yield`` says so: the worker is shutting down, has
used its work budget, or no longer trusts its lease.

The engine knows nothing of claims, leases, or SQL: the worker implements this
protocol (``agent_factory.worker``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.claims import OwnershipLost


@runtime_checkable
class RunOwnership(Protocol):
    """Proof that this process may change one run."""

    @property
    def run_id(self) -> str:
        """The run this ownership is for. The executor refuses to use it for any other run."""

    @property
    def owner(self) -> str:
        """Who holds the run, as recorded on execution attempts: ``<worker id>@<fencing token>``."""

    def verify(self) -> None:
        """Return only if this process owns the run now, by the database's clock; else raise ``OwnershipLost``."""

    def should_yield(self) -> bool:
        """True if the executor should hand the run back at the next node boundary."""


__all__ = ["OwnershipLost", "RunOwnership"]
