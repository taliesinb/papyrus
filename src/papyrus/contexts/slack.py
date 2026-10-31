"""Paper links shared in a Slack workspace: find them, and describe each message they were in.

How (measured in docs/slack.md): two `search.messages` queries with the user's token,
``has:link after:<date>`` and ``type:pdf after:<date>``, cover what a full scan of every
channel finds (928 of 929 web-link messages, 20 of 20 PDF uploads over 180 days) in ~16
calls instead of ~1,000. Links are then classified locally (`contexts.links`).

Every call goes through `enrich.http.HttpClient`, so raw responses land in the API log;
nothing here is cached (`use_cache=False`), since the cache would keep messages for 30 days
outside the library's home.

Token: ``PAPYRUS_SLACK_TOKEN``, else the macOS Keychain, service ``papyrus.slack``,
account ``<variant>/<workspace>``. Settings: ``[slack]`` in config.toml (see `settings`).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import config, db
from ..enrich import http
from . import links as linkmod

API = "https://slack.com/api/"
KEYCHAIN_SERVICE = "papyrus.slack"
TOKEN_ENV = "PAPYRUS_SLACK_TOKEN"
SEARCH_QUERIES = ("has:link after:{after}", "type:pdf after:{after}")
MAX_SEARCH_PAGES = 100  # Slack's own cap (100 results per page)

USER_SCOPES = ("channels:history", "channels:read", "groups:history", "groups:read", "users:read", "search:read")


class SlackError(RuntimeError):
    pass


# --------------------------------------------------------------------------- settings / token


def settings() -> dict[str, Any]:
    """The ``[slack]`` table of config.toml: workspace, days, dms, channels,
    exclude_channels, domains (extra allow-list entries)."""
    s = config.load().get("slack") or {}
    if not isinstance(s, dict):
        raise config.ConfigError(f"{config.path()}: [slack] must be a table")
    return s


def workspace_name(cli: str | None = None) -> str:
    """Short name the token is stored under: option > PAPYRUS_SLACK_WORKSPACE > config >
    the first Keychain entry of this variant."""
    w = cli or os.environ.get("PAPYRUS_SLACK_WORKSPACE", "").strip() or settings().get("workspace")
    if w:
        return str(w)
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True, text=True)
    m = re.search(r'"acct"<blob>="([^"]+)"', r.stdout)
    if m and m.group(1).startswith(db.variant() + "/"):
        return m.group(1).split("/", 1)[1]
    raise SlackError("no Slack workspace: run `papyrus slack login --workspace NAME`, or set [slack] workspace")


def keychain_account(workspace: str) -> str:
    return f"{db.variant()}/{workspace}"


def get_token(workspace: str | None = None) -> str:
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    ws = workspace_name(workspace)
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", keychain_account(ws), "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SlackError(f"no Slack token in the Keychain for {keychain_account(ws)}: run `papyrus slack login`")
    return r.stdout.strip()


def store_token(workspace: str, token: str) -> None:
    """Save (or replace) the token. Fed through `security -i` on stdin, so it never appears
    in a process list."""
    if '"' in token or "\n" in token:
        raise SlackError("that doesn't look like a Slack token")
    cmd = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a "{keychain_account(workspace)}" -w "{token}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0 or "error" in (r.stdout + r.stderr).lower():
        raise SlackError(f"could not store the token in the Keychain: {(r.stderr or r.stdout).strip()[:200]}")


def manifest(name: str = "papyrus") -> dict[str, Any]:
    """The app manifest `papyrus slack setup` hands to Slack (user scopes only, read-only)."""
    return {
        "display_information": {"name": name, "description": "Reads messages for paper links (personal, read-only)."},
        "oauth_config": {"scopes": {"user": list(USER_SCOPES)}},
        "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
    }


# --------------------------------------------------------------------------- API


class Slack:
    def __init__(self, token: str, client: http.HttpClient | None = None):
        self._token = token
        self._http = client or http.default_client()
        self.calls = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.calls += 1
        r = self._http.request("GET", API + method, params=params, use_cache=False,
                               headers={"Authorization": f"Bearer {self._token}"})
        d = r.json()
        if not d.get("ok"):
            raise SlackError(f"{method}: {d.get('error')}")
        d["_scopes"] = r.headers.get("x-oauth-scopes", "")
        return d

    def paged(self, method: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            d = self.call(method, **params, **({"cursor": cursor} if cursor else {}))
            yield from d.get(key) or []
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return

    def search(self, query: str) -> tuple[list[dict[str, Any]], int]:
        """All matches of `query` (newest first) and Slack's reported total."""
        out: list[dict[str, Any]] = []
        page, total = 1, 0
        while True:
            d = self.call("search.messages", query=query, count=100, page=page, sort="timestamp", sort_dir="desc")
            msgs = d["messages"]
            total = (msgs.get("paging") or {}).get("total", total)
            out += msgs.get("matches") or []
            pages = (msgs.get("paging") or {}).get("pages", 1)
            if page >= min(pages, MAX_SEARCH_PAGES):
                return out, total
            page += 1

    def user_names(self) -> dict[str, str]:
        names = {}
        for u in self.paged("users.list", "members", limit=200):
            p = u.get("profile") or {}
            names[u["id"]] = p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or u["id"]
        return names

    def channel_names(self) -> dict[str, str]:
        return {c["id"]: c.get("name") or c["id"] for c in
                self.paged("users.conversations", "channels", types="public_channel,private_channel",
                           exclude_archived="false", limit=200)}


# --------------------------------------------------------------------------- Markdown


@dataclass
class Names:
    users: dict[str, str] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)

    def user(self, uid: str) -> str:
        return "@" + self.users.get(uid, uid)

    def channel(self, cid: str) -> str:
        return "#" + self.channels.get(cid, cid)


def _styled(s: str, style: dict | None) -> str:
    if not style or not s.strip():
        return s
    lead, core, trail = re.match(r"(\s*)(.*?)(\s*)$", s, re.S).groups()
    if style.get("code"):
        core = f"`{core}`"
    if style.get("strike"):
        core = f"~~{core}~~"
    if style.get("italic"):
        core = f"*{core}*"
    if style.get("bold"):
        core = f"**{core}**"
    return lead + core + trail


def _emoji(e: dict) -> str:
    u = e.get("unicode")
    if u:
        try:
            return "".join(chr(int(p, 16)) for p in u.split("-"))
        except ValueError:
            pass
    return f":{e.get('name', '')}:"


def _same_url(label: str, url: str) -> bool:
    """Slack labels a pasted URL with itself minus the scheme (and `www.`): not a real label."""
    def bare(s: str) -> str:
        return re.sub(r"^(?:https?://)?(?:www\.)?", "", s.strip()).rstrip("/")
    return bare(label) == bare(url)


def _inline(e: dict, names: Names, *, raw: bool = False) -> str:
    t = e.get("type")
    style = None if raw else e.get("style")
    if t == "text":
        return _styled(e.get("text", ""), style)
    if t == "link":
        url, label = e.get("url", ""), (e.get("text") or "").strip()
        if raw:
            return label or url
        return _styled(f"[{label}]({url})" if label and not _same_url(label, url) else url, style)
    if t == "user":
        return _styled(names.user(e.get("user_id", "")), style)
    if t == "usergroup":
        return "@" + e.get("usergroup_id", "")
    if t == "channel":
        return _styled(names.channel(e.get("channel_id", "")), style)
    if t == "emoji":
        return _emoji(e)
    if t == "broadcast":
        return "@" + e.get("range", "here")
    if t in ("message_mention", "attachment_mention"):
        label = e.get("text") or "message"
        return f"[{label}]({e['url']})" if e.get("url") and not raw else label
    if t == "date":
        return e.get("fallback") or e.get("text") or ""
    if t == "color":
        return e.get("value", "")
    return e.get("text", "")


def _section(s: dict, names: Names, *, raw: bool = False) -> str:
    return "".join(_inline(e, names, raw=raw) for e in s.get("elements") or [])


def blocks_markdown(blocks: list[dict], names: Names) -> str:
    out: list[str] = []
    for b in blocks:
        if b.get("type") != "rich_text":
            if b.get("text", {}).get("text"):
                out.append(mrkdwn_markdown(b["text"]["text"], names))
            continue
        for el in b.get("elements") or []:
            t = el.get("type")
            if t == "rich_text_section":
                out.append(_section(el, names).rstrip("\n"))
            elif t == "rich_text_list":
                indent = "    " * int(el.get("indent") or 0)
                start = int(el.get("offset") or 0)
                for i, item in enumerate(el.get("elements") or []):
                    marker = f"{start + i + 1}." if el.get("style") == "ordered" else "-"
                    out.append(f"{indent}{marker} {_section(item, names).strip()}")
            elif t == "rich_text_quote":
                out.append("\n".join("> " + line for line in _section(el, names).rstrip("\n").split("\n")))
            elif t == "rich_text_preformatted":
                out.append("```\n" + _section(el, names, raw=True).rstrip("\n") + "\n```")
            else:
                out.append(_section(el, names))
    return "\n".join(out).strip()


_MRKDWN_TOKEN = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")


def mrkdwn_markdown(text: str, names: Names) -> str:
    """Slack's mrkdwn (the `text` field) to Markdown, for messages without blocks."""
    def token(m: re.Match) -> str:
        target, label = m.group(1), m.group(2)
        if target.startswith("@"):
            return names.user(target[1:])
        if target.startswith("#"):
            return "#" + (label or names.channels.get(target[1:], target[1:]))
        if target.startswith("!"):
            return "@" + (label or target[1:].split("^")[0])
        return f"[{label}]({target})" if label and label != target else target

    s = _MRKDWN_TOKEN.sub(token, text)
    s = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"**\1**", s)
    s = re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"*\1*", s)
    s = re.sub(r"(?<![\w~])~(?=\S)([^~\n]+?)(?<=\S)~(?![\w~])", r"~~\1~~", s)
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def message_markdown(m: dict, names: Names) -> str:
    if m.get("blocks"):
        return blocks_markdown(m["blocks"], names)
    return mrkdwn_markdown(m.get("text") or "", names)


# --------------------------------------------------------------------------- links


def _walk_links(x: Any) -> Iterator[dict]:
    if isinstance(x, dict):
        if x.get("type") == "link" and x.get("url"):
            yield x
        for v in x.values():
            yield from _walk_links(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_links(v)


def message_links(m: dict) -> list[dict[str, Any]]:
    """Every link in a message, de-duplicated by URL, in order: body links (blocks, else
    mrkdwn), links inside shared messages, unfurl URLs, uploaded files. Unfurl titles are
    attached to the link they describe."""
    found: dict[str, dict[str, Any]] = {}

    def add(url: str, source: str, **kw: Any) -> None:
        url = url.strip()
        if not url or url.startswith(("mailto:", "tel:")):
            return
        if url in found:
            for k, v in kw.items():
                if v and not found[url].get(k):
                    found[url][k] = v
            return
        found[url] = {"url": url, "source": source, **{k: v for k, v in kw.items() if v}}

    body = list(_walk_links(m.get("blocks") or []))
    for e in body:
        label = (e.get("text") or "").strip()
        add(e["url"], "message", label=None if _same_url(label, e["url"]) else label)
    if not body:
        for target, label in _MRKDWN_TOKEN.findall(m.get("text") or ""):
            if target.startswith("http"):
                add(target, "message", label=label if label != target else None)
    for a in m.get("attachments") or []:
        if a.get("is_msg_unfurl") or a.get("is_share"):
            for e in _walk_links(a.get("message_blocks") or a.get("blocks") or []):
                add(e["url"], "shared_message", label=(e.get("text") or "").strip() or None)
            for target, label in _MRKDWN_TOKEN.findall(a.get("text") or ""):
                if target.startswith("http"):
                    add(target, "shared_message", label=label or None)
            continue
        url = a.get("original_url") or a.get("from_url") or a.get("title_link")
        if url:
            add(url, "unfurl", title=a.get("title"), site=a.get("service_name"))
    for f in m.get("files") or []:
        add(f.get("permalink") or f.get("url_private") or "", "file", label=f.get("title") or f.get("name"),
            file_id=f.get("id"), filetype=f.get("filetype"), name=f.get("name"), size=f.get("size"),
            download_url=f.get("url_private_download"))
    return list(found.values())


def classify_links(found: list[dict[str, Any]], domains: Iterable[str]) -> list[dict[str, Any]]:
    domains = tuple(domains)
    for link in found:
        if link["source"] == "file":
            link["kind"] = "pdf_upload" if link.get("filetype") == "pdf" else None
            continue
        c = linkmod.classify(link["url"], domains)
        link["kind"] = c.kind if c else None
        if c and c.id:
            link["id"] = c.id
    return found


# --------------------------------------------------------------------------- the scan


@dataclass
class Filters:
    since: float  # epoch seconds
    dms: bool = False
    channels: tuple[str, ...] = ()  # allow list: names (with or without '#') or ids
    exclude_channels: tuple[str, ...] = ()
    domains: tuple[str, ...] = linkmod.DEFAULT_DOMAINS
    all_links: bool = False


def channel_type(ch: dict) -> str:
    if ch.get("is_im"):
        return "im"
    if ch.get("is_mpim"):
        return "mpim"
    return "private" if ch.get("is_private") or ch.get("is_group") else "public"


def _matches_channel(ch: dict, wanted: Iterable[str]) -> bool:
    keys = {ch.get("id"), ch.get("name"), "#" + (ch.get("name") or "")}
    return any(w in keys or w.lstrip("#") == ch.get("name") for w in wanted)


def _occurred_at(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record(m: dict, links: list[dict], names: Names, workspace: str) -> dict[str, Any]:
    """One message as the future `contexts` row (see docs/slack.md, "What we collect")."""
    ch = m.get("channel") or {}
    ctype = channel_type(ch)
    permalink = m.get("permalink") or ""
    thread_ts = (parse_qs(urlsplit(permalink).query).get("thread_ts") or [None])[0]
    if ctype == "im":
        where = "@" + names.users.get(ch.get("user") or ch.get("name", ""), ch.get("name", ""))
    elif ctype == "mpim":
        where = "group DM"
    else:
        where = "#" + (ch.get("name") or names.channels.get(ch.get("id", ""), ch.get("id", "")))
    uid = m.get("user") or ""
    return {
        "permalink": permalink,
        "occurred_at": _occurred_at(m["ts"]),
        "source": f"{workspace}/{where}",
        "author": {"id": uid or None, "handle": m.get("username"), "name": names.users.get(uid) or m.get("username")},
        "markdown": message_markdown(m, names),
        "links": links,
        "extra": {"team_id": m.get("team"), "channel_id": ch.get("id"), "channel_name": ch.get("name"),
                  "channel_type": ctype, "ts": m["ts"], "thread_ts": thread_ts if thread_ts != m["ts"] else None},
    }


def find(slack: Slack, filters: Filters, *, workspace: str, stats: dict | None = None) -> list[dict[str, Any]]:
    """Messages since `filters.since` with at least one paper link (any link with
    `all_links`), newest first."""
    stats = stats if stats is not None else {}
    after = datetime.fromtimestamp(filters.since - 86400, UTC).strftime("%Y-%m-%d")  # after: is exclusive
    merged: dict[tuple[str, str], dict] = {}
    for q in SEARCH_QUERIES:
        matches, total = slack.search(q.format(after=after))
        stats[f"search {q.split()[0]}"] = total
        if total > len(matches):
            stats["truncated"] = True  # more than Slack's 10,000-result cap: narrow --days
        for mt in matches:
            merged.setdefault(((mt.get("channel") or {}).get("id", ""), mt["ts"]), mt)
    names = Names(slack.user_names(), slack.channel_names())
    out = []
    for mt in merged.values():
        ch = mt.get("channel") or {}
        if float(mt["ts"]) < filters.since:
            continue
        if channel_type(ch) in ("im", "mpim") and not filters.dms:
            stats["skipped_dm"] = stats.get("skipped_dm", 0) + 1
            continue
        if filters.channels and not _matches_channel(ch, filters.channels):
            continue
        if filters.exclude_channels and _matches_channel(ch, filters.exclude_channels):
            continue
        found = classify_links(message_links(mt), filters.domains)
        keep = [f for f in found if f.get("kind") or (filters.all_links and f["source"] != "file")]
        if not keep:
            continue
        out.append(record(mt, keep, names, workspace))
    out.sort(key=lambda r: r["extra"]["ts"], reverse=True)
    stats["messages"] = len(out)
    stats["links"] = sum(len(r["links"]) for r in out)
    stats["api_calls"] = slack.calls
    return out


def since_days(days: float) -> float:
    return time.time() - days * 86400


# --------------------------------------------------------------------------- writing


def store(conn, records: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert one ``kind='slack'`` context per record (locator = permalink) and one
    unresolved ref per link, in one transaction. Re-imports refresh the text, author and
    links (messages get edited) and keep existing refs; `parent_id` points to the thread's
    root message when that has a context too."""
    import json

    now = db.now_iso()
    stats = {"contexts_new": 0, "contexts_updated": 0, "refs_new": 0}
    with db.tx(conn):
        ids: list[int] = []
        for r in records:
            ex = r["extra"]
            extra = {**ex, "author_handle": r["author"].get("handle"), "links": r["links"]}
            title = r["source"].split("/", 1)[1] if "/" in r["source"] else None
            row = conn.execute("SELECT id FROM contexts WHERE kind='slack' AND locator=?", (r["permalink"],)).fetchone()
            vals = (r["source"], r["occurred_at"], r["author"].get("name"), r["author"].get("id"), title,
                    r["markdown"], r["permalink"], json.dumps(extra, ensure_ascii=False))
            if row:
                cid = row["id"]
                conn.execute("UPDATE contexts SET source=?, occurred_at=?, actor=?, actor_id=?, title=?, text=?, url=?,"
                             " extra=?, last_seen_at=?, missing_at=NULL WHERE id=?", (*vals, now, cid))
                stats["contexts_updated"] += 1
            else:
                cid = conn.execute(
                    "INSERT INTO contexts(kind, locator, source, occurred_at, actor, actor_id, title, text, url, extra,"
                    " first_seen_at, last_seen_at) VALUES ('slack', ?,?,?,?,?,?,?,?,?,?,?)",
                    (r["permalink"], *vals, now, now)).lastrowid
                stats["contexts_new"] += 1
            ids.append(cid)
            for link in r["links"]:
                # UNIQUE(context_id, url, mention) does not dedupe a NULL mention, so check first
                if not conn.execute("SELECT 1 FROM context_refs WHERE context_id=? AND url=? AND mention IS NULL",
                                    (cid, link["url"])).fetchone():
                    conn.execute("INSERT INTO context_refs(context_id, url, status, source) VALUES (?,?,'unresolved','slack')",
                                 (cid, link["url"]))
                    stats["refs_new"] += 1
        # thread replies -> their root message's context, when it has one
        for cid in ids:
            conn.execute(
                """UPDATE contexts SET parent_id = (
                       SELECT p.id FROM contexts p WHERE p.kind='slack'
                         AND json_extract(p.extra, '$.channel_id') = json_extract(contexts.extra, '$.channel_id')
                         AND json_extract(p.extra, '$.ts') = json_extract(contexts.extra, '$.thread_ts'))
                   WHERE id=? AND json_extract(extra, '$.thread_ts') IS NOT NULL""", (cid,))
    return stats
