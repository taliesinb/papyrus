#!/usr/bin/env python3
"""Install the papyrus web server on Linux as a systemd user service, one per variant (stdlib only;
needs Node 22.5+, pnpm, and poppler's pdftoppm for page thumbnails).

    python3 apps/linux/install.py                      # release: build web/, install, start, publish
    python3 apps/linux/install.py --variant personal   # Papyrus Personal on its own port and /papyrus-personal
    python3 apps/linux/install.py --all                # every variant that has a variant.yml
    python3 apps/linux/install.py --uninstall          # stop and remove the service (the library stays)
    python3 apps/linux/install.py --no-systemd         # install the web build only; prints the command to run

The Linux counterpart of apps/macos/build.py, as DeepSea's `dsd build --install` is on Linux: no
app, the server alone, reached in a browser at http://127.0.0.1:<port><base>/ or on the tailnet at
https://<machine>.<tailnet>.ts.net<base>/. Variants and their variant.yml are shared with macOS
(apps/variants.py). Per variant, named by its variant name io.github.taliesinb.papyrus.<id>:

    ${XDG_CONFIG_HOME:-~/.config}/systemd/user/<variant name>.server.service
    ${XDG_DATA_HOME:-~/.local/share}/<variant name>/web/     the built server (<home>/support)
    ${XDG_STATE_HOME:-~/.local/state}/<variant name>/server.log   (<home>/logs)

The unit is enabled (it starts with the user's systemd manager: at login, or at boot when the
account lingers) and restarted when it fails. It publishes the server with `tailscale serve --bg`
when it starts; tailscaled keeps that across reboots, and --uninstall withdraws it.

A variant that watches (release by default; `--watch` / `--no-watch`, recorded in variant.yml)
also gets `<variant name>.watch.service`: `papyrus watch` from this checkout (`uv run --project
<repo>`), at idle CPU and I/O priority, which adds new PDFs from ~/Downloads and ~/Desktop and
processes them; its log is `<state dir>/watch.log`. Exit status 75 (another watcher holds the
library's lock) is not restarted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from variants import (  # noqa: E402  (apps/variants.py, shared with apps/macos/build.py)
    FIRST_PORT, PORT_STEP, RELEASE, all_variants, copy_web, die, display_name, ensure_library, ensure_words, find, find_node,
    link_home,
    papyrus_root, record_install, resolve_variant, set_watch, variant_name, version, watch_command, watches, web_build_for,
    write_flat_yaml,
)

WRITER = "apps/linux/install.py"
NODE_PLACES = ("/usr/local/bin/node", "/usr/bin/node", str(Path.home() / ".nix-profile/bin/node"),
               "/run/current-system/sw/bin/node")
POPPLER_HINT = ("install poppler for page thumbnails: Debian/Ubuntu `sudo apt install poppler-utils`, "
                "Fedora `sudo dnf install poppler-utils`, Arch `sudo pacman -S poppler`, "
                "Nix `nix profile add nixpkgs#poppler-utils` (or --no-thumbnails)")


# --- places -----------------------------------------------------------------------------------

def xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


def unit_dir() -> Path:
    return xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def unit_name(vid: str) -> str:
    return f"{variant_name(vid)}.server.service"


def watch_unit_name(vid: str) -> str:
    return f"{variant_name(vid)}.watch.service"


def data_dir(vid: str) -> Path:
    return xdg("XDG_DATA_HOME", ".local/share") / variant_name(vid)


def state_dir(vid: str) -> Path:
    return xdg("XDG_STATE_HOME", ".local/state") / variant_name(vid)


# --- the unit ---------------------------------------------------------------------------------

def systemd_quote(arg: str, dollars: bool = True) -> str:
    """One argument as systemd reads it (as DeepSea's dsd writes them): double quotes, `\\` and `"`
    escaped, `%` doubled (specifiers), and `$` doubled where variables would expand (ExecStart=;
    not Environment=)."""
    text = str(arg)
    if "\n" in text or "\r" in text:
        die(f"a unit argument cannot hold a line break: {text!r}")
    quoted = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (quoted.replace("$", "$$") if dollars else quoted) + '"'


def serve_args(tailscale: str, base: str, port: int) -> list[str]:
    return [tailscale, "serve", "--bg", "--yes", "--https=443", f"--set-path={base}", f"http://127.0.0.1:{port}{base}"]


def unit_text(*, vid: str, node: str, web: Path, port: int, base: str, log: Path, env: dict[str, str],
              tailscale: str | None) -> str:
    environment = {"PORT": str(port), "HOST": "127.0.0.1", "NODE_ENV": "production", "PAPYRUS_VARIANT": vid,
                   # The user manager's PATH is short; the server finds pdftoppm there or in PDFTOPPM.
                   "PATH": os.pathsep.join(dict.fromkeys([str(Path(node).parent), "/usr/local/bin", "/usr/bin", "/bin"])),
                   **env}
    lines = [
        f"# {display_name(vid)} Server: written by {WRITER}",
        "[Unit]",
        f"Description={display_name(vid)} Server: the papyrus web server on 127.0.0.1:{port}{base}",
        # Five quick failures stop it, as Papyrus Server.app does on macOS.
        "StartLimitIntervalSec=60",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={web}",
        "ExecStart=" + " ".join(map(systemd_quote, [node, str(web / "build" / "index.js")])),
        *(f"Environment={systemd_quote(f'{k}={v}', dollars=False)}" for k, v in environment.items()),
    ]
    if tailscale:
        # `-`: a tailnet that is down (or no operator rights) must not stop the server.
        lines.append("ExecStartPost=-" + " ".join(map(systemd_quote, serve_args(tailscale, base, port))))
    lines += [
        "Restart=on-failure",
        "RestartSec=2",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def watch_unit_text(*, vid: str, command: list[str], log: Path, env: dict[str, str]) -> str:
    """`papyrus watch` as a service. systemd lowers its priority (the watcher also does it itself);
    SIGTERM closes its run as interrupted."""
    environment = {"PAPYRUS_VARIANT": vid, "PYTHONUNBUFFERED": "1",
                   "PATH": os.pathsep.join(dict.fromkeys([str(Path(command[0]).parent), "/usr/local/bin", "/usr/bin", "/bin"])),
                   **env}
    return "\n".join([
        f"# {display_name(vid)} watcher: written by {WRITER}",
        "[Unit]",
        f"Description={display_name(vid)} watcher: papyrus watch adds new PDFs from ~/Downloads and ~/Desktop",
        "StartLimitIntervalSec=600",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(map(systemd_quote, command)),
        *(f"Environment={systemd_quote(f'{k}={v}', dollars=False)}" for k, v in environment.items()),
        "Nice=19",
        "CPUSchedulingPolicy=idle",
        "IOSchedulingClass=idle",
        "Restart=on-failure",
        "RestartSec=10",
        "RestartPreventExitStatus=75",  # another watcher holds the library's lock
        "TimeoutStopSec=30",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


# --- systemd and tailscale --------------------------------------------------------------------

def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def have_user_systemd() -> bool:
    try:
        return systemctl("show-environment").returncode == 0
    except FileNotFoundError:
        return False


def check(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        die(f"systemctl --user {what} failed: {(result.stderr or result.stdout).strip()}")


def lingering() -> bool | None:
    try:
        r = subprocess.run(["loginctl", "show-user", os.environ.get("USER") or str(os.getuid()), "--property=Linger"],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return None if r.returncode != 0 else "Linger=yes" in r.stdout


def tailnet_host(tailscale: str) -> tuple[str | None, str | None]:
    """This machine's MagicDNS name, or why there is none."""
    r = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True)
    try:
        status = json.loads(r.stdout) if r.returncode == 0 else {}
    except json.JSONDecodeError:
        status = {}
    if status.get("BackendState") != "Running":
        return None, f"Tailscale is {status.get('BackendState') or 'not running'}"
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return (name, None) if name else (None, "this machine has no MagicDNS name")


def publish(tailscale: str, base: str, port: int) -> tuple[str | None, str | None]:
    """Publish now, so that a problem is reported here rather than only in the unit's log."""
    host, note = tailnet_host(tailscale)
    if not host:
        return None, note
    r = subprocess.run(serve_args(tailscale, base, port), capture_output=True, text=True)
    if r.returncode == 0:
        return f"https://{host}{base}/", None
    out = (r.stderr or r.stdout).strip()
    if "denied" in out.lower() or "operator" in out.lower():
        return None, f"tailscale serve needs operator rights: run `sudo tailscale set --operator=$USER` once, then reinstall ({out.splitlines()[-1]})"
    return None, f"tailscale serve failed: {out.splitlines()[-1] if out else r.returncode}"


def withdraw(tailscale: str | None, base: str) -> bool:
    if not tailscale:
        return False
    return subprocess.run([tailscale, "serve", "--https=443", f"--set-path={base}", "off"],
                          capture_output=True, text=True).returncode == 0


def wait_until_answering(port: int, base: str, seconds: float = 30) -> dict | None:
    url = f"http://127.0.0.1:{port}{base}/api/version"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return json.load(r)
        except (OSError, ValueError):
            time.sleep(0.3)
    return None


# --- install / uninstall ----------------------------------------------------------------------

def install_web(web_build: Path, dest: Path) -> None:
    """Replace `dest` (…/web) with the new build, keeping the old one until the new one is in place."""
    staging = dest.with_name("web.new")
    shutil.rmtree(staging, ignore_errors=True)
    copy_web(web_build, staging)
    old = dest.with_name("web.old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    staging.rename(dest)
    shutil.rmtree(old, ignore_errors=True)


def install(vid: str, variant: dict, args, tools: dict, build: str, systemd: bool) -> dict:
    home = papyrus_root() / vid
    support, logs = data_dir(vid), state_dir(vid)
    link_home(home, {"logs": logs, "support": support})
    port, base = int(variant["port"]), str(variant["servePath"])
    serve = bool(variant.get("serve", True))
    web_build = web_build_for(base, args.skip_web)
    unit = unit_name(vid)
    result: dict = {"variant": vid, "home": str(home), "port": port, "base": base,
                    "local": f"http://127.0.0.1:{port}{base}/", "web": str(support / "web")}

    env = dict(e.split("=", 1) for e in args.env)
    if os.environ.get("PAPYRUS_ROOT"):
        env.setdefault("PAPYRUS_ROOT", os.environ["PAPYRUS_ROOT"])
    if tools["pdftoppm"]:
        env.setdefault("PDFTOPPM", tools["pdftoppm"])
    tailscale = tools["tailscale"] if serve else None
    if ensure_library(vid, env):
        result["created"] = str(home / "papyrus.sqlite")

    if systemd and systemctl("is-active", unit).stdout.strip() in ("active", "activating", "reloading"):
        check(systemctl("stop", unit), f"stop {unit}")
    install_web(web_build, support / "web")

    watch_env = {k: v for k, v in env.items() if k != "PDFTOPPM"}
    if not systemd:
        command = " ".join([*(f"{k}={json.dumps(v)}" for k, v in {
            "PORT": str(port), "HOST": "127.0.0.1", "NODE_ENV": "production", "PAPYRUS_VARIANT": vid, **env}.items()),
            tools["node"], str(support / "web" / "build" / "index.js")])
        result["run"] = command
        if watches(vid, variant):
            result["runWatch"] = " ".join([*(f"{k}={json.dumps(v)}" for k, v in {"PAPYRUS_VARIANT": vid, **watch_env}.items()),
                                         *map(json.dumps, watch_command())])
        record_install(vid, build, WRITER)
        return result

    unit_dir().mkdir(parents=True, exist_ok=True)
    text = unit_text(vid=vid, node=tools["node"], web=support / "web", port=port, base=base,
                     log=logs / "server.log", env=env, tailscale=tailscale)
    (unit_dir() / unit).write_text(text)
    check(systemctl("daemon-reload"), "daemon-reload")
    check(systemctl("enable", unit), f"enable {unit}")
    check(systemctl("restart", unit), f"restart {unit}")
    result["unit"] = str(unit_dir() / unit)
    result["log"] = str(logs / "server.log")

    wunit = watch_unit_name(vid)
    if watches(vid, variant):
        (unit_dir() / wunit).write_text(watch_unit_text(vid=vid, command=watch_command(), log=logs / "watch.log",
                                                        env=watch_env))
        check(systemctl("daemon-reload"), "daemon-reload")
        check(systemctl("enable", wunit), f"enable {wunit}")
        check(systemctl("restart", wunit), f"restart {wunit}")
        result["watchUnit"] = str(unit_dir() / wunit)
        result["watchLog"] = str(logs / "watch.log")
    elif remove_watch_unit(vid):
        result["watchUnitRemoved"] = str(unit_dir() / wunit)

    status = wait_until_answering(port, base)
    if status is None:
        die(f"the server does not answer on {result['local']} after 30 s; see {logs / 'server.log'} "
            f"and `systemctl --user status {unit}`")
    result["counts"] = status.get("counts")
    if tailscale:
        result["tailnet"], note = publish(tailscale, base, port)
        if note:
            result["tailnetNote"] = note
    elif serve:
        result["tailnetNote"] = "tailscale not found: not published on the tailnet"
    record_install(vid, build, WRITER)
    return result


def remove_watch_unit(vid: str, systemd: bool = True) -> bool:
    """Stop, disable and remove the variant's watcher unit, if there is one."""
    wunit = watch_unit_name(vid)
    path = unit_dir() / wunit
    if not path.exists():
        return False
    if systemd:
        systemctl("stop", wunit)
        systemctl("disable", wunit)
    path.unlink()
    if systemd:
        systemctl("daemon-reload")
    return True


def uninstall(vid: str, variant: dict, tools: dict, systemd: bool) -> dict:
    unit = unit_name(vid)
    path = unit_dir() / unit
    removed = []
    if remove_watch_unit(vid, systemd):
        removed.append(str(unit_dir() / watch_unit_name(vid)))
    if systemd and path.exists():
        systemctl("stop", unit)
        systemctl("disable", unit)
    if path.exists():
        path.unlink()
        removed.append(str(path))
        if systemd:
            systemctl("daemon-reload")
    web = data_dir(vid) / "web"
    if web.exists():
        shutil.rmtree(web)
        removed.append(str(web))
    base = str(variant.get("servePath") or ("/papyrus" if vid == RELEASE else f"/papyrus-{vid}"))
    if withdraw(tools["tailscale"], base):
        removed.append(f"tailscale serve {base}")
    return {"variant": vid, "removed": removed, "kept": [str(papyrus_root() / vid), str(state_dir(vid))]}


def main() -> None:
    if not sys.platform.startswith("linux"):
        die("Linux only (on macOS: python3 apps/macos/build.py)")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", action="append", metavar="ID",
                   help=f"the variant (repeatable; default {RELEASE}); a new id makes ~/.papyrus/<ID>/variant.yml")
    p.add_argument("--all", action="store_true", help="every variant that has a variant.yml")
    p.add_argument("--uninstall", action="store_true",
                   help="stop and remove the service, its web build and its tailnet path (the library and logs stay)")
    p.add_argument("--port", type=int, help=f"set the variant's loopback port (recorded; new variants take {FIRST_PORT}, "
                   f"{FIRST_PORT + PORT_STEP}, …)")
    p.add_argument("--color", metavar="#RRGGBB", help="set the variant's colour (recorded; used by the macOS apps)")
    p.add_argument("--serve", action=argparse.BooleanOptionalAction, default=None,
                   help="publish on the tailnet with tailscale serve (recorded; default on)")
    p.add_argument("--watch", action=argparse.BooleanOptionalAction, default=None,
                   help="also run `papyrus watch` as a service (recorded; default: release only)")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="environment for the web server and the watcher, e.g. PAPYRUS_HOME=/tmp/ph (repeatable)")
    p.add_argument("--no-systemd", action="store_true", help="install the web build only and print the command that runs it")
    p.add_argument("--no-thumbnails", action="store_true", help="install without pdftoppm (page thumbnails fail)")
    p.add_argument("--skip-web", action="store_true", help="reuse web/build as it is (it must be built for the variant's base)")
    args = p.parse_args()

    if args.all and args.variant:
        die("--all or --variant, not both")
    ids = list(all_variants()) if args.all else list(dict.fromkeys(args.variant or [RELEASE]))
    ids.sort(key=lambda v: v == RELEASE)  # release last, so web/build is left as `pnpm start` expects (/papyrus)
    if not ids:
        die(f"no variant has a variant.yml under {papyrus_root()}")
    if len(ids) > 1 and (args.color or args.port):
        die("--color and --port apply to one variant")
    for e in args.env:
        if "=" not in e:
            die(f"--env takes KEY=VALUE, not {e!r}")

    systemd = not args.no_systemd and have_user_systemd()
    if not args.no_systemd and not systemd:
        die("no systemd user manager here (`systemctl --user` fails); use --no-systemd to install the web "
            "build alone and run it yourself")
    tools = {"tailscale": find("tailscale", "/usr/bin/tailscale", "/usr/local/bin/tailscale",
                               "/run/current-system/sw/bin/tailscale")}

    if args.uninstall:
        results = [uninstall(vid, all_variants().get(vid, {}), tools, systemd) for vid in ids]
        print(json.dumps({"variants": results}, indent=2))
        return

    tools["node"] = find_node(*NODE_PLACES)
    if not args.skip_web and not find("pnpm"):
        die("pnpm not found (`corepack enable pnpm`, or `npm install -g pnpm`)")
    tools["pdftoppm"] = find("pdftoppm", "/usr/bin/pdftoppm", "/usr/local/bin/pdftoppm",
                             str(Path.home() / ".nix-profile/bin/pdftoppm"), "/run/current-system/sw/bin/pdftoppm")
    if not tools["pdftoppm"] and not args.no_thumbnails:
        die(POPPLER_HINT)

    tools["words"] = ensure_words()
    short, build = version()
    variants = {}
    for vid in ids:
        variants[vid] = resolve_variant(vid, args.color, args.port, WRITER)
        if args.serve is not None and variants[vid].get("serve", True) != args.serve:
            path = papyrus_root() / vid / "variant.yml"
            record = {**variants[vid], "serve": args.serve}
            write_flat_yaml(path, record, f"{display_name(vid)}: written by {WRITER}")
            variants[vid] = record
        variants[vid] = set_watch(vid, variants[vid], args.watch, WRITER)
    results = [install(vid, v, args, tools, build, systemd) for vid, v in variants.items()]

    notes = []
    if systemd and lingering() is False:
        notes.append("this account does not linger: the server runs from login to the last logout; "
                     "`sudo loginctl enable-linger $USER` keeps it running without a session")
    print(json.dumps({"version": f"{short} ({build})", **tools, "variants": results, **({"notes": notes} if notes else {})},
                     indent=2))


if __name__ == "__main__":
    main()
