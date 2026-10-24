"""The AWS backend boundary: a narrow, typed interface, with only a fake behind it (docs/aws-capabilities.md).

Agents never see a backend. Tools in this pack call exactly one fixed operation each, through
``AwsBackend``. There is no generic "call any operation" method, no way to name an SDK method, no
script runner, and no CLI: the complete list of operations is ``OPERATIONS``, and a test pins it.

``FakeAwsBackend`` makes no network call, needs no credential, and returns controlled fake data
(including canaries that the result filter must remove). It is the default and the test backend.
``BotoAwsBackend`` (``aws/boto.py``, Stage E1) is the real one: activated only explicitly, it resolves one
approved temporary credential without any default chain and maps ``get_caller_identity`` to exactly
STS GetCallerIdentity. Before Stage E2's authorization it is tested only against botocore's Stubber.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from .credentials import CredentialMetadata, SlotBinding

#: Every AWS operation any backend may perform, by (service, operation). Nothing else exists.
OPERATIONS = frozenset({("sts", "GetCallerIdentity"), ("s3", "HeadObject")})
#: Canary values in fake responses: they must never survive the result filter.
FAKE_SECRET_CANARY = "AKIAFAKECANARY000001"
FAKE_SESSION_CANARY = "FwoGZXIvYXdzEFAKE-session-token-canary"
FAKE_ACCOUNT = "000000000000"


class AwsBackendError(Exception):
    """A failure with a fixed category; the message is fixed text, never the SDK's. Unknown categories become
    AWS_SERVICE_UNAVAILABLE, so nothing outside this list can reach a tool result, the API, or a model."""

    CATEGORIES = frozenset({
        "AWS_NOT_CONFIGURED", "AWS_IDENTITY_UNAVAILABLE", "AWS_PERMISSION_DENIED", "AWS_EXPIRED_CREDENTIAL",
        "AWS_UNSUPPORTED_REGION", "AWS_THROTTLED", "AWS_SERVICE_UNAVAILABLE", "AWS_RESPONSE_INVALID", "AWS_NOT_FOUND",
    })  # fmt: skip

    def __init__(self, category: str, detail: str = "") -> None:
        if category not in self.CATEGORIES:
            category = "AWS_SERVICE_UNAVAILABLE"
        self.category = category
        #: Fixed, secret-free text for trusted logs and availability details only (never an SDK message).
        self.detail = detail[:120]
        super().__init__(f"aws: {category}")


class AwsBackend(ABC):
    #: "fake" now; "boto" later.
    kind: ClassVar[str]

    @abstractmethod
    def credential_metadata(self, binding: SlotBinding) -> CredentialMetadata:
        """The kind of credential the bound profile resolves to. Never a value. Raises AwsBackendError(no_identity)."""

    @abstractmethod
    def get_caller_identity(self, binding: SlotBinding, region: str) -> Mapping[str, Any]:
        """STS GetCallerIdentity, and nothing else."""

    @abstractmethod
    def head_object(self, binding: SlotBinding, region: str, *, bucket: str, key: str) -> Mapping[str, Any]:
        """S3 HeadObject on one bucket and key, and nothing else."""


@dataclass
class FakeAwsBackend(AwsBackend):
    """Controlled fake responses. Records each call as (operation, slot, region, params) for tests."""

    kind: ClassVar[str] = "fake"
    credential_kind: str = "sso"
    temporary: bool = True
    #: How long the fake temporary credential stays valid; None means "no known expiry" (refused).
    expires_in: timedelta | None = timedelta(hours=1)
    identity: bool = True
    denied: frozenset[str] = frozenset()  # operations answered with AccessDenied
    regions: frozenset[str] = frozenset({"us-west-2", "us-east-1"})
    objects: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    def credential_metadata(self, binding: SlotBinding) -> CredentialMetadata:
        if not self.identity:
            raise AwsBackendError("AWS_IDENTITY_UNAVAILABLE")
        expires = None if self.expires_in is None else datetime.now(timezone.utc) + self.expires_in
        return CredentialMetadata(slot=binding.slot, profile=binding.profile, kind=self.credential_kind,
                                  temporary=self.temporary, expires_at=expires)  # fmt: skip

    def _enter(self, operation: str, binding: SlotBinding, region: str, **params: Any) -> None:
        self.calls.append((operation, binding.slot, region, dict(params)))
        if not self.identity:
            raise AwsBackendError("AWS_IDENTITY_UNAVAILABLE")
        if region not in self.regions:
            raise AwsBackendError("AWS_UNSUPPORTED_REGION")
        if operation in self.denied:
            raise AwsBackendError("AWS_PERMISSION_DENIED")

    def get_caller_identity(self, binding: SlotBinding, region: str) -> Mapping[str, Any]:
        self._enter("GetCallerIdentity", binding, region)
        return {
            "UserId": "AROAFAKEFAKEFAKEFAKE:fake-session",
            "Account": FAKE_ACCOUNT,
            "Arn": f"arn:aws:sts::{FAKE_ACCOUNT}:assumed-role/af-dev-identity/fake-session",
            "ResponseMetadata": {"RequestId": "fake-request", "HTTPHeaders": {"x-amz-security-token": FAKE_SESSION_CANARY}},
            # Never part of a real GetCallerIdentity answer: proves the filter keeps only known fields.
            "Credentials": {"AccessKeyId": FAKE_SECRET_CANARY, "SessionToken": FAKE_SESSION_CANARY},
        }

    def head_object(self, binding: SlotBinding, region: str, *, bucket: str, key: str) -> Mapping[str, Any]:
        self._enter("HeadObject", binding, region, bucket=bucket, key=key)
        found = self.objects.get((bucket, key))
        if found is None:
            raise AwsBackendError("AWS_NOT_FOUND")
        return found


def fake_object(size: int = 1234, **overrides: Any) -> dict[str, Any]:
    """A HeadObject-shaped fake answer with canaries in places a filter must clean."""
    answer: dict[str, Any] = {
        "ContentLength": size,
        "ContentType": "text/plain",
        "ETag": '"0123456789abcdef0123456789abcdef"',
        "LastModified": "2026-10-01T12:00:00Z",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": f"arn:aws:kms:us-west-2:{FAKE_ACCOUNT}:key/11111111-2222-3333-4444-555555555555",
        "Metadata": {"owner": "data-team", "x-session-token": FAKE_SESSION_CANARY, "note": f"key {FAKE_SECRET_CANARY}"},
        "ResponseMetadata": {"RequestId": "fake-request", "HTTPHeaders": {"x-amz-id-2": "fake"}},
    }
    answer.update(overrides)
    return answer


__all__ = [
    "FAKE_ACCOUNT",
    "FAKE_SECRET_CANARY",
    "FAKE_SESSION_CANARY",
    "OPERATIONS",
    "AwsBackend",
    "AwsBackendError",
    "FakeAwsBackend",
    "fake_object",
]
