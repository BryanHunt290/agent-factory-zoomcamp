"""Validation of execution requests. Every backend calls this in prepare()."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType

from .types import CodeTrust, ExecutionRequest, NetworkPolicy, RequestRejected, ResourceLimits

EXECUTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
#: Names that look like credentials are rejected even if allowlisted. This includes
#: database connection settings (DATABASE_URL, *_DSN, PGPASSFILE, PGSERVICEFILE, ...):
#: the orchestrator's product database is never reachable from untrusted code. It also
#: includes the API's authentication settings (AGENT_FACTORY_AUTH_*) and anything named
#: like a token or an identity provider's key (JWT, BEARER, OIDC, JWK), every Agent Factory
#: setting (AGENT_FACTORY_*), and notification providers' account settings (AWS_*, TWILIO_*,
#: *_SID, SES/SNS/SMTP): docs/notifications.md.
SECRET_NAME_RE = re.compile(
    r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|CERT|DATABASE_URL|DSN|PGPASS|PGSERVICE"
    r"|JWT|BEARER|OIDC|JWK"
    r"|AGENT_FACTORY_|AWS_|TWILIO|SID|SES_|SNS_|SMTP"
)
#: A URL carrying credentials in its user-info part (``scheme://user:password@host``), under any name.
CREDENTIAL_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#\s]*@")
#: A value shaped like a JWT (base64url JSON header "eyJ..." and two more parts), under any name:
#: an access token never reaches untrusted code, however its variable is named.
JWT_VALUE_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")

#: Interpreters a test command may start with; each backend resolves them.
INTERPRETERS = frozenset({"python", "python3"})

MAX_ARGS = 64
MAX_ARG_LEN = 1024
MAX_ENV_VARS = 32
MAX_ENV_VALUE_LEN = 4096

#: Environment variables a sandboxed process may receive, with fixed values.
#: Values are set by the orchestrator, never copied from the host.
DEFAULT_SANDBOX_ENV: Mapping[str, str] = MappingProxyType(
    {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }
)
DEFAULT_ENV_ALLOWLIST = frozenset(DEFAULT_SANDBOX_ENV)


def validate_env(env: Mapping[str, str], allowlist: frozenset[str]) -> list[str]:
    problems = []
    if len(env) > MAX_ENV_VARS:
        problems.append(f"too many environment variables ({len(env)} > {MAX_ENV_VARS})")
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME_RE.match(name):
            problems.append(f"invalid environment variable name {name!r}")
            continue
        if SECRET_NAME_RE.search(name):
            problems.append(f"environment variable {name!r} looks like a credential and is never passed")
        if name not in allowlist:
            problems.append(f"environment variable {name!r} is not allowlisted")
        if not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE_LEN:
            problems.append(f"invalid value for environment variable {name!r}")
        elif CREDENTIAL_URL_RE.search(value):
            problems.append(f"environment variable {name!r} holds a URL with embedded credentials and is never passed")
        elif JWT_VALUE_RE.search(value):
            problems.append(f"environment variable {name!r} holds what looks like an access token and is never passed")
    for name in allowlist:
        if SECRET_NAME_RE.search(name):
            problems.append(f"allowlist entry {name!r} looks like a credential")
    return problems


def validate_command(command: tuple[str, ...]) -> list[str]:
    if not isinstance(command, tuple) or not command:
        return ["command must be a non-empty tuple of strings"]
    problems = []
    if len(command) > MAX_ARGS:
        problems.append(f"command has too many arguments ({len(command)} > {MAX_ARGS})")
    for arg in command:
        if not isinstance(arg, str) or "\x00" in arg or len(arg) > MAX_ARG_LEN:
            problems.append(f"invalid command argument {arg!r}")
    if isinstance(command[0], str) and command[0] not in INTERPRETERS:
        problems.append(f"command must start with one of {sorted(INTERPRETERS)}, not {command[0]!r}")
    return problems


def validate_relative_dir(path: str) -> list[str]:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path or ":" in path:
        return [f"invalid working_dir {path!r}"]
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return [f"working_dir {path!r} must be relative and stay inside the project"]
    return []


def validate_request(request: ExecutionRequest) -> None:
    """Raise :class:`RequestRejected` listing every problem with ``request``."""
    from ..workspace import Workspace

    problems: list[str] = []
    if not isinstance(request.execution_id, str) or not EXECUTION_ID_RE.match(request.execution_id):
        problems.append(f"invalid execution_id {request.execution_id!r}")
    if not isinstance(request.workspace, Workspace):
        problems.append("workspace must be a Workspace")
    if not isinstance(request.limits, ResourceLimits):
        problems.append("limits must be ResourceLimits")
    if request.network != NetworkPolicy.NONE:
        problems.append(f"network policy {request.network!r} is not permitted (only 'none')")
    try:
        CodeTrust(request.trust)
    except ValueError:
        problems.append(f"unknown trust value {request.trust!r}")
    problems += validate_command(request.command)
    problems += validate_relative_dir(request.working_dir)
    problems += validate_env(request.env, request.env_allowlist)
    if problems:
        raise RequestRejected("invalid execution request: " + "; ".join(problems))
