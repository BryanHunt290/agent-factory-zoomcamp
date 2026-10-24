"""Strict project-owned observation contract, not an Upwork wire schema.

Descriptions are untrusted data retained only in memory. Reports project typed
facts and hashes, never source text. Provenance is supplied, not authenticated.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlsplit

VERSION = "opportunity-observation-v1"
MAX_BYTES = 100_000
MAX_JOBS = 3
MAX_AGE = timedelta(hours=1)
TOOL = "mcp__upwork__upwork__find_jobs"


class ObservationError(ValueError):
    """Constant errors must not disclose rejected source data."""

    def __init__(self):
        super().__init__("INVALID_OPPORTUNITY_OBSERVATION")


def require(condition):
    if not condition:
        raise ObservationError()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def obj(value, keys):
    require(type(value) is dict and set(value) == set(keys.split()))
    return value


def text(value, limit):
    require(type(value) is str and 0 < len(value) <= limit)
    require(not any(ord(c) < 32 and c not in "\n\r\t" for c in value))
    return value


def nullable_enum(value, choices):
    require(value is None or (type(value) is str and value in choices))


def count(value):
    require(value is None or (type(value) is int and 0 <= value <= 1_000_000_000))


def boolean(value):
    require(value is None or type(value) is bool)


def amount(value):
    if value is None:
        return None
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,4})?", value) is not None)
    return Decimal(value)


def timestamp(value):
    text(value, 32)
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is not None)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ObservationError() from None


def canonical_url(value, job_id):
    text(value, 2000)
    try:
        url = urlsplit(value)
        require(url.scheme == "https" and url.netloc in ("www.upwork.com", "upwork.com"))
        require(url.path == "/jobs/~02" + job_id and not url.fragment)
        # Queries are never fetched or persisted, including tracking parameters.
        return "https://www.upwork.com" + url.path
    except ValueError:
        raise ObservationError() from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


@dataclass(frozen=True)
class ObservationBatch:
    _canonical: str = field(repr=False)

    @property
    def fingerprint(self):
        return digest(json.loads(self._canonical))

    def records(self):
        """Independent copies; callers cannot mutate the accepted observation."""
        return json.loads(self._canonical)["jobs"]

    def facts(self):
        return [{"job_id": j["job_id"], "url": j["url"], "observed_at": j["observed_at"],
                 "observation_digest": digest(j), "provenance": "unverified_offline_fixture",
                 "contract_type": j["contract_type"], "terms": j["terms"], "client": j["client"],
                 "activity": j["activity"], "connects_cost": j["connects_cost"],
                 "applied": j["applied"], "can_apply": j["can_apply"]} for j in self.records()]


def parse_observations(payload, *, now):
    """All-or-nothing, bounded intake. U1 accepts offline fixtures only.

No caller-supplied label, timestamp or hash proves a live acquisition. A future
host transport needs a separate verified receipt contract and explicit activation.
"""
    require(type(payload) is str and len(payload) <= MAX_BYTES)
    try:
        require(len(payload.encode("utf-8")) <= MAX_BYTES)
        data = json.loads(payload, object_pairs_hook=_pairs,
                          parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise ObservationError() from None
    obj(data, "schema_version mode jobs")
    require(data["schema_version"] == VERSION and data["mode"] == "offline_fake")
    require(type(now) is datetime and now.tzinfo is not None and now.utcoffset() == timedelta(0))
    require(type(data["jobs"]) is list and 1 <= len(data["jobs"]) <= MAX_JOBS)
    ids = set()
    for j in data["jobs"]:
        obj(j, "job_id url title description observed_at provenance contract_type terms client activity connects_cost applied can_apply")
        job_id = text(j["job_id"], 24)
        require(re.fullmatch(r"[1-9][0-9]{0,23}", job_id) is not None and job_id not in ids)
        ids.add(job_id)
        j["url"] = canonical_url(j["url"], job_id)
        text(j["title"], 300)
        text(j["description"], 20_000)
        observed = timestamp(j["observed_at"])
        require(timedelta(0) <= now - observed < MAX_AGE)
        p = obj(j["provenance"], "tool search_action detail_action search_job_id detail_job_id trace_id")
        require(p["tool"] == TOOL and p["search_action"] in ("search", "smart_search") and p["detail_action"] == "get")
        require(p["search_job_id"] == p["detail_job_id"] == job_id)
        require(type(p["trace_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", p["trace_id"]) is not None)
        nullable_enum(j["contract_type"], ("hourly", "fixed"))
        t = obj(j["terms"], "currency fixed_amount hourly_min hourly_max duration workload")
        nullable_enum(t["currency"], ("USD",))
        fixed, lo, hi = (amount(t[k]) for k in ("fixed_amount", "hourly_min", "hourly_max"))
        require(lo is None or hi is None or lo <= hi)
        require(j["contract_type"] != "hourly" or fixed is None)
        require(j["contract_type"] != "fixed" or (lo is None and hi is None))
        require(j["contract_type"] is not None or (fixed is None and lo is None and hi is None))
        nullable_enum(t["duration"], ("less_than_1_month", "1_to_3_months", "3_to_6_months", "more_than_6_months"))
        nullable_enum(t["workload"], ("full_time", "part_time", "as_needed"))
        c = obj(j["client"], "payment_verified spend_usd contracts_total feedback_score feedback_count hire_rate_percent")
        boolean(c["payment_verified"])
        amount(c["spend_usd"])
        for key in ("contracts_total", "feedback_count"):
            count(c[key])
        rating, rate = amount(c["feedback_score"]), amount(c["hire_rate_percent"])
        require(rating is None or rating <= 5)
        require(rate is None or rate <= 100)
        a = obj(j["activity"], "proposals_tier interviewing hired offered")
        nullable_enum(a["proposals_tier"], ("Fewer than 5", "5 to 10", "10 to 15", "15 to 20", "20 to 50", "50+"))
        for key in ("interviewing", "hired", "offered"):
            count(a[key])
        count(j["connects_cost"])
        boolean(j["applied"])
        boolean(j["can_apply"])
        require(not (j["applied"] is True and j["can_apply"] is True))
    data["jobs"].sort(key=lambda j: j["job_id"])
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    try:
        # Acquisition returns this normalized payload to the same bounded parser.
        require(len(canonical.encode("utf-8")) <= MAX_BYTES)
    except UnicodeError:
        raise ObservationError() from None
    return ObservationBatch(canonical)
