"""Validation helpers, JSON-safety, timestamps, and the versioned record envelope.

Every persisted domain record serializes to a JSON object of the form::

    {"record_type": "<type>", "schema_version": <int>, ...fields}

Deserialization is strict and deliberate:

* the record type must match;
* a newer schema version than this code supports is rejected (never guessed);
* an older version must have an explicit upgrade function;
* unknown fields are rejected rather than silently dropped;
* missing fields are rejected.

Serialization is deterministic (sorted keys, no insignificant whitespace), so
equal records always produce byte-identical JSON.

Timestamps are UTC with a trailing ``Z`` and are stored in one canonical form,
``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (microseconds, the precision PostgreSQL keeps).
Any accepted spelling of an instant is normalised to it, so equal instants are
equal values and survive a round trip through any store unchanged.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, TypeVar

from .errors import DomainValidationError, SchemaVersionError

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
MAX_METADATA_BYTES = 16 * 1024
MAX_METADATA_DEPTH = 8
_CONTROL_SINGLE_LINE = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_MULTI_LINE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

E = TypeVar("E", bound=Enum)


def canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, no NaN/Infinity."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ------------------------------------------------------------------ scalars


def check_text(value: Any, field: str, *, max_len: int = 500, allow_empty: bool = False, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{field}: expected text, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise DomainValidationError(f"{field}: must not be empty")
    if len(value) > max_len:
        raise DomainValidationError(f"{field}: longer than {max_len} characters")
    control = _CONTROL_MULTI_LINE if multiline else _CONTROL_SINGLE_LINE
    if control.search(value):
        raise DomainValidationError(f"{field}: contains control characters")
    return value


def check_optional_text(value: Any, field: str, **kw: Any) -> str | None:
    return None if value is None else check_text(value, field, **kw)


def check_pattern(value: Any, regex: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise DomainValidationError(f"{field}: invalid value {value!r}")
    return value


def check_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field}: expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise DomainValidationError(f"{field}: must be >= {minimum}, got {value}")
    return value


def check_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DomainValidationError(f"{field}: expected true or false, got {value!r}")
    return value


def check_enum(enum_cls: type[E], value: Any, field: str) -> E:
    try:
        return enum_cls(value)
    except ValueError:
        allowed = [m.value for m in enum_cls]
        raise DomainValidationError(f"{field}: {value!r} is not one of {allowed}") from None


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def format_timestamp(moment: datetime) -> str:
    """The canonical text form of an aware datetime: UTC, microseconds, trailing Z."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DomainValidationError("timestamp: a datetime without a time zone is ambiguous")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def check_timestamp(value: Any, field: str) -> str:
    """Validate a UTC timestamp and return its canonical form."""
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise DomainValidationError(f"{field}: expected a UTC timestamp like 2026-01-01T00:00:00.000000Z, got {value!r}")
    try:
        moment = parse_timestamp(value)
    except ValueError:
        raise DomainValidationError(f"{field}: {value!r} is not a real date/time") from None
    return format_timestamp(moment)


def check_optional_timestamp(value: Any, field: str) -> str | None:
    return None if value is None else check_timestamp(value, field)


# ------------------------------------------------------------- JSON safety


def _check_json(value: Any, path: str, depth: int) -> None:
    if depth > MAX_METADATA_DEPTH:
        raise DomainValidationError(f"{path}: nested deeper than {MAX_METADATA_DEPTH} levels")
    if isinstance(value, str):
        if "\x00" in value:  # not representable in PostgreSQL text or jsonb; never meaningful here
            raise DomainValidationError(f"{path}: contains a NUL character")
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError(f"{path}: {value!r} is not representable in JSON")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_json(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(f"{path}: object keys must be strings, got {key!r}")
            if "\x00" in key:
                raise DomainValidationError(f"{path}: object key contains a NUL character")
            _check_json(item, f"{path}.{key}", depth + 1)
        return
    raise DomainValidationError(f"{path}: {type(value).__name__} is not JSON-safe")


def check_json_object(value: Any, field: str, *, max_bytes: int = MAX_METADATA_BYTES) -> dict[str, Any]:
    """Validate a JSON-safe object and return an independent deep copy."""
    if not isinstance(value, dict):
        raise DomainValidationError(f"{field}: expected an object, got {type(value).__name__}")
    _check_json(value, field, 0)
    if len(canonical(value).encode("utf-8")) > max_bytes:
        raise DomainValidationError(f"{field}: larger than {max_bytes} bytes when serialized")
    return copy.deepcopy(value)


check_metadata = check_json_object


# ---------------------------------------------------------------- envelope

Upgrade = Callable[[dict[str, Any]], dict[str, Any]]


def open_envelope(
    data: Any, *, record_type: str, version: int, upgrades: Mapping[int, Upgrade] | None = None
) -> dict[str, Any]:
    """Check the envelope and return the record's fields at the current schema version."""
    if not isinstance(data, Mapping):
        raise DomainValidationError(f"{record_type}: expected a JSON object, got {type(data).__name__}")
    body = dict(data)
    found_type = body.pop("record_type", None)
    if found_type != record_type:
        raise DomainValidationError(f"record_type: expected {record_type!r}, got {found_type!r}")
    found_version = body.pop("schema_version", None)
    if isinstance(found_version, bool) or not isinstance(found_version, int):
        raise SchemaVersionError(f"{record_type}: schema_version must be an integer, got {found_version!r}")
    if found_version > version:
        raise SchemaVersionError(
            f"{record_type}: schema_version {found_version} is newer than this Agent Factory supports ({version})"
        )
    upgrades = upgrades or {}
    while found_version < version:
        if found_version not in upgrades:
            raise SchemaVersionError(f"{record_type}: no upgrade path from schema_version {found_version}")
        body = upgrades[found_version](body)
        found_version += 1
    return body


def check_fields(data: Mapping[str, Any], *, required: Iterable[str], optional: Iterable[str] = (), where: str) -> None:
    required, optional = set(required), set(optional)
    unknown = sorted(set(data) - required - optional)
    if unknown:
        raise DomainValidationError(f"{where}: unknown field(s) {unknown}")
    missing = sorted(required - set(data))
    if missing:
        raise DomainValidationError(f"{where}: missing field(s) {missing}")


def seal(record_type: str, version: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"record_type": record_type, "schema_version": version, **body}


class RecordMixin:
    """Shared JSON helpers for enveloped records (subclasses define to_dict/from_dict)."""

    RECORD_TYPE: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int]

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def to_json(self) -> str:
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any):  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def from_json(cls, text: str):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise DomainValidationError(f"{cls.RECORD_TYPE}: not valid JSON: {exc}") from None
        return cls.from_dict(data)
