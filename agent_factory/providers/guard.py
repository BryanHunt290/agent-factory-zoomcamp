"""The request guard: the last check before a model request leaves the process (docs/model-providers.md).

It fails closed. A request is refused, and nothing is sent, if its serialized
form contains:

* the value of any secret the orchestrator holds: environment variables named
  like credentials (keys, tokens, secrets, passwords, database URLs, ...), and
  the contents of the credential files that ``*_FILE`` settings point to
  (the database URL file, the model API key file, ...);
* a URL with embedded credentials (``scheme://user:password@host``);
* something shaped like a JWT (an access token).

The error names the kind of secret, never the value. The guard does not make
context safe by itself: context assembly already sends only what a role
needs. It is the backstop that catches a mistake anywhere upstream.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..sandbox.validation import CREDENTIAL_URL_RE, JWT_VALUE_RE
from ..util import canonical_json
from .base import ModelRequest, ProviderError

#: Environment variable names whose values are treated as secrets.
SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|DATABASE_URL|DSN|PRIVATE|_SID\b|SID$|AUTH_TOKEN|COOKIE|SESSION")
#: ``*_FILE`` settings whose files hold a secret (a URL or a key): their contents are secrets too.
SECRET_FILE_ENV_RE = re.compile(r"(URL|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?_FILE$")
MIN_SECRET_LEN = 12
MAX_SECRET_FILE_BYTES = 16_384


class ModelRequestRefused(ProviderError):
    """The request would have carried a secret: nothing was sent."""

    category = "secret_in_request"


def _looks_secret(value: str) -> bool:
    # Short or purely alphabetic values ("development", "production") are settings, not secrets.
    return len(value) >= MIN_SECRET_LEN and not value.isalpha()


@dataclass(frozen=True)
class RequestGuard:
    secrets: frozenset[str]

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *, extra: Iterable[str] = ()) -> RequestGuard:
        env = os.environ if environ is None else environ
        found = {v.strip() for k, v in env.items() if SECRET_ENV_RE.search(k.upper()) and _looks_secret(v.strip())}
        for name, value in env.items():
            if SECRET_FILE_ENV_RE.search(name.upper()) and value:
                try:
                    path = Path(value)
                    if path.is_file() and path.stat().st_size <= MAX_SECRET_FILE_BYTES:
                        content = path.read_text(encoding="utf-8", errors="replace").strip()
                        if _looks_secret(content):
                            found.add(content)
                except OSError:
                    pass
        found |= {s for s in extra if s and _looks_secret(s)}
        return cls(frozenset(found))

    def check(self, request: ModelRequest) -> None:
        """Raise ``ModelRequestRefused`` if the request carries a secret. Never returns the secret."""
        text = canonical_json({
            "instructions": request.instructions, "inputs": request.inputs, "capabilities": request.capabilities,
            "schema": request.output_schema, "run_id": request.run_id, "agent": request.agent,
        })  # fmt: skip
        escaped = text.replace("\\\\", "\\")  # JSON doubles backslashes (Windows paths in a URL file, ...)
        for secret in self.secrets:
            if secret in text or secret in escaped:
                raise ModelRequestRefused("the model request would contain a credential the orchestrator holds; nothing was sent")
        if CREDENTIAL_URL_RE.search(text):
            raise ModelRequestRefused("the model request would contain a URL with embedded credentials; nothing was sent")
        if JWT_VALUE_RE.search(text):
            raise ModelRequestRefused("the model request would contain an access token; nothing was sent")


__all__ = ["ModelRequestRefused", "RequestGuard"]
