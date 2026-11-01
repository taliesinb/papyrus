"""`papyrus download`: fetch documents from a list of URLs into the managed store.

Input comes from arguments, files, stdin or the clipboard (macOS; on Linux
through `wl-paste` or `xclip`, which give HTML and plain text). Copying a
selection of Safari tabs puts, per tab, a title and a URL on the clipboard: as
RTF, a list of `HYPERLINK` fields; as plain text, `title\\nURL\\n\\n` (a PDF tab
is titled by its URL). We read the RTF (or HTML) links first and add any other
URLs from the plain text, which may also be a bare list of URLs separated by
newlines, commas or spaces, or markdown links. Bare arXiv ids work too.

For each URL:

* arXiv abs/html pages and OpenReview forum pages are mapped to their PDF;
* whether the response is a PDF is decided by its first bytes, not its
  Content-Type (some servers send PDFs as `application/json`);
* an HTML landing page is followed once through its `citation_pdf_url` meta tag.

Each URL becomes one `download` context (locator = the URL as given, title =
the tab title) with a resolved ref to the stored file, or, if the fetch failed,
an unresolved ref and the error, so a later run retries it. URLs already in the
library (downloaded before, or found on disk by `scan` with that download URL)
are not fetched again unless `refetch` is set.

Downloads to different hosts run in parallel; each host is fetched one URL at
a time. The database is written only from the calling thread.
"""

from __future__ import annotations

import html.parser
import json
import os
import queue
import re
import subprocess
import sqlite3
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from . import db, purge, store
from . import http
from .pdf import PDF_MAGIC, sha256_file
from .scan import add_file

MAX_HTML_BYTES = 2_000_000


# ---------------------------------------------------------------------------
# Parsing link lists
# ---------------------------------------------------------------------------

@dataclass
class Link:
    url: str
    title: str | None = None


_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((https?://[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)", re.I)
_ARXIV_TOKEN_RE = re.compile(
    r"(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)", re.I)


def normalize_url(url: str) -> str:
    """Strip surrounding punctuation and the #fragment (a page anchor names the same document)."""
    url = url.strip().strip("<>")
    while url and url[-1] in ".,;:!?'\"]}*>":
        url = url[:-1]
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    parts = urlsplit(url)
    return urlunsplit(parts._replace(fragment="")) if parts.fragment else url


def _clean_title(title: str | None, url: str) -> str | None:
    t = re.sub(r"\s+", " ", title or "").strip()
    return t if t and normalize_url(t) != url else None


def dedupe(links: list[Link]) -> list[Link]:
    """One link per URL, in first-seen order, keeping the first title found."""
    out: dict[str, Link] = {}
    for link in links:
        url = normalize_url(link.url)
        if not url:
            continue
        title = _clean_title(link.title, url)
        if url not in out:
            out[url] = Link(url, title)
        elif title and not out[url].title:
            out[url].title = title
    return list(out.values())


def parse_text(text: str) -> list[Link]:
    """Links in plain text: URLs (newline / comma / space separated), markdown `[title](url)`,
    bare arXiv ids, and Safari's copied-tabs layout, where a line holding only a URL is
    preceded by its tab's title."""
    links: list[Link] = []
    prev_plain: str | None = None  # the previous line, if it was text without links
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            prev_plain = None
            continue
        found = [Link(m.group(2), m.group(1)) for m in _MD_LINK_RE.finditer(line)]
        rest = _MD_LINK_RE.sub(" ", line)
        rest = re.sub(r"[,;](?=\s*https?://)", " ", rest)  # 'url1,url2'
        urls = _URL_RE.findall(rest)
        if len(urls) == 1 and not found and not _URL_RE.sub("", rest).strip(" <>,;"):
            found.append(Link(urls[0], prev_plain))  # a line that is just a URL
        else:
            found += [Link(u) for u in urls]
        if not found:
            tokens = [t for t in re.split(r"[\s,;]+", line) if t]
            if tokens and all(_ARXIV_TOKEN_RE.fullmatch(t) for t in tokens):
                found = [Link("https://arxiv.org/abs/" + _ARXIV_TOKEN_RE.fullmatch(t).group(1)) for t in tokens]
        links += found
        prev_plain = None if found else line
    return dedupe(links)


def _rtf_group_end(rtf: str, start: int) -> int:
    """Index just past the group whose '{' is at `start` (escaped braces don't count)."""
    depth, i = 0, start
    while i < len(rtf):
        c = rtf[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(rtf)


_RTF_TOKEN_RE = re.compile(
    r"\\'([0-9a-fA-F]{2})|\\uc(\d+) ?|\\u(-?\d+) ?|\\([a-zA-Z]+)(-?\d+)? ?|\\([\\{}])|\\\r?\n|[{}\r\n]|[^\\{}\r\n]+")


def rtf_to_text(rtf: str) -> str:
    """Plain text of a small RTF fragment (enough for link titles)."""
    rtf = re.sub(r"\{\\listtext[^{}]*\}", "", rtf)  # list bullets
    rtf = re.sub(r"\{\\\*[^{}]*\}", "", rtf)  # ignorable destinations
    out: list[str] = []
    pending = bytearray()  # \'hh bytes, decoded together (code page 1252)
    skip, uc = 0, 1
    for m in _RTF_TOKEN_RE.finditer(rtf):
        hexbyte, ucn, uni, word, _arg, esc = m.groups()
        if hexbyte is None and pending:
            out.append(pending.decode("cp1252", "replace"))
            pending.clear()
        if hexbyte is not None:
            if skip:
                skip -= 1
            else:
                pending.append(int(hexbyte, 16))
        elif ucn is not None:
            uc = int(ucn)
        elif uni is not None:
            n = int(uni)
            out.append(chr(n + 65536 if n < 0 else n))
            skip = uc
        elif word is not None:
            if word in ("par", "line", "tab", "sect"):
                out.append(" ")
        elif esc is not None:
            out.append(esc)
        else:
            text = m.group(0)
            if text in "{}\r\n" or text.startswith("\\"):
                continue
            if skip:
                n = min(skip, len(text))
                text, skip = text[n:], skip - n
            out.append(text)
    if pending:
        out.append(pending.decode("cp1252", "replace"))
    return re.sub(r"\s+", " ", "".join(out)).strip()


_RTF_FIELD_RE = re.compile(r'\\fldinst\s*\{\s*HYPERLINK\s+"([^"]+)"[^{}]*\}\s*\}\s*(\{)\\fldrslt', re.S)


def parse_rtf(rtf: str) -> list[Link]:
    """Hyperlinks in RTF (`{\\field{\\*\\fldinst{HYPERLINK "url"}}{\\fldrslt title}}`)."""
    links = []
    for m in _RTF_FIELD_RE.finditer(rtf):
        start = m.start(2)
        inner = rtf[start + len("{\\fldrslt"):_rtf_group_end(rtf, start) - 1]
        links.append(Link(m.group(1), rtf_to_text(inner)))
    return dedupe(links)


class _AnchorParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.lower().startswith(("http://", "https://")) else None
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append(Link(self._href, "".join(self._text)))
            self._href = None


def parse_html(text: str) -> list[Link]:
    p = _AnchorParser()
    p.feed(text)
    p.close()
    return dedupe(p.links)


# ---------------------------------------------------------------------------
# The clipboard: macOS, or Linux through wl-paste (Wayland) / xclip (X11)
# ---------------------------------------------------------------------------

LINUX_MIME = {"RTF ": "text/rtf", "HTML": "text/html", "text": "text/plain;charset=utf-8"}


def _linux_clipboard(kind: str) -> bytes | None:
    """The clipboard as one MIME type (LINUX_MIME), from the first tool of the session that has it."""
    mime = LINUX_MIME[kind]
    tools = []
    if os.environ.get("WAYLAND_DISPLAY"):
        tools.append(["wl-paste", "--no-newline", "--type", mime])
    if os.environ.get("DISPLAY"):
        tools.append(["xclip", "-selection", "clipboard", "-target", "UTF8_STRING" if kind == "text" else mime, "-out"])
    for cmd in tools:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None


def _clipboard_data(cls: str) -> bytes | None:
    """Raw clipboard data of an AppleScript class ('RTF ', 'HTML'), or None."""
    if sys.platform != "darwin":
        return _linux_clipboard(cls) if sys.platform.startswith("linux") else None
    try:
        r = subprocess.run(["osascript", "-e", f"the clipboard as «class {cls}»"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.fullmatch(r"«data .{4}([0-9A-Fa-f]*)»", r.stdout.strip())
    return bytes.fromhex(m.group(1)) if r.returncode == 0 and m else None


def _clipboard_text() -> str:
    if sys.platform != "darwin":
        raw = _linux_clipboard("text") if sys.platform.startswith("linux") else None
        return raw.decode("utf-8", "replace") if raw else ""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=10,
                           env={**os.environ, "LANG": "en_US.UTF-8"})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace")


def read_clipboard() -> tuple[list[Link], str]:
    """Links on the clipboard and a description of where they came from.

    Rich links (RTF, then HTML) come first because they carry titles; URLs in the
    plain text that the rich flavour didn't hyperlink are appended."""
    rich, flavour = [], "text"
    if rtf := _clipboard_data("RTF "):
        rich, flavour = parse_rtf(rtf.decode("latin-1")), "rtf"
    if not rich and (h := _clipboard_data("HTML")):
        rich, flavour = parse_html(h.decode("utf-8", "replace")), "html"
    plain = parse_text(_clipboard_text())
    links = dedupe(rich + plain)
    if rich and len(links) > len(rich):
        flavour += "+text"
    return links, flavour if links else "empty"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

_ARXIV_URL_RE = re.compile(r"^https?://(?:www\.|export\.)?arxiv\.org/(abs|pdf|html)/(.+?)(?:\.pdf)?/?$", re.I)
_OPENREVIEW_RE = re.compile(r"^https?://openreview\.net/forum\?(?:.*&)?id=([^&]+)", re.I)


def resolve(url: str) -> tuple[str, str | None]:
    """(URL to fetch, referring page) for a link: known landing pages map to their PDF."""
    if m := _ARXIV_URL_RE.match(url):
        return f"https://arxiv.org/pdf/{m.group(2)}", (url if m.group(1).lower() != "pdf" else None)
    if m := _OPENREVIEW_RE.match(url):
        return f"https://openreview.net/pdf?id={m.group(1)}", url
    return url, None


class _MetaParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = {k.lower(): v or "" for k, v in attrs}
            name = (a.get("name") or a.get("property") or "").lower()
            if name and "content" in a and name not in self.meta:
                self.meta[name] = a["content"]


def citation_pdf_url(page: str, base: str) -> str | None:
    """The PDF a scholarly landing page points to (Google Scholar's `citation_pdf_url`)."""
    p = _MetaParser()
    try:
        p.feed(page)
    except Exception:  # malformed HTML: take what we got
        pass
    u = p.meta.get("citation_pdf_url")
    return urljoin(base, u.strip()) if u and u.strip() else None


_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*[\w-]+'[^']*'([^;]+)", re.I)
_CD_FILENAME = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.I)


def filename_from(headers: dict[str, str], url: str) -> str | None:
    cd = headers.get("content-disposition", "")
    if m := _CD_FILENAME_STAR.search(cd):
        name = unquote(m.group(1).strip())
    elif m := _CD_FILENAME.search(cd):
        name = (m.group(1) or m.group(2)).strip()
    else:
        name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = os.path.basename(name.replace("\\", "/")).strip()
    return name or None


@dataclass
class Fetched:
    link: Link
    ok: bool = False
    tmp: Path | None = None
    sha: str | None = None
    size: int = 0
    pdf_url: str | None = None  # where the PDF came from, after redirects
    referrer: str | None = None  # landing page it was found through, if any
    content_type: str | None = None
    filename: str | None = None
    status: int | None = None
    error: str | None = None


def fetch(link: Link, client: http.HttpClient) -> Fetched:
    """Download one link into store/tmp; the result is a PDF or an error, never an HTML page."""
    target, referrer = resolve(link.url)
    for hop in range(2):
        tmp = store.new_tmp()
        try:
            d = client.download(target, tmp)
        except http.HttpError as e:
            tmp.unlink(missing_ok=True)
            return Fetched(link, referrer=referrer, status=e.status, error=str(e)[:500])
        ctype = d.headers.get("content-type")
        with open(tmp, "rb") as fh:
            head = fh.read(1024)
        if PDF_MAGIC in head:
            return Fetched(link, ok=True, tmp=tmp, sha=sha256_file(tmp), size=d.size, pdf_url=d.url,
                           referrer=referrer, content_type=ctype, filename=filename_from(d.headers, d.url),
                           status=d.status)
        is_html = "html" in (ctype or "").lower() or re.search(rb"<(!doctype html|html|head)\b", head, re.I)
        page = tmp.read_bytes()[:MAX_HTML_BYTES].decode("utf-8", "replace") if is_html else ""
        tmp.unlink(missing_ok=True)
        if hop == 0 and is_html and (pdf := citation_pdf_url(page, d.url)):
            target, referrer = pdf, d.url
            continue
        why = "an HTML page with no citation_pdf_url" if is_html else f"not a PDF (starts {head[:12]!r})"
        return Fetched(link, referrer=referrer, content_type=ctype, status=d.status,
                       error=f"{d.url} is {why}; Content-Type {ctype}")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    link: Link
    state: str  # new | dup (bytes already known) | known (not fetched) | failed
    file_id: int | None = None
    detail: str = ""


def known_file(conn: sqlite3.Connection, url: str) -> tuple[int, str] | None:
    """A file already in the library for this URL that we can still read: (file_id, how)."""
    target, _ = resolve(url)
    rows = conn.execute(
        """SELECT r.file_id, c.kind, c.locator, f.stored_path, f.size, c.missing_at, f.purged_at
           FROM contexts c JOIN context_refs r ON r.context_id=c.id JOIN files f ON f.id=r.file_id
           WHERE (c.kind='download' AND c.locator=?) OR (c.kind='filesystem' AND c.url IN (?, ?))
           ORDER BY c.kind='download' DESC""", (url, url, target)).fetchall()
    for r in rows:
        if r["purged_at"]:  # personal paperwork, purged: nothing to fetch again
            return r["file_id"], "purged"
        if store.is_stored(r["stored_path"], r["size"]):
            return r["file_id"], "stored"
        if r["kind"] == "filesystem" and r["missing_at"] is None and os.path.exists(r["locator"]):
            return r["file_id"], r["locator"]
    return None


def _upsert_context(conn: sqlite3.Connection, link: Link, *, url: str | None, extra: dict,
                    now: str, success: bool) -> int:
    host = urlsplit(link.url).hostname
    row = conn.execute("SELECT id, extra FROM contexts WHERE kind='download' AND locator=?", (link.url,)).fetchone()
    if row:
        old = json.loads(row["extra"] or "{}")
        if not success and old.get("where_froms"):  # keep what an earlier success recorded
            extra = {**old, "last_error": extra.get("error"), "last_status": extra.get("status")}
        conn.execute(
            "UPDATE contexts SET title=COALESCE(?, title), url=COALESCE(?, url), extra=?, last_seen_at=?,"
            " occurred_at=CASE WHEN ? THEN COALESCE(occurred_at, ?) ELSE occurred_at END WHERE id=?",
            (link.title, url, json.dumps(extra), now, success, now, row["id"]))
        return row["id"]
    return conn.execute(
        """INSERT INTO contexts(kind, locator, source, occurred_at, title, url, extra, first_seen_at, last_seen_at)
           VALUES ('download', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (link.url, host, now if success else None, link.title, url, json.dumps(extra), now, now)).lastrowid


def _resolve_ref(conn: sqlite3.Connection, ctx_id: int, url: str, file_id: int, source: str) -> None:
    conn.execute("DELETE FROM context_refs WHERE context_id=? AND (file_id IS NULL OR file_id<>?)", (ctx_id, file_id))
    if not conn.execute("SELECT 1 FROM context_refs WHERE context_id=? AND file_id=?", (ctx_id, file_id)).fetchone():
        conn.execute("INSERT INTO context_refs(context_id, url, file_id, status, confidence, source)"
                     " VALUES (?,?,?, 'resolved', 1.0, ?)", (ctx_id, url, file_id, source))


def record(conn: sqlite3.Connection, f: Fetched, run_id: int, stats: Counter) -> Outcome:
    now = db.now_iso()
    link = f.link
    if not f.ok:
        extra = {"error": f.error, "status": f.status, "content_type": f.content_type}
        if f.referrer:
            extra["referrer"] = f.referrer
        with db.tx(conn):
            ctx_id = _upsert_context(conn, link, url=None, extra=extra, now=now, success=False)
            if not conn.execute("SELECT 1 FROM context_refs WHERE context_id=?", (ctx_id,)).fetchone():
                conn.execute("INSERT INTO context_refs(context_id, url, status, source)"
                             " VALUES (?, ?, 'unresolved', 'download')", (ctx_id, link.url))
        stats["failed"] += 1
        return Outcome(link, "failed", detail=f.error or "")
    assert f.tmp and f.sha
    froms = [f.pdf_url] + ([f.referrer] if f.referrer and f.referrer != f.pdf_url else [])
    if (purged := purge.is_purged(conn, f.sha)) is not None:
        # Bytes purged as personal paperwork: keep only the URL (as purge leaves download contexts).
        f.tmp.unlink(missing_ok=True)
        with db.tx(conn):
            ctx_id = _upsert_context(conn, Link(link.url), url=f.pdf_url,
                                     extra={"where_froms": froms, "purged": True}, now=now, success=False)
            _resolve_ref(conn, ctx_id, f.pdf_url or link.url, purged, "download")
        stats["purged_skipped"] += 1
        return Outcome(link, "purged", purged)
    rel, _created = store.commit(f.tmp, f.sha)
    path = store.abs_path(rel)
    extra = {"filename": f.filename, "where_froms": froms, "content_type": f.content_type, "size": f.size}
    with db.tx(conn):
        file_id, new = add_file(conn, path, f.sha, f.size, name=f.filename, stored_rel=rel, run_id=run_id,
                                stats=stats)
        ctx_id = _upsert_context(conn, link, url=f.pdf_url, extra=extra, now=now, success=True)
        _resolve_ref(conn, ctx_id, f.pdf_url or link.url, file_id, "download")
    return Outcome(link, "new" if new else "dup", file_id, f.pdf_url or "")


def record_known(conn: sqlite3.Connection, link: Link, file_id: int, how: str) -> Outcome:
    """Note that this URL was asked for again (refreshing its title) without fetching it."""
    now = db.now_iso()
    with db.tx(conn):
        row = conn.execute("SELECT id FROM contexts WHERE kind='download' AND locator=?", (link.url,)).fetchone()
        if row and how == "purged":  # no title for personal paperwork
            conn.execute("UPDATE contexts SET last_seen_at=? WHERE id=?", (now, row["id"]))
        elif row:
            conn.execute("UPDATE contexts SET title=COALESCE(?, title), last_seen_at=? WHERE id=?",
                         (link.title, now, row["id"]))
    return Outcome(link, "known", file_id, how)


def download(conn: sqlite3.Connection, links: list[Link], *, client: http.HttpClient | None = None,
             jobs: int = 4, refetch: bool = False,
             progress: Callable[[Outcome], None] | None = None) -> tuple[Counter, list[Outcome]]:
    client = client or http.default_client()
    links = dedupe(links)
    store.clean_tmp()
    outcomes: list[Outcome] = []

    def done(o: Outcome) -> None:
        outcomes.append(o)
        if progress:
            progress(o)

    with db.recorded_run(conn, "download", {"urls": len(links), "refetch": refetch}) as (run_id, run_stats):
        stats: Counter = Counter()
        todo: dict[str, list[Link]] = defaultdict(list)  # host -> links, fetched one at a time per host
        for link in links:
            known = None if refetch else known_file(conn, link.url)
            if known:
                stats["known"] += 1
                done(record_known(conn, link, *known))
            else:
                todo[urlsplit(resolve(link.url)[0]).hostname or ""].append(link)
        results: queue.Queue[Fetched] = queue.Queue()
        stop = threading.Event()

        def worker(batch: list[Link]) -> None:
            for link in batch:
                if stop.is_set():
                    return
                try:
                    results.put(fetch(link, client))
                except Exception as e:  # one bad URL must not take down the others
                    results.put(Fetched(link, error=f"{type(e).__name__}: {e}"[:500]))

        pending = sum(len(b) for b in todo.values())
        ex = ThreadPoolExecutor(max_workers=max(1, jobs), thread_name_prefix="download")
        finished = False
        try:
            for batch in todo.values():
                ex.submit(worker, batch)
            for _ in range(pending):
                done(record(conn, results.get(), run_id, stats))
            finished = True
        finally:
            stop.set()  # on interrupt: workers stop after their current URL; don't wait for them
            ex.shutdown(wait=finished, cancel_futures=True)
            run_stats.update(stats)
    return stats, outcomes
