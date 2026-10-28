# apps/macos: Papyrus.app and Papyrus Server.app

Two native apps around the read-only web browser in [`web/`](../../web):

| app | bundle id | what it is |
|---|---|---|
| **Papyrus Server.app** | `io.github.taliesinb.papyrus.server` | A menu-bar app with no Dock tile. It runs the web server on `127.0.0.1:5182` under `/papyrus` and publishes it on the tailnet with `tailscale serve`. |
| **Papyrus.app** | `io.github.taliesinb.papyrus` | A window (WKWebView) on the library. It starts Papyrus Server.app when nothing answers. |

Both are read-only: the Python CLI (`papyrus scan`, `download`, `process`, …)
is still the only writer. The pipeline's writes show up in the open window as
"library updated · refresh", as in a browser.

```sh
python3 apps/macos/build.py                # → apps/macos/dist/{Papyrus,Papyrus Server}.app
python3 apps/macos/build.py --install      # … and copy both into ~/Applications
```

`build.py` uses only the standard library. It needs the Command Line Tools'
`swiftc` (no Xcode), Node 22.5+ (for `node:sqlite`) and pnpm. It:

1. runs `pnpm build` in `web/`;
2. compiles the two executables and the icon renderer, cached by source hash
   in `apps/macos/.build/`;
3. renders the icon;
4. assembles and ad-hoc signs both bundles.

Papyrus Server.app carries the built web server in `Contents/Resources/web`,
so a change to `web/` needs a rebuild of the app.

Each bundle has a `Contents/Resources/papyrus-app.json`:

```json
{ "port": 5182, "base": "/papyrus",
  "appBundleId": "io.github.taliesinb.papyrus", "serverBundleId": "io.github.taliesinb.papyrus.server",
  "node": "/opt/homebrew/bin/node", "tailscale": "/usr/local/bin/tailscale",
  "pdftoppm": "/opt/homebrew/bin/pdftoppm", "env": {} }
```

A GUI app has no shell `PATH`, so the tools' absolute paths are recorded at
build time. The build options change the rest:

* `--port N` sets the port;
* `--env KEY=VALUE` passes environment to the server. For example,
  `--env PAPYRUS_HOME=/tmp/ph` points the apps at a scratch library (see
  AGENTS.md, "Data safety").

`--install [DIR]` quits running copies, replaces the bundles in DIR
(`~/Applications` by default) and restarts whichever copies were running.

## Papyrus Server.app

* **Runs** `node Contents/Resources/web/build/index.js` with
  `HOST=127.0.0.1`, `PORT`, `PDFTOPPM` and the configured env. The server's
  output goes to `~/Library/Logs/Papyrus/server.log`.
* **Restarts** the server when it exits unexpectedly, waiting 1, 2, 4… s.
  Five exits within a minute stop it, with the reason shown in the menu.
* **Stale servers:** it records the server's pid in
  `~/Library/Application Support/Papyrus/server.pid`. If the app was killed
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
  * Quitting withdraws `/papyrus` and leaves every other `serve` handler alone.
  * Publish on Tailnet (on by default) turns publishing off and on.
* **The menu** shows:
  * whether the server is running;
  * the library's counts and any pipeline run in progress (from
    `/api/version`, polled every 5 s);
  * the tailnet address.

  Its items are Open Papyrus (⌘O), Open in Browser, Open / Copy Tailnet
  Address, Publish on Tailnet, Restart Server (⌘R), Show Server Log (⌘L),
  Open at Login (`SMAppService`) and Quit (⌘Q).
* **Single instance:** a second copy exits at once.
* **Privacy prompts:** macOS attributes file access to the process that
  spawned the server, so its prompts name "Papyrus Server" (for example, when
  a PDF is still only in `~/Downloads`). The Info.plist carries the usage
  strings for Desktop, Documents, Downloads and network or removable volumes.

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
* **Logs:** `~/Library/Logs/Papyrus/app.log`. `kill -USR1 <pid>` saves the
  key window's page as `snapshot.png` beside the log, which lets you check
  the app from a script without screen-recording permission.

## Files

| file | role |
|---|---|
| `Sources/Shared/Shared.swift` | `papyrus-app.json`, the `/api/version` probe, run logs, the glyph |
| `Sources/Server/main.swift` | Papyrus Server.app |
| `Sources/Papyrus/main.swift` | Papyrus.app |
| `Tools/make-icon.swift` | renders the glyph on a rounded tile (every iconset size), or full-bleed (`--png`, for `web/static/apple-touch-icon.png`) |
| `Resources/glyph.svg` | [boxicons](https://github.com/box-icons/boxicons-core) "scroll" (MIT): the Dock icon, the menu-bar template image and (recoloured) `web/static/favicon.svg` |
| `build.py` | builds, signs and installs both apps |
