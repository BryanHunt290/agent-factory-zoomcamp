"""Database configuration: explicit sources, validation, and a password that is never shown."""

import pytest

from agent_factory.persistence.config import (
    DEFAULT_SCHEMA,
    TEST_PREFIX,
    DatabaseConfig,
    DatabaseConfigError,
)

PASSWORD = "s3cret-canary-pw"
URL = f"postgresql://agent_factory_app:{PASSWORD}@127.0.0.1:55432/agent_factory_dev"


def test_url_from_the_environment():
    config = DatabaseConfig.from_environment({"AGENT_FACTORY_DATABASE_URL": URL})
    assert (config.host, config.port, config.dbname, config.user) == ("127.0.0.1", 55432, "agent_factory_dev", "agent_factory_app")
    assert config.schema == DEFAULT_SCHEMA and config.source == "AGENT_FACTORY_DATABASE_URL"


def test_url_from_a_file_keeps_the_password_out_of_the_environment(tmp_path):
    secret = tmp_path / "database.url"
    secret.write_text(URL + "\n", encoding="utf-8")
    config = DatabaseConfig.from_environment(
        {"AGENT_FACTORY_DATABASE_URL_FILE": str(secret), "AGENT_FACTORY_DATABASE_SCHEMA": "af_dev"}
    )
    assert config.url == URL and config.schema == "af_dev"
    assert config.source == f"AGENT_FACTORY_DATABASE_URL_FILE={secret}"


@pytest.mark.parametrize(
    "environ, message",
    [
        ({}, "no database configured"),
        ({"AGENT_FACTORY_DATABASE_URL": URL, "AGENT_FACTORY_DATABASE_URL_FILE": "x"}, "set exactly one"),
        ({"AGENT_FACTORY_DATABASE_URL_FILE": "does-not-exist.url"}, "cannot read"),
        ({"AGENT_FACTORY_DATABASE_URL": "mysql://u:p@h/db"}, "postgresql://"),
        ({"AGENT_FACTORY_DATABASE_URL": "postgresql://u:p@h"}, "name a database"),
        ({"AGENT_FACTORY_DATABASE_URL": "postgresql://h/db"}, "name a user"),
        ({"AGENT_FACTORY_DATABASE_URL": "postgresql://u:p@h:notaport/db"}, "malformed"),
        ({"AGENT_FACTORY_DATABASE_URL": URL, "AGENT_FACTORY_DATABASE_SCHEMA": "Robert'); DROP"}, "lower-case identifier"),
    ],
)
def test_bad_configuration_is_refused(environ, message):
    with pytest.raises(DatabaseConfigError, match=message) as info:
        DatabaseConfig.from_environment(environ)
    assert PASSWORD not in str(info.value)


def test_an_empty_url_file_is_refused(tmp_path):
    empty = tmp_path / "empty.url"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(DatabaseConfigError, match="empty"):
        DatabaseConfig.from_environment({"AGENT_FACTORY_DATABASE_URL_FILE": str(empty)})


def test_the_password_is_never_shown():
    config = DatabaseConfig(url=URL, schema="agent_factory", source="test")
    for text in (repr(config), str(config), config.redacted(), f"{config}", f"{config!r}"):
        assert PASSWORD not in text and "***" in text
    assert PASSWORD not in repr([config]) and PASSWORD not in repr({"db": config})


def test_tests_never_pick_up_the_development_database():
    with pytest.raises(DatabaseConfigError, match="AGENT_FACTORY_TEST_DATABASE_URL"):
        DatabaseConfig.from_environment({"AGENT_FACTORY_DATABASE_URL": URL}, prefix=TEST_PREFIX)


def test_with_schema_changes_only_the_schema():
    config = DatabaseConfig(url=URL)
    other = config.with_schema("af_test_abc")
    assert other.schema == "af_test_abc" and other.url == config.url
    with pytest.raises(DatabaseConfigError):
        config.with_schema("public; DROP SCHEMA x")
