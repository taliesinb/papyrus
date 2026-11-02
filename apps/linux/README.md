# apps/linux: the papyrus web server as a systemd user service

The Linux counterpart of [`apps/macos`](../macos/README.md), modelled on how
DeepSea's `dsd build --install` runs a variant on Linux. There is no app. The
read-only web browser in [`web/`](../../web) runs as a systemd user service, one
per [variant](../macos/README.md#variants). You reach it in a browser at
`http://127.0.0.1:5182/papyrus/` or, on the tailnet, at
`https://<machine>.<tailnet>.ts.net/papyrus/`. The Python CLI (`papyrus scan`,
`download`, `ingest`, …) is still the only writer, as on macOS. A second
unit runs `papyrus watch`, which adds new PDFs from `~/Downloads` and
`~/Desktop` as they appear.

```sh
python3 apps/linux/install.py                      # release: build web/, install, start, publish
python3 apps/linux/install.py --variant personal   # Papyrus Personal: port 5192, /papyrus-personal
python3 apps/linux/install.py --all                # every variant (after a code change)
python3 apps/linux/install.py --uninstall          # stop and remove the services; the library stays
python3 apps/linux/install.py --variant personal --watch   # personal watches too (default: release only)
```

## Requirements

* **Node 22.5+** (for `node:sqlite`). Distribution packages are often older
  (Debian 13 has Node 20); nodejs.org's tarball or nvm work. Also **pnpm**
  (`corepack enable pnpm`).
* **uv**, for the CLI. The installer also uses it once to create a new
  variant's empty library (`papyrus --variant <v> stats`), because the web
  server opens the database read-only.
* **poppler's `pdftoppm`** renders the page thumbnails (on macOS,
  `papyrus-thumb` in Papyrus Server.app does). Package names:
  * Debian, Ubuntu, Fedora: `poppler-utils`
  * Arch: `poppler`
  * Nix: `nixpkgs#poppler-utils`

  The installer refuses to install without it unless you pass
  `--no-thumbnails`.
* **A systemd user manager** (`systemctl --user`). Without one (some
  containers, WSL without systemd), `--no-systemd` installs the web build and
  prints the command that runs it.
* **Tailscale**, optional, for the tailnet address. `tailscale serve` needs
  operator rights for your user, granted once with
  `sudo tailscale set --operator=$USER`; the installer says so when they are
  missing.

## What it installs

Everything carries the variant name `io.github.taliesinb.papyrus.<variant>`,
as on macOS:

| | path |
|---|---|
| unit | `${XDG_CONFIG_HOME:-~/.config}/systemd/user/<variant name>.server.service` |
| watcher unit | `…/systemd/user/<variant name>.watch.service`, log `<state dir>/watch.log` |
| the built server | `${XDG_DATA_HOME:-~/.local/share}/<variant name>/web/` (`<home>/support`) |
| log | `${XDG_STATE_HOME:-~/.local/state}/<variant name>/server.log` (`<home>/logs`) |
| library | `~/.papyrus/<variant>/` (`PAPYRUS_ROOT` moves `~/.papyrus`), with its `variant.yml` |

The unit runs `node <data>/web/build/index.js` on `127.0.0.1:<port>`. It sets
`PAPYRUS_VARIANT`, `PDFTOPPM` (the path found at install time), any `--env
KEY=VALUE`, and `PAPYRUS_ROOT` if the installer ran with it. Two restart
rules:

* It restarts after a crash (`Restart=on-failure`).
* Five failures within a minute stop it, as Papyrus Server.app does on macOS.

It is enabled, so it starts with your systemd user manager: at login, or at
boot if the account lingers. Without lingering it stops at your last logout,
and the installer prints a note about it. Lingering usually needs an
administrator: `sudo loginctl enable-linger $USER`.

**Tailnet:**

* The installer publishes the server with `tailscale serve --bg --https=443
  --set-path=<base>`. tailscaled keeps that setting across reboots.
* The unit repeats it every time it starts (`ExecStartPost`; a failure there
  doesn't stop the server).
* `--uninstall` withdraws it.
* `--no-serve` turns publishing off, and `--serve` turns it back on. The
  choice is recorded as `serve:` in `variant.yml`.

**The watcher** (`<variant name>.watch.service`, for a variant that watches:
release by default, others with `--watch`, recorded as `watch:` in
`variant.yml`; `--no-watch` removes the unit):

* It runs `uv run --project <this checkout> --frozen --quiet papyrus watch`
  with `PAPYRUS_VARIANT`, `--env` and `PAPYRUS_ROOT`, so a `git pull` reaches
  it at its next restart.
* `Nice=19`, `CPUSchedulingPolicy=idle` and `IOSchedulingClass=idle`; the
  watcher lowers its own priority the same way when run by hand.
* It restarts after a failure (`Restart=on-failure`, five within ten
  minutes stop it), except after exit status 75: another watcher already
  holds the library's lock (`RestartPreventExitStatus=75`). Start it again
  with `systemctl --user restart` once that one has gone.
* Its state is in `~/.papyrus/<variant>/watch.json` (with the queue's counts
  and an estimate of the time left), and `papyrus watch --status` prints it.
  Failed steps are retried after 1 h, 4 h and 16 h from
  `~/.papyrus/<variant>/queue.sqlite`; `papyrus watch --retry-failed` revives
  the ones it gave up on. `papyrus pause` / `papyrus resume` halts and resumes
  its loop (nothing is added or ingested meanwhile; new files wait).
  `papyrus start` / `stop` start and stop the variant's units, `papyrus open`
  opens the server's URL (`xdg-open`), and `papyrus status` says what runs.

`--uninstall` stops, disables and deletes the units, deletes the installed web
build and withdraws the tailnet path. The library and the logs stay.

## Day to day

```sh
systemctl --user status io.github.taliesinb.papyrus.release.server
systemctl --user restart io.github.taliesinb.papyrus.release.server
tail -f ~/.papyrus/release/logs/server.log
systemctl --user status io.github.taliesinb.papyrus.release.watch
tail -f ~/.papyrus/release/logs/watch.log
uv run papyrus watch --status
```

A change to `web/` needs a reinstall, because the service runs the installed
copy, not `web/build`.

## The rest of papyrus on Linux

The CLI runs as on macOS. These are the Linux counterparts of its macOS parts:

| macOS | Linux |
|---|---|
| APFS clones into the store (`clonefile`) | reflinks (`FICLONE`) on btrfs, XFS, bcachefs and OpenZFS 2.2+; a copy elsewhere (ext4, tmpfs) |
| download URL and referrer from `kMDItemWhereFroms` | the freedesktop `user.xdg.origin.url` / `user.xdg.referrer.url` attributes (written by Chromium-based browsers, `wget --xattr` and `curl --xattr`) |
| file birth time (Found, Recently added) | the modification time, which for a download is when it was written |
| `papyrus download -c` reads RTF/HTML/text with `osascript` and `pbpaste` | `wl-paste` (Wayland) or `xclip` (X11): HTML, then plain text |
| Reveal in Finder | "Show in Files" (`org.freedesktop.FileManager1` on the session bus), shown only when the server has a desktop session |
| Apple Foundation Models and Vision OCR (`tools/afm-bridge`) | none: use LM Studio, and pages without a text layer stay unsearchable |
| `papyrus watch`: FSEvents, `PRIO_DARWIN_BG`, `kern.memorystatus_vm_pressure_level`; run by Papyrus Server.app | inotify, nice + `SCHED_IDLE` + idle I/O class, `/proc/pressure/memory` (else `MemAvailable`); a systemd user unit |

## Testing

[`tests/test_installers.py`](../../tests/test_installers.py) covers the unit
text, the quoting, the XDG paths and the variant rules.
[`tests/test_linux.py`](../../tests/test_linux.py) covers the xattrs, reflinks
and the clipboard; it fakes the clipboard tools, so it runs on macOS too.

The installer itself was tested end to end on Debian 13 (aarch64) in an
Apple `container` VM. The VM booted systemd as PID 1 and had a lingering
user, which is what the container needs to run user units:

```sh
container run -d --name plt --cap-add ALL --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  -e container=docker <image with systemd, Node 22.5+, uv, poppler-utils> /sbin/init
```
