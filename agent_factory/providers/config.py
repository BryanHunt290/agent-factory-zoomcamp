"""Which model provider a run uses: explicit, never inferred (docs/model-providers.md).

The default is the deterministic ``FakeProvider``: tests, demos, and workers
make no model calls. A real provider is used only when **all** of these are set:

* ``AGENT_FACTORY_MODEL_PROVIDER`` names it (today: ``anthropic``);
* ``AGENT_FACTORY_MODEL_MODE=live``: the deliberate "real model calls may
  leave this machine and cost money" switch;
* ``AGENT_FACTORY_MODEL_NAME``: the model, chosen by the owner (none is built in);
* ``AGENT_FACTORY_MODEL_API_KEY_FILE``: a file holding the API key. The key is
  read from that file only. A provider key that merely exists in the
  environment (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ...) is never read and
  never activates anything.

Budgets and (optional) prices come from ``AGENT_FACTORY_MODEL_*`` settings
too. Without prices, no dollar figure is ever claimed.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .base import ModelProvider, ProviderConfigError
from .budget import ModelBudget, Pricing

PREFIX = "AGENT_FACTORY_MODEL_"
REAL_PROVIDERS = frozenset({"anthropic"})
DEFAULT_BASE_URLS = {"anthropic": "https://api.anthropic.com"}


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "fake"
    live: bool = False
    model: str | None = None
    api_key_file: Path | None = None
    base_url: str | None = None
    timeout_s: float = 120.0
    budget: ModelBudget = field(default_factory=ModelBudget)

    @property
    def real(self) -> bool:
        return self.provider in REAL_PROVIDERS

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ModelConfig:
        env = os.environ if environ is None else environ

        def get(name: str) -> str | None:
            value = env.get(PREFIX + name, "").strip()
            return value or None

        provider = (get("PROVIDER") or "fake").lower()
        if provider != "fake" and provider not in REAL_PROVIDERS:
            raise ModelConfigError(f"unknown model provider {provider!r} (supported: fake, {', '.join(sorted(REAL_PROVIDERS))})")
        mode = (get("MODE") or "off").lower()
        if mode not in ("off", "live"):
            raise ModelConfigError("AGENT_FACTORY_MODEL_MODE must be 'off' or 'live'")
        budget = _budget(get)
        if provider == "fake":
            return cls(budget=budget)
        if mode != "live":
            raise ModelConfigError(
                f"{provider} is a real model provider: set AGENT_FACTORY_MODEL_MODE=live to allow real (possibly paid) calls"
            )
        model = get("NAME")
        if not model:
            raise ModelConfigError("set AGENT_FACTORY_MODEL_NAME: no model is chosen by default")
        key_file = get("API_KEY_FILE")
        if not key_file:
            raise ModelConfigError("set AGENT_FACTORY_MODEL_API_KEY_FILE to a file holding the API key (never the key itself)")
        path = Path(key_file)
        if not path.is_file():
            raise ModelConfigError("AGENT_FACTORY_MODEL_API_KEY_FILE does not name a readable file")
        base_url = check_base_url(get("BASE_URL") or DEFAULT_BASE_URLS[provider])
        timeout = float(get("TIMEOUT_S") or 120)
        if not 1 <= timeout <= 600:
            raise ModelConfigError("AGENT_FACTORY_MODEL_TIMEOUT_S must be between 1 and 600 seconds")
        return cls(provider=provider, live=True, model=model, api_key_file=path, base_url=base_url, timeout_s=timeout, budget=budget)

    def describe(self) -> dict[str, object]:
        """For ``model check`` and logs: never the key, only whether its file exists."""
        return {
            "provider": self.provider, "live": self.live, "model": self.model, "base_url": self.base_url,
            "api_key_file": "set" if self.api_key_file else None, "timeout_s": self.timeout_s,
            "budget": {k: v for k, v in vars(self.budget).items() if k != "pricing"},
            "pricing": "configured" if self.budget.pricing else "not configured (no dollar estimates)",
        }  # fmt: skip


def _budget(get) -> ModelBudget:
    def number(name: str, cast=int):
        value = get(name)
        if value is None:
            return None
        try:
            return cast(value)
        except ValueError:
            raise ModelConfigError(f"AGENT_FACTORY_MODEL_{name} must be a number") from None

    values = {
        "max_input_tokens_per_call": number("MAX_INPUT_TOKENS_PER_CALL"),
        "max_output_tokens_per_call": number("MAX_OUTPUT_TOKENS_PER_CALL"),
        "max_calls_per_visit": number("MAX_CALLS_PER_VISIT"),
        "max_calls_per_run": number("MAX_CALLS_PER_RUN"),
        "max_input_tokens_per_run": number("MAX_INPUT_TOKENS_PER_RUN"),
        "max_output_tokens_per_run": number("MAX_OUTPUT_TOKENS_PER_RUN"),
        "max_estimated_usd_per_run": number("MAX_USD_PER_RUN", float),
    }
    price_in, price_out = number("PRICE_INPUT_PER_MTOK", float), number("PRICE_OUTPUT_PER_MTOK", float)
    if (price_in is None) != (price_out is None):
        raise ModelConfigError("set both AGENT_FACTORY_MODEL_PRICE_INPUT_PER_MTOK and ..._OUTPUT_PER_MTOK, or neither")
    pricing = Pricing(price_in, price_out, number("PRICE_CACHED_INPUT_PER_MTOK", float)) if price_in is not None else None
    try:
        return ModelBudget(pricing=pricing, **{k: v for k, v in values.items() if v is not None})
    except ValueError as exc:
        raise ModelConfigError(str(exc)) from None


def check_base_url(url: str) -> str:
    """https, or plain http only to this machine (tests). No credentials, query, or fragment."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ModelConfigError("the model base URL must be an https URL without credentials, query, or fragment")
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if not loopback:
            raise ModelConfigError("a remote model endpoint must use https")
    return url.rstrip("/")


def build_provider(config: ModelConfig) -> ModelProvider:
    """The configured real provider. The fake provider is built by the caller (it needs a script)."""
    if not config.real:
        raise ProviderConfigError("no real model provider is configured (the default is the deterministic fake)")
    if not config.live:
        raise ProviderConfigError("real model calls are not enabled (AGENT_FACTORY_MODEL_MODE=live)")
    if config.provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            model=config.model or "", api_key=read_key(config.api_key_file), base_url=config.base_url or "",  # type: ignore[arg-type]
            timeout_s=config.timeout_s,
        )
    raise ProviderConfigError(f"provider {config.provider!r} has no adapter")


def read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderConfigError("the API key file cannot be read") from None
    if not key or "\n" in key or len(key) > 512:
        raise ProviderConfigError("the API key file must hold exactly one key")
    return key


__all__ = ["ModelConfig", "ModelConfigError", "build_provider", "check_base_url", "read_key"]
