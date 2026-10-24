"""An in-memory stand-in for the docker CLI. Launches nothing, touches no network."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field

from agent_factory.sandbox.docker import LABEL_EXEC, SELF_TEST_MARKER
from agent_factory.sandbox.process import ProcessOutcome

IMAGE_ID = "sha256:" + "ab" * 32
LOCAL_PIPE = "npipe:////./pipe/dockerDesktopLinuxEngine"
GOOD_SELF_TEST = {
    "uid": 10001,
    "gid": 10001,
    "net": ["lo"],
    "root_writable": False,
    "cap_eff": "0000000000000000",
    "env": ["HOME", "HOSTNAME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONIOENCODING", "PYTHONUNBUFFERED"],
}
PASSING_TESTS = "Ran 5 tests in 0.010s\n\nOK\n"


def _out(stdout: str = "", stderr: str = "", code: int | None = 0, **kw) -> ProcessOutcome:
    return ProcessOutcome(
        exit_code=code,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
        stdout_truncated=kw.get("stdout_truncated", False),
        stderr_truncated=False,
        timed_out=kw.get("timed_out", False),
        output_exceeded=kw.get("output_exceeded", False),
        duration_s=0.01,
        error=kw.get("error"),
    )


@dataclass
class StartBehaviour:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = PASSING_TESTS
    oom: bool = False
    timeout: bool = False
    overflow: bool = False
    error: str = ""


@dataclass
class FakeDocker:
    engine_up: bool = True
    server_os: str = "linux"
    context_host: str = LOCAL_PIPE
    image_id: str = IMAGE_ID
    image_present: bool = True
    self_test_report: dict = field(default_factory=lambda: dict(GOOD_SELF_TEST))
    start: StartBehaviour = field(default_factory=StartBehaviour)
    fail: set = field(default_factory=set)  # subcommands that fail: create, rm, ps, inspect
    partial_create: bool = False  # create fails but leaves a container behind
    sticky: bool = False  # rm "succeeds" but the container remains
    calls: list = field(default_factory=list)  # (host, args, stdin)
    containers: dict = field(default_factory=dict)  # name -> record

    def __call__(self, args, *, host, stdin, timeout_s, output_limit, on_kill):
        args = list(args)
        self.calls.append((host, args, stdin))
        cmd = args[0]
        handler = getattr(self, f"_{cmd}", None)
        if cmd in ("context", "image", "container"):
            handler = getattr(self, f"_{cmd}_{args[1]}", None)
        if handler is None:
            raise AssertionError(f"unexpected docker subcommand reached the runner: {args}")
        return handler(args, stdin=stdin, on_kill=on_kill)

    # -------------------------------------------------------------- info

    def _version(self, args, **_):
        if not self.engine_up:
            return _out(json.dumps({"Client": {"Version": "29.6.1"}, "Server": None}), "Cannot connect", 1)
        return _out(json.dumps({"Client": {"Version": "29.6.1"}, "Server": {"Version": "29.6.1", "Os": self.server_os}}))

    def _context_inspect(self, args, **_):
        return _out(json.dumps([{"Name": "desktop-linux", "Endpoints": {"docker": {"Host": self.context_host}}}]))

    def _image_inspect(self, args, **_):
        if not self.image_present:
            return _out("", "Error: No such image", 1)
        return _out(json.dumps({"Id": self.image_id, "Os": "linux"}))

    # ---------------------------------------------------------- lifecycle

    def _create(self, args, **_):
        name = args[args.index("--name") + 1]
        labels = dict(v.split("=", 1) for i, v in enumerate(args) if i and args[i - 1] == "--label")
        record = {"id": secrets.token_hex(32), "labels": labels, "args": args, "state": None}
        if "create" in self.fail:
            if self.partial_create:
                self.containers[name] = record
            return _out("", "Error response from daemon: simulated create failure", 125)
        self.containers[name] = record
        return _out(record["id"] + "\n")

    def _start(self, args, *, stdin, on_kill, **_):
        name = args[-1]
        record = self.containers[name]
        record["stdin"] = stdin
        b = self.start
        if SELF_TEST_MARKER.strip() in " ".join(record["args"]):
            b = StartBehaviour(stdout=SELF_TEST_MARKER + json.dumps(self.self_test_report) + "\n", stderr="")
        if b.timeout or b.overflow:
            on_kill()
        state = {
            "Status": "exited",
            "Running": False,
            "ExitCode": 137 if (b.timeout or b.oom or b.overflow) else b.exit_code,
            "OOMKilled": b.oom,
            "Error": b.error,
            "StartedAt": "2026-10-01T00:00:00.1Z",
            "FinishedAt": "2026-10-01T00:00:01.1Z",
        }
        record["state"] = state
        return _out(
            b.stdout, b.stderr, None if b.timeout else state["ExitCode"], timed_out=b.timeout, output_exceeded=b.overflow,
            stdout_truncated=b.overflow,
        )

    def _kill(self, args, **_):
        record = self.containers.get(args[-1])
        if record is not None:
            record["killed"] = True
        return _out(args[-1] + "\n")

    def _container_inspect(self, args, **_):
        if "inspect" in self.fail:
            return _out("", "Error: inspect failed", 1)
        record = self.containers.get(args[-1])
        if record is None or record["state"] is None:
            return _out("", "Error: No such container", 1)
        return _out(json.dumps(record["state"]))

    def _rm(self, args, **_):
        target = args[-1]
        name = next((n for n, r in self.containers.items() if n == target or r["id"] == target), None)
        if "rm" in self.fail or self.sticky:
            return _out("", "Error: simulated rm failure", 1)
        if name is not None:
            del self.containers[name]
        return _out(target + "\n")

    def _ps(self, args, **_):
        if "ps" in self.fail:
            return _out("", "Error: simulated ps failure", 1)
        flt = args[args.index("--filter") + 1].removeprefix("label=")
        key, _, value = flt.partition("=")
        matches = [r for r in self.containers.values() if key in r["labels"] and (not value or r["labels"][key] == value)]
        if "--format" in args:
            lines = [
                "\t".join([r["id"], r["labels"].get(LABEL_EXEC, ""), r["labels"].get("agent-factory.owner", ""), r["labels"].get("agent-factory.deadline", "")])
                for r in matches
            ]
            return _out("\n".join(lines) + ("\n" if lines else ""))
        return _out("".join(r["id"] + "\n" for r in matches))

    # ------------------------------------------------------------ helpers

    def subcommands(self):
        return [tuple(a[:2]) if a[0] in ("context", "image", "container") else (a[0],) for _, a, _ in self.calls]

    def add_foreign_container(self, *, exec_id, owner, deadline):
        self.containers[f"af-{exec_id}"] = {
            "id": secrets.token_hex(32),
            "labels": {LABEL_EXEC: exec_id, "agent-factory.owner": owner, "agent-factory.deadline": str(deadline)},
            "args": [],
            "state": None,
        }
