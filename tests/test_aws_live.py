"""Stage E2b: the live verification of ``aws.identity.inspect`` (docs/aws-capabilities.md, section 10). Opt-in only.

    aws sso login --profile agent-factory-dev
    $env:AGENT_FACTORY_AWS_LIVE_AUTHORIZED_BY = "owner, <date>, Stage E2"
    .venv\\Scripts\\python -m pytest -m aws_live --run-aws-live tests/test_aws_live.py -v -s

**Not run until the owner authorizes Stage E2b.** Without ``--run-aws-live`` this test is skipped and the
conftest guard refuses every non-loopback name and connection. Requested, it fails rather than skips on any
missing configuration, and the conftest replaces that guard with one that allows only the three us-west-2
hosts in ``aws_live_support.LIVE_HOSTS`` (port 443) and no process but two read-only git commands.

What it sends: one SSO GetRoleCredentials exchange (possibly one OIDC token refresh), then two STS
GetCallerIdentity calls (the probe and the capability). All free, all in us-west-2. On success it appends
one record to ``evidence/live-capabilities.jsonl``, which the owner reviews and commits.

The harness and every failure path are tested offline in ``tests/test_aws_live_harness.py``.
"""

from __future__ import annotations

import logging
import os

import pytest

from aws_live_support import (
    APPROVED_PLAN,
    LiveVerificationFailed,
    authorization_from_environment,
    preflight,
    repository_state,
    run_identity_verification,
)

pytestmark = [pytest.mark.aws_live]


def test_identity_inspection_live(request, tmp_path, caplog):
    from agent_factory.capabilities.live_evidence import EVIDENCE_PATH
    from conftest import REPO_ROOT

    guard = getattr(request.node, "aws_live_guard", None)
    if guard is None:
        pytest.fail("the AWS live network guard is not installed: run with --run-aws-live")
    caplog.set_level(logging.INFO, logger="agent_factory")
    try:
        authorization = authorization_from_environment()
        commit, dirty = repository_state(REPO_ROOT)
        preflight(APPROVED_PLAN, environ=os.environ, evidence_path=EVIDENCE_PATH, commit=commit, dirty=dirty)
        if guard.resolved or guard.connected or guard.refused:
            pytest.fail("the pre-flight touched the network")
        report = run_identity_verification(authorization=authorization, commit=commit, evidence_path=EVIDENCE_PATH,
                                           guard=guard, workspace_root=tmp_path / "ws", log_text=lambda: caplog.text)  # fmt: skip
    except LiveVerificationFailed as exc:
        pytest.fail(f"live verification failed at {exc.step}: {exc.reason} (no evidence was written)")
    assert not guard.refused and set(guard.resolved) <= {h for h in guard.allowed_hosts}
    print(f"\naws live verification passed: availability {report.availability}, principal type {report.principal_type}, "
          f"verification {report.verification_id}; hosts contacted: {sorted(set(guard.resolved))}")
