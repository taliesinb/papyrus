"""Start, stop, open and pause a library's apps and services (`papyrus start | stop | open |
status | pause | resume`), for the variant in use (`papyrus --variant personal start`).

    macOS: Papyrus Server.app (bundle id `io.github.taliesinb.papyrus.<v>.server`) runs the web
           server and the watcher; Papyrus.app (`io.github.taliesinb.papyrus.<v>`) is the window.
           start = `open -g -b <server id>` (in the background); stop = SIGTERM to the server app,
           which quits as its menu's Quit does (the watcher's run closes as interrupted, the
           tailnet path is withdrawn); open = `open -b <app id>` (it starts the server if needed).
    Linux: systemd user units `<variant name>.server.service` and `.watch.service` (apps/linux);
           start / stop = `systemctl --user start|stop`; open = the browser at the server's URL.

The port and base path come from `<home>/variant.yml`, which the installers write. Pausing is
the watcher's own switch (`<home>/watch.pause`, the menu's Pause Ingestion): its loop halts after
the step in progress, so nothing is added, read, classified, extracted, indexed or matched; new
files' events wait, and are taken (with a catch-up scan if one was skipped) on resume.

The status probe asks our own server on 127.0.0.1 directly, not through http.HttpClient: it is
not a third-party API, and recording every probe would only fill the services store.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db

ROOT_NAME = "io.github.taliesinb.papyrus"  # apps/variants.py's, which isn't part of the package
DEFAULT_PORT, DEFAULT_BASE = 5182, "/papyrus"

Run = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


class ControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class Variant:
    id: str
    home: Path
    port: int
    base: str  # "/papyrus", "/papyrus-personal"

    @property
    def name(self) -> str:  # bundle ids, unit names
        return f"{ROOT_NAME}.{self.id}"

    @property
    def app_id(self) -> str:
        return self.name

    @property
    def server_id(self) -> str:
        return f"{self.name}.server"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base}/"


def read_flat_yaml(path: Path) -> dict[str, object]:
    """`key: value` lines (variant.yml; the same reader as apps/variants.py)."""
    out: dict[str, object] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            out[key] = value[1:-1]
        elif re.fullmatch(r"-?\d+", value):
            out[key] = int(value)
        elif value in ("true", "false"):
            out[key] = value == "true"
        else:
            out[key] = value
    return out


def variant() -> Variant:
    """The variant in use, with its port and base path from variant.yml (else the defaults)."""
    vid, home = db.variant(), db.library_home()
    try:
        cfg = read_flat_yaml(home / "variant.yml")
    except OSError:
        cfg = {}
    port = cfg.get("port")
    base = cfg.get("servePath")
    return Variant(vid, home, port if isinstance(port, int) else DEFAULT_PORT,
                   base if isinstance(base, str) and base.startswith("/") else
                   DEFAULT_BASE if vid == "release" else f"{DEFAULT_BASE}-{vid}")


def macos() -> bool:
    return sys.platform == "darwin"


# --- macOS ----------------------------------------------------------------------------------


def app_pid(bundle_id: str, run: Run | None = None) -> int | None:
    """The pid of a running app, by bundle id (lsappinfo), or None."""
    run = run or _run
    asn = run(["lsappinfo", "find", f"bundleid={bundle_id}"]).stdout.strip()
    if not asn:
        return None
    m = re.search(r"pid\s*=\s*(\d+)", run(["lsappinfo", "info", "-only", "pid", asn]).stdout)
    return int(m.group(1)) if m else None


def _open(args: list[str], what: str, run: Run) -> None:  # (callers pass their resolved run)
    r = run(["open", *args])
    if r.returncode != 0:
        raise ControlError(f"could not open {what}: {(r.stderr or r.stdout).strip()} "
                           "(build it with python3 apps/macos/build.py --install)")


# --- Linux ----------------------------------------------------------------------------------


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def units(v: Variant) -> list[str]:
    """The variant's installed units: the server's, and the watcher's if it watches."""
    return [u for u in (f"{v.name}.server.service", f"{v.name}.watch.service") if (unit_dir() / u).is_file()]


def _systemctl(action: str, v: Variant, run: Run) -> list[str]:
    us = units(v)
    if not us:
        raise ControlError(f"no systemd units for {v.id} in {unit_dir()} (install them with python3 apps/linux/install.py"
                           + (f" --variant {v.id}" if v.id != "release" else "") + ")")
    r = run(["systemctl", "--user", action, *us])
    if r.returncode != 0:
        raise ControlError(f"systemctl --user {action} failed: {(r.stderr or r.stdout).strip()}")
    return us


# --- the commands ---------------------------------------------------------------------------


def server_answers(v: Variant, timeout: float = 1.5) -> dict | None:
    """The server's /api/version, if it answers on its port."""
    try:
        with urllib.request.urlopen(f"{v.url}api/version", timeout=timeout) as r:  # noqa: S310 (our own, local)
            return json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return None


def start(v: Variant | None = None, run: Run | None = None) -> str:
    v, run = v or variant(), run or _run
    if macos():
        if (pid := app_pid(v.server_id, run)) is not None:
            return f"{v.server_id} is already running (pid {pid})"
        _open(["-g", "-b", v.server_id], v.server_id, run)
        return f"started {v.server_id} (the web server{' and the watcher' if _watches(v) else ''}): {v.url}"
    return "started " + ", ".join(_systemctl("start", v, run))


def stop(v: Variant | None = None, run: Run | None = None, wait_s: float = 20.0,
         kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep) -> str:
    v, run = v or variant(), run or _run
    if macos():
        pid = app_pid(v.server_id, run)
        if pid is None:
            return f"{v.server_id} is not running"
        kill(pid, signal.SIGTERM)  # it quits as its menu's Quit does (watcher first, up to ~10 s)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if app_pid(v.server_id, run) is None:
                return f"stopped {v.server_id}"
            sleep(0.5)
        raise ControlError(f"{v.server_id} (pid {pid}) is still running after {wait_s:.0f} s")
    return "stopped " + ", ".join(_systemctl("stop", v, run))


def open_viewer(v: Variant | None = None, run: Run | None = None) -> str:
    v, run = v or variant(), run or _run
    if macos():
        _open(["-b", v.app_id], v.app_id, run)
        return f"opened {v.app_id}"
    r = run(["xdg-open", v.url])
    if r.returncode != 0:
        raise ControlError(f"xdg-open {v.url} failed: {(r.stderr or r.stdout).strip()}")
    return f"opened {v.url}"


def _watches(v: Variant) -> bool:
    try:
        w = read_flat_yaml(v.home / "variant.yml").get("watch")
    except OSError:
        w = None
    return w if isinstance(w, bool) else v.id == "release"


def pause_path(v: Variant | None = None) -> Path:
    return (v or variant()).home / "watch.pause"


def pause(v: Variant | None = None) -> str:
    pause_path(v).touch()
    return ("ingestion paused: the watcher adds and ingests nothing after the step in progress (also after a "
            "restart); new files wait. papyrus resume ends it")


def resume(v: Variant | None = None) -> str:
    p = pause_path(v)
    if not p.exists():
        return "ingestion wasn't paused"
    p.unlink(missing_ok=True)
    return "ingestion resumed"


def status(v: Variant | None = None, run: Run | None = None) -> dict:
    """What runs: the server app or units, whether the server answers, the watcher's state."""
    from .watch import read_status

    v, run = v or variant(), run or _run
    out: dict = {"variant": v.id, "home": str(v.home), "url": v.url}
    if macos():
        out["server_app"] = app_pid(v.server_id, run)
        out["viewer_app"] = app_pid(v.app_id, run)
    else:
        out["units"] = {u: run(["systemctl", "--user", "is-active", u]).stdout.strip() or "unknown" for u in units(v)}
    out["server_answers"] = server_answers(v) is not None
    w = read_status(v.home)
    out["watcher"] = {k: w.get(k) for k in ("state", "detail", "pending", "ingesting", "eta_s") if w.get(k) is not None}
    out["paused"] = pause_path(v).exists()
    return out
