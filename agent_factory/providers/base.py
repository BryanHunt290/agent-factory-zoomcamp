"""Model-provider interface (docs/model-providers.md).

Agents never call a provider themselves: they ask the run's
:class:`~agent_factory.providers.gateway.ModelGateway`, which builds a
:class:`ModelRequest`, checks it, budgets it, calls
:meth:`ModelProvider.complete`, and validates the answer. A provider only
turns one request into one :class:`ModelResponse` (a JSON object), or raises
a classified :class:`ProviderError`.

A request keeps three kinds of text apart, so that a provider adapter can
keep them apart too:

* ``instructions``: the versioned prompt template for the agent's role
  (trusted, written by maintainers);
* ``capabilities``: what the agent may do, derived from the permission policy
  (trusted, never from project content);
* ``inputs``: project data (the task, files, test output). **Untrusted.**
  Text in it is data, never instructions, however it is phrased.

The model is untrusted too. Whatever it returns is validated against
``output_schema``; schema validation is not authorization. Anything the
answer asks for still goes through the Tool Gateway.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


class ProviderError(RuntimeError):
    """A provider call that did not produce a usable answer. The message never holds credentials.

    ``transient``: the same request may succeed later (timeout, rate limit,
    overload, connection failure). ``unknown_usage``: the provider may have
    processed (and billed) the request although no answer arrived.
    """

    category = "provider_error"
    transient = False
    unknown_usage = False

    def __init__(self, message: str, *, retry_after: float | None = None, usage: dict[str, int] | None = None,
                 shape: dict[str, Any] | None = None, hint: str | None = None) -> None:  # fmt: skip
        super().__init__(message)
        self.retry_after = retry_after
        #: Tokens the provider reported for an answer it did return (a refusal, a malformed answer): recorded as spent.
        self.usage = dict(usage or {})
        #: The answer's structure for the audit (stop reason, block types, call counts): never text, never thinking.
        self.shape = dict(shape) if shape else None
        #: What the corrective re-ask tells the model, written by the adapter (fixed text, never model output).
        self.hint = hint


class ProviderTimeout(ProviderError):
    category = "timeout"
    transient = True
    unknown_usage = True  # the request may have been processed and billed


class ProviderRateLimited(ProviderError):
    category = "rate_limited"
    transient = True


class ProviderUnavailable(ProviderError):
    """Connection failure, 5xx, or overload."""

    category = "provider_unavailable"
    transient = True
    unknown_usage = True


class ProviderBadResponse(ProviderError):
    """The answer could not be parsed: not JSON, the wrong shape, truncated, or too large."""

    category = "malformed_response"


class ContextTooLarge(ProviderError):
    category = "context_too_large"


class ProviderConfigError(ProviderError):
    """Authentication, permission, or configuration refused by the provider: a person must fix it."""

    category = "provider_configuration"


class ProviderSpendLimit(ProviderConfigError):
    """A provider-side spend limit was reached. Never retried: the run blocks until a person raises the limit."""

    category = "spend_limit"


class ProviderRefusal(ProviderError):
    """The model declined the request. Its text is never treated as a result; the node fails (no re-ask)."""

    category = "refusal"


class ProviderRequestRejected(ProviderError):
    """The provider refused the request as invalid (a 4xx other than the above)."""

    category = "request_rejected"


@dataclass(frozen=True)
class ModelRequest:
    run_id: str
    agent: str
    purpose: str
    instructions: str
    inputs: dict[str, Any]
    output_schema: dict[str, Any]
    #: 1-based visit number of the calling node. Deterministic across resume.
    attempt: int
    #: The prompt template that produced ``instructions`` and ``output_schema`` (e.g. "planner/1").
    prompt_version: str = ""
    #: Unique per call: run, node, visit, and the call's number within the visit.
    request_id: str = ""
    #: 1-based number of this call within the node visit (a corrective re-ask is call 2).
    call: int = 1
    #: What the agent may do (from the permission policy). Trusted; never derived from project data.
    capabilities: dict[str, Any] = field(default_factory=dict)
    #: Hard caps for this one call.
    max_output_tokens: int = 4096
    timeout_s: float = 120.0


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "cached_tokens": self.cached_tokens}


@dataclass(frozen=True)
class ModelResponse:
    content: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    #: The answer's structure for the audit, when the provider reports one (the same safe fields as ProviderError.shape).
    shape: dict[str, Any] | None = None


class ModelProvider(ABC):
    name: str = "abstract"
    #: The model this provider answers with (recorded with every call).
    model: str = "unknown"
    #: Whether content from this provider may be treated as a trusted fixture
    #: (run without container isolation). Fail-closed default: False. Only
    #: providers whose output is authored by repository maintainers, such as
    #: the scripted FakeProvider, may set True. Real model providers never do.
    produces_trusted_code: ClassVar[bool] = False
    #: True for providers that send data off this machine and may cost money.
    remote: ClassVar[bool] = False

    @abstractmethod
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a JSON object for ``request``, or raise a ``ProviderError``."""


def provider_produces_trusted_code(provider: ModelProvider) -> bool:
    return getattr(provider, "produces_trusted_code", False) is True


__all__ = [
    "ContextTooLarge",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ProviderBadResponse",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRefusal",
    "ProviderRequestRejected",
    "ProviderSpendLimit",
    "ProviderTimeout",
    "ProviderUnavailable",
    "provider_produces_trusted_code",
]
