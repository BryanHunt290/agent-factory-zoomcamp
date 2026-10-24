"""Context assembly: the minimum necessary project data for one model call (docs/model-providers.md).

The rule is **never "send everything"**. Each role's agent chooses what it
needs (the planner: the task and the list of file names; the developer: the
plan, the files it will change, and the feedback it must fix; the reviewer:
the task, the plan, the changed files, and the test evidence). This module
then enforces, for every role:

* **No secret-looking files.** ``.env``, keys, certificates, credential files,
  ``.git`` and Agent Factory's own metadata are withheld even when asked for;
  the model is told only that a file was withheld.
* **Bounded size.** Each file and the whole context are capped; long text is
  cut with a marker, and test output keeps its tail.
* **No host paths.** The workspace's absolute location, and the user's home
  directory, are replaced with ``<workspace>`` and ``<home>``.
* **Provenance.** Every file that leaves the machine is listed (path, size,
  SHA-256) in the call's audit record.

Everything returned here is project data: untrusted, and sent only as
``ModelRequest.inputs``. The final request is checked once more by the
request guard (``guard.py``) before any provider sees it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import sha256_hex

#: File names that may hold secrets. They are never put into model context.
SECRET_PATH_RE = re.compile(
    r"(^|/)(\.env(\..*)?|\.envrc|.*\.(pem|key|p12|pfx|crt|cer|der|kdbx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|\.netrc|\.pgpass|\.npmrc|\.pypirc|credentials(\..*)?|secrets?(\..*)?|[^/]*tokens?\.(json|txt|ya?ml|env)|\.git(/.*)?|\.agent_factory(/.*)?"
    r"|\.aws(/.*)?|\.ssh(/.*)?)$",
    re.IGNORECASE,
)


def withheld(path: str) -> bool:
    return bool(SECRET_PATH_RE.search(path.replace("\\", "/")))


@dataclass(frozen=True)
class ContextLimits:
    max_file_chars: int = 20_000
    max_total_chars: int = 80_000
    max_list_items: int = 500
    max_output_chars: int = 4_000  # test output and similar logs: the tail is kept


@dataclass
class ContextBuilder:
    """Collects one call's project data, enforcing the limits and recording what is sent."""

    limits: ContextLimits = field(default_factory=ContextLimits)
    host_paths: Iterable[str] = ()
    sent: list[dict[str, Any]] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    _total: int = 0

    def __post_init__(self) -> None:
        roots = {str(p) for p in self.host_paths if p}
        roots |= {str(Path.home())}
        # Longest first, both slash styles, so nested roots are replaced whole.
        variants = set()
        for root in roots:
            variants |= {root, root.replace("\\", "/"), root.replace("/", "\\")}
        self._roots = sorted((v for v in variants if len(v) > 3), key=len, reverse=True)

    def text(self, value: str, *, limit: int | None = None, tail: bool = False) -> str:
        """Redact host paths and cap the length (keeping the tail for logs)."""
        value = self.redact(value)
        cap = limit or self.limits.max_file_chars
        if len(value) > cap:
            value = ("…[truncated]\n" + value[-cap:]) if tail else (value[:cap] + "\n…[truncated]")
        return value

    def redact(self, value: str) -> str:
        for root in self._roots:
            if root in value:
                label = "<home>" if root.rstrip("\\/").lower() == str(Path.home()).rstrip("\\/").lower() else "<workspace>"
                value = value.replace(root, label)
        return value

    def paths(self, names: Iterable[str]) -> list[str]:
        """File names for a listing: secret-looking ones left out, the list bounded."""
        out = []
        for name in names:
            if withheld(name):
                if name not in self.withheld:
                    self.withheld.append(name)
                continue
            out.append(self.redact(name))
        return sorted(out)[: self.limits.max_list_items]

    def files(self, contents: Mapping[str, str]) -> dict[str, str]:
        """File contents to send: secret-looking files withheld, each and all capped, provenance recorded."""
        out: dict[str, str] = {}
        for path in sorted(contents):
            if withheld(path):
                self.withheld.append(path)
                continue
            body = self.text(contents[path])
            remaining = self.limits.max_total_chars - self._total
            if remaining <= 0:
                self.withheld.append(path)
                continue
            if len(body) > remaining:
                body = body[:remaining] + "\n…[truncated: context budget]"
            out[path] = body
            self._total += len(body)
            self.sent.append({"path": path, "chars": len(body), "sha256": sha256_hex(contents[path])})
        return out

    def manifest(self) -> dict[str, Any]:
        """What left (or would leave) the machine with this call: for the audit record."""
        return {"files": list(self.sent), "withheld": sorted(set(self.withheld))}


__all__ = ["SECRET_PATH_RE", "ContextBuilder", "ContextLimits", "withheld"]
