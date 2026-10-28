#!/usr/bin/env python3
"""Build Papyrus.app and Papyrus Server.app (stdlib only; needs the Command
Line Tools' swiftc, node and pnpm, no Xcode).

    python3 apps/macos/build.py                 # → apps/macos/dist/
    python3 apps/macos/build.py --install       # … and copy both into ~/Applications
    python3 apps/macos/build.py --install /Applications --env PAPYRUS_HOME=/tmp/ph

Steps: `pnpm build` in web/ (served under /papyrus); compile the two Swift
executables and the icon renderer (cached by source hash in
apps/macos/.build/); render the icon; assemble both bundles with
`papyrus-app.json` (port, base path, and the absolute paths of node,
tailscale and pdftoppm, since a GUI app has no shell PATH); sign them ad hoc.
Papyrus Server.app carries the built web server in Contents/Resources/web.

`--install` quits running copies, replaces the installed bundles and starts
Papyrus Server again if it was running. `--touch-icon` also rewrites
web/static/apple-touch-icon.png from the same renderer.
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
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WEB = REPO / "web"

APP_NAME = "Papyrus"
SERVER_NAME = "Papyrus Server"
APP_ID = "io.github.taliesinb.papyrus"
SERVER_ID = "io.github.taliesinb.papyrus.server"
BASE = "/papyrus"
DEFAULT_PORT = 5182
MIN_MACOS = "13.0"
# The dock tile: the web UI's accent on its background.
GLYPH_COLOR, TILE_TOP, TILE_BOTTOM = "#7fb0ff", "#232833", "#0d0f12"

# The server reads PDFs wherever the library found them; macOS asks the user on its behalf.
SERVER_USAGE = {
    key: "Papyrus Server reads the PDFs in your library to show them and render page thumbnails."
    for key in ("NSDesktopFolderUsageDescription", "NSDocumentsFolderUsageDescription",
                "NSDownloadsFolderUsageDescription", "NSNetworkVolumesUsageDescription",
                "NSRemovableVolumesUsageDescription", "NSFileProviderDomainUsageDescription")
}


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


def render_icon(compiled: Path) -> Path:
    glyph = HERE / "Resources" / "glyph.svg"
    tag = hashlib.sha256(glyph.read_bytes() + f"{GLYPH_COLOR}{TILE_TOP}{TILE_BOTTOM}".encode()).hexdigest()[:12]
    icns = compiled / f"AppIcon-{tag}.icns"
    if not icns.exists():
        iconset = compiled / f"AppIcon-{tag}.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        run([str(compiled / "make-icon"), str(glyph), "--iconset", str(iconset), "--glyph-color", GLYPH_COLOR,
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


def quit_app(bundle_id: str) -> bool:
    """Ask a running app to quit. Returns whether it was running."""
    running = run(["/usr/bin/osascript", "-e", f'application id "{bundle_id}" is running']).strip() == "true"
    if running:
        subprocess.run(["/usr/bin/osascript", "-e", f'tell application id "{bundle_id}" to quit'], capture_output=True)
        for _ in range(50):
            if run(["/usr/bin/osascript", "-e", f'application id "{bundle_id}" is running']).strip() != "true":
                break
            time.sleep(0.2)
    return running


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("build.py: macOS only")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=HERE / "dist", help="where to build the bundles (default apps/macos/dist)")
    p.add_argument("--install", nargs="?", const=Path.home() / "Applications", type=Path, metavar="DIR",
                   help="copy both apps into DIR (default ~/Applications)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"the web server's loopback port (default {DEFAULT_PORT})")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="environment for the web server, e.g. PAPYRUS_HOME=/tmp/ph (repeatable)")
    p.add_argument("--skip-web", action="store_true", help="reuse web/build as it is")
    p.add_argument("--touch-icon", action="store_true", help="also write web/static/apple-touch-icon.png")
    args = p.parse_args()

    node = find("node", "/opt/homebrew/bin/node", "/usr/local/bin/node")
    if not node:
        sys.exit("build.py: node not found")
    major = int(run([node, "--version"]).strip().lstrip("v").split(".")[0])
    if major < 22:
        sys.exit(f"build.py: {node} is Node {major}; the web server needs Node 22.5+ (node:sqlite)")
    tailscale = find("tailscale", "/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    pdftoppm = find("pdftoppm", "/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")
    env = dict(e.split("=", 1) for e in args.env)

    compiled = compile_all(HERE / ".build")
    icns = render_icon(compiled)
    if args.touch_icon:
        run([str(compiled / "make-icon"), str(HERE / "Resources" / "glyph.svg"), "--png", "180", str(WEB / "static" / "apple-touch-icon.png"),
             "--glyph-color", GLYPH_COLOR, "--tile-top", TILE_TOP, "--tile-bottom", TILE_BOTTOM])

    if not args.skip_web:
        print("building web/ (pnpm build)…", file=sys.stderr)
        build_env = {k: v for k, v in os.environ.items() if k != "PAPYRUS_WEB_BASE"}
        run(["pnpm", "build"], cwd=WEB, env=build_env)
    web_build = WEB / "build"
    if not (web_build / "index.js").exists():
        sys.exit("build.py: web/build has no index.js (run without --skip-web)")

    short, build = version()
    config = {"port": args.port, "base": BASE, "appBundleId": APP_ID, "serverBundleId": SERVER_ID,
              "node": node, "tailscale": tailscale, "pdftoppm": pdftoppm, "env": env}
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    server = assemble(out, SERVER_NAME, SERVER_ID, compiled / "papyrus-server",
                      info_plist(SERVER_NAME, SERVER_ID, "papyrus-server", short, build, LSUIElement=True, **SERVER_USAGE),
                      icns, config, web_build)
    url_types = [{"CFBundleURLName": APP_ID, "CFBundleURLSchemes": ["papyrus"]}]
    app = assemble(out, APP_NAME, APP_ID, compiled / "Papyrus",
                   info_plist(APP_NAME, APP_ID, "Papyrus", short, build, CFBundleURLTypes=url_types), icns, config)
    result = {"app": str(app), "server": str(server), "version": f"{short} ({build})", "node": node,
              "tailscale": tailscale, "pdftoppm": pdftoppm, "port": args.port}

    if args.install:
        dest = args.install.expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        app_was_running = quit_app(APP_ID)
        server_was_running = quit_app(SERVER_ID)
        for bundle in (server, app):
            target = dest / bundle.name
            shutil.rmtree(target, ignore_errors=True)
            run(["/usr/bin/ditto", str(bundle), str(target)])
        result["installed"] = [str(dest / server.name), str(dest / app.name)]
        if server_was_running:
            run(["/usr/bin/open", "-g", str(dest / server.name)])
        if app_was_running:
            run(["/usr/bin/open", str(dest / app.name)])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
