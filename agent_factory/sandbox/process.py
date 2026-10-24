"""Bounded subprocess execution shared by backends.

Deadlines use :func:`time.monotonic`, so wall-clock changes cannot extend
them. Output is captured up to a per-stream byte limit; beyond it the
process is killed (``output_exceeded``) while the pipes keep draining so the
child cannot block on a full pipe.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    output_exceeded: bool
    duration_s: float
    #: Set when the process could not be started at all.
    error: str | None = None


class _CappedReader(threading.Thread):
    def __init__(self, stream, limit: int, overflow: threading.Event) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self.buffer = bytearray()
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(_CHUNK) if hasattr(self._stream, "read1") else self._stream.read(_CHUNK)
                if not chunk:
                    break
                room = self._limit - len(self.buffer)
                if room > 0:
                    self.buffer += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True
                    self._overflow.set()
        except (OSError, ValueError):
            pass


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_capped(
    argv: Sequence[str],
    *,
    timeout_s: float,
    output_limit: int,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    on_kill: Callable[[], None] | None = None,
    kill_grace_s: float = 5.0,
) -> ProcessOutcome:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return ProcessOutcome(None, b"", b"", False, False, False, False, time.monotonic() - start, str(exc))

    overflow = threading.Event()
    readers = [_CappedReader(proc.stdout, output_limit, overflow), _CappedReader(proc.stderr, output_limit, overflow)]
    for r in readers:
        r.start()
    if stdin is not None:
        threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True).start()

    deadline = start + timeout_s
    timed_out = output_exceeded = False
    while proc.poll() is None:
        if overflow.is_set():
            output_exceeded = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        overflow.wait(min(0.05, remaining))

    if proc.poll() is None:
        if on_kill is not None:
            try:
                on_kill()
            except Exception:  # the process kill below still happens
                pass
        proc.kill()
        try:
            proc.wait(timeout=kill_grace_s)
        except subprocess.TimeoutExpired:
            pass

    for r in readers:
        r.join(timeout=kill_grace_s)
    out, err = readers
    return ProcessOutcome(
        exit_code=proc.returncode,
        stdout=bytes(out.buffer),
        stderr=bytes(err.buffer),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        output_exceeded=output_exceeded or out.truncated or err.truncated,
        duration_s=time.monotonic() - start,
    )


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
