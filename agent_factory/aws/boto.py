"""BotoAwsBackend: the real AWS backend, explicitly activated, with no default credential chain (docs/aws-capabilities.md).

Stage E1: written and tested OFFLINE only (botocore's Stubber and synthetic config files). It has never
contacted AWS. Its first live call (Stage E2) needs the owner's authorization.

How it differs from "just use boto3":

* **No default session, no default chain.** It never calls ``boto3.client``, ``boto3.Session()``,
  ``get_credentials`` or ``create_credential_resolver``. The default chain would try environment keys,
  ``~/.aws/credentials`` static keys, a credential process, the container endpoint, and the instance
  metadata service (169.254.169.254), in that order, and silently use whichever answers first.
* **One named profile, one approved provider.** The slot's binding names a profile. It must be on the
  backend's explicit allowlist, and it must belong to the backend's environment. Its configuration KEYS
  (never values) are classified first: static keys, a credential process, a role chain, or anything
  unknown are refused before any provider runs. Then exactly one botocore provider, the one whose
  ``METHOD`` matches the approved kind, is built for that profile (``ProfileProviderBuilder``, with
  environment variables disabled). No other provider exists in the resolution, so there is nothing to
  fall back to.
* **Temporary and unexpired only.** The resolved credential must report the approved ``method``, carry a
  session token, and expire more than a minute from now (``check_credential``).
* **Explicit client.** The STS client is created with the resolved credentials passed explicitly, the
  explicit region, short timeouts, at most two attempts, and configured endpoint overrides ignored. Its
  endpoint must be ``https://sts.<region>.amazonaws.com``.
* **One operation.** ``get_caller_identity`` calls STS GetCallerIdentity and nothing else; the answer is
  reduced to ``Account``, ``Arn``, and ``UserId`` before it leaves (the tool reduces it further).
  ``head_object`` is not implemented for the real backend (Stage F): it refuses.
* **Fixed errors.** Every failure becomes one ``AwsBackendError`` category. The trusted log gets the
  exception class and AWS error code only, never a message (it may contain an ARN) or a credential.

Credential kinds, by botocore 1.43.57's provider labels (verified in its source):

* ``sso`` (IAM Identity Center): accepted locally. Resolving it reads the SSO token cache and, when the
  role credentials are not cached, calls the SSO portal (GetRoleCredentials) in the profile's SSO region.
* ``login`` (``aws login``): classified and accepted, but botocore needs ``awscrt`` (``botocore[crt]``),
  which is not in requirements-aws.txt: resolution fails closed as AWS_NOT_CONFIGURED.
* ``assume-role`` from a source profile, ``assume-role`` with a workload ``credential_source``, and web
  identity: refused in E1. A source profile could resolve to static keys (botocore checks them first),
  and workload roles belong to the deployment stage.
* static keys, a credential process, anything unknown: refused.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from .backend import AwsBackend, AwsBackendError
from .credentials import (
    OUT_OF_SCOPE_PROFILES,
    CredentialMetadata,
    CredentialRefused,
    SlotBinding,
    check_credential,
    slot_environment,
)

LOG = logging.getLogger("agent_factory.aws")
#: Where the session "finds" the shared credentials file: nowhere (a path inside a directory that never exists).
NO_CREDENTIALS_FILE = Path(__file__).resolve().parent / "__never_a_directory__" / "credentials"
#: Profile kinds this backend accepts, by provider METHOD. Everything else is refused before resolution.
ACCEPTED_KINDS = frozenset({"sso", "login"})
_STATIC_KEYS = frozenset({"aws_access_key_id", "aws_secret_access_key", "aws_session_token"})
_CLIENT_CODES = {
    "AccessDenied": "AWS_PERMISSION_DENIED", "AccessDeniedException": "AWS_PERMISSION_DENIED",
    "UnauthorizedOperation": "AWS_PERMISSION_DENIED",
    "ExpiredToken": "AWS_EXPIRED_CREDENTIAL", "ExpiredTokenException": "AWS_EXPIRED_CREDENTIAL",
    "RequestExpired": "AWS_EXPIRED_CREDENTIAL",
    "InvalidClientTokenId": "AWS_IDENTITY_UNAVAILABLE", "UnrecognizedClientException": "AWS_IDENTITY_UNAVAILABLE",
    "SignatureDoesNotMatch": "AWS_IDENTITY_UNAVAILABLE", "IncompleteSignature": "AWS_IDENTITY_UNAVAILABLE",
    "InvalidAccessKeyId": "AWS_IDENTITY_UNAVAILABLE", "AuthFailure": "AWS_IDENTITY_UNAVAILABLE",
    "Throttling": "AWS_THROTTLED", "ThrottlingException": "AWS_THROTTLED", "RequestLimitExceeded": "AWS_THROTTLED",
    "TooManyRequestsException": "AWS_THROTTLED",
    "RegionDisabledException": "AWS_UNSUPPORTED_REGION",
    "ServiceUnavailable": "AWS_SERVICE_UNAVAILABLE", "InternalFailure": "AWS_SERVICE_UNAVAILABLE",
    "InternalError": "AWS_SERVICE_UNAVAILABLE",
}  # fmt: skip
_EXCEPTION_CATEGORIES = {
    "UnauthorizedSSOTokenError": "AWS_EXPIRED_CREDENTIAL", "LoginRefreshRequired": "AWS_EXPIRED_CREDENTIAL",
    "SSOTokenLoadError": "AWS_IDENTITY_UNAVAILABLE", "LoginTokenLoadError": "AWS_IDENTITY_UNAVAILABLE",
    "LoginError": "AWS_IDENTITY_UNAVAILABLE", "LoginInsufficientPermissions": "AWS_PERMISSION_DENIED",
    "NoCredentialsError": "AWS_IDENTITY_UNAVAILABLE", "PartialCredentialsError": "AWS_IDENTITY_UNAVAILABLE",
    "CredentialRetrievalError": "AWS_IDENTITY_UNAVAILABLE", "TokenRetrievalError": "AWS_IDENTITY_UNAVAILABLE",
    "ProfileNotFound": "AWS_NOT_CONFIGURED", "InvalidConfigError": "AWS_NOT_CONFIGURED",
    "MissingDependencyException": "AWS_NOT_CONFIGURED",
    "NoRegionError": "AWS_UNSUPPORTED_REGION", "UnknownRegionError": "AWS_UNSUPPORTED_REGION",
    "EndpointConnectionError": "AWS_SERVICE_UNAVAILABLE", "ConnectTimeoutError": "AWS_SERVICE_UNAVAILABLE",
    "ReadTimeoutError": "AWS_SERVICE_UNAVAILABLE", "ConnectionClosedError": "AWS_SERVICE_UNAVAILABLE",
    "HTTPClientError": "AWS_SERVICE_UNAVAILABLE",
}  # fmt: skip


def classify_profile(profile_config: Mapping[str, Any]) -> str:
    """The credential kind a profile's configuration KEYS describe. Values are never inspected."""
    keys = set(profile_config)
    if keys & _STATIC_KEYS:
        return "static"
    if "credential_process" in keys:
        return "process"
    if "web_identity_token_file" in keys:
        return "web-identity"
    if "role_arn" in keys:
        return "workload-assume-role" if "credential_source" in keys else "assume-role-chain"
    if "login_session" in keys:
        return "login"
    if {"sso_account_id", "sso_role_name"} <= keys and keys & {"sso_session", "sso_start_url"}:
        return "sso"
    return "unknown"


_REFUSALS = {
    "static": "long-term access keys are refused; use a temporary credential (IAM Identity Center)",
    "process": "unsupported credential type: a credential process is refused",
    "web-identity": "unsupported credential type: web identity belongs to the deployment stage",
    "workload-assume-role": "unsupported credential type: workload roles belong to the deployment stage",
    "assume-role-chain": "unsupported credential type: a role chain could source long-term keys",
    "unknown": "unsupported credential type",
}


def map_error(exc: BaseException) -> AwsBackendError:
    """One fixed category for any failure. Never the exception's message."""
    if isinstance(exc, AwsBackendError):
        return exc
    name = type(exc).__name__
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = str((response.get("Error") or {}).get("Code") or "")[:64]
        category = _CLIENT_CODES.get(code)
        if category is None:
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            category = "AWS_SERVICE_UNAVAILABLE" if not isinstance(status, int) or status >= 500 else "AWS_RESPONSE_INVALID"
    else:
        category = next((c for n, c in _EXCEPTION_CATEGORIES.items() if n == name or
                         any(base.__name__ == n for base in type(exc).__mro__)), "AWS_SERVICE_UNAVAILABLE")  # fmt: skip
    LOG.warning("aws operation failed: %s %s -> %s", name, code, category)  # class and code only, never a message
    return AwsBackendError(category, f"{name} {code}".strip())


class BotoAwsBackend(AwsBackend):
    kind: ClassVar[str] = "boto"

    def __init__(self, *, environment: str, allowed_profiles: frozenset[str], allowed_regions: frozenset[str],
                 credential_loader: Callable[[Any, str, str], Any] | None = None,
                 client_hook: Callable[[Any], None] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:  # fmt: skip
        """``credential_loader`` and ``client_hook`` exist for offline tests only: the loader replaces the real
        provider's load (it receives the session, the profile, and the approved kind), and the hook lets a
        test attach botocore's Stubber to the STS client. Production passes neither."""
        if environment not in ("dev", "staging", "prod"):
            raise ValueError("environment must be dev, staging, or prod")
        profiles = frozenset(allowed_profiles)
        if not profiles or profiles & OUT_OF_SCOPE_PROFILES:
            raise ValueError("an explicit allowlist of approved profiles is required, without default or internal-dev")
        if not allowed_regions:
            raise ValueError("an explicit set of allowed regions is required")
        self.environment = environment
        self.allowed_profiles = profiles
        self.allowed_regions = frozenset(allowed_regions)
        self._loader = credential_loader
        self._client_hook = client_hook
        self._clock = clock
        self._resolved: dict[str, tuple[Any, Any, CredentialMetadata]] = {}

    # ------------------------------------------------------------------ credentials

    def _session(self, profile: str) -> Any:
        import botocore.session

        # An explicit profile: botocore disables environment-variable credentials for it. Credentials are never
        # resolved through this session's default chain; only the single provider built below is used.
        session = botocore.session.Session(profile=profile)
        # botocore merges the shared credentials file into its configuration. That file holds long-term keys
        # (the owner's out-of-scope profiles among them), which Agent Factory never uses, so it is never even
        # opened: the session is pointed at a path that cannot exist.
        session.set_config_variable("credentials_file", str(NO_CREDENTIALS_FILE))
        return session

    def _load(self, session: Any, profile: str, kind: str) -> Any:
        if self._loader is not None:
            return self._loader(session, profile, kind)
        from botocore.credentials import ProfileProviderBuilder

        providers = [p for p in ProfileProviderBuilder(session).providers(profile_name=profile, disable_env_vars=True)
                     if p.METHOD == kind]  # fmt: skip
        if len(providers) != 1:
            raise AwsBackendError("AWS_NOT_CONFIGURED", "no single provider for the approved kind")
        return providers[0].load()

    def _resolve(self, binding: SlotBinding) -> tuple[Any, Any, CredentialMetadata]:
        if slot_environment(binding.slot) != self.environment:
            raise AwsBackendError("AWS_NOT_CONFIGURED", "the slot belongs to another environment")
        if binding.profile not in self.allowed_profiles:
            raise AwsBackendError("AWS_NOT_CONFIGURED", "the profile is not on the backend's allowlist")
        cached = self._resolved.get(binding.slot)
        if cached is not None:
            try:
                check_credential(binding, cached[2], now=self._clock())
                return cached
            except CredentialRefused:
                self._resolved.pop(binding.slot, None)
        try:
            session = self._session(binding.profile)
            profile_config = (session.full_config.get("profiles") or {}).get(binding.profile)
            if profile_config is None:
                raise AwsBackendError("AWS_NOT_CONFIGURED", "the profile does not exist")
            kind = classify_profile(profile_config)
            if kind not in ACCEPTED_KINDS:
                raise AwsBackendError("AWS_IDENTITY_UNAVAILABLE", _REFUSALS.get(kind, _REFUSALS["unknown"]))
            credentials = self._load(session, binding.profile, kind)
            if credentials is None:
                raise AwsBackendError("AWS_IDENTITY_UNAVAILABLE", "no credential for the profile")
            if getattr(credentials, "method", None) != kind:
                raise AwsBackendError("AWS_IDENTITY_UNAVAILABLE", "unsupported credential type")
            frozen = credentials.get_frozen_credentials()
            expiry = getattr(credentials, "_expiry_time", None)
        except AwsBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - every SDK failure becomes one fixed category
            raise map_error(exc) from None
        metadata = CredentialMetadata(slot=binding.slot, profile=binding.profile, kind=kind,
                                      temporary=bool(getattr(frozen, "token", None)),
                                      expires_at=expiry if isinstance(expiry, datetime) else None)  # fmt: skip
        try:
            check_credential(binding, metadata, now=self._clock())
        except CredentialRefused as exc:
            category = "AWS_EXPIRED_CREDENTIAL" if "expire" in exc.reason else "AWS_IDENTITY_UNAVAILABLE"
            raise AwsBackendError(category, exc.reason) from None
        self._resolved[binding.slot] = (session, frozen, metadata)
        return session, frozen, metadata

    def credential_metadata(self, binding: SlotBinding) -> CredentialMetadata:
        return self._resolve(binding)[2]

    # ------------------------------------------------------------------ operations

    def _sts_client(self, session: Any, frozen: Any, region: str) -> Any:
        from botocore.config import Config

        client = session.create_client(
            "sts", region_name=region,
            aws_access_key_id=frozen.access_key, aws_secret_access_key=frozen.secret_key,
            aws_session_token=frozen.token,
            config=Config(retries={"total_max_attempts": 2, "mode": "standard"}, connect_timeout=5, read_timeout=10,
                          ignore_configured_endpoint_urls=True, user_agent_extra="agent-factory"),
        )  # fmt: skip
        if client.meta.endpoint_url != f"https://sts.{region}.amazonaws.com":
            raise AwsBackendError("AWS_NOT_CONFIGURED", "unexpected STS endpoint")
        if self._client_hook is not None:
            self._client_hook(client)
        return client

    def get_caller_identity(self, binding: SlotBinding, region: str) -> Mapping[str, Any]:
        if region not in self.allowed_regions:
            raise AwsBackendError("AWS_UNSUPPORTED_REGION", "region not allowed")
        session, frozen, _ = self._resolve(binding)
        try:
            client = self._sts_client(session, frozen, region)
            response = client.get_caller_identity()
        except AwsBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - as above
            raise map_error(exc) from None
        if not isinstance(response, Mapping):
            raise AwsBackendError("AWS_RESPONSE_INVALID", "not a mapping")
        answer = {k: response.get(k) for k in ("Account", "Arn", "UserId")}
        if not all(isinstance(v, str) and 0 < len(v) <= 2048 for v in answer.values()):
            raise AwsBackendError("AWS_RESPONSE_INVALID", "missing or malformed identity fields")
        return answer

    def head_object(self, binding: SlotBinding, region: str, *, bucket: str, key: str) -> Mapping[str, Any]:
        raise AwsBackendError("AWS_NOT_CONFIGURED", "S3 is not implemented for the real backend before Stage F")


__all__ = ["ACCEPTED_KINDS", "BotoAwsBackend", "classify_profile", "map_error"]
