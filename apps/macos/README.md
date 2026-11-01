# apps/macos: Papyrus.app and Papyrus Server.app

Two native apps around the read-only web browser in [`web/`](../../web):

| app | bundle id | what it is |
|---|---|---|
| **Papyrus Server.app** | `io.github.taliesinb.papyrus.release.server` | A menu-bar app with no Dock tile. It runs the web server on `127.0.0.1:5182` under `/papyrus` and publishes it on the tailnet with `tailscale serve`. It also runs `papyrus watch`, which adds new PDFs from `~/Downloads` and `~/Desktop`. |
| **Papyrus.app** | `io.github.taliesinb.papyrus.release` | A window (WKWebView) on the library. It starts Papyrus Server.app when nothing answers. |

That is the `release` variant; every other variant has a pair of its own
(`Papyrus Personal.app`, `Papyrus Personal Server.app`), see
[Variants](#variants).

Both are read-only: the Python CLI (`papyrus scan`, `download`, `ingest`,
`watch`, …) is still the only writer. The pipeline's writes show up in the open window as
"library updated · refresh", as in a browser.

```sh
python3 apps/macos/build.py                # → apps/macos/dist/{Papyrus,Papyrus Server}.app
python3 apps/macos/build.py --install      # … and copy both into ~/Applications
python3 apps/macos/build.py --variant personal --install   # Papyrus Personal(.app, Server.app)
python3 apps/macos/build.py --all --install                # every variant (after a code change)
```

`build.py` uses only the standard library. It needs the Command Line Tools'
`swiftc` (no Xcode), Node 22.5+ (for `node:sqlite`) and pnpm. It:

1. compiles the two executables, the page renderer `papyrus-thumb` and the
   icon renderer, cached by source hash in `apps/macos/.build/` (shared by
   every variant; `.build/papyrus-thumb` links to the latest renderer);
2. renders the icon in the variant's colour;
3. runs `pnpm build` in `web/` under the variant's base path (SvelteKit fixes
   it at build time, so each variant has a web build of its own);
4. assembles and ad-hoc signs both bundles.

Papyrus Server.app carries the built web server in `Contents/Resources/web`,
so a change to `web/` needs a rebuild of the app. It also carries
`Contents/MacOS/papyrus-thumb`, which renders page thumbnails with
CoreGraphics (`Sources/Thumb`), so macOS needs no poppler. The web server
falls back to poppler's `pdftoppm` without it, and on Linux
([`apps/linux`](../linux/README.md)).

The variant rules (`variant.yml`, ports, colours, the web build) are in
[`apps/variants.py`](../variants.py), shared with the Linux installer.
`--install` also creates a new variant's empty library with the CLI
(`papyrus --variant <v> stats`, through uv), because the web server opens the
database read-only and can't create it.

Each bundle has a `Contents/Resources/papyrus-app.json`:

```json
{ "variant": "personal", "name": "Papyrus Personal", "serverName": "Papyrus Personal Server",
  "scheme": "papyrus-personal", "port": 5192, "base": "/papyrus-personal",
  "appBundleId": "io.github.taliesinb.papyrus.personal",
  "serverBundleId": "io.github.taliesinb.papyrus.personal.server", "color": "#8E4EC6",
  "node": "/opt/homebrew/bin/node", "tailscale": "/usr/local/bin/tailscale", "env": {},
  "watch": ["/opt/homebrew/bin/uv", "run", "--project", "/Users/me/github/papyrus", "--frozen", "--quiet",
            "papyrus", "watch"] }
```

`watch` is there when the variant watches: `release` by default, another
variant after `--watch` (`--no-watch` turns it off; both are recorded as
`watch:` in `variant.yml`). Every variant's watcher would otherwise add the
same `~/Downloads` to its own library. The command runs the CLI from this
checkout through uv, so a `git pull` reaches the watcher when it next starts.

A GUI app has no shell `PATH`, so the tools' absolute paths are recorded at
build time. The variant's port, base path and colour come from its
`variant.yml`. The build options change the rest:

* `--port N` / `--color '#rrggbb'` change the variant's port / colour (and
  record them in its `variant.yml`);
* `--env KEY=VALUE` passes environment to the server. For example,
  `--env PAPYRUS_HOME=/tmp/ph` points the apps at a scratch library (see
  AGENTS.md, "Data safety").

`--install [DIR]` quits the variant's running copies, replaces its bundles in
DIR (`~/Applications` by default) and restarts whichever copies were running.
Other variants' apps are left alone.

## Variants

A variant is a library of its own with its own pair of apps, as DeepSea's
variants are (`~/ds/deepsea/LAYOUT.md`). Its id is lowercase `[a-z0-9-]`;
the everyday one is **`release`**. One string names a variant everywhere,
its **variant name** `io.github.taliesinb.papyrus.<id>`, also its app's
bundle id; components append to it (`.server`).

| | `release` | any other `<v>` (`personal`) |
|---|---|---|
| Library home (`PAPYRUS_HOME`) | `~/.papyrus/release/` | `~/.papyrus/<v>/` |
| Display name, app | **Papyrus**, `Papyrus.app` | Papyrus Personal, `Papyrus Personal.app` |
| Server app | `Papyrus Server.app` | `Papyrus Personal Server.app` |
| App / server bundle id | `io.github.taliesinb.papyrus.release[.server]` | `io.github.taliesinb.papyrus.<v>[.server]` |
| Logs | `~/Library/Logs/io.github.taliesinb.papyrus.release/` | `~/Library/Logs/io.github.taliesinb.papyrus.<v>/` |
| Linux: systemd user units ([`apps/linux`](../linux/README.md)) | `io.github.taliesinb.papyrus.release.{server,watch}.service` | `io.github.taliesinb.papyrus.<v>.server.service` (and `.watch.service` with `--watch`) |
| Application Support (`server.pid`) | `~/Library/Application Support/io.github.taliesinb.papyrus.release/` | `…/io.github.taliesinb.papyrus.<v>/` |
| Tailscale Serve path = web base | **`/papyrus`** | `/papyrus-<v>` |
| URL scheme | **`papyrus://`** | `papyrus-<v>://` |
| Port | 5182 | 5192, 5202, … (the first free, recorded) |
| Menu bar | the scroll | the scroll with the id's initial on its paper (`P`) |
| Dock icon | the scroll in `#7fb0ff` | the scroll in the variant's colour |
| Web page titles | `papyrus` | `papyrus personal` |

**Rule (DeepSea's): what a person reads or types leaves `release` out** (the
display name, the app's file name, the tailnet path, the URL scheme);
everything else carries it. macOS's own per-bundle directories follow the
bundle ids (`~/Library/Caches/`, `WebKit/`, `Preferences/`), so
`ls -d ~/Library/*/io.github.taliesinb.papyrus.<v>*` finds everything of one
variant. The bare root `io.github.taliesinb.papyrus` belongs to no variant.

The home holds the library (`papyrus.sqlite`, `store/`, `config.toml`,
backups) and:

```
~/.papyrus/<v>/
  variant.yml     id, display, color, port, servePath, created; build, installed (written by the installers); serve (Linux); watch
  watch.json      what `papyrus watch` is doing (state, ingesting N/M, stage lines); watch.lock: one watcher per library
  logs -> ~/Library/Logs/<variant name>/                (Linux: ${XDG_STATE_HOME:-~/.local/state}/<variant name>/)
  support -> ~/Library/Application Support/<variant name>/  (Linux: ${XDG_DATA_HOME:-~/.local/share}/<variant name>/)
```

The installers write `variant.yml` the first time they build a variant: a new
variant takes the first palette colour and port no other variant uses.
`PAPYRUS_ROOT` moves `~/.papyrus` (for a scratch set of variants).

The CLI and the web server find the home the same way: `PAPYRUS_HOME` if
set, else `$PAPYRUS_ROOT/$PAPYRUS_VARIANT`. Papyrus Server.app starts the web
server with `PAPYRUS_VARIANT=<v>`; on the command line use
`papyrus --variant personal …` (the CLI refuses a variant whose home does
not exist, so a typo never starts an empty library).

Shared by every variant, since they are keyed by content and hold nothing
of one library's own: the compiled executables (`apps/macos/.build/`), the
page-thumbnail cache (`~/.cache/papyrus-web/thumbs`, named by the PDF's
sha256), the matching APIs' response cache (`~/.cache/papyrus/http`), the
Apple Foundation Models bridge and LM Studio. The permanent API log is per
variant (`<home>/api-log/`), since it can hold one library's Slack messages.

## Papyrus Server.app

* **Runs** `node Contents/Resources/web/build/index.js` with
  `HOST=127.0.0.1`, `PORT`, `PAPYRUS_THUMB` (its `papyrus-thumb`) and the
  configured env. The server's
  output goes to `~/Library/Logs/<variant name>/server.log`, and it sets
  `PAPYRUS_VARIANT`.
* **Restarts** the server when it exits unexpectedly, waiting 1, 2, 4… s.
  Five exits within a minute stop it, with the reason shown in the menu.
* **Stale servers:** it records the server's pid in
  `~/Library/Application Support/<variant name>/server.pid`. If the app was killed
  and left a server running, the next launch stops that server before
  starting its own.
* **Servers it didn't start:** if a papyrus server already answers on the
  port (`cd web && PORT=5182 pnpm start`), it uses that one and starts none.
* **Publishes** the server once it answers:
  `tailscale serve --bg --yes --https=443 --set-path=/papyrus http://127.0.0.1:5182/papyrus`,
  which gives `https://<this Mac>.<tailnet>.ts.net/papyrus/`.
  * The target carries the `/papyrus` path because tailscale strips the
    mount point before proxying. The app is built with that base path, so
    loopback and the tailnet see the same URLs.
  * Another variant publishes its own path (`/papyrus-personal`) to its own port.
  * Quitting withdraws `/papyrus` and leaves every other `serve` handler alone.
  * Publish on Tailnet (on by default) turns publishing off and on.
* **The menu** shows:
  * whether the server is running;
  * the library's counts and any pipeline run in progress (from
    `/api/version`, polled every 5 s);
  * the tailnet address.

  Its items are Open Papyrus (⌘O), Open in Browser, Open / Copy Tailnet
  Address, Publish on Tailnet, Watch for New PDFs, Pause Model Work, Restart Server (⌘R), Show
  Server Log (⌘L), Show Watcher Log, Open at Login (`SMAppService`) and Quit
  (⌘Q).
* **Runs the watcher** (`papyrus watch`, the `watch` command in
  `papyrus-app.json`), with `PAPYRUS_VARIANT` and the configured env. Its
  output goes to `~/Library/Logs/<variant name>/watch.log` (file ids and
  counts only).
  * It restarts the watcher when it exits unexpectedly, waiting 2, 4, 8… s
    (at most 60). Five exits within ten minutes stop it, with the reason in
    the menu.
  * Exit status 75 means another watcher holds the library's lock (one in a
    terminal, say): it tries again a minute later.
  * Quit sends it SIGTERM first (its run closes as `interrupted`, and it
    unloads any model it loaded), waiting up to 10 s.
  * Watch for New PDFs (on by default) stops and starts it.
  * Pause Model Work creates `<home>/watch.pause` (`papyrus watch --pause`):
    the watcher, whoever runs it, then asks no model but keeps adding files,
    until the item is unticked (`--resume`). For LM Studio experiments
    without stopping the run.
* **The menu's watcher lines**, in a section of their own, come from
  `/api/version`'s `watch` (the library's `watch.json`): "Watching for new
  files" when there is nothing to do; otherwise "Ingesting N/M files" (files
  through the pipeline, or set aside, of those it has had since it was last
  idle) and one line per stage with files left, in pipeline order: Reading
  (OCR), Classifying, Extracting, Indexing, Matching. The stage being worked
  shows its pace and the time left ("Classifying: 52 files · ~3.2s/file ·
  ~2m46s left", a moving average started from past timings); the others say
  "pending" (the stages take turns: one loop, local models), "waiting: <reason>"
  (memory pressure, LM Studio busy with another model, rate limited by
  OpenAlex…) or "paused". Then how many items it gave up on. Show Watcher Log
  opens its log.
* **The menu-bar scroll's paper is solid** while the watcher's queue has
  items (due, or waiting for a retry), drawn from `Resources/glyph-filled.svg`;
  a variant's initial is then cut out of the paper.
* **Single instance:** a second copy exits at once.
* **Privacy prompts:** macOS attributes file access to the process that
  spawned the server and the watcher, so its prompts name "Papyrus Server"
  (for example, when the watcher first reads `~/Downloads`). The Info.plist
  carries the usage strings for Desktop, Documents, Downloads and network or
  removable volumes. This is why the watcher runs here rather than as a
  launchd agent: there, the prompt would name Python, or never appear.

## Papyrus.app

* **On launch**, and on View ▸ Reconnect, it probes `/api/version`. If
  nothing answers, it launches Papyrus Server.app in the background, shows
  "Starting Papyrus Server…" and loads the library as soon as the server
  answers. It looks for the server bundle beside itself first (a build in
  `dist/`), then wherever LaunchServices knows it.
* **If the server goes away** (it was quit or restarted), the next failed
  load starts it again.
* **Links** that leave `http://127.0.0.1:5182/papyrus/` open in the default
  browser. In-app `target=_blank` links, such as a PDF's "Open ↗", open in a
  second Papyrus window.
* **PDFs** show inline in WebKit's own viewer. "Download" saves the PDF to
  `~/Downloads`.
* **`papyrus://` URLs** open a path in the app: `open papyrus://work/43`,
  `open 'papyrus://?q=diffusion'`.
* **Menus:**
  * File: ⌘F focuses the library search; ⇧⌘L goes to the Library.
  * View: ⌘[ / ⌘] go back and forward; ⌘R reloads; zoom; Open in Browser
    (⇧⌘O); Copy Address (⇧⌘C).
  * Papyrus: Show Papyrus Server.

  The page's own keys (`/`, `j`/`k`, `o`, `s`, `x`, `v`) work as in a
  browser.
* **Logs:** `~/Library/Logs/<variant name>/app.log`. `kill -USR1 <pid>` saves the
  key window's page as `snapshot.png` beside the log, which lets you check
  the app from a script without screen-recording permission.

## Files

| file | role |
|---|---|
| `Sources/Shared/Shared.swift` | `papyrus-app.json` (the variant and its names), the `/api/version` probe, run logs, the menu-bar image (the scroll cropped to its mark, with a variant's initial) |
| `Sources/Server/main.swift` | Papyrus Server.app |
| `Sources/Papyrus/main.swift` | Papyrus.app |
| `Tools/make-icon.swift` | renders the glyph on a rounded tile (every iconset size), or full-bleed (`--png`, for `web/static/apple-touch-icon.png`) |
| `Resources/glyph.svg` | [boxicons](https://github.com/box-icons/boxicons-core) "scroll" (MIT): the Dock icon, the menu-bar template image and (recoloured) `web/static/favicon.svg` |
| `Resources/glyph-filled.svg` | the same scroll with its paper solid: the menu bar while the watcher has work queued |
| `build.py` | variants (`variant.yml`); builds, signs and installs both apps of each |
