"""LLM triage: from filename + path + PDF info + pages (`build_state`), classify what a document is.

Produces assertions on the file (never overwrites anything):
  kind, is_library_material      - Classifier probabilities (Jev-style) if available,
                                    else from the extraction call
  title, authors, year, venue,
  abstract (verbatim), summary,
  topics [{name, field}], language - extraction call (Backend.extract)
  ids {arxiv, doi, isbn}           - regex over filename/URLs/text (no LLM)
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, pagetext
from .llm.base import Backend, Classifier, Question
from .pdf import TRIAGE_PAGES
from .textspan import best_abstract

# triage-1: model copied the abstract verbatim (slow: hundreds of output tokens per paper).
# triage-2: model gives start/end anchors; the exact span is cut from the page text.
# triage-3: the state gives the file's size, and caps each PDF metadata field at META_CHARS.
# triage-4: with a classifier, it runs first and settles kind and library-ness: extraction no longer
#           asks for `kind` / `is_library_material` (EXTRACT_SCHEMA_DECIDED), and runs only for files
#           the classifier keeps (P(kind=personal) < 0.5) or the user kept: two stages,
#           classify_file (triage_status 'classified') and extract_metadata.
# triage-5: the state is about STATE_TOKENS tokens, not 4000 characters: a '--- document ---' and a
#           '--- metadata ---' section of `key: value` lines, then pages from the front and, for a
#           longer document, the back (pagetext.py: '...' marks text left out, '(no text)' pages
#           without a text layer), normalised. INSTRUCTIONS say so. The screen reads it too.
# triage-6: the classifier asks only `kind`; its is_library_material question is gone (unused, and
#           a third of the classifier's work: a second pass over the state's tail). No prime then.
PROMPT_VERSION = "triage-6"
META_CHARS = 200  # per PDF metadata field in the state: a long keywords list must not crowd out page 1
ANCHOR_WORDS = 8

KINDS = {
    "paper": "a research paper or preprint (journal, conference, arXiv, workshop paper)",
    "book": "a whole book or textbook (title page, table of contents, ISBN, publisher)",
    "chapter": "a single chapter or excerpt from a book or handbook",
    "thesis": "a PhD, master's or bachelor's thesis or dissertation",
    "slides": "presentation slides or a poster",
    "tutorial": "a tutorial, primer, survey-style introduction or blog-like explainer",
    "lecture_notes": "lecture notes or course material",
    "article": "a magazine, newspaper, essay or blog article",
    "report": "a technical report, white paper or system card",
    "manual": "software documentation, a manual, specification or datasheet",
    "personal": "personal or administrative paperwork: payslips, invoices, receipts, CVs, forms, contracts, "
                "tickets, statements, letters, exam papers, reviews of someone's work",
    "other": "anything else",
}

INSTRUCTIONS = """You catalogue documents for a personal research library.
You are given a PDF's name, where it was found and downloaded from, its embedded PDF
metadata and the text of its pages: the first pages and, for a longer document, the last
ones. '...' on a line of its own marks text left out; '(no text)' marks pages without a
text layer. Extract bibliographic metadata. Rules:
- Use only information present in the input; do not invent authors, years or venues.
- title: the document's real title as printed (not the filename), in normal capitalisation.
- authors: full names in printed order, without affiliations, footnote marks or emails.
- abstract: do NOT copy it. If the text contains an abstract, set has_abstract=true and give
  abstract_start = its first 8 words and abstract_end = its last 8 words, copied exactly as they
  appear (excluding the heading word 'Abstract'). Otherwise has_abstract=false and both are empty.
  Never point at other page text (copyright pages, forms, tables).
- summary: one sentence in your own words describing what the document is about.
- topics: 2-6 specific topics, each with the academic field it belongs to
  (e.g. {"name": "equality saturation", "field": "programming languages"}).
- year: the publication year if stated or clearly implied (arXiv id 2412.xxxxx means 2024)."""

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "is_library_material": {"type": "boolean",
                                "description": "true for papers/books/notes worth keeping in a research "
                                               "library; false for personal/administrative documents"},
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "year": {"type": "integer"},
        "venue": {"type": "string"},
        "has_abstract": {"type": "boolean",
                         "description": "true only if the text contains an Abstract section or an obvious "
                                        "abstract paragraph"},
        "abstract_start": {"type": "string",
                           "description": "first 8 words of the abstract, verbatim; empty if no abstract"},
        "abstract_end": {"type": "string",
                         "description": "last 8 words of the abstract, verbatim; empty if no abstract"},
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "field": {"type": "string"}},
            "required": ["name", "field"]}},
        "language": {"type": "string", "description": "ISO 639-1 code, e.g. en"},
    },
    "required": ["kind", "is_library_material", "title", "authors", "has_abstract", "abstract_start",
                 "abstract_end", "summary", "topics", "language"],
}

# With a classifier, kind and library-ness are its questions; the extraction model isn't asked them.
EXTRACT_SCHEMA_DECIDED: dict[str, Any] = {
    **EXTRACT_SCHEMA,
    "properties": {k: v for k, v in EXTRACT_SCHEMA["properties"].items() if k not in ("kind", "is_library_material")},
    "required": [k for k in EXTRACT_SCHEMA["required"] if k not in ("kind", "is_library_material")],
}

# One question: library-ness is P(kind=personal) (works.is_library). The yes/no "is this library
# material?" question was dropped in triage-6: biased towards yes, and nothing read its answer.
CLASSIFICATIONS = [
    Question("kind", "choice", "What kind of document is this?", KINDS),
]


@dataclass
class TriageInput:
    file_id: int
    state: str


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
    from .fulltext import readable_path
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
    from .enrich import ids as idmod

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


def classify_file(conn: sqlite3.Connection, file_id: int, classifier: Classifier | None,
                  run_id: int | None = None, max_tokens: int | None = None, screen: Any = None) -> dict[str, Any]:
    """Triage's first stage: the screen, then the classifier (kind, library or not).

    Sets triage_status to `classified` when the file goes on to metadata extraction (library
    material, or no classifier to say otherwise), else to `done`: set aside by the screen, or
    judged not library material (promote then purges or holds it, named by its file name: personal
    paperwork never gets a title, summary or topics from a model). The user's `keep` outranks the
    classifier."""
    from . import inference
    from .purge import user_says

    result: dict[str, Any] = {"file_id": file_id}
    if screen is not None:
        from .screen import screen_file

        s = screen_file(conn, file_id, screen, run_id=run_id)
        if s is not None:
            result["screen"] = s
            if s["rejected"]:
                result["screened"] = True
                result["status"] = "done"
                return result
    answers, ccalls = {}, {}
    if classifier is not None:  # outside the write transaction: model calls can take seconds
        state = build_state(conn, file_id, max_tokens=max_tokens)
        sha = conn.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()[0]
        t1 = time.monotonic()
        with inference.subject(sha):
            answers = classifier.classify(state, CLASSIFICATIONS)
        ccalls = inference.calls_of(classifier)
        result["classify_s"] = round(time.monotonic() - t1, 2)
    p_personal = answers["kind"].probabilities.get("personal", 0.0) if "kind" in answers else None
    user = user_says(conn, file_id)
    library = user if user is not None else (p_personal < 0.5 if p_personal is not None else None)
    status = "done" if library is False else "classified"
    with db.tx(conn):
        if answers:
            ctag = "classify:" + classifier.info().tag
            for key, ans in answers.items():
                value = ans.best if key == "kind" else ans.best == "yes"
                db.assert_(conn, "file", file_id, key, value, source=ctag, confidence=ans.confidence,
                           run_id=run_id, call_id=ccalls.get(key))
                db.assert_(conn, "file", file_id, key + "_probs", ans.probabilities, source=ctag, run_id=run_id,
                           call_id=ccalls.get(key))
                result[key] = (value, round(ans.confidence, 3))
        ids = detect_ids(conn, file_id)
        if ids:
            db.assert_(conn, "file", file_id, "ids", ids, source="heuristic:regex", run_id=run_id)
            result["ids"] = ids
        conn.execute("UPDATE files SET triage_status=? WHERE id=?", (status, file_id))
    result["status"], result["p_personal"], result["library"] = status, p_personal, library
    return result


def classified_p_personal(conn: sqlite3.Connection, file_id: int) -> float | None:
    """P(kind=personal) from the file's current classifier claim, if it has one."""
    r = conn.execute("SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND field='kind_probs'"
                     " AND source LIKE 'classify:%' AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
                     (file_id,)).fetchone()
    return None if r is None else float(json.loads(r[0]).get("personal", 0.0))


def extract_metadata(conn: sqlite3.Connection, file_id: int, backend: Backend, run_id: int | None = None,
                     max_tokens: int | None = None) -> dict[str, Any]:
    """Triage's second stage, for `classified` files: title, authors, year, venue, summary,
    topics, language and the abstract, from the extraction model. Sets triage_status to `done`.

    After a classifier, the model isn't asked for kind or library-ness (EXTRACT_SCHEMA_DECIDED).
    Without one (classifier "none"), it is, and its own answer gates topics and the abstract."""
    from . import inference
    from .purge import user_says

    result: dict[str, Any] = {"file_id": file_id}
    p_personal = classified_p_personal(conn, file_id)
    user = user_says(conn, file_id)
    state = build_state(conn, file_id, max_tokens=max_tokens)
    sha = conn.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()[0]
    btag = "llm:" + backend.info().tag
    t0 = time.monotonic()
    with inference.subject(sha):
        extracted = backend.extract(instructions=INSTRUCTIONS, prompt=state,
                                    schema=EXTRACT_SCHEMA if p_personal is None else EXTRACT_SCHEMA_DECIDED,
                                    max_tokens=1500)
    ecall = inference.call_of(backend)
    if p_personal is not None:  # not asked; LM Studio doesn't enforce the schema, so drop them
        extracted.pop("kind", None)
        extracted.pop("is_library_material", None)
    result["extract_s"] = round(time.monotonic() - t0, 2)
    result["cached"] = bool(getattr(backend, "last_hit", False))
    library = (user if user is not None else p_personal < 0.5 if p_personal is not None
               else extracted.get("is_library_material") is not False)
    # Abstract: cut the exact span out of the page text using the model's anchors, falling back
    # to an 'Abstract ... Introduction' heuristic. Only for library material, so that personal
    # paperwork never gets copied into the database as an "abstract".
    extracted["abstract"], abstract_source = None, None
    if extracted.get("has_abstract") and library:
        pages = "\n".join(r[0] for r in conn.execute(  # the first pages, even once the full text is in
            "SELECT text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page", (file_id, TRIAGE_PAGES)))
        text, method = best_abstract(pages, extracted.get("abstract_start"), extracted.get("abstract_end"))
        if text:
            extracted["abstract"] = text
            abstract_source = "heuristic:abstract" if method == "heuristic" else f"{btag}#{method}"
        result["abstract_method"] = method
    if not library:
        extracted["topics"] = []  # don't index personal paperwork by topic
    with db.tx(conn):
        for field in ["title", "authors", "year", "venue", "summary", "topics", "language",
                      "kind", "is_library_material"]:
            v = extracted.get(field)
            if v not in (None, "", []):
                db.assert_(conn, "file", file_id, field, v, source=btag, run_id=run_id, call_id=ecall)
        if extracted["abstract"]:
            db.assert_(conn, "file", file_id, "abstract", extracted["abstract"], source=abstract_source,
                       run_id=run_id, call_id=ecall)
        conn.execute("UPDATE files SET triage_status='done' WHERE id=?", (file_id,))
    result["extracted"] = extracted
    return result


def triage_file(conn: sqlite3.Connection, file_id: int, backend: Backend, classifier: Classifier | None,
                run_id: int | None = None, max_tokens: int | None = None, screen: Any = None) -> dict[str, Any]:
    """Both stages for one file (`classify_file`, then `extract_metadata` if it was classified
    as library material). The CLI and the watcher run the stages in batches instead, one model
    at a time; this is for single files and tests.

    result: the classifier's answers (key -> (value, confidence)), `screened` if the screen set it
    aside, `extract_skipped` if no extraction was needed, and `extracted` (empty then)."""
    result = classify_file(conn, file_id, classifier, run_id=run_id, max_tokens=max_tokens, screen=screen)
    if result["status"] == "classified":
        e = extract_metadata(conn, file_id, backend, run_id=run_id, max_tokens=max_tokens)
        result.update({k: v for k, v in e.items() if k != "file_id"})
    else:
        result["extract_skipped"] = not result.get("screened")
        result["extracted"] = {"abstract": None}
    return result
