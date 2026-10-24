"""Deterministic scripted provider for tests and demos (no network, no keys)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .base import ModelProvider, ModelRequest, ModelResponse, ProviderError

Responder = Mapping[str, Any] | Callable[[ModelRequest], Mapping[str, Any]]


class FakeProvider(ModelProvider):
    """Returns scripted responses keyed by agent name and attempt number.

    ``script["developer"][0]`` answers the developer's first visit,
    ``[1]`` its second, and so on. Selection depends only on the request,
    never on hidden call counters, so a resumed run gets the same answers
    as an uninterrupted one. A responder may be a callable for responses
    that depend on the request inputs.
    """

    name = "fake"
    #: Scripted responses are written by repository maintainers, not a model.
    produces_trusted_code = True

    def __init__(
        self,
        script: Mapping[str, Sequence[Responder]],
        *,
        repeat_last: bool = False,
        model: str = "fake-deterministic-1",
    ) -> None:
        self.script = {agent: list(responses) for agent, responses in script.items()}
        self.repeat_last = repeat_last
        self.model = model
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        responses = self.script.get(request.agent)
        if not responses:
            raise ProviderError(f"fake provider has no script for agent {request.agent!r}")
        index = request.attempt - 1
        if index >= len(responses):
            if not self.repeat_last:
                raise ProviderError(
                    f"fake provider script for {request.agent!r} has {len(responses)} responses; "
                    f"attempt {request.attempt} requested"
                )
            index = len(responses) - 1
        responder = responses[index]
        content = responder(request) if callable(responder) else responder
        self.calls.append(request)
        return ModelResponse(
            content=copy.deepcopy(dict(content)),
            provider=self.name,
            model=self.model,
            usage={"input_tokens": 0, "output_tokens": 0},
        )

    def calls_for(self, agent: str) -> list[ModelRequest]:
        return [c for c in self.calls if c.agent == agent]
