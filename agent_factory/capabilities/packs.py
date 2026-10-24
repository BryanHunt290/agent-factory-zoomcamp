"""Tool packs: the unit in which capabilities, their tools, and their probes arrive (docs/capabilities.md).

A pack owns one namespace (``core``, ``git``, ``aws``) and registers:

* its capability definitions;
* the tool implementations they name (registered in the ToolRegistry only while the pack is enabled);
* an availability probe (safe, read-only, secret-free);
* the backend kinds whose live verifications count as LIVE_TESTED evidence for it (``live_backends``).

A pack cannot supply live evidence itself: LIVE_TESTED comes only from the reviewed evidence file
(``capabilities/live_evidence.py``).

A pack is enabled only by explicit configuration in code or settings, never because a credential
happens to exist. A disabled pack's capabilities stay listed, as UNAVAILABLE (``not_configured``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from .model import Availability, AvailabilityStatus, Capability

if TYPE_CHECKING:
    from ..tools.base import Tool


class ToolPack(ABC):
    #: The pack's name, which is also the namespace of every capability it owns.
    name: ClassVar[str]
    #: Real backend kinds whose recorded live verifications count for this pack (never "fake").
    live_backends: ClassVar[frozenset[str]] = frozenset()

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """True only when the pack was explicitly configured to be used."""

    @abstractmethod
    def capabilities(self) -> tuple[Capability, ...]: ...

    @abstractmethod
    def tools(self) -> tuple[Tool, ...]:
        """The implementations its capabilities name. Registered only while the pack is enabled."""

    def probe(self, now: str) -> Availability:
        """Whether the pack can be used now. Must be read-only, free, and secret-free."""
        return Availability(AvailabilityStatus.AVAILABLE, checked_at=now, detail="local")

    def probe_capability(self, capability: Capability, now: str) -> Availability | None:
        """An optional, narrower probe for one capability, run only while the pack itself is available.
        None means the pack's result applies. The same rules: read-only, free, secret-free."""
        return None


__all__ = ["ToolPack"]
