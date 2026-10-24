"""LIVE_TESTED evidence: a durable, verifiable record that a capability version passed an approved live check
(docs/capabilities.md).

``LIVE_TESTED`` is the strongest claim the registry makes, so it has the narrowest source. Documentation
never creates it. A unit test never creates it. Only an explicitly authorized live capability
verification may append a record, and the registry trusts records only from this file:

    evidence/live-capabilities.jsonl   (in the repository: reviewed, committed, and versioned)

Each record holds:
* the capability ID and version;
* the backend kind (never a fake or stub);
* the verification ID and suite;
* the environment (``dev``, ``staging``, ``prod``);
* the commit it ran at, and the time;
* a SHA-256 digest of the safe result (never the result itself);
* who authorized the run.

Records are hash-chained, each holding the previous one's hash. A record that is malformed, out of
chain, or names a fake backend makes the WHOLE file untrusted: the registry then grants no LIVE_TESTED
at all (fail closed).

A record proves that one version passed once, in one environment. It never proves the capability is
available now (that is the probe's job, AVAILABLE_NOW), never transfers to another version, and never
transfers to another environment.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..util import canonical_json, sha256_hex
from .model import CAPABILITY_ID_RE, CapabilityError

#: The only file the registry reads LIVE_TESTED evidence from (absent until an authorized live verification).
EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "live-capabilities.jsonl"
ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
#: Backends that can never produce live evidence.
NOT_LIVE_BACKENDS = frozenset({"fake", "stub", "stubber", "loopback", "test", "mock", ""})
GENESIS = "0" * 64


class LiveEvidenceError(CapabilityError):
    """A live evidence record or file is invalid. The registry then trusts none of it."""


@dataclass(frozen=True)
class LiveVerificationAuthorization:
    """The owner's authorization for one live verification of one capability version in one environment."""

    capability_id: str
    version: int
    environment: str
    authorized_by: str  # e.g. "owner, 2026-10-07, Stage E2"; never a credential

    def __post_init__(self) -> None:
        if not CAPABILITY_ID_RE.fullmatch(self.capability_id) or self.version < 1 or self.environment not in ENVIRONMENTS:
            raise LiveEvidenceError("a live verification authorization names one capability version and environment")
        if not 3 <= len(self.authorized_by) <= 120:
            raise LiveEvidenceError("a live verification authorization says who authorized it")


@dataclass(frozen=True)
class LiveVerification:
    capability_id: str
    version: int
    backend: str
    verification_id: str
    suite: str
    environment: str
    commit: str
    recorded_at: str
    result_digest: str
    authorized_by: str
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("hash")
        return data


def _check(record: LiveVerification) -> None:
    if not CAPABILITY_ID_RE.fullmatch(record.capability_id) or not isinstance(record.version, int) or record.version < 1:
        raise LiveEvidenceError("malformed capability id or version")
    if record.backend.lower() in NOT_LIVE_BACKENDS or not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", record.backend):
        raise LiveEvidenceError("a fake, stub, or test backend can never produce live evidence")
    if record.environment not in ENVIRONMENTS:
        raise LiveEvidenceError("unknown environment")
    if not re.fullmatch(r"[0-9a-f]{7,40}", record.commit):
        raise LiveEvidenceError("the commit the verification ran at is required")
    if not re.fullmatch(r"[0-9a-f]{64}", record.result_digest):
        raise LiveEvidenceError("a SHA-256 digest of the safe result is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,80}", record.verification_id) or not record.suite:
        raise LiveEvidenceError("a verification id and suite are required")
    if not 3 <= len(record.authorized_by) <= 120:
        raise LiveEvidenceError("who authorized the verification is required")
    datetime.fromisoformat(record.recorded_at.replace("Z", "+00:00"))
    if record.hash != sha256_hex(canonical_json(record.body())):
        raise LiveEvidenceError("a record's hash does not match its content")


def load_live_evidence(path: Path = EVIDENCE_PATH) -> tuple[LiveVerification, ...]:
    """Every record, verified as a chain. A missing file is no evidence; an invalid one raises."""
    if not path.is_file():
        return ()
    records: list[LiveVerification] = []
    prev = GENESIS
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = LiveVerification(**json.loads(line))
        except (TypeError, ValueError) as exc:
            raise LiveEvidenceError(f"line {number}: not a live verification record") from exc
        try:
            _check(record)
        except ValueError as exc:
            raise LiveEvidenceError(f"line {number}: {exc}") from None
        if record.prev_hash != prev:
            raise LiveEvidenceError(f"line {number}: the hash chain is broken")
        prev = record.hash
        records.append(record)
    return tuple(records)


def append_live_verification(*, authorization: LiveVerificationAuthorization, capability_id: str, version: int,
                             backend: str, verification_id: str, suite: str, environment: str, commit: str,
                             recorded_at: str, safe_result: Any, path: Path = EVIDENCE_PATH) -> LiveVerification:  # fmt: skip
    """Append one record. Refused unless it matches its authorization exactly and the backend is real.

    This is the ONLY writer. It is meant to be called by an authorized live verification (Stage E2's opt-in
    suite), never by the default test suite, and the record must then be reviewed and committed.
    """
    if (authorization.capability_id, authorization.version, authorization.environment) != (capability_id, version, environment):
        raise LiveEvidenceError("the record does not match its authorization")
    existing = load_live_evidence(path)
    body = {
        "capability_id": capability_id, "version": version, "backend": backend, "verification_id": verification_id,
        "suite": suite, "environment": environment, "commit": commit, "recorded_at": recorded_at,
        "result_digest": sha256_hex(canonical_json(safe_result)), "authorized_by": authorization.authorized_by,
        "prev_hash": existing[-1].hash if existing else GENESIS,
    }  # fmt: skip
    record = LiveVerification(**body, hash=sha256_hex(canonical_json(body)))
    _check(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(asdict(record)) + "\n")
    return record


__all__ = [
    "EVIDENCE_PATH",
    "LiveEvidenceError",
    "LiveVerification",
    "LiveVerificationAuthorization",
    "append_live_verification",
    "load_live_evidence",
]
