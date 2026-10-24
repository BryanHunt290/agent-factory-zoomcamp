"""The Agent Factory HTTP API: an implementation of the checked-in openapi.yaml.

Every product request is authenticated (an OpenID Connect access token) and
authorized (one permission per operation; ``auth/``, docs/authentication.md).
The server still listens on 127.0.0.1 only, refuses requests whose Host header
is not a loopback name, and must not be deployed or exposed until a deployment
and an identity provider exist (see docs/backend.md#local-only).

Layers, each depending only on the ones below it::

    routes/        HTTP: paths, parameters, status codes, headers      (FastAPI)
    schemas/       API models: the contract's request/response shapes (Pydantic)
    services/      use cases, one per operation: transactions,
                   pagination, idempotency, error codes; no HTTP
    domain         records, state machines, ApprovalService            (agent_factory.domain)
    repositories   storage interfaces                                  (agent_factory.domain.repositories)
    persistence    PostgreSQL repositories, connection pool            (agent_factory.persistence)

Cross-cutting: ``auth`` (authentication, permissions, rate limits),
``errors``/``handlers`` (RFC 9457 problems), ``middleware``
and ``observability`` (request IDs, logs), ``pagination`` (cursors),
``redaction`` (curated engine messages), ``dependencies`` (injection,
strict bodies), ``config`` and ``server`` (settings, uvicorn).

Nothing here runs graphs, sandboxes, or tools, and the engine does not
import this package: the API records intent (projects, decisions) in
PostgreSQL; workers act on it.
"""

from .app import CONTRACT_VERSION, create_app
from .config import ApiSettings, UnsafeBindError
from .stores import InMemoryStoreProvider, StoreProvider

__all__ = ["CONTRACT_VERSION", "ApiSettings", "InMemoryStoreProvider", "StoreProvider", "UnsafeBindError", "create_app"]
