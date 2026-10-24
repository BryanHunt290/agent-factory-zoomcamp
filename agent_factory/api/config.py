"""API server settings, and the rule that keeps the API on this machine.

Every product request is authenticated and authorized (docs/authentication.md),
but the API is not yet a deployed service: it has no TLS, its identity
provider is configured per machine, and no deployment has been designed or
reviewed. It therefore listens on 127.0.0.1 by default and refuses
non-loopback addresses. Public exposure is a later deployment decision.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
CURSOR_SECRET_FILE_ENV = "AGENT_FACTORY_API_CURSOR_SECRET_FILE"
#: Host header values accepted by default: the loopback interface only.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

class UnsafeBindError(ValueError):
    """A non-loopback address was requested; the API is not designed for public exposure yet."""


def is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ApiSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    pool_min_size: int = 1
    pool_max_size: int = 10
    pool_timeout_s: float = 3.0
    idempotency_ttl: timedelta = timedelta(hours=24)
    cursor_ttl: timedelta = timedelta(hours=24)
    cursor_secret_file: Path | None = None

    def __post_init__(self) -> None:
        self.bind_warnings()
        if not 1 <= self.port <= 65535:
            raise ValueError("the API port must be between 1 and 65535")
        if self.idempotency_ttl <= timedelta(0) or self.cursor_ttl <= timedelta(0):
            raise ValueError("API retention periods must be positive")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, **values: object) -> ApiSettings:
        """Settings with the cursor key file from ``AGENT_FACTORY_API_CURSOR_SECRET_FILE``, if set."""
        environ = os.environ if environ is None else environ
        secret_file = environ.get(CURSOR_SECRET_FILE_ENV, "").strip()
        values.setdefault("cursor_secret_file", Path(secret_file) if secret_file else None)
        return cls(**values)  # type: ignore[arg-type]

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host header values the server answers (protection against DNS rebinding)."""
        hosts = set(LOOPBACK_HOSTS)
        hosts.add(self.host.strip().strip("[]").lower())
        return frozenset(hosts)

    def bind_warnings(self) -> list[str]:
        """Refuse a non-loopback address; retained as the CLI's explicit bind check."""
        if is_loopback(self.host):
            return []
        raise UnsafeBindError(
            "refusing a non-loopback bind: the API listens on loopback only (127.0.0.1). Public exposure "
            "needs a deployment design (TLS, identity provider, review) that does not exist yet."
        )
