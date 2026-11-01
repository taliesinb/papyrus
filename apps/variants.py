"""Variants, shared by the platform installers (stdlib only): apps/macos/build.py and
apps/linux/install.py.

A variant is a library of its own, `~/.papyrus/<id>/` (PAPYRUS_ROOT moves `~/.papyrus`), with
its port, tailscale path and colour in `<home>/variant.yml`, written the first time an installer
builds the variant. `release` is the everyday one and leaves its id out of what people read and
type (`Papyrus`, `/papyrus`, `papyrus://`); everything else carries the variant name
`io.github.taliesinb.papyrus.<id>` (bundle ids, systemd units, log and data directories), as
DeepSea's variants do. The naming table is in apps/macos/README.md, "Variants".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APPS = Path(__file__).resolve().parent
REPO = APPS.parent
WEB = REPO / "web"

ROOT_NAME = "io.github.taliesinb.papyrus"
RELEASE = "release"
VARIANT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIRST_PORT, PORT_STEP = 5182, 10
# release: the web UI's accent. New variants take the first colour no other variant uses
# (the first few are DeepSea's).
RELEASE_COLOR = "#7fb0ff"
PALETTE = ["#8E4EC6", "#F76B15", "#30A46C", "#E5484D", "#12A594", "#FFC53D", "#D6409F"]
MIN_NODE = (22, 5)  # node:sqlite


def die(message: str) -> None:
    sys.exit(f"{Path(sys.argv[0]).name}: {message}")


def papyrus_root() -> Path:
    return Path(os.environ.get("PAPYRUS_ROOT") or Path.home() / ".papyrus").expanduser()


def variant_name(vid: str) -> str:
    """`io.github.taliesinb.papyrus.<id>`: bundle ids, unit names, per-variant directories."""
    return f"{ROOT_NAME}.{vid}"


def display_name(vid: str) -> str:
    """`release` → Papyrus; `personal` → Papyrus Personal; `big-lab` → Papyrus Big Lab."""
    return "Papyrus" if vid == RELEASE else "Papyrus " + " ".join(w[:1].upper() + w[1:] for w in vid.split("-"))


def scheme(vid: str) -> str:
    return "papyrus" if vid == RELEASE else f"papyrus-{vid}"


def read_flat_yaml(path: Path) -> dict[str, str | int | bool]:
    """`key: value` lines (the flat YAML DeepSea's variant.yml uses): comments, quoted strings, ints, booleans."""
    out: dict[str, str | int | bool] = {}
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


def write_flat_yaml(path: Path, data: dict, header: str) -> None:
    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return json.dumps(v) if re.search(r"[#:'\"]|^\s|\s$", v) else v
    path.write_text(f"# {header}\n" + "".join(f"{k}: {fmt(v)}\n" for k, v in data.items()))


def all_variants() -> dict[str, dict]:
    root = papyrus_root()
    if not root.is_dir():
        return {}
    return {p.name: read_flat_yaml(p / "variant.yml") for p in sorted(root.iterdir())
            if VARIANT_ID.match(p.name) and (p / "variant.yml").is_file()}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def resolve_variant(vid: str, color: str | None, port: int | None, writer: str) -> dict:
    """The variant's configuration, written to `<home>/variant.yml` the first time (and when
    --color/--port change it). `writer` names the installer in the file's header."""
    if not VARIANT_ID.match(vid):
        die(f"{vid!r} is not a variant id (lowercase letters, digits and single dashes)")
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        die("--color takes #rrggbb")
    root = papyrus_root()
    home = root / vid
    if vid == RELEASE and not home.exists() and (root / "papyrus.sqlite").exists():
        die(f"{root} holds a library in the old layout; move its contents into {home}/ first")
    others = {k: v for k, v in all_variants().items() if k != vid}
    path = home / "variant.yml"
    config = read_flat_yaml(path) if path.is_file() else {}
    changed = not config
    if not config:
        used_colors = {str(v.get("color", "")).lower() for v in others.values()}
        used_ports = {v.get("port") for v in others.values()}
        if vid == RELEASE:
            first_port, default_color = FIRST_PORT, RELEASE_COLOR
        else:
            first_port = next(p for p in range(FIRST_PORT + PORT_STEP, 65000, PORT_STEP) if p not in used_ports and port_free(p))
            default_color = next((c for c in PALETTE if c.lower() not in used_colors), PALETTE[0])
        config = {"id": vid, "display": display_name(vid), "color": default_color, "port": first_port,
                  "servePath": "/papyrus" if vid == RELEASE else f"/papyrus-{vid}",
                  "created": datetime.now().astimezone().isoformat(timespec="seconds")}
    for key, value in (("color", color), ("port", port)):
        if value is not None and config.get(key) != value:
            config[key] = value
            changed = True
    clash = [k for k, v in others.items() if v.get("port") == config["port"]]
    if clash:
        die(f"port {config['port']} is already variant {clash[0]}'s (--port to choose another)")
    if changed:
        existed = path.exists()
        home.mkdir(parents=True, exist_ok=True)
        write_flat_yaml(path, config, f"{display_name(vid)}: written by {writer}")
        print(f"{path}: {'updated' if existed else 'written'}", file=sys.stderr)
    return config


def record_install(vid: str, build: str, writer: str, **extra) -> None:
    """Note in variant.yml which commit was installed, and when."""
    path = papyrus_root() / vid / "variant.yml"
    write_flat_yaml(path, {**read_flat_yaml(path), "build": build, **extra,
                           "installed": datetime.now().astimezone().isoformat(timespec="seconds")},
                    f"{display_name(vid)}: written by {writer}")


def ensure_library(vid: str, server_env: dict[str, str]) -> bool:
    """Create a new variant's empty library with the CLI (db.init), since the web server opens the
    database read-only and cannot. Skipped when the server is pointed elsewhere (PAPYRUS_HOME or
    PAPYRUS_DB in its environment). Returns whether it created one."""
    if "PAPYRUS_HOME" in server_env or "PAPYRUS_DB" in server_env:
        return False
    if (papyrus_root() / vid / "papyrus.sqlite").exists():
        return False
    uv = find_uv()
    if not uv:
        die("uv not found (https://docs.astral.sh/uv/): the papyrus CLI runs with it, and it creates the new "
            f"library here; or create it yourself with `papyrus --variant {vid} stats`")
    print(f"creating the library {papyrus_root() / vid} (papyrus --variant {vid} stats)…", file=sys.stderr)
    env = {k: v for k, v in os.environ.items() if k not in ("PAPYRUS_HOME", "PAPYRUS_DB", "PAPYRUS_VARIANT")}
    run([uv, "run", "--project", str(REPO), "papyrus", "--variant", vid, "stats"],
        env={**env, "PAPYRUS_ROOT": str(papyrus_root())})
    return True


WORDS = REPO / "src" / "papyrus" / "data" / "words.txt"


def ensure_words() -> bool:
    """Fetch the common-words list (tools/words/fetch.py) unless it is there. Without it papyrus
    still works (small caps stay split in triage's text), so a failed download only warns.
    Returns whether the list is there."""
    if WORDS.exists():
        return True
    uv = find_uv()
    if not uv:
        print(f"{Path(sys.argv[0]).name}: uv not found; skipping the word list (tools/words/fetch.py)", file=sys.stderr)
        return False
    print("fetching the word list (tools/words/fetch.py)…", file=sys.stderr)
    r = subprocess.run([uv, "run", "--project", str(REPO), "--frozen", "--quiet", "python",
                        str(REPO / "tools" / "words" / "fetch.py")], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"{Path(sys.argv[0]).name}: the word list could not be fetched; small caps stay split in triage's "
              f"text until `uv run python tools/words/fetch.py` succeeds:\n{(r.stderr or r.stdout).strip()[-1000:]}",
              file=sys.stderr)
    return WORDS.exists()


def find_uv() -> str | None:
    return find("uv", str(Path.home() / ".local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv")


def watches(vid: str, variant: dict) -> bool:
    """Whether the variant runs `papyrus watch` (variant.yml `watch`). Default: release only, since
    every variant's watcher would add the same ~/Downloads to its own library."""
    return bool(variant.get("watch", vid == RELEASE))


def set_watch(vid: str, variant: dict, value: bool | None, writer: str) -> dict:
    """Record --watch / --no-watch in variant.yml (when given and different)."""
    if value is None or watches(vid, variant) == value:
        return variant
    record = {**variant, "watch": value}
    write_flat_yaml(papyrus_root() / vid / "variant.yml", record, f"{display_name(vid)}: written by {writer}")
    return record


def watch_command() -> list[str]:
    """`papyrus watch` from this checkout, as a service runs it (absolute paths: no shell PATH). The
    CLI runs from the repository with uv, so a `git pull` reaches the watcher when it next starts;
    PAPYRUS_VARIANT (and PAPYRUS_HOME, …) come from the service's environment."""
    uv = find_uv()
    if not uv:
        die("uv not found (https://docs.astral.sh/uv/): `papyrus watch` runs with it (or install with --no-watch)")
    return [uv, "run", "--project", str(REPO), "--frozen", "--quiet", "papyrus", "watch"]


def link_home(home: Path, links: dict[str, Path]) -> None:
    """`<home>/logs`, `<home>/support`, … lead to the variant's directories elsewhere
    (~/Library on macOS, the XDG directories on Linux), as DeepSea's homes do."""
    for link, target in links.items():
        target.mkdir(parents=True, exist_ok=True)
        p = home / link
        if p.is_symlink() and Path(os.readlink(p)) == target:
            continue
        if p.is_symlink():
            p.unlink()
        elif p.exists():
            print(f"{Path(sys.argv[0]).name}: {p} exists and is not a link; leaving it", file=sys.stderr)
            continue
        p.symlink_to(target)


def run(cmd: list[str], **kw) -> str:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout
    except FileNotFoundError:
        die(f"{cmd[0]} not found")
    except subprocess.CalledProcessError as e:
        die(f"{' '.join(cmd[:3])}… failed:\n{(e.stderr or e.stdout).strip()[-3000:]}")


def find(name: str, *extra: str) -> str | None:
    for candidate in (shutil.which(name), *extra):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_node(*extra: str) -> str:
    """Node 22.5+ (the web server uses node:sqlite), as an absolute path: services and GUI apps
    have no shell PATH."""
    node = find("node", *extra)
    if not node:
        die("node not found (the web server needs Node 22.5+)")
    raw = run([node, "--version"]).strip().lstrip("v")
    have = tuple(int(x) for x in raw.split(".")[:2])
    if have < MIN_NODE:
        die(f"{node} is Node {raw}; the web server needs Node {MIN_NODE[0]}.{MIN_NODE[1]}+ (node:sqlite)")
    # As found, not resolved: Homebrew's link survives `brew upgrade node`, its Cellar path does not.
    return node


def version() -> tuple[str, str]:
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    short = m.group(1) if m else "0.0.0"
    try:
        rev = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], check=True, text=True, capture_output=True).stdout.strip()
        return short, rev + ("+" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return short, "0"


def build_web(base: str) -> Path:
    """`pnpm build` in web/ served under `base` (SvelteKit fixes it at build time)."""
    web_build = WEB / "build"
    if not (WEB / "node_modules").is_dir():
        print("installing web/ dependencies (pnpm install)…", file=sys.stderr)
        run(["pnpm", "install", "--frozen-lockfile"], cwd=WEB)
    print(f"building web/ under {base} (pnpm build)…", file=sys.stderr)
    run(["pnpm", "build"], cwd=WEB, env={**os.environ, "PAPYRUS_WEB_BASE": base})
    if built_base(web_build) != base:
        die(f"web/build is not served under {base} after pnpm build")
    return web_build


def built_base(web_build: Path) -> str | None:
    try:
        m = re.search(r'^export const base = "([^"]*)";', (web_build / "adapter-node.js").read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def web_build_for(base: str, skip_web: bool) -> Path:
    """web/build served under `base`: built now, or (skip_web) checked as it is."""
    if skip_web:
        web_build = WEB / "build"
        if built_base(web_build) != base:
            die(f"web/build is served under {built_base(web_build)}, not {base} (run without --skip-web)")
    else:
        web_build = build_web(base)
    if not (web_build / "index.js").exists():
        die("web/build has no index.js (run without --skip-web)")
    return web_build


def copy_web(web_build: Path, dest: Path) -> None:
    """The built server and the package.json beside it (adapter-node's output is ESM: node needs
    the package's "type": "module")."""
    shutil.copytree(web_build, dest / "build")
    shutil.copy2(WEB / "package.json", dest / "package.json")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
