"""U2A offline host authentication contract; never an Upwork authentication client.

An explicitly paired fake host and verifier share a fresh in-memory HMAC key.
The verifier issues single-use, expiring requests bound to a run and account.
Only the trusted coordinator consumes receipts, before constructing a workflow.
No key, raw receipt, query, or source prose is projected into workflow state.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
import secrets
from threading import Lock

from .contracts import ObservationError, TOOL, _pairs, digest, obj, parse_observations, require, timestamp

VERSION = "upwork-host-receipt-v1"
SERVER = "https://mcp.upwork.com/mcp"
MAX_RECEIPT_BYTES = 120_000
REQUEST_TTL = timedelta(minutes=5)
MAX_REQUESTS = 16


class ReceiptError(ValueError):
    def __init__(self):
        super().__init__("INVALID_HOST_RECEIPT")


def _check(condition):
    if not condition:
        raise ReceiptError()


def _utc(now):
    _check(type(now) is datetime and now.tzinfo is not None and now.utcoffset() == timedelta(0))
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(value, length):
    _check(type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is not None)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _wire_json(value):
    # Wire limits count UTF-8 bytes; HMAC/digest canonicalization remains ASCII.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _decode(payload):
    _check(type(payload) is str and len(payload) <= MAX_RECEIPT_BYTES)
    try:
        _check(len(payload.encode("utf-8")) <= MAX_RECEIPT_BYTES)
        return json.loads(payload, object_pairs_hook=_pairs, parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise ReceiptError() from None


def _mac(key, body):
    return hmac.new(key, _canonical(body).encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class HostRequest:
    _json: str = field(repr=False)

    def data(self):
        return json.loads(self._json)


@dataclass(frozen=True)
class AcceptedReceipt:
    """Session-held acceptance, not a transferable approval or bearer credential."""
    _payload: str = field(repr=False)
    _audit: str = field(repr=False)

    def audit(self):
        return json.loads(self._audit)


class ReceiptSession:
    """Bounded, process-local verifier; recreated sessions reject all old receipts.

    This class does not load keys or credentials from files/environment. A future
    remote host must establish independent key provisioning and durable ownership.
    """

    def __init__(self, *, key, host_id, session_id, account_binding):
        _check(type(key) is bytes and len(key) == 32)
        for value in (host_id, session_id, account_binding):
            _hex(value, 64)
        self._key = key
        self._host_id = host_id
        self._session_id = session_id
        self._account_binding = account_binding
        self._lock = Lock()
        self._requests = {}
        self._accepted = {}

    def issue(self, *, run_id, title, job_type="hourly", limit=3, now):
        """Issue exactly one search/get scope. No pagination, retry, or write scope."""
        _utc(now)
        _check(type(run_id) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", run_id) is not None)
        _check(type(title) is str and 1 <= len(title) <= 100 and all(ord(c) >= 32 for c in title))
        _check(job_type in ("hourly", "fixed") and type(limit) is int and 1 <= limit <= 3)
        with self._lock:
            _check(self._key is not None and len(self._requests) < MAX_REQUESTS)
            _check(all(r[0].data()["run_id"] != run_id for r in self._requests.values()))
            nonce = secrets.token_hex(32)
            data = {"schema_version": "upwork-host-request-v1", "mode": "offline_fake",
                    "host_id": self._host_id, "session_id": self._session_id,
                    "account_binding": self._account_binding, "run_id": run_id, "nonce": nonce,
                    "issued_at": _utc(now), "expires_at": _utc(now + REQUEST_TTL),
                    "server": SERVER, "tool": TOOL, "action": "search",
                    "params": {"title": title, "job_type": job_type, "sort": "recency", "limit": limit},
                    "bounds": {"searches": 1, "details": limit}}
            request = HostRequest(_canonical(data))
            self._requests[nonce] = (request, False)
            return request

    def accept(self, receipt, *, request, now):
        """Verify everything before atomically consuming the request; no partial acceptance."""
        try:
            return self._accept(receipt, request=request, now=now)
        except (ObservationError, ValueError, TypeError, KeyError, RecursionError):
            # No upstream fields/exception text or chained exceptions may escape.
            raise ReceiptError() from None

    def _accept(self, receipt, *, request, now):
        _utc(now)
        _check(type(request) is HostRequest)
        data = _decode(receipt)
        obj(data, "body mac")
        _hex(data["mac"], 64)
        expected = request.data()
        # Only locally issued request objects are accepted, not caller reconstructions.
        with self._lock:
            _check(self._key is not None)
            registered = self._requests.get(expected["nonce"])
            _check(registered is not None and registered[0] is request and not registered[1])
            _check(hmac.compare_digest(data["mac"], _mac(self._key, data["body"])))
            body = obj(data["body"], "schema_version mode host_id session_id account_binding run_id nonce request_digest server started_at completed_at bounds events observations")
            obj(body["bounds"], "searches details")
            _check(all(type(value) is int for value in body["bounds"].values()))
            _check(body["schema_version"] == VERSION and body["mode"] == "offline_fake")
            for key in ("host_id", "session_id", "account_binding", "run_id", "nonce", "server", "bounds"):
                _check(body[key] == expected[key])
            _check(body["request_digest"] == digest(expected))
            start, end = timestamp(body["started_at"]), timestamp(body["completed_at"])
            _check(timestamp(expected["issued_at"]) <= start <= end <= now < timestamp(expected["expires_at"]))
            batch = parse_observations(_wire_json(body["observations"]), now=now)
            jobs = batch.records()
            limit = expected["params"]["limit"]
            _check(len(jobs) <= limit)
            events = body["events"]
            _check(type(events) is list and len(events) == 1 + len(jobs))
            all_ids = [j["job_id"] for j in jobs]
            seen_calls, detailed = set(), set()
            previous = start
            references = []
            by_id = {j["job_id"]: j for j in jobs}
            for index, event in enumerate(events):
                obj(event, "call_id tool action params_digest observed_at trace_id source_reference job_ids result_digest status")
                _hex(event["call_id"], 32)
                _hex(event["trace_id"], 32)
                _check(event["call_id"] not in seen_calls)
                seen_calls.add(event["call_id"])
                _check(event["tool"] == TOOL and event["status"] == "ok")
                at = timestamp(event["observed_at"])
                _check(previous <= at <= end)
                previous = at
                if index == 0:
                    _check(event["action"] == "search" and event["source_reference"] == SERVER)
                    _check(event["params_digest"] == digest(expected["params"]))
                    _check(event["job_ids"] == all_ids and event["result_digest"] == digest(all_ids))
                else:
                    ids = event["job_ids"]
                    _check(type(ids) is list and len(ids) == 1 and type(ids[0]) is str)
                    job_id = ids[0]
                    _check(job_id in by_id and job_id not in detailed)
                    detailed.add(job_id)
                    job = by_id[job_id]
                    _check(event["action"] == "get" and event["params_digest"] == digest({"id": job_id}))
                    _check(event["source_reference"] == job["url"] and event["result_digest"] == digest(job))
                    _check(event["observed_at"] == job["observed_at"])
                    _check(job["provenance"]["trace_id"] == event["trace_id"])
                    _check(job["provenance"]["search_action"] == "search")
                references.append({k: event[k] for k in ("call_id", "tool", "action", "observed_at", "trace_id", "source_reference", "job_ids")})
            _check(detailed == set(all_ids))
            audit = {"schema_version": VERSION, "authentication": "authenticated_offline_fake_host",
                     "live_upwork_verified": False, "receipt_digest": digest(body),
                     "request_digest": body["request_digest"], "host_id": self._host_id,
                     "account_binding": self._account_binding, "run_id": expected["run_id"],
                     "started_at": body["started_at"], "completed_at": body["completed_at"],
                     "expires_at": expected["expires_at"], "bounds": expected["bounds"], "events": references}
            accepted = AcceptedReceipt(batch._canonical, _canonical(audit))
            self._requests[expected["nonce"]] = (request, True)
            self._accepted[audit["receipt_digest"]] = accepted
            return accepted

    def check_accepted(self, accepted, *, run_id, now):
        """Read-only validation for each workflow stage, including resumed stages."""
        try:
            _utc(now)
            _check(type(accepted) is AcceptedReceipt)
            audit = accepted.audit()
            with self._lock:
                _check(self._key is not None and self._accepted.get(audit["receipt_digest"]) is accepted)
            _check(run_id == audit["run_id"])
            _check(timestamp(audit["completed_at"]) <= now < timestamp(audit["expires_at"]))
            parse_observations(accepted._payload, now=now)
            return audit
        except (ObservationError, ValueError, TypeError, KeyError, RecursionError):
            raise ReceiptError() from None

    def close(self):
        with self._lock:
            self._key = None
            self._requests.clear()
            self._accepted.clear()


class FakeReceiptHost:
    """Fixture-only signer. It attests fixtures, never a real MCP connection."""

    def __init__(self, key):
        _check(type(key) is bytes and len(key) == 32)
        self._key = key

    def acquire(self, request, payload, *, started_at, completed_at):
        _check(type(request) is HostRequest)
        scope = request.data()
        start, end = _utc(started_at), _utc(completed_at)
        batch = parse_observations(payload, now=completed_at)
        jobs = batch.records()
        ids = [j["job_id"] for j in jobs]
        events = [{"call_id": secrets.token_hex(16), "tool": TOOL, "action": "search",
                   "params_digest": digest(scope["params"]), "observed_at": start,
                   "trace_id": secrets.token_hex(16), "source_reference": SERVER, "job_ids": ids,
                   "result_digest": digest(ids), "status": "ok"}]
        for job in sorted(jobs, key=lambda job: (job["observed_at"], job["job_id"])):
            events.append({"call_id": secrets.token_hex(16), "tool": TOOL, "action": "get",
                           "params_digest": digest({"id": job["job_id"]}), "observed_at": job["observed_at"],
                           "trace_id": job["provenance"]["trace_id"], "source_reference": job["url"],
                           "job_ids": [job["job_id"]], "result_digest": digest(job), "status": "ok"})
        body = {k: scope[k] for k in ("mode", "host_id", "session_id", "account_binding", "run_id", "nonce", "server", "bounds")}
        body.update(schema_version=VERSION, request_digest=digest(scope), started_at=start,
                    completed_at=end, events=events, observations=json.loads(batch._canonical))
        return _wire_json({"body": body, "mac": _mac(self._key, body)})


def offline_host_session(*, account_binding):
    """Explicit fake pairing. No default identity, config lookup, key file or network."""
    key = secrets.token_bytes(32)
    session = ReceiptSession(key=key, host_id=secrets.token_hex(32), session_id=secrets.token_hex(32),
                             account_binding=account_binding)
    return session, FakeReceiptHost(key)
