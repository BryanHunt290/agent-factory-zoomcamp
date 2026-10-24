"""Operator commands on database-backed runs: claimed like a worker, so they never race one.

In database mode, every change to a run is made under the run's claim
(docs/workers.md, "operator commands"):

* ``approve`` and ``reconcile-action`` only record the person's decision (and
  wake the run) in PostgreSQL; a worker applies it;
* ``resume``, the demo, and ``db reconcile`` change the run themselves, so
  they first claim it, exactly as a worker would: a fencing token, a lease
  kept by heartbeats, and a store whose every write is fenced. If a worker
  holds the run, the command refuses (``RunOwnedElsewhere``) instead of
  waiting or racing.

Runs without a database keep the single-host behaviour (the run lock).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..domain.claims import Claim, Fence, LeasePolicy, OwnershipLost
from ..domain.repositories import ClaimRepository, ProductStore
from ..state import ProjectState
from .events import WorkerEvents
from .identity import WorkerIdentity
from .lease import ClaimOwnership, LeaseKeeper
from .loop import Release, release_plan


class RunOwnedElsewhere(Exception):
    """A worker (or another operator command) holds the run's claim right now; nothing was changed."""


@dataclass
class OperatorClaim:
    """What a claimed operator command works with. Set ``state`` to the run's final state before leaving."""

    claim: Claim
    store: ProductStore  # fenced by the claim
    ownership: ClaimOwnership
    state: ProjectState | None = field(default=None)
    release: Release | None = field(default=None)  # overrides what ``state`` would mean for the claim

    @property
    def fence(self) -> Fence:
        return self.claim.fence


@contextmanager
def claimed_run(
    open_claims: Callable[[], ClaimRepository],
    open_store: Callable[[Fence], ProductStore],
    project_id: str,
    run_id: str,
    *,
    policy: LeasePolicy | None = None,
    events: WorkerEvents | None = None,
    identity: WorkerIdentity | None = None,
) -> Iterator[OperatorClaim]:
    """Hold one run's claim for the duration of the block, then release it by the run's final state.

    Released as a worker would (``release_plan``) once ``state`` is set; left
    as it was if the block fails; never released if ownership was lost (the
    next owner takes over).
    """
    policy = policy or LeasePolicy()
    identity = identity or WorkerIdentity.new()
    events = events or WorkerEvents(identity.worker_id)
    claims = open_claims()
    try:
        sent_at = time.monotonic()
        claim = claims.claim_run(project_id, run_id, identity.worker_id, policy)
        if claim is None:
            current = claims.get(project_id, run_id)
            holder = (
                f"worker {current.worker_id} (generation {current.fencing_token})"
                if current is not None and current.worker_id
                else "another process"
            )
            raise RunOwnedElsewhere(f"run {run_id} is being worked on by {holder}; it continues there")
        where: dict[str, Any] = {"project_id": project_id, "run_id": run_id, "fencing_token": claim.fence.fencing_token}
        events.emit("run_claimed", **where, lease_expires_at=claim.lease_expires_at)
        keeper = LeaseKeeper(open_claims, claim, policy, events, sent_at=sent_at)
        store: ProductStore | None = None
        plan = Release(None, "error")  # if the block fails, the run's availability is left as it was
        try:
            try:
                keeper.start()
                store = open_store(claim.fence)
                held = OperatorClaim(claim, store, ClaimOwnership(keeper, store.unit_of_work.prove_ownership, stop=threading.Event()))
                yield held
                if held.release is not None:
                    plan = held.release
                elif held.state is not None:
                    planned = release_plan(held.state)
                    plan = Release(planned.requeue, "operator_done", planned.delay, planned.at)
            finally:
                keeper.stop()
                if store is not None:
                    store.close()
        except OwnershipLost as exc:
            events.emit("stale_worker_rejected", **where, operation="advance", reason=exc.reason)
            raise  # never release a claim this process no longer owns: the next owner takes over
        except BaseException:
            _release(claims, claim, plan, events, where)
            raise
        _release(claims, claim, plan, events, where)
    finally:
        claims.close()


def _release(claims: ClaimRepository, claim: Claim, plan: Release, events: WorkerEvents, where: dict[str, Any]) -> None:
    if claims.release(claim.fence, requeue=plan.requeue, delay=plan.delay, at=plan.at):
        events.emit(
            "run_released", **where, reason=plan.reason, requeued=bool(plan.requeue), retry_in_s=None,
            wake_at=plan.at if plan.requeue else None,
        )  # fmt: skip


__all__ = ["OperatorClaim", "RunOwnedElsewhere", "claimed_run"]
