"""The identity provider's public signing keys (a JSON Web Key Set, RFC 7517).

A token names the key that signed it (its ``kid`` header); the verifier asks a
key source for that key. Two sources exist:

* :class:`StaticKeySet`: a fixed set, for tests and the development issuer.
* :class:`RemoteKeySet`: the provider's published JWKS, fetched over HTTPS and
  cached. Providers rotate keys by publishing the new one first, so a token
  with an unknown ``kid`` triggers a refresh, at most once a minute. If the
  keys cannot be fetched, the last good set is used for at most ``max_stale``
  past its expiry; after that nothing verifies (fail closed: 503, never a
  guess).

Only public, asymmetric signature keys are accepted: RSA, EC, or OKP
(Ed25519). A symmetric key (``kty`` ``oct``) or one carrying private
members is refused outright: a published JWKS never has either, and a
symmetric "key" would let anyone who can read it sign tokens.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import jwt
from jwt.exceptions import InvalidKeyError, PyJWKError

log = logging.getLogger("agent_factory.api.auth")

MAX_KEYS = 32
MAX_JWKS_BYTES = 64 * 1024
ASYMMETRIC_KEY_TYPES = frozenset({"RSA", "EC", "OKP"})
#: JWK members that only a private (or symmetric) key has.
PRIVATE_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


class UnknownKey(Exception):
    """No usable published key has this ``kid`` for this algorithm (the token is rejected: 401)."""


class KeysUnavailable(Exception):
    """The provider's keys cannot be obtained, so no token can be verified (503, fail closed)."""


class InvalidKeySet(ValueError):
    """A JWKS document that is malformed, or holds anything other than public signature keys."""


def parse_jwks(document: Any) -> dict[str, dict[str, Any]]:
    """The signature keys of a JWKS document, by ``kid``; anything unexpected is refused."""
    if not isinstance(document, Mapping) or not isinstance(document.get("keys"), list):
        raise InvalidKeySet("a JWKS is an object with a 'keys' list")
    keys = document["keys"]
    if not 1 <= len(keys) <= MAX_KEYS:
        raise InvalidKeySet(f"a JWKS must hold 1 to {MAX_KEYS} keys")
    found: dict[str, dict[str, Any]] = {}
    for key in keys:
        if not isinstance(key, Mapping):
            raise InvalidKeySet("every JWKS entry must be an object")
        kid = key.get("kid")
        if not isinstance(kid, str) or not 1 <= len(kid) <= 256:
            raise InvalidKeySet("every key needs a kid")
        if kid in found:
            raise InvalidKeySet(f"key id {kid!r} appears twice")
        if key.get("kty") not in ASYMMETRIC_KEY_TYPES:
            raise InvalidKeySet(f"key {kid!r}: only public RSA, EC, or OKP keys are accepted")
        if PRIVATE_MEMBERS & set(key):
            raise InvalidKeySet(f"key {kid!r}: a published key must not contain private material")
        if key.get("use", "sig") != "sig":
            raise InvalidKeySet(f"key {kid!r}: not a signature key")
        found[kid] = dict(key)
    return found


class _KeyCache:
    """PyJWK objects for (kid, algorithm), built once from the parsed key set."""

    def __init__(self, keys: Mapping[str, Mapping[str, Any]]) -> None:
        self.keys = dict(keys)
        self._built: dict[tuple[str, str], jwt.PyJWK] = {}

    def get(self, kid: str, algorithm: str) -> jwt.PyJWK:
        built = self._built.get((kid, algorithm))
        if built is not None:
            return built
        jwk = self.keys.get(kid)
        if jwk is None:
            raise UnknownKey(kid)
        if jwk.get("alg", algorithm) != algorithm:
            raise UnknownKey(kid)  # the provider bound this key to another algorithm
        try:
            # The algorithm is bound to the key here: PyJWT then refuses a token whose
            # header names any other algorithm, so a key can never be used "as" HS256.
            built = jwt.PyJWK(dict(jwk), algorithm=algorithm)
        except (PyJWKError, InvalidKeyError, ValueError, TypeError):
            raise UnknownKey(kid) from None
        self._built[(kid, algorithm)] = built
        return built


class StaticKeySet:
    """A fixed key set: the test issuer's, or the development issuer's public keys."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._cache = _KeyCache(parse_jwks(document))

    def key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        return self._cache.get(kid, algorithm)


def fetch_https(url: str, timeout: float) -> bytes:
    """GET ``url`` over HTTPS: no redirects to other schemes, a timeout, and a size limit."""
    if urlsplit(url).scheme != "https":
        raise KeysUnavailable("the JWKS URL must use https")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only, checked above
        if urlsplit(response.geturl()).scheme != "https":
            raise KeysUnavailable("the JWKS URL redirected away from https")
        body = response.read(MAX_JWKS_BYTES + 1)
    if len(body) > MAX_JWKS_BYTES:
        raise KeysUnavailable("the JWKS document is too large")
    return body


class RemoteKeySet:
    """The provider's published keys, fetched over HTTPS, cached, refreshed for rotation, and fail closed."""

    def __init__(
        self,
        url: str,
        *,
        fetch: Callable[[str, float], bytes] = fetch_https,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = 600,
        min_refresh_interval_s: float = 60,
        max_stale_s: float = 3600,
        timeout_s: float = 5,
    ) -> None:
        if urlsplit(url).scheme != "https":
            raise ValueError("the JWKS URL must use https")
        self.url = url
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl_s
        self._min_refresh = min_refresh_interval_s
        self._max_stale = max_stale_s
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._cache: _KeyCache | None = None
        self._fetched_at = float("-inf")
        self._attempted_at = float("-inf")

    def key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        with self._lock:
            now = self._clock()
            if self._cache is None or now - self._fetched_at >= self._ttl:
                self._refresh(now, required=True)
            elif kid not in self._cache.keys and now - self._attempted_at >= self._min_refresh:
                self._refresh(now, required=False)  # perhaps a newly rotated key
            assert self._cache is not None
            return self._cache.get(kid, algorithm)

    def _refresh(self, now: float, *, required: bool) -> None:
        self._attempted_at = now
        try:
            document = json.loads(self._fetch(self.url, self._timeout).decode("utf-8"))
            self._cache = _KeyCache(parse_jwks(document))
            self._fetched_at = now
        except (OSError, ValueError, http.client.HTTPException, KeysUnavailable) as exc:
            # OSError: network and HTTP errors; ValueError: not UTF-8, not JSON, or not a valid key set.
            usable = self._cache is not None and now - self._fetched_at < self._ttl + self._max_stale
            log.warning("could not refresh the identity provider's keys (%s); %s", type(exc).__name__,
                        "keeping the last good set" if usable else "no usable keys")
            if required and not usable:
                raise KeysUnavailable(f"the identity provider's keys cannot be obtained ({type(exc).__name__})") from None


__all__ = [
    "InvalidKeySet",
    "KeysUnavailable",
    "RemoteKeySet",
    "StaticKeySet",
    "UnknownKey",
    "fetch_https",
    "parse_jwks",
]
