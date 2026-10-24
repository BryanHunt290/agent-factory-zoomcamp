"""Errors raised by the product domain model."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for product-domain errors."""


class DomainValidationError(DomainError, ValueError):
    """A record or value violates the domain model's rules."""


class SchemaVersionError(DomainValidationError):
    """A serialized record has a missing, invalid, or unsupported schema version."""


class StatusTransitionError(DomainError):
    """A status change that the record's state machine does not allow."""


class RecordNotFoundError(DomainError):
    """A referenced record does not exist."""


class ConflictError(DomainError):
    """A write conflicts with what is already recorded (e.g. a different second decision)."""


class ApprovalBindingError(ConflictError):
    """A decision was presented for a different action than the one requested."""


class ApprovalExpiredError(DomainError):
    """The approval request expired before a decision was made."""


class DecisionRefused(ApprovalBindingError):
    """A stored human decision is never applied (fail closed).

    It is bound to something other than the engine's exact pending action (the
    stored request, or the decision, is for a different action), or the person
    who made it may no longer decide. The run stays where it is; a person must
    investigate.
    """


class StorageError(Exception):
    """A product store could not complete an operation for an infrastructure reason.

    Not a rule violation: the same call may succeed once the store is healthy.
    Repositories translate their backend's errors into this hierarchy so that
    business logic never depends on a particular database driver.
    """


class StorageUnavailableError(StorageError):
    """The store cannot be reached (connection refused, lost, or timed out)."""


class SchemaNotReadyError(StorageError):
    """The database schema is missing, behind, or ahead of this Agent Factory: run ``db migrate``."""
