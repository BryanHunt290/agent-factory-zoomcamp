"""Authentication configuration fails closed; the development issuer can never act as production.

* Production is the default, and an unconfigured production API refuses to start.
* Development authentication needs an explicit opt-in, and is refused in production.
* Production and development settings can never be active together.
"""

import json

import pytest

pytest.importorskip("jwt", reason="authentication tests need PyJWT: run with .venv (requirements-api.txt)")

from auth_support import ISSUER  # noqa: E402

from agent_factory import cli  # noqa: E402
from agent_factory.api.auth import AuthConfigError, AuthenticationFailed, AuthFailure, AuthMode, AuthSettings  # noqa: E402
from agent_factory.api.auth import settings as auth_settings  # noqa: E402
from agent_factory.api.auth.development import (  # noqa: E402
    DEV_AUDIENCE, DEV_ISSUER, JWKS_FILE, PRIVATE_KEY_FILE, DevelopmentIssuer, DevelopmentIssuerError,
)
from agent_factory.api.auth.settings import build_authenticator, development_policy  # noqa: E402
from agent_factory.domain.repositories import in_memory_store  # noqa: E402

PRODUCTION = {
    auth_settings.ISSUER_ENV: "https://issuer.example.test",
    auth_settings.AUDIENCE_ENV: "agent-factory-api",
    auth_settings.JWKS_URL_ENV: "https://issuer.example.test/.well-known/jwks.json",
}


@pytest.fixture(autouse=True)
def _no_ambient_auth_settings(monkeypatch):
    """Tests decide every authentication setting themselves; none leaks in from the developer's shell."""
    for name in (auth_settings.MODE_ENV, *auth_settings.PRODUCTION_SETTINGS, *auth_settings.DEVELOPMENT_SETTINGS):
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------- production


def test_production_is_the_default_and_an_unconfigured_api_does_not_start():
    with pytest.raises(AuthConfigError, match="not configured"):
        AuthSettings.from_environment({})
    with pytest.raises(AuthConfigError, match="not configured"):
        AuthSettings.from_environment({auth_settings.MODE_ENV: "production"})


def test_a_complete_production_configuration():
    settings = AuthSettings.from_environment(PRODUCTION)
    assert settings.mode is AuthMode.PRODUCTION and settings.dev_issuer_dir is None
    assert (settings.policy.issuer, settings.policy.audience) == ("https://issuer.example.test", "agent-factory-api")
    assert settings.policy.algorithms == ("RS256", "ES256")


@pytest.mark.parametrize(
    "change",
    [
        {auth_settings.ISSUER_ENV: "http://issuer.example.test"},
        {auth_settings.ISSUER_ENV: "https://user:pass@issuer.example.test"},
        {auth_settings.ISSUER_ENV: DEV_ISSUER},
        {auth_settings.JWKS_URL_ENV: "http://issuer.example.test/jwks.json"},
        {auth_settings.JWKS_URL_ENV: "file:///etc/jwks.json"},
        {auth_settings.ALGORITHMS_ENV: "HS256"},
        {auth_settings.ALGORITHMS_ENV: "none"},
    ],
)
def test_production_refuses_anything_unsafe(change):
    with pytest.raises(AuthConfigError):
        AuthSettings.from_environment({**PRODUCTION, **change})


def test_production_refuses_the_development_issuer_in_every_form():
    with pytest.raises(AuthConfigError, match="refuses development settings|both"):
        AuthSettings.from_environment({auth_settings.DEV_ISSUER_DIR_ENV: ".local/dev-issuer"})  # mode defaults to production
    with pytest.raises(AuthConfigError, match="development issuer"):
        AuthSettings(AuthMode.PRODUCTION, development_policy(), jwks_url=PRODUCTION[auth_settings.JWKS_URL_ENV])
    with pytest.raises(AuthConfigError, match="development issuer"):
        AuthSettings(
            AuthMode.PRODUCTION, ISSUER.policy(issuer="https://issuer.example.test"),
            jwks_url=PRODUCTION[auth_settings.JWKS_URL_ENV], dev_issuer_dir=".local/dev-issuer",
        )  # fmt: skip


@pytest.mark.parametrize("mode", ["production", "development"])
def test_production_and_development_settings_are_never_active_together(mode):
    both = {**PRODUCTION, auth_settings.DEV_ISSUER_DIR_ENV: ".local/dev-issuer", auth_settings.MODE_ENV: mode}
    with pytest.raises(AuthConfigError, match="both production and development"):
        AuthSettings.from_environment(both)


def test_an_unknown_mode_is_refused():
    with pytest.raises(AuthConfigError, match="production' or 'development"):
        AuthSettings.from_environment({auth_settings.MODE_ENV: "insecure", **PRODUCTION})


# ---------------------------------------------------------------- development


def test_development_authentication_needs_an_explicit_opt_in(tmp_path):
    settings = AuthSettings.from_environment(
        {auth_settings.MODE_ENV: "development", auth_settings.DEV_ISSUER_DIR_ENV: str(tmp_path)}
    )
    assert settings.mode is AuthMode.DEVELOPMENT
    assert (settings.policy.issuer, settings.policy.audience) == (DEV_ISSUER, DEV_AUDIENCE)
    with pytest.raises(AuthConfigError, match="needs"):
        AuthSettings.from_environment({auth_settings.MODE_ENV: "development"})
    with pytest.raises(AuthConfigError, match="refuses production settings"):
        AuthSettings.from_environment({auth_settings.MODE_ENV: "development", auth_settings.ISSUER_ENV: "https://x.test"})


def test_a_development_token_verifies_in_development_mode_from_public_keys_only(tmp_path):
    issuer = DevelopmentIssuer.create(tmp_path)
    token = issuer.mint("dev-owner", display_name="Dev")
    (tmp_path / PRIVATE_KEY_FILE).unlink()  # the API process never needs the private key
    settings = AuthSettings.from_environment(
        {auth_settings.MODE_ENV: "development", auth_settings.DEV_ISSUER_DIR_ENV: str(tmp_path)}
    )
    verified = build_authenticator(settings).verifier.verify(token)
    assert (verified.issuer, verified.subject) == (DEV_ISSUER, "dev-owner")


def test_a_production_api_refuses_development_tokens_even_when_pointed_at_the_development_keys(tmp_path):
    """The worst misconfiguration: production's JWKS URL serves the development issuer's public keys."""
    issuer = DevelopmentIssuer.create(tmp_path)
    token = issuer.mint("dev-owner")
    dev_keys = json.dumps(issuer.jwks()).encode()
    settings = AuthSettings.from_environment({**PRODUCTION, auth_settings.ALGORITHMS_ENV: "ES256"})
    authenticator = build_authenticator(settings, fetch=lambda url, timeout: dev_keys)
    with pytest.raises(AuthenticationFailed) as caught:
        authenticator.authenticate([f"Bearer {token}"], "127.0.0.1", in_memory_store())
    assert caught.value.failure is AuthFailure.WRONG_ISSUER


def test_the_development_issuer_never_overwrites_and_mints_only_short_lived_tokens(tmp_path):
    from datetime import timedelta

    issuer = DevelopmentIssuer.create(tmp_path)
    with pytest.raises(DevelopmentIssuerError, match="refusing to replace"):
        DevelopmentIssuer.create(tmp_path)
    with pytest.raises(DevelopmentIssuerError, match="one day"):
        issuer.mint("x", lifetime=timedelta(days=2))
    assert json.loads((tmp_path / JWKS_FILE).read_text(encoding="utf-8")) == issuer.jwks()
    assert "d" not in issuer.jwks()["keys"][0]  # the published set holds only the public key


def test_the_development_cli_works_only_in_development_mode_and_never_prints_tokens(tmp_path, monkeypatch, capsys):
    directory, out = tmp_path / "dev-issuer", tmp_path / "token.txt"
    with pytest.raises(SystemExit, match="DEVELOPMENT ONLY"):
        cli.main(["auth", "dev-issuer", "init", "--dir", str(directory)])
    assert not directory.exists()
    monkeypatch.setenv(auth_settings.MODE_ENV, "development")
    assert cli.main(["auth", "dev-issuer", "init", "--dir", str(directory)]) == 0
    assert cli.main(["auth", "dev-issuer", "token", "--subject", "dev-owner", "--out", str(out), "--dir", str(directory)]) == 0
    token = out.read_text(encoding="ascii").strip()
    assert token not in capsys.readouterr().out
    settings = AuthSettings.from_environment(
        {auth_settings.MODE_ENV: "development", auth_settings.DEV_ISSUER_DIR_ENV: str(directory)}
    )
    assert build_authenticator(settings).verifier.verify(token).subject == "dev-owner"


def test_the_api_server_does_not_start_without_an_authentication_configuration(capsys):
    assert cli.main(["api", "serve"]) == 2
    assert "authentication is not configured" in capsys.readouterr().err
