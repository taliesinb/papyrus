"""`papyrus slack …`: set up the Slack app, store its token, and list the paper links found."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from urllib.parse import quote

import click

from .. import db


def _err(e: Exception) -> click.ClickException:
    return click.ClickException(str(e))


@click.group("slack")
def slack_group() -> None:
    """Paper links shared in a Slack workspace (setup, login, links). See docs/slack.md."""


@slack_group.command("setup")
@click.option("--open", "open_browser", is_flag=True, help="Open the create-app page in the browser.")
def setup_cmd(open_browser: bool) -> None:
    """Print the Slack app manifest and the link that creates the app from it.

    \b
    1. Open the link, pick the workspace, create the app.
    2. Install it ("Install to <workspace>"; "Request to Install" means an admin must approve).
    3. Copy the User OAuth Token (xoxp-…) from OAuth & Permissions.
    4. papyrus slack login --workspace NAME
    """
    from .slack import manifest

    m = manifest()
    url = "https://api.slack.com/apps?new_app=1&manifest_json=" + quote(json.dumps(m, separators=(",", ":")))
    click.echo(json.dumps(m, indent=2))
    click.echo(f"\nCreate the app: {url}")
    if open_browser:
        click.launch(url)


@slack_group.command("login")
@click.option("--workspace", "-w", help="Short name to store the token under (e.g. the company name).")
def login_cmd(workspace: str | None) -> None:
    """Check a user token (xoxp-…) and store it in the macOS Keychain (prompted, not echoed)."""
    from .slack import Slack, SlackError, keychain_account, store_token, workspace_name

    try:
        ws = workspace or workspace_name()
    except SlackError:
        raise click.UsageError("pass --workspace NAME (a short name for this workspace)") from None
    token = click.prompt("User OAuth Token (xoxp-…)", hide_input=True).strip()
    if not token.startswith("xoxp-"):
        raise click.ClickException("expected a user token starting with xoxp- (OAuth & Permissions → User OAuth Token)")
    try:
        me = Slack(token).call("auth.test")
        store_token(ws, token)
    except SlackError as e:
        raise _err(e) from e
    click.echo(f"stored as {keychain_account(ws)}: {me.get('user')} in {me.get('team')} ({me.get('url')})")


@slack_group.command("whoami")
@click.option("--workspace", "-w")
def whoami_cmd(workspace: str | None) -> None:
    """Show who the stored token belongs to and its scopes."""
    from .slack import Slack, SlackError, get_token, keychain_account, workspace_name

    try:
        ws = workspace_name(workspace)
        me = Slack(get_token(ws)).call("auth.test")
    except SlackError as e:
        raise _err(e) from e
    click.echo(f"{keychain_account(ws)}: {me.get('user')} ({me.get('user_id')}) in {me.get('team')} "
               f"({me.get('team_id')}, {me.get('url')})\nscopes: {me.get('_scopes')}")


def _filter_options(f):
    for opt in reversed([
        click.option("--workspace", "-w"),
        click.option("--days", type=float, help="Look back this many days (default: [slack] days, else 30)."),
        click.option("--since", "since_date", metavar="YYYY-MM-DD", help="Look back to this date instead."),
        click.option("--dms/--no-dms", default=None,
                     help="Include direct and group messages (default: [slack] dms, else no)."),
        click.option("--channel", "channels", multiple=True, metavar="NAME",
                     help="Only these channels (name, #name or id; repeatable)."),
        click.option("--exclude-channel", "exclude_channels", multiple=True, metavar="NAME",
                     help="Skip these channels."),
        click.option("--domain", "domains", multiple=True, metavar="HOST[/PATH]",
                     help="Also count links to this host as papers (repeatable; adds to the built-in list)."),
        click.option("--all-links", is_flag=True, help="Keep every link, not only paper links (kind is then empty)."),
    ]):
        f = opt(f)
    return f


def _search(conn, command: str, workspace, days, since_date, dms, channels, exclude_channels, domains, all_links,
            after=None):
    """Run the searches as a recorded run; `after(run_id, stats, records)` runs inside it."""
    from . import links as linkmod
    from .slack import Filters, Slack, SlackError, find, get_token, settings, since_days, workspace_name

    s = settings()
    if since_date:
        try:
            since = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        except ValueError:
            raise click.BadParameter("use YYYY-MM-DD", param_hint="--since") from None
    else:
        since = since_days(days if days is not None else float(s.get("days", 30)))
    extra_domains = tuple(s.get("domains") or ()) + domains
    filters = Filters(
        since=since,
        dms=bool(s.get("dms", False)) if dms is None else dms,
        channels=channels or tuple(s.get("channels") or ()),
        exclude_channels=exclude_channels or tuple(s.get("exclude_channels") or ()),
        domains=linkmod.DEFAULT_DOMAINS + extra_domains,
        all_links=all_links,
    )
    try:
        ws = workspace_name(workspace)
        slack = Slack(get_token(ws))
    except SlackError as e:
        raise _err(e) from e
    args = {"workspace": ws, "since": since, "dms": filters.dms, "channels": list(filters.channels),
            "exclude_channels": list(filters.exclude_channels), "extra_domains": list(extra_domains),
            "all_links": all_links}
    with db.recorded_run(conn, command, args) as (run_id, stats):
        try:
            records = find(slack, filters, workspace=ws, stats=stats)
        except SlackError as e:
            raise _err(e) from e
        if after:
            after(run_id, stats, records)
    return records, stats


def _summary(records, stats) -> str:
    kinds: dict[str, int] = {}
    for r in records:
        for lk in r["links"]:
            kinds[lk.get("kind") or "other"] = kinds.get(lk.get("kind") or "other", 0) + 1
    return (f"{len(records)} messages, {sum(kinds.values())} links "
            f"({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}); "
            f"{stats.get('skipped_dm', 0)} DM matches skipped; {stats.get('api_calls')} Slack API calls"
            + ("; WARNING: search hit Slack's 10,000-result cap, use fewer --days" if stats.get("truncated") else ""))


@slack_group.command("links")
@_filter_options
@click.option("--json", "as_json", is_flag=True, help="One JSON record per message (what `import` stores).")
@click.option("-o", "--output", type=click.File("w"), help="Write the records here instead of stdout.")
@click.option("-v", "--verbose", is_flag=True, help="Also print each message as Markdown.")
@click.pass_context
def links_cmd(ctx: click.Context, workspace, days, since_date, dms, channels, exclude_channels, domains,
              all_links: bool, as_json: bool, output, verbose: bool) -> None:
    """List messages with paper links: arXiv, DOI, PDF and allow-listed hosts, plus uploaded PDFs.

    Reads only (see `import` to store them). Two Slack searches cover the whole period
    (docs/slack.md), and every API response is kept in the API log.
    """
    records, stats = _search(ctx.obj["conn"], "slack-links", workspace, days, since_date, dms, channels,
                             exclude_channels, domains, all_links)
    out = output or sys.stdout
    for r in records:
        if as_json:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            continue
        kinds = ", ".join(f"{lk['kind']}:{lk['id']}" if lk.get("id") else (lk.get("kind") or "link") for lk in r["links"])
        out.write(f"{r['occurred_at'][:16].replace('T', ' ')}  {r['source']}  {r['author']['name']}  [{kinds}]\n"
                  f"    {r['permalink']}\n")
        if verbose:
            out.write("".join(f"    | {line}\n" for line in r["markdown"].splitlines()) + "\n")
    out.flush()
    click.echo(_summary(records, stats), err=True)


@slack_group.command("import")
@_filter_options
@click.option("--resolve/--no-resolve", default=True,
              help="Then link arXiv/DOI refs to works, creating and matching new ones (default on).")
@click.pass_context
def import_cmd(ctx: click.Context, workspace, days, since_date, dms, channels, exclude_channels, domains,
               all_links: bool, resolve: bool) -> None:
    """Store messages with paper links as `slack` contexts, one ref per link; then resolve refs.

    Re-running is safe: contexts are keyed by permalink and refreshed, refs are kept.
    """
    from .slack import store

    conn = ctx.obj["conn"]

    def write(run_id, stats, records):
        stats.update(store(conn, records))
        if resolve:
            _resolve(conn, run_id, stats)

    records, stats = _search(conn, "slack-import", workspace, days, since_date, dms, channels, exclude_channels,
                             domains, all_links, after=write)
    click.echo(_summary(records, stats), err=True)
    click.echo("stored: " + " ".join(f"{k}={stats.get(k, 0)}" for k in ("contexts_new", "contexts_updated", "refs_new")),
               err=True)
    if resolve:
        click.echo("resolved: " + " ".join(f"{k}={v}" for k, v in stats.get("resolve", {}).items()), err=True)


def _resolve(conn, run_id: int, stats: dict) -> None:
    from ..resolve import resolve_refs

    def progress(ident, r):
        what = r.get("title") or ""
        if r.get("work_id"):
            what = f"work {r['work_id']} {what}"
        click.echo(f"  {ident[0]}:{ident[1]:24} {r['status']:16} {what[:70]}"
                   + (f" ({r['error'][:80]})" if r.get("error") else ""), err=True)

    stats["resolve"] = dict(resolve_refs(conn, context_kind="slack", run_id=run_id, progress=progress))
