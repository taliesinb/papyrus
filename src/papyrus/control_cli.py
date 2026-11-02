"""`papyrus start | stop | open | status | pause | resume` (control.py)."""

from __future__ import annotations

import json

import click

from . import control


def _do(fn) -> None:
    try:
        click.echo(fn())
    except control.ControlError as e:
        raise click.ClickException(str(e)) from e


@click.command("start")
def start_cmd() -> None:
    """Start the library's web server and its watcher: Papyrus Server.app on macOS (in the
    menu bar), the systemd user units on Linux. `papyrus --variant V start` for another library."""
    _do(control.start)


@click.command("stop")
def stop_cmd() -> None:
    """Stop the web server and the watcher (the watcher's run closes as interrupted; on macOS the
    tailnet path is withdrawn, as the menu's Quit does)."""
    _do(control.stop)


@click.command("open")
def open_cmd() -> None:
    """Open the library's viewer: Papyrus.app on macOS (it starts the server if needed), the
    browser at the server's URL on Linux."""
    _do(control.open_viewer)


@click.command("status")
@click.option("--json", "as_json", is_flag=True)
def status_cmd(as_json: bool) -> None:
    """What is running: the server app (or units), whether the server answers, and what the
    watcher is doing (`papyrus watch --status` has its full status)."""
    s = control.status()
    if as_json:
        click.echo(json.dumps(s, indent=2))
        return
    click.echo(f"{s['variant']}: {s['home']}  {s['url']}")
    if "server_app" in s:
        click.echo(f"  server app  {'running (pid %s)' % s['server_app'] if s['server_app'] else 'not running'}")
        click.echo(f"  viewer app  {'running (pid %s)' % s['viewer_app'] if s['viewer_app'] else 'not running'}")
    for u, state in (s.get("units") or {}).items():
        click.echo(f"  {u}  {state}")
    click.echo(f"  web server  {'answering' if s['server_answers'] else 'not answering'}")
    w = s["watcher"]
    state = w.get("state", "stopped")
    line = state + (f": {w['detail']}" if w.get("detail") and state != "stopped" else "")
    if state != "stopped":  # (a stopped watcher's watch.json is what it last said)
        if ing := w.get("ingesting"):
            line += f"; ingested {ing['done']}/{ing['total']} files"
        if w.get("eta_s"):
            line += f"; about {w['eta_s'] / 60:.0f} min of work left"
    click.echo(f"  watcher     {line}")
    if s["paused"]:
        click.echo("  ingestion   paused (papyrus resume)")


@click.command("pause")
def pause_cmd() -> None:
    """Pause ingestion: the watcher's loop halts after the step in progress (nothing is added, read,
    classified, extracted, indexed or matched; new files wait), as the menu's Pause Ingestion does.
    Lasts, across restarts, until `papyrus resume`."""
    _do(control.pause)


@click.command("resume")
def resume_cmd() -> None:
    """Resume ingestion: the watcher takes the files that arrived meanwhile, then goes on."""
    _do(control.resume)


COMMANDS = (start_cmd, stop_cmd, open_cmd, status_cmd, pause_cmd, resume_cmd)
