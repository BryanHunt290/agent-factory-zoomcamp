"""Host-impact observations for adversarial tests (Windows, via ctypes; launches no process)."""

from __future__ import annotations

import ctypes
import os
import shutil
import threading
import time
from ctypes import wintypes


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_count() -> int:
    arr = (wintypes.DWORD * 16384)()
    needed = wintypes.DWORD()
    ctypes.windll.psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed))
    return needed.value // ctypes.sizeof(wintypes.DWORD)


def working_set() -> int:
    counters = _PMC()
    counters.cb = ctypes.sizeof(_PMC)
    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize


def free_disk(path: str = os.getcwd()) -> int:
    return shutil.disk_usage(path).free


class HostMonitor:
    """Context manager recording host process count, memory, free disk and responsiveness."""

    def __init__(self, tick: float = 0.05) -> None:
        self.tick = tick
        self.max_gap = 0.0
        self._stop = threading.Event()

    def _beat(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.tick):
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    def __enter__(self) -> HostMonitor:
        self.proc_before, self.ws_before, self.disk_before = process_count(), working_set(), free_disk()
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self.proc_after, self.ws_after, self.disk_after = process_count(), working_set(), free_disk()

    def summary(self) -> dict:
        return {
            "host_processes": [self.proc_before, self.proc_after],
            "orchestrator_working_set_mb": [round(self.ws_before / 2**20, 1), round(self.ws_after / 2**20, 1)],
            "host_free_disk_delta_mb": round((self.disk_after - self.disk_before) / 2**20, 1),
            "max_heartbeat_gap_s": round(self.max_gap, 3),
        }
