"""TEST ONLY: an identity provider for automated tests. Never used outside tests/.

Its tokens are real: signed with keys generated in memory for this test run
and checked by the production verifier (agent_factory/api/auth/tokens.py).
Only the issuer is fake, and nothing outside tests/ can trust it, because its
issuer URL and keys exist only in this process.
"""

import time
from datetime import timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from agent_factory.api.auth import Authenticator, SlidingWindowLimiter, StaticKeySet, TokenPolicy, TokenVerifier
from agent_factory.domain.ids import new_principal_id
from agent_factory.domain.principals import ALL_PERMISSIONS, Principal, PrincipalStatus

TEST_ISSUER = "https://issuer.agent-factory.test"
TEST_AUDIENCE = "agent-factory-api-test"
OWNER_SUBJECT = "owner-7f3c"
ENROLLED_AT = "2026-10-01T09:00:00.000000Z"


class TestIssuer:
    """TEST ONLY: signing keys for an EC (ES256) and an RSA (RS256) key, published as a JWKS."""

    __test__ = False  # not a test class, despite its name

    def __init__(self, issuer: str = TEST_ISSUER, audience: str = TEST_AUDIENCE) -> None:
        self.issuer = issuer
        self.audience = audience
        self.private = {
            "ec-1": (ec.generate_private_key(ec.SECP256R1()), "ES256"),
            "rsa-1": (rsa.generate_private_key(public_exponent=65537, key_size=2048), "RS256"),
        }

    def jwks(self) -> dict:
        keys = []
        for kid, (key, algorithm) in self.private.items():
            codec = ECAlgorithm if algorithm.startswith("ES") else RSAAlgorithm
            keys.append({**codec.to_jwk(key.public_key(), as_dict=True), "kid": kid, "alg": algorithm, "use": "sig"})
        return {"keys": keys}

    def policy(self, **overrides) -> TokenPolicy:
        values = {"issuer": self.issuer, "audience": self.audience, "algorithms": ("ES256", "RS256")}
        return TokenPolicy(**{**values, **overrides})

    def authenticator(self, *, failures_per_minute: int = 1000, decisions_per_minute: int = 1000, **policy) -> Authenticator:
        """Generous limits by default; rate-limit tests ask for small ones."""
        return Authenticator(
            TokenVerifier(self.policy(**policy), StaticKeySet(self.jwks())),
            failures=SlidingWindowLimiter(failures_per_minute, timedelta(minutes=1)),
            decisions=SlidingWindowLimiter(decisions_per_minute, timedelta(minutes=1)),
        )

    def token(self, subject: str = OWNER_SUBJECT, *, kid: str = "ec-1", lifetime: timedelta = timedelta(hours=1),
              headers: dict | None = None, key=None, algorithm: str | None = None, drop=(), **claims) -> str:
        """A signed token. ``claims`` override or add claims; ``drop`` removes claims."""
        private, default_algorithm = self.private[kid] if kid in self.private else self.private["ec-1"]
        now = int(time.time())
        body = {"iss": self.issuer, "aud": self.audience, "sub": subject, "iat": now, "exp": now + int(lifetime.total_seconds())}
        body.update(claims)
        for name in drop:
            body.pop(name, None)
        return jwt.encode(body, key if key is not None else private, algorithm=algorithm or default_algorithm,
                          headers={"kid": kid, **(headers or {})})


#: One issuer per test run: generating an RSA key takes a moment.
ISSUER = TestIssuer()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def enroll(store, subject: str = OWNER_SUBJECT, permissions=ALL_PERMISSIONS, *, issuer: str = TEST_ISSUER,
           status: PrincipalStatus = PrincipalStatus.ACTIVE, display_name: str | None = "Test Owner",
           email: str | None = None) -> Principal:
    """Enroll a principal and grant it ``permissions``, as the owner's CLI would."""
    principal = Principal(id=new_principal_id(), issuer=issuer, subject=subject, status=status,
                          created_at=ENROLLED_AT, updated_at=ENROLLED_AT, display_name=display_name, email=email)
    store.principals.add(principal)
    if permissions:
        store.principals.grant(principal.id, permissions, at=ENROLLED_AT)
    return principal


def ensure_owner(store) -> Principal:
    """The test owner (every permission), enrolled once per store."""
    return store.principals.find(TEST_ISSUER, OWNER_SUBJECT) or enroll(store)
