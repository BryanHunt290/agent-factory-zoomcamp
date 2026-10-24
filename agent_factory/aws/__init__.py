"""The AWS tool pack: typed capabilities behind the Tool Gateway, FAKE backend only (docs/aws-capabilities.md).

Nothing in this package imports an AWS SDK, runs a process, or opens a network connection.
"""

from .backend import OPERATIONS, AwsBackend, AwsBackendError, FakeAwsBackend, fake_object
from .credentials import CredentialMetadata, CredentialRefused, CredentialSlots, SlotBinding, check_credential
from .pack import AwsIdentityInspectTool, AwsPackSettings, AwsS3MetadataReadTool, AwsToolPack, aws_capabilities

__all__ = [
    "OPERATIONS",
    "AwsBackend",
    "AwsBackendError",
    "AwsIdentityInspectTool",
    "AwsPackSettings",
    "AwsS3MetadataReadTool",
    "AwsToolPack",
    "CredentialMetadata",
    "CredentialRefused",
    "CredentialSlots",
    "FakeAwsBackend",
    "SlotBinding",
    "aws_capabilities",
    "check_credential",
    "fake_object",
]
