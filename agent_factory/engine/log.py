"""Append-only execution log.

One JSON object per line (JSON Lines). Every entry carries a sequence
number, a UTC timestamp, the event type, and a SHA-256 hash chained to the
previous entry, so edits, deletions, or reordering are detectable with
:meth:`ExecutionLog.verify`.

Event types written by the executor:

    run_started, run_resumed, node_entered, action, node_result,
    transition, approval_requested, approval_decision, run_paused,
    run_completed, run_failed, run_exhausted
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..util import Clock, canonical_json, sha256_hex, timestamp, utc_now

GENESIS_HASH = "0" * 64
_RESERVED = frozenset({"seq", "timestamp", "event", "prev_hash", "hash"})


class LogIntegrityError(RuntimeError):
    pass


class ExecutionLog:
    FILENAME = "execution_log.jsonl"

    def __init__(self, path: Path, clock: Clock = utc_now) -> None:
        self.path = Path(path)
        self._clock = clock
        entries = self.entries()
        self._seq = entries[-1]["seq"] if entries else 0
        self._last_hash = entries[-1]["hash"] if entries else GENESIS_HASH

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        clash = _RESERVED & fields.keys()
        if clash:
            raise ValueError(f"reserved log fields: {sorted(clash)}")
        entry: dict[str, Any] = {
            "seq": self._seq + 1,
            "timestamp": timestamp(self._clock),
            "event": event,
            **fields,
            "prev_hash": self._last_hash,
        }
        entry["hash"] = sha256_hex(canonical_json(entry))
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Mode "a" only ever appends; existing lines are never rewritten.
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._seq = entry["seq"]
        self._last_hash = entry["hash"]
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            raise LogIntegrityError(f"{self.path}: last line is incomplete (torn write)")
        out = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LogIntegrityError(f"{self.path}:{lineno}: invalid JSON: {exc}") from exc
        return out

    def verify(self) -> int:
        """Check sequence numbers and the hash chain. Returns the entry count."""
        prev = GENESIS_HASH
        entries = self.entries()
        for expected_seq, entry in enumerate(entries, start=1):
            if entry.get("seq") != expected_seq:
                raise LogIntegrityError(f"entry {expected_seq}: sequence is {entry.get('seq')!r}")
            if entry.get("prev_hash") != prev:
                raise LogIntegrityError(f"entry {expected_seq}: broken hash chain")
            body = {k: v for k, v in entry.items() if k != "hash"}
            if sha256_hex(canonical_json(body)) != entry.get("hash"):
                raise LogIntegrityError(f"entry {expected_seq}: hash mismatch (entry was modified)")
            prev = entry["hash"]
        return len(entries)
