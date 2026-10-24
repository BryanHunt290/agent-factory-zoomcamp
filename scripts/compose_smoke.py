"""Exercise a fresh, isolated Compose reviewer stack; retain all volumes on exit.

This opt-in integration check approves only the fixed calculator fixture's
legacy_calc.py deletion. It refuses every existing Compose project resource,
never prints credentials, and never runs untrusted code on the host.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import reviewer

ROOT = Path(__file__).resolve().parent.parent
RUN = "review-approval"
SERVICES = {"prepare", "db", "app"}


class SmokeError(RuntimeError):
    """A bounded, credential-free integration failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def terminate_command(process) -> None:
    """Stop only this command's owned process tree, including Compose's plugin."""
    if os.name == "nt":
        if process.poll() is None:
            result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                    capture_output=True, timeout=20, check=False)
            require(result.returncode == 0 or process.poll() is not None, "Could not stop the owned Docker command tree.")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.communicate(timeout=20)


def command(args: list[str], env: dict[str, str], *, timeout: int = 120, label: str = "Docker command") -> str:
    try:
        process = subprocess.Popen(args, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != "nt",
                                   creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
    except OSError:
        raise SmokeError(label + " could not start.") from None
    try:
        stdout, _stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            terminate_command(process)
        except (OSError, subprocess.SubprocessError):
            raise SmokeError(label + " could not confirm its command-tree cleanup.") from None
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SmokeError(label + " timed out; its owned command tree was stopped.") from None
    # Do not relay Docker inspect/exec output or raw exceptions into CI logs.
    require(process.returncode == 0, label + " failed (exit %d); inspect this isolated project locally." % process.returncode)
    return stdout


def local_endpoint(endpoint: str) -> bool:
    if re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_.-]+", endpoint):
        return True
    parsed = urlsplit(endpoint)
    return (parsed.scheme == "unix" and not parsed.netloc and parsed.path.startswith("/")
            and not parsed.path.startswith("//") and not parsed.query and not parsed.fragment)


class Stack:
    def __init__(self, project: str, port: int):
        require(bool(re.fullmatch(r"af-smoke-[a-z0-9]{8,32}", project)), "Use a fresh af-smoke-<8..32 lowercase letters/digits> project.")
        require(1024 <= port <= 65535, "Dashboard port must be between 1024 and 65535.")
        self.project, self.port = project, port
        self.env = {k: v for k, v in reviewer.clean_env().items() if not k.upper().startswith("COMPOSE_")}
        self.env["AF_DASHBOARD_PORT"] = str(port)
        self.prefix = ["docker", "compose", "--file", str(ROOT / "compose.yaml"), "--project-name", project]
        self.endpoint = None
        self.created = False

    def docker(self, *args: str, timeout: int = 120) -> str:
        prefix = ["docker", "--host", self.endpoint] if self.endpoint else ["docker"]
        return command([*prefix, *args], self.env, timeout=timeout, label="docker " + args[0])

    def compose(self, *args: str, timeout: int = 120) -> str:
        return command([*self.prefix, *args], self.env, timeout=timeout, label="docker compose " + args[0])

    def preflight(self) -> None:
        context = json.loads(self.docker("context", "inspect"))
        endpoint = context[0]["Endpoints"]["docker"]["Host"]
        require(local_endpoint(endpoint), "Smoke checks require a local Docker socket.")
        self.endpoint = endpoint
        self.prefix[1:1] = ["--host", endpoint]  # Pin the inspected endpoint for every later operation.
        require(self.docker("info", "--format", "{{.OSType}}").strip() == "linux", "Docker must run Linux containers.")
        label = "label=com.docker.compose.project=" + self.project
        for kind, flags in (("container", ["ls", "--all", "--quiet"]), ("volume", ["ls", "--quiet"]), ("network", ["ls", "--quiet"])):
            require(not self.docker(kind, *flags, "--filter", label).strip(), "Existing Compose resources refused; choose a new smoke project.")
            names = self.docker(kind, "ls", *( ["--all"] if kind == "container" else []),
                                "--format", "{{.Names}}" if kind == "container" else "{{.Name}}")
            require(not any(name.startswith((self.project + "-", self.project + "_")) for name in names.splitlines()),
                    "Existing resource names refused even without Compose ownership labels.")
        reviewer.port_available(self.port)

    def start(self, *, build: bool = True) -> None:
        self.preflight()
        # A failed build/up can still leave newly created resources; final cleanup
        # only stops this previously empty, uniquely named project.
        self.created = True
        args = ["up", "--detach", "--wait", "--wait-timeout", "180"]
        args.append("--build" if build else "--no-build")
        self.compose(*args, timeout=1800)

    def stop(self) -> None:
        if self.created:
            self.compose("stop", "--timeout", "30", timeout=120)

    def copy_identity(self, directory: Path) -> tuple[str, str]:
        token_file, project_file = directory / "token.txt", directory / "project.json"
        # Restrict these files before Docker writes into them. TemporaryDirectory
        # is private on POSIX; Windows inherits the current user's temp ACL.
        for path in (token_file, project_file):
            path.touch(mode=0o600, exist_ok=True)
        self.compose("cp", "app:/state/token.txt", str(token_file))
        self.compose("cp", "app:/state/project.json", str(project_file))
        token = token_file.read_text(encoding="utf-8").strip()
        project = json.loads(project_file.read_text(encoding="utf-8"))["id"]
        require(bool(token), "Development token file is empty.")
        require(isinstance(project, str) and bool(re.fullmatch(r"prj-[0-9a-f]{20}", project)), "Saved fixture project identity is invalid.")
        return token, project

    def resource_identity(self) -> dict:
        ids = self.compose("ps", "--all", "--quiet").split()
        require(len(ids) == 3, "Expected exactly prepare, database and app containers.")
        records = json.loads(self.docker("inspect", *ids))
        resources = {}
        for record in records:
            labels = record.get("Config", {}).get("Labels", {})
            service = labels.get("com.docker.compose.service")
            require(labels.get("com.docker.compose.project") == self.project and service in SERVICES,
                    "Container ownership does not match the isolated smoke project.")
            require(service not in resources, "Duplicate Compose service container refused.")
            bindings = record["HostConfig"].get("PortBindings") or {}
            if service == "app":
                require(bindings == {"14173/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}]},
                        "Dashboard must publish only the selected host loopback port.")
            else:
                require(not bindings, "Database and prepare must have no published host ports.")
            mounts = record.get("Mounts", [])
            require(all(m.get("Type") in {"volume", "tmpfs"} for m in mounts), "Host bind mounts are forbidden in the smoke stack.")
            volumes = {m["Destination"]: m["Name"] for m in mounts if m["Type"] == "volume"}
            require(all(name.startswith(self.project + "_") for name in volumes.values()), "Shared or foreign volumes refused.")
            resources[service] = {"container_id": record["Id"], "volumes": volumes}
        return resources

    def workspace_identity(self, project: str) -> dict:
        require(bool(re.fullmatch(r"prj-[a-f0-9]{20}", project)), "Invalid fixture workspace identity.")
        # Read known fixture artifacts inside the container; never execute or
        # copy generated code to the host. Only hashes leave the container.
        script = """import hashlib,json,sys
from pathlib import Path
root=Path('/workspaces')/sys.argv[1]
names=['project/calculator.py','project/tests/test_calculator.py','.agent_factory/workspace.json','.agent_factory/runs/review-approval/checkpoint.json','.agent_factory/runs/review-approval/execution_log.jsonl']
assert not (root/'project/legacy_calc.py').exists()
result={}
for name in names:
    path=root/name
    assert path.is_file() and not path.is_symlink() and path.stat().st_size < 2000000
    assert path.resolve().is_relative_to(root.resolve())
    result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(result))
"""
        result = json.loads(self.compose("exec", "--no-TTY", "app", "python", "-c", script, project))
        require(len(result) == 5 and all(re.fullmatch(r"[a-f0-9]{64}", digest) for digest in result.values()),
                "Expected five durable fixture artifacts with content hashes.")
        return result


def api(stack: Stack, path: str, *, token: str = "", body: dict | None = None, expected: int = 200) -> dict:
    status, result = reviewer.http(stack.port, path, token=token, body=body)
    require(status == expected, f"API check expected HTTP {expected}, received {status}.")
    return result


def wait_run(stack: Stack, project: str, token: str, wanted: str, *, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = api(stack, f"/api/v1/projects/{project}/runs/{RUN}", token=token)
        if run["status"] == wanted:
            return run
        require(run["status"] not in {"failed", "blocked", "exhausted", "completed", "cancelled"},
                "Fixture reached an unexpected terminal state.")
        time.sleep(0.5)
    raise SmokeError("Fixture did not reach the required state within the bounded wait.")


def check_fixture(approval: dict, project: str) -> None:
    require(approval.get("project_id") == project and approval.get("run_id") == RUN, "Approval belongs to another run.")
    require(approval.get("status") == "pending" and approval.get("decision") is None and approval.get("execution") is None,
            "Fresh fixture must pause before either decision or execution.")
    require(approval.get("action") == {"tool": "delete_file", "category": "destructive_delete", "params": {"path": "legacy_calc.py"}},
            "Smoke check approves only the fixed calculator legacy-file fixture.")
    require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", approval.get("action_fingerprint", ""))), "Approval fingerprint is invalid.")


def check_completed(run: dict, approval: dict) -> None:
    require(run.get("status") == "completed" and run.get("pending_approval_id") is None, "Run did not complete.")
    require(approval.get("status") == "approved" and approval.get("decision", {}).get("outcome") == "approved", "Decision was not durably approved.")
    execution = approval.get("execution") or {}
    require(execution.get("state") == "succeeded" and execution.get("attempt") == 1, "Approved action must succeed in one recorded attempt.")
    kinds = [step["kind"] for step in approval["timeline"]]
    require(kinds.count("approved") == 1 and kinds.count("action_dispatching") == 1 and kinds.count("action_succeeded") == 1,
            "Timeline must show one decision, one dispatch and one successful outcome.")
    require(not any(kind in kinds for kind in ("retry_dispatched", "reconciliation_required", "outcome_uncertain")), "Unexpected retry or uncertain action.")


def exercise(stack: Stack, directory: Path) -> dict:
    resources_before = stack.resource_identity()
    token, project = stack.copy_identity(directory)
    api(stack, "/api/v1/projects", expected=401)
    projects = api(stack, "/api/v1/projects", token=token)
    require([item["id"] for item in projects["items"]] == [project] and projects["next_cursor"] is None, "Expected exactly one seeded project.")
    run = wait_run(stack, project, token, "awaiting_approval")
    approval_id = run["pending_approval_id"]
    path = f"/api/v1/projects/{project}/approvals/{approval_id}"
    approval = api(stack, path, token=token)
    check_fixture(approval, project)
    # Send once. Unknown response is a failure; never retry a decision blindly.
    api(stack, path + "/decision", token=token,
        body={"outcome": "approved", "action_fingerprint": approval["action_fingerprint"], "comment": "Isolated Compose fixture smoke check."}, expected=201)
    before_run = wait_run(stack, project, token, "completed")
    before_approval = api(stack, path, token=token)
    check_completed(before_run, before_approval)
    workspace_before = stack.workspace_identity(project)
    stack.compose("stop", "--timeout", "30", "app", "db")
    stack.compose("start", "--wait", "--wait-timeout", "180", "db", "app", timeout=240)
    # Acceptance of the still-valid old token proves the signing identity and
    # enrolled principal survived, before reading the freshly issued token.
    api(stack, "/api/v1/projects", token=token)
    token, restarted_project = stack.copy_identity(directory)
    require(restarted_project == project, "Restart changed the seeded project identity.")
    api(stack, "/api/v1/projects", expected=401)
    after_run = wait_run(stack, project, token, "completed")
    after_approval = api(stack, path, token=token)
    check_completed(after_run, after_approval)
    require(after_run == before_run and after_approval == before_approval, "Restart altered durable run, approval, execution or timeline records.")
    require(stack.resource_identity() == resources_before, "Restart replaced a container or durable volume.")
    require(stack.workspace_identity(project) == workspace_before, "Restart changed generated code or durable workspace records.")
    runs = api(stack, f"/api/v1/projects/{project}/runs", token=token)
    require(len(runs["items"]) == 1 and runs["items"][0]["run_id"] == RUN and runs["next_cursor"] is None, "Restart duplicated the seeded run.")
    return {"project_id": project, "run_id": RUN, "approval_id": approval_id,
            "decision_id": after_approval["decision"]["id"], "execution_id": after_approval["execution"]["id"],
            "status": "completed", "execution_attempts": 1, "anonymous_status": 401,
            "restart_records_unchanged": True, "resources_unchanged": True, "resources": resources_before,
            "workspace_artifacts_unchanged": True, "workspace_sha256": workspace_before, "old_token_accepted_after_restart": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="af-smoke-" + uuid.uuid4().hex[:12])
    parser.add_argument("--port", type=int, default=15473)
    parser.add_argument("--report", type=Path, default=Path("reports/compose-smoke.json"))
    parser.add_argument("--no-build", action="store_true", help="Use the already built app image (default builds the candidate).")
    args = parser.parse_args(argv)
    stack = Stack(args.project, args.port)
    result = {"compose_project": args.project, "passed": False}
    try:
        print(f"Compose smoke: fresh isolated project {args.project}; loopback port {args.port}.", flush=True)
        stack.start(build=not args.no_build)
        print("Compose services are healthy; checking the authenticated approval workflow.", flush=True)
        with tempfile.TemporaryDirectory(prefix="af-compose-smoke-") as temporary:
            result.update(exercise(stack, Path(temporary)))
        result["passed"] = True
    except (SmokeError, reviewer.ReviewerError):
        raise
    finally:
        try:
            stack.stop()
            result["services_stopped_volumes_retained"] = stack.created
        except SmokeError:
            result["passed"] = False
            result["services_stopped_volumes_retained"] = False
            raise
        finally:
            result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Compose smoke passed: authenticated approval, one execution attempt, retained state after restart; services stopped, volumes retained.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SmokeError, reviewer.ReviewerError) as error:
        print(f"Compose smoke failed: {error}")
        raise SystemExit(1) from None
