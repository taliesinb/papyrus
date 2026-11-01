#!/usr/bin/env python3
"""Build Papyrus.app and Papyrus Server.app, for one or more variants (stdlib only; needs the
Command Line Tools' swiftc, node and pnpm, no Xcode).

    python3 apps/macos/build.py                 # release → apps/macos/dist/
    python3 apps/macos/build.py --install       # … and copy both into ~/Applications
    python3 apps/macos/build.py --variant personal --install      # Papyrus Personal(.app, Server.app)
    python3 apps/macos/build.py --all --install                   # every variant that has a variant.yml
    python3 apps/macos/build.py --install /Applications --env PAPYRUS_HOME=/tmp/ph
    python3 apps/macos/build.py --variant personal --watch --install  # its server also runs `papyrus watch`

A variant is a library of its own, `~/.papyrus/<id>/` (PAPYRUS_ROOT moves `~/.papyrus`), with
its apps, port, tailscale path and colour in `<home>/variant.yml`, which this script writes the
first time it builds the variant (`--color` / `--port` change it later). `release` is the
everyday one and leaves its id out of what people read and type: `Papyrus.app`, `/papyrus`,
`papyrus://`; `personal` is `Papyrus Personal.app`, `/papyrus-personal`, `papyrus-personal://`.
Everything else carries the variant name `io.github.taliesinb.papyrus.<id>` (bundle ids,
~/Library/Logs, ~/Library/Application Support), as DeepSea's variants do.

Steps: compile the two Swift executables, the page renderer and the icon renderer (cached by
source hash in apps/macos/.build/); render each variant's icon (its colour); `pnpm build` in web/
once per variant (SvelteKit's base path is fixed at build time); assemble both bundles with
`papyrus-app.json` (variant, names, port, base path, and the absolute paths of node and
tailscale, since a GUI app has no shell PATH); sign them ad hoc. Papyrus Server.app carries the
built web server in Contents/Resources/web and the page renderer `papyrus-thumb` (CoreGraphics;
no poppler on macOS) in Contents/MacOS. The variant logic is shared with the Linux installer
(apps/variants.py).

Papyrus Server.app also runs `papyrus watch` (`uv run --project <repo> …`, recorded in
papyrus-app.json as `watch`) when the variant watches: release does by default, other variants
with `--watch` (recorded in variant.yml; `--no-watch` turns it off). Being its parent, the server
is the app macOS asks for access to ~/Downloads and ~/Desktop.

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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from variants import (  # noqa: E402  (apps/variants.py, shared with apps/linux/install.py)
    FIRST_PORT, PORT_STEP, RELEASE, RELEASE_COLOR, WEB, all_variants, alive, copy_web, die, display_name, find, find_node,
    ensure_library, ensure_words, link_home, papyrus_root, record_install, resolve_variant, run, scheme, set_watch, variant_name, version,
    watch_command, watches, web_build_for,
)

WRITER = "apps/macos/build.py"
MIN_MACOS = "13.0"
# The dock tile: the glyph in the variant's colour (release: the web UI's accent) on the UI's background.
TILE_TOP, TILE_BOTTOM = "#232833", "#0d0f12"


def server_usage(server_name: str) -> dict[str, str]:
    """The server reads PDFs wherever the library found them; macOS asks the user on its behalf."""
    return {
        key: f"{server_name} reads the PDFs in your library to show them and render page thumbnails, "
             "and adds new PDFs from your Downloads and Desktop folders."
        for key in ("NSDesktopFolderUsageDescription", "NSDocumentsFolderUsageDescription",
                    "NSDownloadsFolderUsageDescription", "NSNetworkVolumesUsageDescription",
                    "NSRemovableVolumesUsageDescription", "NSFileProviderDomainUsageDescription")
    }


def library_links(vid: str) -> dict[str, Path]:
    """`<home>/logs` and `<home>/support`: the variant's directories in ~/Library."""
    library = Path.home() / "Library"
    return {"logs": library / "Logs" / variant_name(vid), "support": library / "Application Support" / variant_name(vid)}


def compile_all(cache: Path) -> Path:
    """The executables, the page renderer and the icon renderer, compiled once per source hash.
    `.build/papyrus-thumb` links to the latest page renderer, for a web server run from web/
    (`pnpm dev` / `pnpm start`) rather than from Papyrus Server.app."""
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    target = f"{arch}-apple-macos{MIN_MACOS}"
    compiler = run(["/usr/bin/xcrun", "swiftc", "--version"]).splitlines()[0]
    sources = sorted((HERE / "Sources").rglob("*.swift")) + [HERE / "Tools" / "make-icon.swift"]
    h = hashlib.sha256(f"{compiler}\n{target}\n".encode())
    for f in sources:
        h.update(str(f.relative_to(HERE)).encode() + b"\n" + f.read_bytes() + b"\n")
    out = cache / h.hexdigest()[:12]
    if (out / "done").exists():
        link_thumb(cache, out)
        return out
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shared = sorted((HERE / "Sources" / "Shared").glob("*.swift"))
    swiftc = ["/usr/bin/xcrun", "swiftc", "-O", "-target", target]
    print("compiling Papyrus, Papyrus Server, papyrus-thumb and make-icon…", file=sys.stderr)
    run(swiftc + ["-o", str(out / "Papyrus"), *map(str, shared), *map(str, sorted((HERE / "Sources" / "Papyrus").glob("*.swift"))),
                  "-framework", "Cocoa", "-framework", "WebKit"])
    run(swiftc + ["-o", str(out / "papyrus-server"), *map(str, shared), *map(str, sorted((HERE / "Sources" / "Server").glob("*.swift"))),
                  "-framework", "Cocoa", "-framework", "ServiceManagement"])
    run(swiftc + ["-o", str(out / "papyrus-thumb"), *map(str, sorted((HERE / "Sources" / "Thumb").glob("*.swift"))),
                  "-framework", "CoreGraphics", "-framework", "ImageIO"])
    run(swiftc + ["-o", str(out / "make-icon"), str(HERE / "Tools" / "make-icon.swift"), "-framework", "Cocoa"])
    (out / "done").touch()
    link_thumb(cache, out)
    return out


def link_thumb(cache: Path, out: Path) -> None:
    link = cache / "papyrus-thumb"
    target = Path(out.name) / "papyrus-thumb"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


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
             web_build: Path | None = None, helpers: tuple[Path, ...] = ()) -> Path:
    app = out / f"{name}.app"
    shutil.rmtree(app, ignore_errors=True)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    (contents / "Info.plist").write_bytes(plist)
    (contents / "PkgInfo").write_text("APPL????")
    shutil.copy2(executable, contents / "MacOS" / executable.name)
    for helper in helpers:
        shutil.copy2(helper, contents / "MacOS" / helper.name)
    shutil.copy2(icns, resources / "AppIcon.icns")
    for glyph in ("glyph.svg", "glyph-filled.svg"):  # the menu bar's scroll, and its solid paper (work queued)
        shutil.copy2(HERE / "Resources" / glyph, resources / glyph)
    (resources / "papyrus-app.json").write_text(json.dumps(config, indent=2) + "\n")
    if web_build:
        copy_web(web_build, resources / "web")
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


def build_variant(vid: str, variant: dict, args, compiled: Path, tools: dict, short: str, build: str, out: Path) -> dict:
    name = display_name(vid)
    server_name = f"{name} Server"
    app_id = variant_name(vid)
    server_id = f"{app_id}.server"
    base = str(variant["servePath"])
    url_scheme = scheme(vid)
    color = str(variant["color"])
    icns = render_icon(compiled, color)
    web_build = web_build_for(base, args.skip_web)

    config = {"variant": vid, "name": name, "serverName": server_name, "scheme": url_scheme,
              "port": int(variant["port"]), "base": base, "appBundleId": app_id, "serverBundleId": server_id,
              "color": color, **tools, "env": dict(e.split("=", 1) for e in args.env)}
    if watches(vid, variant):
        config["watch"] = watch_command()
    server = assemble(out, server_name, server_id, compiled / "papyrus-server",
                      info_plist(server_name, server_id, "papyrus-server", short, build, LSUIElement=True, **server_usage(server_name)),
                      icns, config, web_build, helpers=(compiled / "papyrus-thumb",))
    url_types = [{"CFBundleURLName": app_id, "CFBundleURLSchemes": [url_scheme]}]
    app = assemble(out, name, app_id, compiled / "Papyrus",
                   info_plist(name, app_id, "Papyrus", short, build, CFBundleURLTypes=url_types), icns, config)
    result = {"variant": vid, "app": str(app), "server": str(server), "home": str(papyrus_root() / vid),
              "port": config["port"], "base": base, "scheme": url_scheme, "color": color,
              "watch": "watch" in config}

    if args.install:
        ensure_library(vid, config["env"])
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
        record_install(vid, build, WRITER)
    return result


def main() -> None:
    if sys.platform != "darwin":
        die("macOS only (on Linux: python3 apps/linux/install.py)")
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
    p.add_argument("--watch", action=argparse.BooleanOptionalAction, default=None,
                   help="the server also runs `papyrus watch` (recorded in variant.yml; default: release only)")
    p.add_argument("--skip-web", action="store_true", help="reuse web/build as it is (it must be built for the variant's base)")
    p.add_argument("--touch-icon", action="store_true", help="also write web/static/apple-touch-icon.png")
    args = p.parse_args()

    if args.all and args.variant:
        die("--all or --variant, not both")
    ids = list(all_variants()) if args.all else list(dict.fromkeys(args.variant or [RELEASE]))
    ids.sort(key=lambda v: v == RELEASE)  # release last, so web/build is left as `pnpm start` expects (/papyrus)
    if not ids:
        die(f"no variant has a variant.yml under {papyrus_root()}")
    if len(ids) > 1 and (args.color or args.port):
        die("--color and --port apply to one variant")

    tools = {"node": find_node("/opt/homebrew/bin/node", "/usr/local/bin/node"),
             "tailscale": find("tailscale", "/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")}

    variants = {}
    for vid in ids:
        variants[vid] = set_watch(vid, resolve_variant(vid, args.color, args.port, WRITER), args.watch, WRITER)
        link_home(papyrus_root() / vid, library_links(vid))
    tools["words"] = ensure_words()
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
