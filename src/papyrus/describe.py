"""The document description ("state"): what the screen, the classifier and the extraction model
read about a file. A `--- document ---` and a `--- metadata ---` section (names, folders, download
origins, size, pages, the PDF's own metadata), then pages from the front and, for a longer
document, the back (pagetext.py), within a token budget (`build_state`). Also `detect_ids`: this
document's own identifiers (arXiv, DOI, ISBN), by regex, no model.

PROMPT_VERSION covers everything the models are shown: this description, the classifier's
questions (classify.py) and the extraction prompt (extract.py). It is recorded on every run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pagetext

# 0.1: model copied the abstract verbatim (slow: hundreds of output tokens per paper).
# 0.2: model gives start/end anchors; the exact span is cut from the page text.
# 0.3: the state gives the file's size, and caps each PDF metadata field at META_CHARS.
# 0.4: with a classifier, it runs first and settles kind and library-ness: extraction no longer
#           asks for `kind` / `is_library_material` (EXTRACT_SCHEMA_DECIDED), and runs only for files
#           the classifier keeps (P(kind=personal) < 0.5) or the user kept: two stages,
#           classify_file (triage_status 'classified') and extract_metadata.
# 0.5: the state is about STATE_TOKENS tokens, not 4000 characters: a '--- document ---' and a
#           '--- metadata ---' section of `key: value` lines, then pages from the front and, for a
#           longer document, the back (pagetext.py: '...' marks text left out, '(no text)' pages
#           without a text layer), normalised. INSTRUCTIONS say so. The screen reads it too.
# 0.6: the classifier asks only `kind`; its is_library_material question is gone (unused, and
#           a third of the classifier's work: a second pass over the state's tail). No prime then.
PROMPT_VERSION = "0.6"
META_CHARS = 200  # per PDF metadata field in the state: a long keywords list must not crowd out page 1


# arXiv's margin stamp: 'arXiv:2412.06464v3 [cs.CL] 6 Mar 2025', 'arXiv:math/0601001v1 [math.CO] 1 Jan 2006'
ARXIV_STAMP = re.compile(
    r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)\s*\[[A-Za-z][A-Za-z\-.]*\]\s*"
    r"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}")
MIN_PAGE_TOKENS = 250  # the pages get at least this much, however long the header
CONTEXTS_SHOWN = 3  # names, folders, origins and link titles: at most this many of each
META_FIELDS = ["title", "author", "subject", "keywords", "creator", "producer", "created"]
FIRST_LINE_ONLY = {"creator", "producer"}  # what follows a newline there is tool noise
NO_PAGES = "--- (no extractable text: probably a scanned or image-only PDF) ---"


@dataclass
class Document:
    """What the state says about a document besides its pages."""

    names: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # home-relative: ~/Downloads
    origins: list[str] = field(default_factory=list)  # 'download URL (via referrer)'
    link_titles: list[str] = field(default_factory=list)
    size: int | None = None
    page_count: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # META_FIELDS -> the PDF's own values
    hint: str | None = None  # the filename heuristic's guess, as JSON


def document(conn: sqlite3.Connection, file_id: int) -> Document:
    """A file's names, folders and origins (filesystem and download contexts, present ones first,
    oldest first), size, page count, PDF metadata and filename hint."""
    from .screen import home_relative

    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    d = Document(size=f["size"], page_count=f["page_count"], meta={k: f[f"pdf_{k}"] for k in META_FIELDS})
    for c in conn.execute(
            "SELECT c.kind, c.locator, c.title, c.extra FROM context_refs r JOIN contexts c ON c.id=r.context_id"
            " WHERE r.file_id=? AND c.kind IN ('filesystem', 'download')"
            " ORDER BY c.missing_at IS NOT NULL, c.occurred_at", (file_id,)):
        extra = json.loads(c["extra"] or "{}")
        if extra.get("filename"):
            d.names.append(extra["filename"])
        if c["kind"] == "filesystem":
            d.folders.append(home_relative(str(Path(c["locator"]).parent)))
        elif c["title"]:  # download: the browser tab's title
            d.link_titles.append(c["title"])
        froms = extra.get("where_froms") or []
        if froms:
            d.origins.append(froms[0] + (f" (via {froms[1]})" if len(froms) > 1 else ""))
    hint = conn.execute(
        "SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND field='filename_hint'"
        " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    d.hint = hint["value"] if hint else None
    return d


def _distinct(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))[:CONTEXTS_SHOWN]


def _meta_value(key: str, v: Any) -> str:
    """One line: creator and producer keep their first line; other multi-line values are joined
    with '; ', leading bullets dropped ('# ISBN-10: 1\\r\\n# ISBN-13: 9' -> 'ISBN-10: 1; ISBN-13: 9')."""
    lines = [ln.strip() for ln in re.split(r"\r\n|\r|\n", str(v))]
    if key in FIRST_LINE_ONLY:
        return _cap(next((ln for ln in lines if ln), ""))
    lines = [re.sub(r"^(?:[#*\u2022]+|-(?=\s))\s*", "", ln) for ln in lines]
    return _cap("; ".join(ln for ln in lines if ln))


def state_header(d: Document) -> str:
    out = ["--- document ---"]
    out += [f"name: {_cap(n)}" for n in _distinct(d.names)]
    if d.size:
        out.append(f"size: {_size(d.size)}")
    if d.page_count:
        out.append(f"pages: {d.page_count}")
    out += [f"folder: {_cap(x)}" for x in _distinct(d.folders)]
    out += [f"origin: {_cap(x)}" for x in _distinct(d.origins)]
    out += [f"link title: {_cap(x)}" for x in _distinct(d.link_titles)]
    if d.hint:
        out.append(f"name suggests: {_cap(d.hint)}")
    meta = {k: _meta_value(k, d.meta[k]) for k in META_FIELDS if d.meta.get(k)}
    meta = {k: v for k, v in meta.items() if v}
    if meta:
        out += ["", "--- metadata ---", *(f"{k}: {v}" for k, v in meta.items())]
    return "\n".join(out)


def render_state(d: Document, n_pages: int, page_text: Callable[[int], str], max_tokens: int | None = None,
                 tail_fraction: float | None = None) -> str:
    """The state: the header, then pages chosen by pagetext.select_pages within about
    `max_tokens` (config [triage]). `page_text(p)` is page p's raw text ('' without a text layer)."""
    from . import config

    if max_tokens is None or tail_fraction is None:
        cfg = config.triage()
        max_tokens = cfg.state_tokens if max_tokens is None else max_tokens
        tail_fraction = cfg.tail_fraction if tail_fraction is None else tail_fraction
    header = state_header(d)
    budget = max(MIN_PAGE_TOKENS, max_tokens - pagetext.estimate_tokens(header + "\n\n"))
    cache: dict[int, str] = {}

    def text(p: int) -> str:
        if p not in cache:
            cache[p] = pagetext.normalize(page_text(p))
        return cache[p]

    body = pagetext.select_pages(n_pages, text, budget, tail_fraction).body if n_pages > 0 else ""
    return header + "\n\n" + (body or NO_PAGES)


def build_state(conn: sqlite3.Connection, file_id: int, max_tokens: int | None = None,
                tail_fraction: float | None = None) -> str:
    """What every triage model reads about a file (the screen, the classifier and the extraction
    model). Pages come from `file_pages` (the text layer or OCR) and, past what scanning stored,
    from papyrus's copy of the PDF, read on demand and not stored: personal paperwork stays at
    the pages scanning read."""
    from .index import readable_path
    from .pdf import PageReader

    d = document(conn, file_id)
    stored = {r[0]: r[1] for r in conn.execute("SELECT page, text FROM file_pages WHERE file_id=?", (file_id,))}
    path = readable_path(conn, file_id)
    reader: PageReader | None = None
    n = d.page_count or 0
    if path is None:  # only what is stored
        n = min(n, max(stored, default=0)) if n else max(stored, default=0)

    def page_text(p: int) -> str:
        nonlocal reader, path
        if p in stored:
            return stored[p]
        if path is None:
            return ""
        try:
            reader = reader or PageReader(path)
            return reader.text(p)
        except Exception:  # noqa: BLE001  pdfium: broken or encrypted file; show what is stored
            path = None
            return ""

    try:
        return render_state(d, n, page_text, max_tokens, tail_fraction)
    finally:
        if reader is not None:
            reader.close()


def _cap(v: str, n: int = META_CHARS) -> str:
    v = " ".join(v.split())
    return v if len(v) <= n else v[: n - 1].rstrip() + "…"


def _size(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


def detect_ids(conn: sqlite3.Connection, file_id: int) -> dict[str, Any]:
    """Identifiers of *this* document, not of papers it cites.

    Trusted, in order: filename, download URLs and the title of the link it was downloaded
    from; then the PDF's own subject and keywords fields (DOI and ISBN only: publishers put
    the document's DOI there); then page 1 (arXiv stamp in the margin, DOI/ISBN in the header
    or footer). Later pages are ignored because they are full of references.
    """
    from . import ids as idmod

    names_urls = []
    for r in conn.execute(
        "SELECT c.kind, c.title, c.extra FROM context_refs r JOIN contexts c ON c.id=r.context_id"
        " WHERE r.file_id=? AND c.kind IN ('filesystem', 'download')", (file_id,)):
        extra = json.loads(r["extra"] or "{}")
        names_urls += [extra.get("filename") or "", *(extra.get("where_froms") or [])]
        if r["kind"] == "download" and r["title"]:  # e.g. '[2312.00429] Polygraphs: …' from arXiv
            names_urls.append(r["title"])
    page1 = conn.execute("SELECT text FROM file_pages WHERE file_id=? AND page=1", (file_id,)).fetchone()
    page1 = page1["text"] if page1 else ""

    info = conn.execute("SELECT pdf_subject, pdf_keywords FROM files WHERE id=?", (file_id,)).fetchone()
    pdf_meta = idmod.detect("\n".join(v for v in (info or ()) if v))

    found: dict[str, Any] = {}
    meta = idmod.detect("\n".join(names_urls))
    p1 = idmod.detect(page1)
    arx = meta.best_arxiv
    if arx:
        found["arxiv"], found["arxiv_version"] = arx.id, arx.version
    elif m := ARXIV_STAMP.search(page1):
        # Only arXiv's own margin stamp identifies *this* document; arXiv URLs/ids elsewhere on
        # page 1 are citations (e.g. a CV listing its author's papers).
        if a := idmod.parse_arxiv_id(m.group(1) + m.group(2)):
            found["arxiv"], found["arxiv_version"] = a.id, a.version
    dois = [d for d in meta.dois + pdf_meta.dois + p1.dois if not idmod.is_arxiv_doi(d)]
    if dois:
        found["doi"] = dois[0]
    isbns = list(dict.fromkeys(meta.isbns + pdf_meta.isbns + p1.isbns))
    if isbns:
        found["isbn"] = isbns[0]
        if len(isbns) > 1:
            found["isbns"] = isbns
    return {k: v for k, v in found.items() if v}
