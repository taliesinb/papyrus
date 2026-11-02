"""`papyrus services …`: the web services store (services.py): what was asked of which API, and
where each rate limit and pause stands."""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from . import db


def _since(s: str | None) -> str | None:
    """'7d', '12h', '30m' or an ISO date/time -> an ISO UTC time; None stays None."""
    if not s:
        return None
    from . import services

    units = {"d": 86400, "h": 3600, "m": 60}
    if s[-1:] in units and s[:-1].replace(".", "", 1).isdigit():
        return services.now_iso(time.time() - float(s[:-1]) * units[s[-1]])
    return s


def _when(ts: float | None) -> str:
    if not ts:
        return "-"
    left = ts - time.time()
    return time.strftime("%H:%M:%S", time.localtime(ts)) + (f" (in {left:.0f} s)" if left > 0 else "")


@click.group("services")
def services_group() -> None:
    """The web services store (~/.papyrus/services.sqlite): every request to arXiv, OpenAlex,
    Crossref, Semantic Scholar, Slack and downloads, its timing, status and answer, and each
    service's rate limit and pause, shared by every papyrus process. Query it with sqlite3 too."""


@services_group.command("stats")
@click.option("--since", help="Only calls since: 7d, 12h, 30m, or an ISO date/time.")
@click.option("--json", "as_json", is_flag=True)
def stats_cmd(since: str | None, as_json: bool) -> None:
    """Per service and credential (public, key): calls, failures by status, requests not sent
    while paused, answers reused, and how long successful calls took."""
    from . import services

    rows = services.store().stats(_since(since))
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    click.echo(f"{'service':22} {'cred':7} {'calls':>6} {'ok':>6} {'failed':>6} {'paused':>6} {'reused':>6} "
               f"{'median':>7} {'p90':>7}  failures")
    for r in rows:
        ms = lambda v: "-" if v is None else f"{v / 1000:.2f}s"  # noqa: E731
        errs = ", ".join(f"{k}: {v}" for k, v in sorted(r["errors"].items(), key=lambda kv: -kv[1]))
        click.echo(f"{r['service'][:22]:22} {r['credential']:7} {r['calls']:6} {r['ok']:6} {r['failed']:6} "
                   f"{r['paused']:6} {r['hits']:6} {ms(r['median_ms']):>7} {ms(r['p90_ms']):>7}  {errs}")


@services_group.command("state")
@click.option("--json", "as_json", is_flag=True)
def state_cmd(as_json: bool) -> None:
    """Each service's rate limit and pause: the next allowed request, whether it is paused (until
    when, and why), its last success and failure, failures in a row."""
    from . import services

    rows = services.store().states()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    now = time.time()
    for r in rows:
        paused = r["paused_until"] and r["paused_until"] > now
        line = f"{r['name']:22} {r['credential'] or 'any':7} "
        line += f"PAUSED until {_when(r['paused_until'])}: {r['pause_reason']}" if paused else "ok"
        line += f"; last ok {r['last_ok_at'] or '-'}"
        if r["failures"]:
            line += f"; {r['failures']} failed in a row, last {r['last_failure_at']}"
        click.echo(line)


@services_group.command("show")
@click.argument("call_id")
@click.option("--body/--no-body", default=True, show_default=True, help="Print the response body.")
def show_cmd(call_id: str, body: bool) -> None:
    """One call (id or a prefix): service, credential, policy, request, status, timings, headers,
    and the response body (secrets redacted)."""
    from . import services

    c = services.store().show(call_id)
    if c is None:
        raise click.ClickException(f"no call {call_id}")
    text = c.pop("body")
    req = c.pop("request_body")
    for k in ("req_headers", "resp_headers", "redirects", "policy"):
        if c.get(k):
            c[k] = json.loads(c[k])
    click.echo(json.dumps(c, indent=2))
    if req:
        click.echo("--- request body")
        click.echo(req)
    if body:
        click.echo("--- response body" + (" (none)" if text is None else ""))
        if text is not None:
            click.echo(text)


@services_group.command("resume")
@click.argument("service", required=False)
def resume_cmd(service: str | None) -> None:
    """End the pause of SERVICE (arxiv, openalex, crossref, s2, slack, …), or of every service."""
    from . import services

    n = services.store().resume(service)
    click.echo(f"resumed {n} paused scope{'s' if n != 1 else ''}")


@services_group.command("pause")
@click.argument("service")
@click.option("--minutes", type=float, default=30, show_default=True)
def pause_cmd(service: str, minutes: float) -> None:
    """Ask SERVICE nothing for a while, in every papyrus process (matching counts it as "not asked")."""
    from . import services

    st = services.store()
    svc = services.by_name(service)
    if svc is None:
        raise click.ClickException(f"unknown service {service} ({', '.join(s.name for s in services.SERVICES)})")
    # make sure its (public) scope exists, so there is a row to pause
    st.ensure_scope(st.ids("GET", svc.base_url, None, None, layers=services.layers()))
    n = st.pause(service, minutes * 60)
    click.echo(f"paused {service} for {minutes:g} min ({n} scope{'s' if n != 1 else ''})")


@services_group.command("import")
@click.option("--api-log", "api_logs", multiple=True, type=click.Path(path_type=Path, file_okay=False),
              help="An old API log directory (default: every <variant home>/api-log under PAPYRUS_ROOT).")
@click.option("--http-cache", type=click.Path(path_type=Path, file_okay=False),
              default=Path("~/.cache/papyrus/http").expanduser(), show_default=True,
              help="The old 30-day response cache.")
def import_cmd(api_logs: tuple[Path, ...], http_cache: Path) -> None:
    """Bring the old stores into this one: the JSONL API logs (every attempt, with bodies) and the
    response cache (answers the logs don't have). Safe to repeat; the old files are left alone."""
    from . import services

    st = services.store()
    logs = list(api_logs) or sorted(p for p in db.papyrus_root().glob("*/api-log") if p.is_dir())
    for d in logs:
        n = st.import_api_log(d)
        click.echo(f"{d}: {n['calls']} calls, {n['hits']} reuses, {n['skipped']} skipped")
    if http_cache.is_dir():
        n = st.import_http_cache(http_cache)
        click.echo(f"{http_cache}: {n['calls']} answers, {n['known']} already there, {n['skipped']} unreadable")
