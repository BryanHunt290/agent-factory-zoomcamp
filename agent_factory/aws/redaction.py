"""AWS result filtering: only known fields, no credentials, bounded size (docs/aws-capabilities.md).

A raw SDK answer never leaves the adapter. Each tool projects it onto a fixed, small set of fields
with expected types (anything else is dropped, and a wrong type is refused), then this module removes
credential-like keys and values, masks account numbers, bounds strings and maps, and refuses a result
that is still too large. What survives can enter run state, logs, and later model context.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ..tools.base import ToolError

MAX_RESULT_BYTES = 4096
MAX_STRING = 200
MAX_MAP_ENTRIES = 20
REDACTED = "<redacted>"
#: Keys whose values are never kept (case-insensitive substring match).
SECRET_KEY_RE = re.compile(r"secret|token|password|passwd|credential|session|private|signature|access.?key|auth", re.I)
#: Values that look like credentials anywhere in a string.
SECRET_VALUE_RE = re.compile(
    r"(AKIA|ASIA|AROA|AIDA)[A-Z0-9]{12,}|aws_secret_access_key|aws_session_token|-----BEGIN|FwoGZXIvYXdz[A-Za-z0-9/+=-]*"
)
ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


class AwsResultRejected(ToolError):
    """The answer had an unexpected structure or was too large. Nothing from it is kept."""


def clean_text(value: str) -> str:
    value = SECRET_VALUE_RE.sub(REDACTED, value)
    value = ACCOUNT_RE.sub(lambda m: "********" + m.group(0)[-4:], value)
    return value[:MAX_STRING]


def clean_map(value: Any) -> dict[str, str]:
    """A small string map (for example user metadata): secret-looking keys dropped, values cleaned, bounded."""
    if not isinstance(value, Mapping):
        raise AwsResultRejected("aws: unexpected result structure")
    out: dict[str, str] = {}
    for key in sorted(value)[:MAX_MAP_ENTRIES]:
        if not isinstance(key, str) or SECRET_KEY_RE.search(key):
            continue
        item = value[key]
        out[clean_text(key)[:64]] = clean_text(item) if isinstance(item, str) else REDACTED
    return out


def project(raw: Any, fields: Mapping[str, tuple[str, type | tuple[type, ...], bool]]) -> dict[str, Any]:
    """Keep only ``fields`` (source key -> (output name, expected type, required)). Refuse wrong types."""
    if not isinstance(raw, Mapping):
        raise AwsResultRejected("aws: unexpected result structure")
    out: dict[str, Any] = {}
    for source, (name, kind, required) in fields.items():
        if source not in raw:
            if required:
                raise AwsResultRejected("aws: unexpected result structure")
            continue
        value = raw[source]
        if isinstance(value, bool) and kind is not bool or not isinstance(value, kind):
            raise AwsResultRejected("aws: unexpected result structure")
        out[name] = value
    return out


def bounded(result: dict[str, Any]) -> dict[str, Any]:
    """Clean every string, then refuse anything larger than MAX_RESULT_BYTES."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, Mapping):
            return {k: (REDACTED if SECRET_KEY_RE.search(k) else walk(v)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [walk(v) for v in value[:MAX_MAP_ENTRIES]]
        return value

    cleaned = walk(result)
    if len(json.dumps(cleaned, sort_keys=True)) > MAX_RESULT_BYTES:
        raise AwsResultRejected("aws: the result is too large")
    return cleaned


__all__ = ["MAX_RESULT_BYTES", "REDACTED", "AwsResultRejected", "bounded", "clean_map", "clean_text", "project"]
