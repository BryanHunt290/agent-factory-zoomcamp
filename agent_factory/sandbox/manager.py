"""SandboxManager: the fail-closed gate in front of every sandbox backend.

For each request it:

1. validates the request;
2. derives the required isolation level from the code's trust
   (UNTRUSTED -> CONTAINER or stronger);
3. considers only configured backends that qualify. A backend below the
   required level, a backend that is not a security sandbox (e.g.
   LocalProcessBackend) for untrusted code, or a test double not
   explicitly allowed is never used. There is no downgrade path;
4. skips backends marked unhealthy; on first use runs the backend's janitor
   and isolation self-test;
5. executes under a concurrency semaphore via run_sandboxed(), which always
   destroys and verifies cleanup.

If no qualifying backend can run the request it raises ExecutionDenied or
SandboxUnavailable (both SandboxBlocked) without starting any process. A
cleanup that cannot be verified marks the backend unhealthy, so later
requests are refused until recover() succeeds.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from .base import SandboxBackend, run_sandboxed
from .docker import DockerBackend, DockerConfig, DockerRunner
from .local import LocalProcessBackend
from .types import (
    CodeTrust,
    ExecutionDenied,
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxCleanupError,
    SandboxUnavailable,
    required_isolation,
)
from .validation import validate_request


class SandboxManager:
    def __init__(
        self,
        backends: Sequence[SandboxBackend],
        *,
        allow_test_doubles: bool = False,
        max_concurrent: int = 2,
    ) -> None:
        if not backends:
            raise ValueError("at least one backend must be configured")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.backends = tuple(backends)
        self.allow_test_doubles = allow_test_doubles
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._ready: set[int] = set()
        self._unhealthy: dict[int, str] = {}

    @classmethod
    def default(
        cls,
        *,
        docker_config: DockerConfig | None = None,
        docker_runner: DockerRunner | None = None,
    ) -> SandboxManager:
        """The default routing used by build_executor() and RunTestsTool.

        * Trusted fixtures (scripted FakeProvider content, template scaffolds)
          run with LocalProcessBackend, the first eligible backend; Docker is
          never contacted for them.
        * Untrusted code runs ONLY with DockerBackend and the approved image
          recorded in sandbox_image/image.lock.json. If Docker is unavailable,
          unhealthy, misconfigured, or the image is missing, stale, or not the
          approved one, the request is refused and the run BLOCKS.

        LocalProcessBackend is never eligible for untrusted code, so there is
        no fallback to ordinary subprocess execution.
        """
        from .config import docker_config_from_lock

        config = docker_config or docker_config_from_lock()
        return cls([LocalProcessBackend(), DockerBackend(config, runner=docker_runner)])

    # ---------------------------------------------------------------- policy

    def ineligibility(self, backend: SandboxBackend, trust: CodeTrust) -> str | None:
        """Why ``backend`` may not run code of this trust, or None if it may."""
        required = required_isolation(trust)
        if backend.is_test_double and not self.allow_test_doubles:
            return f"{backend.name}: test double not permitted"
        if trust != CodeTrust.TRUSTED_FIXTURE:
            if isinstance(backend, LocalProcessBackend):
                return f"{backend.name}: not a sandbox; never used for untrusted code"
            if not backend.is_security_sandbox:
                return f"{backend.name}: not a security sandbox"
        if backend.isolation_level < required:
            return f"{backend.name}: isolation {IsolationLevel(backend.isolation_level).name} < required {required.name}"
        return None

    def health(self) -> dict[str, str]:
        return {
            b.name: self._unhealthy.get(id(b), "ready" if id(b) in self._ready else "not yet checked")
            for b in self.backends
        }

    def _mark_unhealthy(self, backend: SandboxBackend, reason: str) -> None:
        with self._lock:
            self._unhealthy[id(backend)] = reason
            self._ready.discard(id(backend))

    def recover(self, backend: SandboxBackend) -> None:
        """Clear an unhealthy mark after the janitor verifies no leftovers remain."""
        backend.janitor()  # raises SandboxCleanupError if leftovers remain
        with self._lock:
            self._unhealthy.pop(id(backend), None)
            self._ready.discard(id(backend))  # force a fresh self-test

    def _ensure_ready(self, backend: SandboxBackend) -> None:
        with self._lock:
            if id(backend) in self._ready:
                return
        backend.janitor()
        status = backend.self_test()
        if not status.available:
            raise SandboxUnavailable(f"{backend.name}: " + ("; ".join(status.reasons) or "unavailable"))
        with self._lock:
            self._ready.add(id(backend))

    # -------------------------------------------------------------- execution

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        validate_request(request)
        required = required_isolation(request.trust)
        reasons: list[str] = []
        candidates = []
        for backend in self.backends:
            why = self.ineligibility(backend, request.trust)
            if why:
                reasons.append(why)
            else:
                candidates.append(backend)
        if not candidates:
            raise ExecutionDenied(
                f"no configured backend may run {request.trust} code (requires {required.name} or stronger): "
                + "; ".join(reasons)
            )

        for backend in candidates:
            if id(backend) in self._unhealthy:
                reasons.append(f"{backend.name}: unhealthy ({self._unhealthy[id(backend)]})")
                continue
            try:
                self._ensure_ready(backend)
            except SandboxCleanupError as exc:
                self._mark_unhealthy(backend, str(exc))
                reasons.append(f"{backend.name}: janitor failed ({exc})")
                continue
            except SandboxUnavailable as exc:
                reasons.append(str(exc))
                continue
            with self._semaphore:
                try:
                    return run_sandboxed(backend, request)
                except SandboxCleanupError as exc:
                    self._mark_unhealthy(backend, str(exc))
                    raise
        raise SandboxUnavailable("no qualifying sandbox backend is available: " + "; ".join(reasons))
