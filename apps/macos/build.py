#!/usr/bin/env python3
"""Build Papyrus.app and Papyrus Server.app, for one or more variants (stdlib only; needs the
Command Line Tools' swiftc, node and pnpm, no Xcode).

    python3 apps/macos/build.py                 # release → apps/macos/dist/
    python3 apps/macos/build.py --install       # … and copy both into ~/Applications
    python3 apps/macos/build.py --variant personal --install      # Papyrus Personal(.app, Server.app)
    python3 apps/macos/build.py --all --install                   # every variant that has a variant.yml
    python3 apps/macos/build.py --install /Applications --env PAPYRUS_HOME=/tmp/ph

A variant is a library of its own, `~/.papyrus/<id>/` (PAPYRUS_ROOT moves `~/.papyrus`), with
its apps, port, tailscale path and colour in `<home>/variant.yml`, which this script writes the
first time it builds the variant (`--color` / `--port` change it later). `release` is the
everyday one and leaves its id out of what people read and type: `Papyrus.app`, `/papyrus`,
`papyrus://`; `personal` is `Papyrus Personal.app`, `/papyrus-personal`, `papyrus-personal://`.
Everything else carries the variant name `io.github.taliesinb.papyrus.<id>` (bundle ids,
~/Library/Logs, ~/Library/Application Support), as DeepSea's variants do.

Steps: compile the two Swift executables and the icon renderer (cached by source hash in
apps/macos/.build/); render each variant's icon (its colour); `pnpm build` in web/ once per
variant (SvelteKit's base path is fixed at build time); assemble both bundles with
`papyrus-app.json` (variant, names, port, base path, and the absolute paths of node, tailscale
and pdftoppm, since a GUI app has no shell PATH); sign them ad hoc. Papyrus Server.app carries
the built web server in Contents/Resources/web.

`--install` quits the variant's running copies, replaces its installed bundles and starts them
again if they were running; other variants' apps are left alone. `--touch-icon` also rewrites
web/static/apple-touch-icon.png from the same renderer (release's colour).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WEB = REPO / "web"

ROOT_NAME = "io.github.taliesinb.papyrus"
RELEASE = "release"
VARIANT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIRST_PORT, PORT_STEP = 5182, 10
MIN_MACOS = "13.0"
# The dock tile: the glyph in the variant's colour (release: the web UI's accent) on the UI's background.
RELEASE_COLOR, TILE_TOP, TILE_BOTTOM = "#7fb0ff", "#232833", "#0d0f12"
# New variants take the first colour no other variant uses (the first few are DeepSea's).
PALETTE = ["#8E4EC6", "#F76B15", "#30A46C", "#E5484D", "#12A594", "#FFC53D", "#D6409F"]


def server_usage(server_name: str) -> dict[str, str]:
    """The server reads PDFs wherever the library found them; macOS asks the user on its behalf."""
    return {
        key: f"{server_name} reads the PDFs in your library to show them and render page thumbnails."
        for key in ("NSDesktopFolderUsageDescription", "NSDocumentsFolderUsageDescription",
                    "NSDownloadsFolderUsageDescription", "NSNetworkVolumesUsageDescription",
                    "NSRemovableVolumesUsageDescription", "NSFileProviderDomainUsageDescription")
    }


# --- variants -------------------------------------------------------------------------------

def papyrus_root() -> Path:
    return Path(os.environ.get("PAPYRUS_ROOT") or Path.home() / ".papyrus").expanduser()


def display_name(vid: str) -> str:
    """`release` → Papyrus; `personal` → Papyrus Personal; `big-lab` → Papyrus Big Lab."""
    return "Papyrus" if vid == RELEASE else "Papyrus " + " ".join(w[:1].upper() + w[1:] for w in vid.split("-"))


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


def resolve_variant(vid: str, color: str | None, port: int | None) -> dict:
    """The variant's configuration, written to `<home>/variant.yml` the first time (and when --color/--port change it)."""
    if not VARIANT_ID.match(vid):
        sys.exit(f"build.py: {vid!r} is not a variant id (lowercase letters, digits and single dashes)")
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        sys.exit("build.py: --color takes #rrggbb")
    root = papyrus_root()
    home = root / vid
    if vid == RELEASE and not home.exists() and (root / "papyrus.sqlite").exists():
        sys.exit(f"build.py: {root} holds a library in the old layout; move its contents into {home}/ first")
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
        sys.exit(f"build.py: port {config['port']} is already variant {clash[0]}'s (--port to choose another)")
    if changed:
        existed = path.exists()
        home.mkdir(parents=True, exist_ok=True)
        write_flat_yaml(path, config, f"{display_name(vid)}: written by apps/macos/build.py")
        print(f"{path}: {'updated' if existed else 'written'}", file=sys.stderr)
    link_home(vid, home)
    return config


def link_home(vid: str, home: Path) -> None:
    """`<home>/logs` and `<home>/support` lead to the variant's directories in ~/Library (as DeepSea's homes do)."""
    library = Path.home() / "Library"
    for link, target in (("logs", library / "Logs" / f"{ROOT_NAME}.{vid}"),
                         ("support", library / "Application Support" / f"{ROOT_NAME}.{vid}")):
        target.mkdir(parents=True, exist_ok=True)
        p = home / link
        if p.is_symlink() and Path(os.readlink(p)) == target:
            continue
        if p.is_symlink():
            p.unlink()
        elif p.exists():
            print(f"build.py: {p} exists and is not a link; leaving it", file=sys.stderr)
            continue
        p.symlink_to(target)


def run(cmd: list[str], **kw) -> str:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"build.py: {' '.join(cmd[:3])}… failed:\n{(e.stderr or e.stdout).strip()[-3000:]}")


def find(name: str, *extra: str) -> str | None:
    for candidate in (shutil.which(name), *extra):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def version() -> tuple[str, str]:
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    short = m.group(1) if m else "0.0.0"
    try:
        rev = run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"]).strip()
        dirty = run(["git", "-C", str(REPO), "status", "--porcelain"]).strip()
        return short, rev + ("+" if dirty else "")
    except SystemExit:
        return short, "0"


def compile_all(cache: Path) -> Path:
    """The executables and the icon renderer, compiled once per source hash."""
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    target = f"{arch}-apple-macos{MIN_MACOS}"
    compiler = run(["/usr/bin/xcrun", "swiftc", "--version"]).splitlines()[0]
    sources = sorted((HERE / "Sources").rglob("*.swift")) + [HERE / "Tools" / "make-icon.swift"]
    h = hashlib.sha256(f"{compiler}\n{target}\n".encode())
    for f in sources:
        h.update(str(f.relative_to(HERE)).encode() + b"\n" + f.read_bytes() + b"\n")
    out = cache / h.hexdigest()[:12]
    if (out / "done").exists():
        return out
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shared = sorted((HERE / "Sources" / "Shared").glob("*.swift"))
    swiftc = ["/usr/bin/xcrun", "swiftc", "-O", "-target", target]
    print("compiling Papyrus, Papyrus Server and make-icon…", file=sys.stderr)
    run(swiftc + ["-o", str(out / "Papyrus"), *map(str, shared), *map(str, sorted((HERE / "Sources" / "Papyrus").glob("*.swift"))),
                  "-framework", "Cocoa", "-framework", "WebKit"])
    run(swiftc + ["-o", str(out / "papyrus-server"), *map(str, shared), *map(str, sorted((HERE / "Sources" / "Server").glob("*.swift"))),
                  "-framework", "Cocoa", "-framework", "ServiceManagement"])
    run(swiftc + ["-o", str(out / "make-icon"), str(HERE / "Tools" / "make-icon.swift"), "-framework", "Cocoa"])
    (out / "done").touch()
    return out


def render_icon(compiled: Path, glyph_color: str) -> Path:
    glyph = HERE / "Resources" / "glyph.svg"
    tag = hashlib.sha256(glyph.read_bytes() + f"{glyph_color}{TILE_TOP}{TILE_BOTTOM}".encode()).hexdigest()[:12]
    icns = compiled / f"AppIcon-{tag}.icns"
    if not icns.exists():
        iconset = compiled / f"AppIcon-{tag}.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        run([str(compiled / "make-icon"), str(glyph), "--iconset", str(iconset), "--glyph-color", glyph_color,
             "--tile-top", TILE_TOP, "--tile-bottom", TILE_BOTTOM])
        run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(icns)])
        shutil.rmtree(iconset)
    return icns


def info_plist(name: str, bundle_id: str, executable: str, short: str, build: str, **extra) -> bytes:
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": name,
        "CFBundleExecutable": executable,
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": short,
        "CFBundleVersion": build,
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSHumanReadableCopyright": "Icon: boxicons \u201cscroll\u201d (MIT).",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        **extra,
    }
    return plistlib.dumps(plist)


def assemble(out: Path, name: str, bundle_id: str, executable: Path, plist: bytes, icns: Path, config: dict,
             web_build: Path | None = None) -> Path:
    app = out / f"{name}.app"
    shutil.rmtree(app, ignore_errors=True)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    (contents / "Info.plist").write_bytes(plist)
    (contents / "PkgInfo").write_text("APPL????")
    shutil.copy2(executable, contents / "MacOS" / executable.name)
    shutil.copy2(icns, resources / "AppIcon.icns")
    shutil.copy2(HERE / "Resources" / "glyph.svg", resources / "glyph.svg")
    (resources / "papyrus-app.json").write_text(json.dumps(config, indent=2) + "\n")
    if web_build:
        shutil.copytree(web_build, resources / "web" / "build")
        # adapter-node's output is ESM: node needs the package's "type": "module" beside it.
        shutil.copy2(WEB / "package.json", resources / "web" / "package.json")
    run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", bundle_id, str(app)])
    return app


def quit_app(bundle_name: str, executable: str) -> bool:
    """Quit every running copy of one variant's app (`<bundle_name>.app`, wherever it is). Returns whether one was running.

    By process, not bundle id: AppleScript's `application id` fails outright for an id LaunchServices
    does not know (a bundle that was removed or never opened). The bundle's file name tells the
    variants apart, since every variant's executables have the same names. Both apps quit cleanly on
    SIGTERM (the server stops its web server and withdraws its tailscale path first).
    """
    found = subprocess.run(["/usr/bin/pgrep", "-x", executable], capture_output=True, text=True).stdout.split()
    pids = []
    for pid in map(int, found):
        path = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if path.endswith(f"/{bundle_name}.app/Contents/MacOS/{executable}"):
            pids.append(pid)
            os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not any(alive(pid) for pid in pids):
            break
        time.sleep(0.2)
    for pid in pids:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    return bool(pids)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def build_web(base: str) -> Path:
    """`pnpm build` in web/ served under `base`, unless web/build already is."""
    web_build = WEB / "build"
    print(f"building web/ under {base} (pnpm build)…", file=sys.stderr)
    run(["pnpm", "build"], cwd=WEB, env={**os.environ, "PAPYRUS_WEB_BASE": base})
    if built_base(web_build) != base:
        sys.exit(f"build.py: web/build is not served under {base} after pnpm build")
    return web_build


def built_base(web_build: Path) -> str | None:
    try:
        m = re.search(r'^export const base = "([^"]*)";', (web_build / "adapter-node.js").read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def build_variant(vid: str, variant: dict, args, compiled: Path, tools: dict, short: str, build: str, out: Path) -> dict:
    name = display_name(vid)
    server_name = f"{name} Server"
    app_id = f"{ROOT_NAME}.{vid}"
    server_id = f"{app_id}.server"
    base = str(variant["servePath"])
    scheme = "papyrus" if vid == RELEASE else f"papyrus-{vid}"
    color = str(variant["color"])
    icns = render_icon(compiled, color)

    if args.skip_web:
        web_build = WEB / "build"
        if built_base(web_build) != base:
            sys.exit(f"build.py: web/build is served under {built_base(web_build)}, not {base} (run without --skip-web)")
    else:
        web_build = build_web(base)
    if not (web_build / "index.js").exists():
        sys.exit("build.py: web/build has no index.js (run without --skip-web)")

    config = {"variant": vid, "name": name, "serverName": server_name, "scheme": scheme,
              "port": int(variant["port"]), "base": base, "appBundleId": app_id, "serverBundleId": server_id,
              "color": color, **tools, "env": dict(e.split("=", 1) for e in args.env)}
    server = assemble(out, server_name, server_id, compiled / "papyrus-server",
                      info_plist(server_name, server_id, "papyrus-server", short, build, LSUIElement=True, **server_usage(server_name)),
                      icns, config, web_build)
    url_types = [{"CFBundleURLName": app_id, "CFBundleURLSchemes": [scheme]}]
    app = assemble(out, name, app_id, compiled / "Papyrus",
                   info_plist(name, app_id, "Papyrus", short, build, CFBundleURLTypes=url_types), icns, config)
    result = {"variant": vid, "app": str(app), "server": str(server), "home": str(papyrus_root() / vid),
              "port": config["port"], "base": base, "scheme": scheme, "color": color}

    if args.install:
        dest = args.install.expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        app_was_running = quit_app(name, "Papyrus")
        server_was_running = quit_app(server_name, "papyrus-server")
        for bundle in (server, app):
            target = dest / bundle.name
            shutil.rmtree(target, ignore_errors=True)
            run(["/usr/bin/ditto", str(bundle), str(target)])
        result["installed"] = [str(dest / server.name), str(dest / app.name)]
        if server_was_running:
            run(["/usr/bin/open", "-g", str(dest / server.name)])
        if app_was_running:
            run(["/usr/bin/open", str(dest / app.name)])
        path = papyrus_root() / vid / "variant.yml"
        write_flat_yaml(path, {**read_flat_yaml(path), "build": build,
                               "installed": datetime.now().astimezone().isoformat(timespec="seconds")},
                        f"{name}: written by apps/macos/build.py")
    return result


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("build.py: macOS only")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", action="append", metavar="ID",
                   help=f"the variant to build (repeatable; default {RELEASE}); a new id makes ~/.papyrus/<ID>/variant.yml")
    p.add_argument("--all", action="store_true", help="build every variant that has a variant.yml")
    p.add_argument("--out", type=Path, default=HERE / "dist", help="where to build the bundles (default apps/macos/dist)")
    p.add_argument("--install", nargs="?", const=Path.home() / "Applications", type=Path, metavar="DIR",
                   help="copy the apps into DIR (default ~/Applications)")
    p.add_argument("--color", metavar="#RRGGBB", help="set the variant's colour (the Dock icon's glyph; recorded in variant.yml)")
    p.add_argument("--port", type=int, help=f"set the variant's loopback port (recorded; new variants take {FIRST_PORT}, "
                   f"{FIRST_PORT + PORT_STEP}, …)")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="environment for the web server, e.g. PAPYRUS_HOME=/tmp/ph (repeatable)")
    p.add_argument("--skip-web", action="store_true", help="reuse web/build as it is (it must be built for the variant's base)")
    p.add_argument("--touch-icon", action="store_true", help="also write web/static/apple-touch-icon.png")
    args = p.parse_args()

    if args.all and args.variant:
        sys.exit("build.py: --all or --variant, not both")
    ids = list(all_variants()) if args.all else list(dict.fromkeys(args.variant or [RELEASE]))
    ids.sort(key=lambda v: v == RELEASE)  # release last, so web/build is left as `pnpm start` expects (/papyrus)
    if not ids:
        sys.exit(f"build.py: no variant has a variant.yml under {papyrus_root()}")
    if len(ids) > 1 and (args.color or args.port):
        sys.exit("build.py: --color and --port apply to one variant")

    node = find("node", "/opt/homebrew/bin/node", "/usr/local/bin/node")
    if not node:
        sys.exit("build.py: node not found")
    major = int(run([node, "--version"]).strip().lstrip("v").split(".")[0])
    if major < 22:
        sys.exit(f"build.py: {node} is Node {major}; the web server needs Node 22.5+ (node:sqlite)")
    tools = {"node": node,
             "tailscale": find("tailscale", "/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
             "pdftoppm": find("pdftoppm", "/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")}

    variants = {vid: resolve_variant(vid, args.color, args.port) for vid in ids}
    compiled = compile_all(HERE / ".build")
    if args.touch_icon:
        run([str(compiled / "make-icon"), str(HERE / "Resources" / "glyph.svg"), "--png", "180", str(WEB / "static" / "apple-touch-icon.png"),
             "--glyph-color", RELEASE_COLOR, "--tile-top", TILE_TOP, "--tile-bottom", TILE_BOTTOM])
    short, build = version()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = [build_variant(vid, v, args, compiled, tools, short, build, out) for vid, v in variants.items()]
    print(json.dumps({"version": f"{short} ({build})", **tools, "variants": results}, indent=2))


if __name__ == "__main__":
    main()
