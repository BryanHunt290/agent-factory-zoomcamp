"""Small shared helpers: clock, canonical JSON, hashing, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(clock: Clock) -> str:
    """ISO-8601 UTC timestamp with a trailing Z."""
    return clock().astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding used for hashing and identifiers."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write pretty, sorted JSON so that a crash never leaves a half-written file.

    The output is stable (sorted keys, LF newlines) so it diffs cleanly in git.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    _replace(tmp, path)
    fsync_directory(path.parent)


# Windows refuses, for a moment, to replace a file that another process (an
# antivirus scanner, the search indexer) has just opened. That denial is
# retried briefly; any other error, or a denial that persists, still raises.
_REPLACE_DENIAL_IS_TRANSIENT = os.name == "nt"
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2)  # seconds: under 0.4 s in total


def _replace(source: Path, target: Path) -> None:
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not _REPLACE_DENIAL_IS_TRANSIENT:
                raise
        time.sleep(delay)
    os.replace(source, target)


def fsync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable (POSIX). A crash could otherwise undo it.

    Windows cannot open a directory for fsync; NTFS journals the rename's
    metadata instead, without a durability guarantee for the latest rename.
    """
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
