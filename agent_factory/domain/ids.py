"""Stable identifiers for product records.

* Project IDs are random and opaque (``prj-`` + 20 hex). They never encode a
  filesystem path, so a workspace can move without changing who the project is.
* Principal IDs (``prn-`` + 20 hex) are random too: Agent Factory's own name
  for an authenticated identity, never derived from an email address.
* Run IDs keep the engine's existing format. They are unique within a project
  only, so runs are identified by ``(project_id, run_id)``.
* Milestone and decision IDs are derived deterministically from their
  identity, so recording the same thing twice yields the same ID (idempotent).
* Approval request IDs are the engine's existing exact-action IDs
  (``apr-`` + 16 hex, a hash of run, node, visit, tool, and parameters).
"""

from __future__ import annotations

import hashlib
import re
import secrets

from .serialization import canonical

PROJECT_ID_RE = re.compile(r"^prj-[0-9a-f]{20}$")
PRINCIPAL_ID_RE = re.compile(r"^prn-[0-9a-f]{20}$")
MILESTONE_ID_RE = re.compile(r"^ms-[0-9a-f]{20}$")
DECISION_ID_RE = re.compile(r"^apd-[0-9a-f]{20}$")
APPROVAL_ID_RE = re.compile(r"^apr-[0-9a-f]{16}$")
#: Same rule as the engine's run IDs (engine/checkpoint.py RUN_ID_PATTERN).
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: Milestone types: lower-case slugs, defined by projects.
TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Emission keys identify one milestone within a run (e.g. "step:3").
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
#: Template names and graph names.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: A client's Idempotency-Key header value (e.g. a UUID).
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def new_project_id() -> str:
    return "prj-" + secrets.token_hex(10)


def new_principal_id() -> str:
    return "prn-" + secrets.token_hex(10)


def _digest(*parts: str) -> str:
    return hashlib.sha256(canonical(list(parts)).encode("utf-8")).hexdigest()


def milestone_id(project_id: str, run_id: str, key: str) -> str:
    return "ms-" + _digest("milestone", project_id, run_id, key)[:20]


def decision_id(project_id: str, request_id: str) -> str:
    return "apd-" + _digest("decision", project_id, request_id)[:20]
