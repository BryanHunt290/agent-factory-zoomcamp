"""Token verification (api/auth/tokens.py) and the provider's key set (api/auth/keys.py).

Every token here is really signed; every check is the production code path.
Nothing contacts a network: remote key sets get an injected fetch function.
"""

import base64
import json
import time
from datetime import timedelta

import pytest

pytest.importorskip("jwt", reason="authentication tests need PyJWT: run with .venv (requirements-api.txt)")

import jwt  # noqa: E402
from auth_support import ISSUER, TEST_AUDIENCE, TEST_ISSUER, TestIssuer  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

from agent_factory.api.auth import AuthenticationFailed, AuthFailure, StaticKeySet, TokenPolicy, TokenVerifier  # noqa: E402
from agent_factory.api.auth.keys import InvalidKeySet, KeysUnavailable, RemoteKeySet, fetch_https, parse_jwks  # noqa: E402


def verifier(**policy):
    return TokenVerifier(ISSUER.policy(**policy), StaticKeySet(ISSUER.jwks()))


def refused(token, *, policy=None, keys=None):
    verify = TokenVerifier(ISSUER.policy(**(policy or {})), keys or StaticKeySet(ISSUER.jwks()))
    with pytest.raises(AuthenticationFailed) as caught:
        verify.verify(token)
    return caught.value.failure


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# ------------------------------------------------------------- valid tokens


@pytest.mark.parametrize("kid", ["ec-1", "rsa-1"])
def test_a_valid_token_establishes_issuer_and_subject(kid):
    verified = verifier().verify(ISSUER.token("owner-7f3c", kid=kid, name="Bryan", email="b@example.test"))
    assert (verified.issuer, verified.subject) == (TEST_ISSUER, "owner-7f3c")
    assert (verified.display_name, verified.email) == ("Bryan", "b@example.test")


def test_an_audience_list_containing_this_api_is_accepted():
    assert verifier().verify(ISSUER.token(aud=["https://other.example.test", TEST_AUDIENCE])).subject


def test_profile_claims_are_for_display_only():
    verified = verifier().verify(ISSUER.token(name="bad\nname", email="x" * 400))
    assert verified.display_name is None and verified.email is None  # ignored, not a reason to refuse


# --------------------------------------------------- malformed and unsigned


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d.e", "!!.??.**", "e30.e30.e30 ", "x" * 9000])
def test_a_malformed_credential_is_refused(token):
    assert refused(token) is AuthFailure.MALFORMED


def test_a_header_that_is_not_json_is_refused():
    assert refused(b64({"sub": "x"})[:-2] + "." + b64({"sub": "x"}) + ".c2ln") in (
        AuthFailure.MALFORMED, AuthFailure.UNSUPPORTED_ALGORITHM,
    )  # fmt: skip


def test_an_unsigned_token_is_refused():
    unsigned = b64({"alg": "none", "typ": "JWT", "kid": "ec-1"}) + "." + b64({"iss": TEST_ISSUER, "sub": "x"}) + ".AAAA"
    assert refused(unsigned) is AuthFailure.UNSUPPORTED_ALGORITHM


def test_hmac_signed_with_the_published_public_key_is_refused():
    """Algorithm confusion: the public RSA key used as an HMAC secret. The token's alg never chooses the check."""
    public_pem = ISSUER.private["rsa-1"][0].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    claims = {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "attacker", "iat": now, "exp": now + 60}
    body = b64({"alg": "HS256", "typ": "JWT", "kid": "rsa-1"}) + "." + b64(claims)
    import hashlib
    import hmac

    signature = base64.urlsafe_b64encode(hmac.new(public_pem, body.encode(), hashlib.sha256).digest()).rstrip(b"=")
    assert refused(body + "." + signature.decode()) is AuthFailure.UNSUPPORTED_ALGORITHM


def test_a_policy_cannot_allow_symmetric_or_unsigned_algorithms():
    for algorithms in (("HS256",), ("none",), ("ES256", "HS512"), ()):
        with pytest.raises(ValueError, match="algorithms"):
            TokenPolicy(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, algorithms=algorithms)


def test_an_algorithm_the_policy_does_not_allow_is_refused():
    assert refused(ISSUER.token(kid="rsa-1"), policy={"algorithms": ("ES256",)}) is AuthFailure.UNSUPPORTED_ALGORITHM


# ------------------------------------------------------------ signatures and keys


def test_a_token_signed_by_another_key_is_refused():
    impostor = ec.generate_private_key(ec.SECP256R1())
    assert refused(ISSUER.token(kid="ec-1", key=impostor)) is AuthFailure.INVALID_SIGNATURE


def test_a_changed_payload_breaks_the_signature():
    header, _, signature = ISSUER.token().split(".")
    now = int(time.time())
    forged = b64({"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "someone-else", "iat": now, "exp": now + 60})
    assert refused(f"{header}.{forged}.{signature}") is AuthFailure.INVALID_SIGNATURE


@pytest.mark.parametrize("headers", [{"kid": "no-such-key"}, {"kid": ""}])
def test_a_token_naming_no_published_key_is_refused(headers):
    assert refused(ISSUER.token(headers=headers)) is AuthFailure.UNKNOWN_KEY


def test_a_key_id_that_is_not_text_is_refused():
    _, payload, signature = ISSUER.token().split(".")
    header = b64({"alg": "ES256", "typ": "JWT", "kid": 7})  # PyJWT will not even encode this
    assert refused(f"{header}.{payload}.{signature}") in (AuthFailure.MALFORMED, AuthFailure.UNKNOWN_KEY)


def test_a_key_is_used_only_with_its_own_algorithm():
    """rsa-1 is published for RS256; a token claiming another algorithm with that key is refused."""
    token = ISSUER.token(kid="ec-1", headers={"kid": "rsa-1"})  # ES256-signed, naming the RSA key
    assert refused(token) is AuthFailure.UNKNOWN_KEY


def test_keys_below_the_minimum_length_are_refused():
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    jwks = {"keys": [{**RSAAlgorithm.to_jwk(weak.public_key(), as_dict=True), "kid": "weak", "alg": "RS256"}]}
    with pytest.warns(jwt.warnings.InsecureKeyLengthWarning):  # signing with it: PyJWT only warns
        token = jwt.encode(
            {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 60},
            weak, algorithm="RS256", headers={"kid": "weak"},
        )  # fmt: skip
    assert refused(token, keys=StaticKeySet(jwks)) is AuthFailure.UNKNOWN_KEY  # verifying with it: refused


def test_critical_header_extensions_are_refused():
    assert refused(ISSUER.token(headers={"crit": ["exp"]})) is AuthFailure.MALFORMED


def test_a_required_token_type_is_enforced():
    assert refused(ISSUER.token(), policy={"token_type": "at+jwt"}) is AuthFailure.INVALID_CLAIMS
    assert verifier(token_type="at+jwt").verify(ISSUER.token(headers={"typ": "at+jwt"})).subject


# --------------------------------------------------------------------- claims


def test_an_expired_token_is_refused_but_clock_skew_is_tolerated():
    now = int(time.time())
    assert refused(ISSUER.token(iat=now - 3600, exp=now - 120)) is AuthFailure.EXPIRED
    assert verifier().verify(ISSUER.token(iat=now - 3600, exp=now - 10)).subject  # within the 30 s leeway


@pytest.mark.parametrize("claim", ["nbf", "iat"])
def test_a_token_used_before_it_is_valid_is_refused(claim):
    now = int(time.time())
    assert refused(ISSUER.token(**{claim: now + 600}, exp=now + 1200)) is AuthFailure.NOT_YET_VALID


@pytest.mark.parametrize("issuer", ["https://evil.example.test", TEST_ISSUER + "/", TEST_ISSUER.upper(), ""])
def test_a_token_from_another_issuer_is_refused(issuer):
    assert refused(ISSUER.token(iss=issuer)) in (AuthFailure.WRONG_ISSUER, AuthFailure.INVALID_CLAIMS)


def test_the_issuer_must_match_exactly():
    assert refused(ISSUER.token(iss=TEST_ISSUER + "/")) is AuthFailure.WRONG_ISSUER


@pytest.mark.parametrize("audience", ["another-api", ["another-api", "a-third"], TEST_AUDIENCE.upper()])
def test_a_token_for_another_audience_is_refused(audience):
    assert refused(ISSUER.token(aud=audience)) is AuthFailure.WRONG_AUDIENCE


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "aud", "iss"])
def test_a_token_missing_a_required_claim_is_refused(claim):
    assert refused(ISSUER.token(drop=[claim])) in (AuthFailure.INVALID_CLAIMS, AuthFailure.WRONG_ISSUER)


@pytest.mark.parametrize("subject", ["", "has space", "x" * 256, "tab\there"])
def test_an_invalid_subject_is_refused(subject):
    assert refused(ISSUER.token(subject)) is AuthFailure.INVALID_CLAIMS


def test_a_non_string_subject_is_refused():
    assert refused(ISSUER.token(sub=12345)) is AuthFailure.INVALID_CLAIMS


def test_a_token_issued_for_too_long_is_refused():
    now = int(time.time())
    assert refused(ISSUER.token(iat=now, exp=now + 25 * 3600)) is AuthFailure.INVALID_CLAIMS
    assert refused(ISSUER.token(), policy={"max_lifetime": timedelta(minutes=30)}) is AuthFailure.INVALID_CLAIMS


def test_another_issuers_token_is_refused_even_with_the_same_subject():
    """Identity is (issuer, subject): a second provider's "owner-7f3c" is somebody else."""
    other = TestIssuer(issuer="https://other-issuer.example.test")
    assert refused(other.token(), keys=StaticKeySet(ISSUER.jwks())) in (AuthFailure.UNKNOWN_KEY, AuthFailure.INVALID_SIGNATURE)


# ------------------------------------------------------------- key set documents


def _jwk(**changes):
    return {**ISSUER.jwks()["keys"][0], **changes}


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"keys": []},
        {"keys": "nope"},
        {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "hmac"}]},  # a symmetric key would let readers sign
        {"keys": [_jwk(d="private-part")]},  # a published key never carries private material
        {"keys": [_jwk(use="enc")]},
        {"keys": [_jwk(kid=None)]},
        {"keys": [_jwk(), _jwk()]},  # the same kid twice
        {"keys": [_jwk(kid=f"k{i}") for i in range(33)]},
    ],
)
def test_only_well_formed_public_signature_key_sets_are_accepted(document):
    with pytest.raises(InvalidKeySet):
        parse_jwks(document)


class FakeProvider:
    """The provider's JWKS endpoint, without a network: counts fetches, can fail, can rotate keys."""

    def __init__(self, issuer):
        self.issuer, self.calls, self.fail = issuer, 0, False

    def __call__(self, url, timeout):
        self.calls += 1
        if self.fail:
            raise OSError("connection refused")
        return json.dumps(self.issuer.jwks()).encode()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def remote(provider, clock, **kwargs):
    return RemoteKeySet("https://issuer.example.test/.well-known/jwks.json", fetch=provider, clock=clock, **kwargs)


def test_remote_keys_are_cached_and_refreshed_after_their_lifetime():
    provider, clock = FakeProvider(ISSUER), Clock()
    keys = remote(provider, clock, ttl_s=600)
    for _ in range(5):
        keys.key("ec-1", "ES256")
    assert provider.calls == 1
    clock.now += 601
    keys.key("ec-1", "ES256")
    assert provider.calls == 2


def test_a_rotated_key_is_fetched_once_but_unknown_keys_cannot_force_refetching():
    issuer = TestIssuer()
    provider, clock = FakeProvider(issuer), Clock()
    keys = remote(provider, clock, min_refresh_interval_s=60)
    keys.key("ec-1", "ES256")
    issuer.private["ec-2"] = (ec.generate_private_key(ec.SECP256R1()), "ES256")  # the provider rotates
    clock.now += 61
    assert keys.key("ec-2", "ES256")  # unknown kid: one refresh finds it
    calls = provider.calls
    for _ in range(10):  # a flood of made-up kids triggers no further fetches inside the interval
        with pytest.raises(Exception):
            keys.key("made-up", "ES256")
    assert provider.calls == calls


def test_keys_that_cannot_be_obtained_fail_closed():
    provider, clock = FakeProvider(ISSUER), Clock()
    provider.fail = True
    with pytest.raises(KeysUnavailable):
        remote(provider, clock).key("ec-1", "ES256")


def test_the_last_good_keys_survive_a_short_outage_but_not_a_long_one():
    provider, clock = FakeProvider(ISSUER), Clock()
    keys = remote(provider, clock, ttl_s=600, max_stale_s=3600)
    keys.key("ec-1", "ES256")
    provider.fail = True
    clock.now += 601  # expired, refresh fails: still within max_stale
    assert keys.key("ec-1", "ES256")
    clock.now += 3600  # beyond max_stale: fail closed
    with pytest.raises(KeysUnavailable):
        keys.key("ec-1", "ES256")


def test_a_remote_key_set_that_is_not_json_or_not_keys_fails_closed():
    clock = Clock()
    for body in (b"<html>", b'{"keys": [{"kty": "oct", "k": "AA", "kid": "x"}]}'):
        with pytest.raises(KeysUnavailable):
            remote(lambda url, timeout, body=body: body, clock).key("ec-1", "ES256")


def test_remote_keys_need_https():
    with pytest.raises(ValueError, match="https"):
        RemoteKeySet("http://issuer.example.test/jwks.json")
    with pytest.raises(KeysUnavailable, match="https"):
        fetch_https("http://issuer.example.test/jwks.json", 1)
