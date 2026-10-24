"""Exclusive OS file locks, from the standard library.

A run is changed by at most one process at a time: any process that starts,
resumes, decides, or reconciles a run first takes an exclusive lock on
``<run>/run.lock`` (docs/design/action-execution.md, "run lock").

* The operating system releases the lock when the holding process exits,
  however it exits, so a crash never leaves a stale lock behind.
* Two lock objects for the same file conflict even inside one process
  (Windows byte-range locks are per handle; POSIX ``flock`` locks belong to
  the open file description), so tests can model two processes with two
  objects.
* Same host only. Locks on network filesystems are unreliable, which is why
  workspaces must be on a local disk.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl


class LockUnavailable(RuntimeError):
    """The lock is held by another process (or another lock object)."""


class FileLock:
    """An exclusive, non-reentrant lock on one file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this lock object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self, timeout: float = 10.0, poll: float = 0.02) -> None:
        """Wait up to ``timeout`` seconds for the lock."""
        deadline = time.monotonic() + timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockUnavailable(f"{self.path} stayed locked for {timeout:g} s")
            time.sleep(poll)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.release()


__all__ = ["FileLock", "LockUnavailable"]
