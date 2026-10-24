"""Where request handlers get a ``ProductStore`` from.

The API depends on this small interface, not on PostgreSQL:

* ``open()`` / ``close()``: called once, at application start-up and shutdown;
* ``session()``: a store for one unit of work, released at the end of the block;
* ``readiness()``: can a request be served now (for ``/readyz``).

``PostgresStorePool`` (agent_factory.persistence.pool) is the real
implementation. ``InMemoryStoreProvider`` exists for tests: nothing it holds
survives the process, and the ``api serve`` command never uses it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from ..domain.repositories import ProductStore, in_memory_store
from ..persistence.health import READY, Readiness


class StoreProvider(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def session(self) -> AbstractContextManager[ProductStore]: ...

    def readiness(self) -> Readiness: ...


class InMemoryStoreProvider:
    """One shared in-memory store. For tests only: not durable."""

    def __init__(self, store: ProductStore | None = None) -> None:
        self.store = store or in_memory_store()

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    @contextmanager
    def session(self) -> Iterator[ProductStore]:
        yield self.store

    def readiness(self) -> Readiness:
        return READY
