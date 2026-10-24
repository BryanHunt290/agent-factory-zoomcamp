"""Stage B: sandbox types, validation, lifecycle driver, fake and local backends, RunTestsTool."""

import os
import socket
import subprocess
import sys
import time

import pytest
from conftest import ForbiddenOperation

from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionDenied,
    ExecutionRequest,
    ExecutionStatus,
    FakeOutcome,
    FakeSandboxBackend,
    IsolationLevel,
    LimitHit,
    LocalProcessBackend,
    RequestRejected,
    ResourceLimits,
    SandboxCleanupError,
    SandboxInfrastructureError,
    required_isolation,
    run_sandboxed,
)
from agent_factory.tools.testing import RunTestsTool, report_from_result
from agent_factory.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(
        tmp_path / "ws",
        scaffold={
            "hello.py": "import sys\nprint('out'); print('err', file=sys.stderr); sys.exit(3)\n",
            "spin.py": "while True:\n    pass\n",
            "flood.py": "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n",
            "envdump.py": "import os, json\nprint(json.dumps(sorted(os.environ)))\nprint(os.environ.get('AF_CANARY_SECRET', 'absent'))\n",
        },
    )


def _req(ws, command=("python", "hello.py"), trust=CodeTrust.TRUSTED_FIXTURE, **kw):
    kw.setdefault("env", dict(DEFAULT_SANDBOX_ENV))
    kw.setdefault("env_allowlist", DEFAULT_ENV_ALLOWLIST)
    return ExecutionRequest(execution_id="af-test-0001", workspace=ws, command=command, trust=trust, **kw)


# ---------------------------------------------------------------- types


def test_trust_to_isolation_mapping_fails_closed():
    assert required_isolation(CodeTrust.TRUSTED_FIXTURE) is IsolationLevel.NONE
    assert required_isolation(CodeTrust.UNTRUSTED) is IsolationLevel.CONTAINER
    assert required_isolation("something-else") is IsolationLevel.CONTAINER
    assert ExecutionRequest.__dataclass_fields__["trust"].default is CodeTrust.UNTRUSTED


@pytest.mark.parametrize(
    "kw",
    [
        {"timeout_s": 0},
        {"timeout_s": 99999},
        {"memory_mb": 1},
        {"disk_mb": 0},
        {"pids": 1},
        {"cpus": 0},
        {"output_bytes": 10},
        {"timeout_s": True},
    ],
)
def test_resource_limits_are_bounded(kw):
    with pytest.raises(ValueError):
        ResourceLimits(**kw)


def test_resource_limits_cpu_backstop():
    assert ResourceLimits(timeout_s=10.2).cpu_seconds == 16


# ----------------------------------------------------------- validation


@pytest.mark.parametrize(
    "change, message",
    [
        ({"execution_id": "BAD ID"}, "execution_id"),
        ({"command": ()}, "non-empty"),
        ({"command": ("bash", "-c", "rm -rf /")}, "must start with"),
        ({"command": ("C:/Python/python.exe", "x.py")}, "must start with"),
        ({"command": ("python", "a\x00b")}, "invalid command argument"),
        ({"command": ["python", "x.py"]}, "tuple"),
        ({"network": "host"}, "network policy"),
        ({"working_dir": "../outside"}, "working_dir"),
        ({"working_dir": "/etc"}, "working_dir"),
        ({"working_dir": "C:/Windows"}, "working_dir"),
        ({"working_dir": "a\\..\\b"}, "working_dir"),
        ({"env": {"PATH": "/x"}}, "not allowlisted"),
        ({"env": {"lower": "x"}, "env_allowlist": frozenset({"lower"})}, "invalid environment variable name"),
        ({"env": {"OPENAI_API_KEY": "sk"}, "env_allowlist": frozenset({"OPENAI_API_KEY"})}, "credential"),
        ({"env": {"GITHUB_TOKEN": "t"}, "env_allowlist": frozenset({"GITHUB_TOKEN"})}, "credential"),
        ({"env": {"DB_PASSWORD": "p"}, "env_allowlist": frozenset({"DB_PASSWORD"})}, "credential"),
        ({"env": {"LANG": "a\x00b"}}, "invalid value"),
        ({"trust": "superuser"}, "trust"),
    ],
)
def test_invalid_requests_are_rejected(ws, change, message):
    fields = {
        "execution_id": "af-test-0001",
        "workspace": ws,
        "command": ("python", "hello.py"),
        "env": dict(DEFAULT_SANDBOX_ENV),
        "env_allowlist": DEFAULT_ENV_ALLOWLIST,
        "trust": CodeTrust.TRUSTED_FIXTURE,
    }
    fields.update(change)
    backend = FakeSandboxBackend()
    with pytest.raises(RequestRejected, match=message):
        run_sandboxed(backend, ExecutionRequest(**fields))
    assert "execute" not in backend.calls


def test_request_summary_never_contains_env_values(ws):
    summary = _req(ws, env={"LANG": "secret-looking-value"}).summary()
    assert "secret-looking-value" not in repr(summary)
    assert summary["env_names"] == ["LANG"]


# ------------------------------------------------------- lifecycle driver


@pytest.mark.parametrize("stage", ["execute", "collect"])
def test_destroy_runs_once_when_a_stage_fails(ws, stage):
    backend = FakeSandboxBackend(fail_at=stage)
    with pytest.raises(SandboxInfrastructureError):
        run_sandboxed(backend, _req(ws))
    assert backend.calls.count("destroy") == 1


def test_prepare_failure_does_not_execute(ws):
    backend = FakeSandboxBackend(fail_at="prepare")
    with pytest.raises(SandboxInfrastructureError):
        run_sandboxed(backend, _req(ws))
    assert "execute" not in backend.calls


def test_unverified_cleanup_raises_even_after_success(ws):
    backend = FakeSandboxBackend(cleanup_verified=False)
    with pytest.raises(SandboxCleanupError):
        run_sandboxed(backend, _req(ws))


def test_unverified_cleanup_takes_precedence_over_execution_error(ws):
    backend = FakeSandboxBackend(fail_at="execute", cleanup_verified=False)
    with pytest.raises(SandboxCleanupError) as exc:
        run_sandboxed(backend, _req(ws))
    assert isinstance(exc.value.__cause__, SandboxInfrastructureError)


def test_destroy_raising_counts_as_unverified(ws):
    backend = FakeSandboxBackend(fail_at="destroy")
    with pytest.raises(SandboxCleanupError, match="destroy raised"):
        run_sandboxed(backend, _req(ws))


def test_successful_result_is_marked_cleanup_verified(ws):
    result = run_sandboxed(FakeSandboxBackend(), _req(ws))
    assert result.cleanup_verified and result.status is ExecutionStatus.COMPLETED


# ------------------------------------------------- local backend (fixtures)


def test_local_backend_is_explicitly_not_a_sandbox():
    backend = LocalProcessBackend()
    assert backend.isolation_level is IsolationLevel.NONE
    assert backend.is_security_sandbox is False
    assert "THIS IS NOT A SANDBOX" in sys.modules[LocalProcessBackend.__module__].__doc__
    assert "not a sandbox" in backend.info().runtime["warning"]
    assert set(backend.info().enforced_limits) == {LimitHit.TIME, LimitHit.OUTPUT}


def test_local_backend_refuses_untrusted_code_without_spawning(ws, monkeypatch):
    def no_spawn(*a, **k):
        raise AssertionError("a process was started for untrusted code")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    with pytest.raises(ExecutionDenied, match="not a sandbox"):
        run_sandboxed(LocalProcessBackend(), _req(ws, trust=CodeTrust.UNTRUSTED))


def test_local_backend_runs_trusted_fixture(ws):
    result = run_sandboxed(LocalProcessBackend(), _req(ws))
    assert result.status is ExecutionStatus.COMPLETED
    assert result.exit_code == 3
    assert result.stdout.strip() == "out" and result.stderr.strip() == "err"
    assert result.cleanup_verified and result.backend.name == "local-process"


def test_local_backend_enforces_time_limit(ws):
    t0 = time.monotonic()
    result = run_sandboxed(LocalProcessBackend(), _req(ws, ("python", "spin.py"), limits=ResourceLimits(timeout_s=1)))
    assert result.status is ExecutionStatus.TIMED_OUT and result.timed_out
    assert result.limits_hit == (LimitHit.TIME,)
    assert time.monotonic() - t0 < 10


def test_local_backend_caps_output(ws):
    limits = ResourceLimits(timeout_s=20, output_bytes=4096)
    result = run_sandboxed(LocalProcessBackend(), _req(ws, ("python", "flood.py"), limits=limits))
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED
    assert LimitHit.OUTPUT in result.limits_hit
    assert result.stdout_truncated and len(result.stdout.encode()) <= 4096


def test_local_backend_does_not_leak_host_secrets(ws, monkeypatch):
    monkeypatch.setenv("AF_CANARY_SECRET", "canary-1234")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    result = run_sandboxed(LocalProcessBackend(), _req(ws, ("python", "envdump.py")))
    assert "canary-1234" not in result.stdout and "absent" in result.stdout
    assert "OPENAI_API_KEY" not in result.stdout


def test_deadlines_do_not_use_wall_clock(ws, monkeypatch):
    # If any deadline used time.time(), this would raise.
    def no_wall_clock():
        raise AssertionError("wall clock used for a deadline")

    monkeypatch.setattr(time, "time", no_wall_clock)
    result = run_sandboxed(LocalProcessBackend(), _req(ws, ("python", "spin.py"), limits=ResourceLimits(timeout_s=0.5)))
    assert result.timed_out


def test_missing_interpreter_is_infrastructure_error(ws):
    with pytest.raises(SandboxInfrastructureError):
        run_sandboxed(LocalProcessBackend(python=os.path.join(str(ws.root), "no-python.exe")), _req(ws))


# -------------------------------------------------------- RunTestsTool


def test_run_tests_tool_maps_results_to_v01_report(ws):
    out = "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\nFAIL: test_x (tests.t.C.test_x)\n"
    tool = RunTestsTool(sandbox=lambda req: run_sandboxed(FakeSandboxBackend(outcome=FakeOutcome(exit_code=1, stderr=out)), req))
    result = tool.run(ws)
    report = result.data
    assert report["passed"] is False and report["tests_run"] == 2
    assert report["failures"] == ["tests.t.C.test_x"]
    assert report["sandbox"]["status"] == "completed" and report["sandbox"]["cleanup_verified"]


def test_limit_or_timeout_never_counts_as_pass(ws):
    passing_output = "Ran 5 tests in 0.01s\n\nOK\n"
    for outcome in (
        FakeOutcome(status=ExecutionStatus.TIMED_OUT, exit_code=0, stderr=passing_output, limits_hit=(LimitHit.TIME,)),
        FakeOutcome(status=ExecutionStatus.LIMIT_EXCEEDED, exit_code=0, stderr=passing_output, limits_hit=(LimitHit.MEMORY,)),
    ):
        report = report_from_result(run_sandboxed(FakeSandboxBackend(outcome=outcome), _req(ws)))
        assert report["passed"] is False
        assert "[sandbox]" in report["output"]


def test_zero_tests_is_not_a_pass(ws):
    report = report_from_result(run_sandboxed(FakeSandboxBackend(outcome=FakeOutcome(stderr="Ran 0 tests\n")), _req(ws)))
    assert report["passed"] is False


def test_run_tests_tool_takes_no_parameters(ws):
    from agent_factory.tools import ToolError

    with pytest.raises(ToolError):
        RunTestsTool().run(ws, command="rm -rf /")


# ------------------------------------------------------------- guards


def test_guard_blocks_docker_cli():
    with pytest.raises(ForbiddenOperation):
        subprocess.Popen(["docker", "version"])


def test_guard_blocks_network():
    with pytest.raises(ForbiddenOperation):
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)



def test_explicit_cpu_time_budget():
    assert ResourceLimits(cpu_time_s=7).cpu_seconds == 7
    assert ResourceLimits(timeout_s=10).cpu_seconds == 15  # default: backstop behind the deadline
    assert ResourceLimits(cpu_time_s=7).to_dict()["cpu_seconds"] == 7
    for bad in (0, -1, 86401, 2.5, True):
        with pytest.raises(ValueError, match="cpu_time_s"):
            ResourceLimits(cpu_time_s=bad)
