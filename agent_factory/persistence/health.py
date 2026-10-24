"""The answer to "can this store serve requests right now?" (no driver needed to import it)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Readiness:
    """``reason`` is one fixed word, never connection details: "ok", "database_unavailable",
    or "schema_not_current"."""

    ready: bool
    reason: str


READY = Readiness(True, "ok")
