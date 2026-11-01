"""What `papyrus watch` asks of the operating system: low priority, memory pressure, one instance.

* `lower_priority`: macOS `setpriority(PRIO_DARWIN_PROCESS, 0, PRIO_DARWIN_BG)` (the background
  band: low CPU priority, throttled disk and network I/O, as `taskpolicy -b`); Linux `nice`,
  `SCHED_IDLE` and the idle I/O class (`ionice -c3`).
* `memory_available` / `has_room`: whether a model of a given size can load beside what is
  loaded (the pressure level can't say: on a Mac it reads "warn" whenever a model is loaded).
* `memory_pressure`: normal | warn | critical. macOS `kern.memorystatus_vm_pressure_level`
  (1, 2, 4); Linux `/proc/pressure/memory` (PSI) or, without it, `MemAvailable`.
* `InstanceLock`: an flock on `<library home>/watch.lock`, so one watcher runs per library
  (variant, or `PAPYRUS_HOME`).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NORMAL, WARN, CRITICAL = "normal", "warn", "critical"


def lower_priority() -> list[str]:
    """Move this process (and the helpers it starts) out of the user's way. Returns what was set."""
    done: list[str] = []
    if sys.platform == "darwin":
        try:
            os.setpriority(os.PRIO_DARWIN_PROCESS, 0, os.PRIO_DARWIN_BG)
            done.append("darwin-bg")
        except (OSError, AttributeError):
            pass
        return done
    try:
        os.nice(19 - os.getpriority(os.PRIO_PROCESS, 0))
        done.append("nice-19")
    except (OSError, AttributeError):
        pass
    if hasattr(os, "SCHED_IDLE"):
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
            done.append("sched-idle")
        except OSError:
            pass
    if _ioprio_idle():
        done.append("io-idle")
    return done


# ioprio_set(IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE << 13): no stdlib wrapper. Syscall numbers.
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "arm64": 30, "riscv64": 30, "i686": 289, "armv7l": 314}


def _ioprio_idle() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    nr = _IOPRIO_SET.get(platform.machine())
    if nr is not None:
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            if libc.syscall(nr, 1, 0, 3 << 13) == 0:
                return True
        except OSError:
            pass
    ionice = shutil.which("ionice")
    if ionice:
        try:
            return subprocess.run([ionice, "-c3", "-p", str(os.getpid())], capture_output=True).returncode == 0
        except OSError:
            pass
    return False


def memory_pressure() -> str:
    if sys.platform == "darwin":
        return _darwin_pressure()
    if sys.platform.startswith("linux"):
        return linux_pressure(_read("/proc/pressure/memory"), _read("/proc/meminfo"))
    return NORMAL


def _darwin_pressure() -> str:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        level, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        if libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(level), ctypes.byref(size),
                             None, ctypes.c_size_t(0)) != 0:
            return NORMAL
    except OSError:
        return NORMAL
    return {2: WARN, 4: CRITICAL}.get(level.value, NORMAL)


def memory_available() -> tuple[int, int] | None:
    """(available bytes, total bytes): macOS's own "memory free percentage"
    (`kern.memorystatus_level`, what `memory_pressure` prints) of `hw.memsize`; Linux MemAvailable.
    None if unknown."""
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            pct, total = ctypes.c_int(0), ctypes.c_uint64(0)
            size = ctypes.c_size_t(ctypes.sizeof(pct))
            if libc.sysctlbyname(b"kern.memorystatus_level", ctypes.byref(pct), ctypes.byref(size), None,
                                 ctypes.c_size_t(0)) != 0:
                return None
            size = ctypes.c_size_t(ctypes.sizeof(total))
            if libc.sysctlbyname(b"hw.memsize", ctypes.byref(total), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
                return None
        except OSError:
            return None
        return int(total.value * pct.value / 100), int(total.value)
    meminfo = _read("/proc/meminfo")
    if meminfo:
        kb = {k: int(v.split()[0]) for k, _, v in (line.partition(":") for line in meminfo.splitlines()) if v.split()}
        if kb.get("MemTotal") and "MemAvailable" in kb:
            return kb["MemAvailable"] * 1024, kb["MemTotal"] * 1024
    return None


def has_room(model_bytes: int | None, available: tuple[int, int] | None = None) -> bool:
    """Whether a model of `model_bytes` (its weights on disk) can load now without pushing the
    machine into swapping: what is available must cover the weights plus a quarter (context, KV
    cache) and still leave 15% of memory (at least 2 GB) free. False when either is unknown."""
    available = available if available is not None else memory_available()
    if not model_bytes or available is None:
        return False
    avail, total = available
    return avail >= model_bytes * 1.25 + max(2 * 1024**3, 0.15 * total)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def linux_pressure(psi: str | None, meminfo: str | None) -> str:
    """From PSI (`some`/`full avg10`: % of the last 10 s stalled on memory) if the kernel has it,
    else from the share of memory available."""
    if psi:
        avg = {}
        for line in psi.splitlines():
            kind, *fields = line.split()
            for f in fields:
                k, _, v = f.partition("=")
                if k == "avg10":
                    avg[kind] = float(v)
        if avg.get("full", 0) >= 5:
            return CRITICAL
        if avg.get("some", 0) >= 10:
            return WARN
    if meminfo:
        kb = {}
        for line in meminfo.splitlines():
            k, _, v = line.partition(":")
            if v.split():
                kb[k] = int(v.split()[0])
        if kb.get("MemTotal") and "MemAvailable" in kb:
            share = kb["MemAvailable"] / kb["MemTotal"]
            return CRITICAL if share < 0.05 else WARN if share < 0.10 else NORMAL
    return NORMAL


class InstanceLock:
    """An exclusive, non-blocking flock on `path`, holding this process's pid. Released when the
    process exits, however it exits."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None
        self.holder: int | None = None  # the other process's pid, when `acquire` fails

    def acquire(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                self.holder = int(os.read(fd, 32).decode().strip() or 0) or None
            except ValueError:
                self.holder = None
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock; the file stays (another may hold it now)
            self._fd = None
