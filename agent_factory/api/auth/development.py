"""DEVELOPMENT ONLY: a local token issuer, so the API can be used before an identity provider exists.

**This is not production authentication.** Anyone holding its private key can
mint a token for any subject. It exists so that a developer can run the API
on their own machine and call it with a real signed token, through exactly
the verification path production uses.

* The API trusts it only in ``AGENT_FACTORY_AUTH_MODE=development``, which is
  never the default. Production configuration refuses its issuer
  (``urn:agent-factory:development``, not an https URL) and its key
  directory, and refuses to start with both kinds of setting present.
* The API process loads only the public key set (``jwks.json``). The private
  key stays in a file read by the ``auth dev-issuer token`` command, which
  writes each token to a file instead of printing it.
* Its tokens are short-lived (15 minutes by default, at most a day) and carry
  no permissions: a principal still has to be enrolled for the subject, and
  granted permissions, like any other identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

DEV_ISSUER = "urn:agent-factory:development"
DEV_AUDIENCE = "agent-factory-api"
DEV_ALGORITHM = "ES256"
PRIVATE_KEY_FILE = "signing-key.pem"
JWKS_FILE = "jwks.json"
DEFAULT_LIFETIME = timedelta(minutes=15)
MAX_LIFETIME = timedelta(days=1)


class DevelopmentIssuerError(ValueError):
    pass


def _thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint of an EC public key: the key's own, stable ID."""
    members = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    digest = hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode("ascii")).digest()
    return jwt.utils.base64url_encode(digest).decode("ascii")


def _public_jwk(private_key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": _thumbprint(jwk), "use": "sig", "alg": DEV_ALGORITHM}


def load_public_jwks(directory: Path | str) -> dict[str, Any]:
    """The development issuer's public key set: all the API process ever reads."""
    path = Path(directory) / JWKS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DevelopmentIssuerError(
            f"cannot read the development issuer's public keys ({type(exc).__name__}); "
            "create them with: python -m agent_factory auth dev-issuer init"
        ) from None


class DevelopmentIssuer:
    """DEVELOPMENT ONLY. Mints short-lived ES256 tokens for the local development issuer."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or private_key.curve.name != "secp256r1":
            raise DevelopmentIssuerError("the development issuer uses a P-256 key")
        self._key = private_key
        self.public_jwk = _public_jwk(private_key)

    @classmethod
    def create(cls, directory: Path | str) -> DevelopmentIssuer:
        """Generate a new key pair in ``directory``; never overwrites an existing one."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        issuer = cls(ec.generate_private_key(ec.SECP256R1()))
        pem = issuer._key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        try:
            fd = os.open(directory / PRIVATE_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise DevelopmentIssuerError(f"{directory / PRIVATE_KEY_FILE} already exists; refusing to replace it") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        (directory / JWKS_FILE).write_text(json.dumps({"keys": [issuer.public_jwk]}, indent=2) + "\n", encoding="utf-8")
        return issuer

    @classmethod
    def load(cls, directory: Path | str) -> DevelopmentIssuer:
        try:
            pem = (Path(directory) / PRIVATE_KEY_FILE).read_bytes()
            key = serialization.load_pem_private_key(pem, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise DevelopmentIssuerError(
                f"cannot load the development signing key ({type(exc).__name__}); "
                "create one with: python -m agent_factory auth dev-issuer init"
            ) from None
        return cls(key)  # type: ignore[arg-type]

    def jwks(self) -> dict[str, Any]:
        return {"keys": [dict(self.public_jwk)]}

    def mint(
        self,
        subject: str,
        *,
        lifetime: timedelta = DEFAULT_LIFETIME,
        display_name: str | None = None,
        email: str | None = None,
        now: float | None = None,
    ) -> str:
        """A signed development access token for ``subject``. It grants nothing by itself."""
        if not timedelta(0) < lifetime <= MAX_LIFETIME:
            raise DevelopmentIssuerError("a development token lives between one second and one day")
        issued = int(time.time() if now is None else now)
        claims: dict[str, Any] = {
            "iss": DEV_ISSUER,
            "aud": DEV_AUDIENCE,
            "sub": subject,
            "iat": issued,
            "nbf": issued,
            "exp": issued + int(lifetime.total_seconds()),
            "jti": secrets.token_hex(16),
        }
        if display_name:
            claims["name"] = display_name
        if email:
            claims["email"] = email
        return jwt.encode(claims, self._key, algorithm=DEV_ALGORITHM, headers={"kid": self.public_jwk["kid"]})


__all__ = [
    "DEV_ALGORITHM",
    "DEV_AUDIENCE",
    "DEV_ISSUER",
    "DevelopmentIssuer",
    "DevelopmentIssuerError",
    "load_public_jwks",
]
