"""LLM triage: from filename + path + PDF info + first pages, classify what a document is.

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
from dataclasses import dataclass
from typing import Any

from . import db
from .llm.base import Backend, Classifier, Question
from .pdf import TRIAGE_PAGES
from .textspan import best_abstract

# triage-1: model copied the abstract verbatim (slow: hundreds of output tokens per paper).
# triage-2: model gives start/end anchors; the exact span is cut from the page text.
# triage-3: the state gives the file's size, and caps each PDF metadata field at META_CHARS.
# triage-4: with a classifier, it runs first and settles kind and library-ness: extraction no longer
#           asks for `kind` / `is_library_material` (EXTRACT_SCHEMA_DECIDED), keeps topics and the
#           abstract only if P(kind=personal) < 0.5, and is skipped when P(personal) >= the purge
#           threshold (promote purges those files anyway).
PROMPT_VERSION = "triage-4"
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
You are given a PDF's filename, where it was found, where it was downloaded from,
its embedded PDF metadata and the text of its first page(s). Extract bibliographic
metadata. Rules:
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

CLASSIFICATIONS = [
    Question("kind", "choice", "What kind of document is this?", KINDS),
    Question("is_library_material", "yesno",
             "Is this something to keep in a personal research/reading library (a paper, book, notes, "
             "tutorial, slides, documentation), as opposed to personal or administrative paperwork?"),
]


@dataclass
class TriageInput:
    file_id: int
    state: str


# arXiv's margin stamp: 'arXiv:2412.06464v3 [cs.CL] 6 Mar 2025', 'arXiv:math/0601001v1 [math.CO] 1 Jan 2006'
ARXIV_STAMP = re.compile(
    r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)\s*\[[A-Za-z][A-Za-z\-.]*\]\s*"
    r"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}")
STATE_CHARS = 4000  # prompt processing dominates latency on small Macs; page 1 is nearly always enough


def build_state(conn: sqlite3.Connection, file_id: int, max_chars: int = STATE_CHARS) -> str:
    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    lines: list[str] = []
    ctxs = conn.execute(
        "SELECT c.kind, c.locator, c.url, c.title, c.extra, c.occurred_at FROM context_refs r"
        " JOIN contexts c ON c.id=r.context_id"
        " WHERE r.file_id=? AND c.kind IN ('filesystem', 'download')"
        " ORDER BY c.missing_at IS NOT NULL, c.occurred_at",
        (file_id,),
    ).fetchall()
    for c in ctxs[:3]:
        extra = json.loads(c["extra"] or "{}")
        if extra.get("filename"):
            lines.append(f"Filename: {extra.get('filename')}")
        if c["kind"] == "filesystem":
            lines.append(f"Found at: {c['locator']}")
        elif c["title"]:  # download: the browser tab's title
            lines.append(f"Link title: {c['title']}")
        froms = extra.get("where_froms") or []
        if froms:
            lines.append(f"Downloaded from: {froms[0]}" + (f" (via page {froms[1]})" if len(froms) > 1 else ""))
    meta = {k: f[f"pdf_{k}"] for k in ["title", "author", "subject", "keywords", "creator", "producer", "created"]}
    meta = {k: _cap(str(v)) for k, v in meta.items() if v}
    lines.append(f"Pages: {f['page_count']}")
    if f["size"]:
        lines.append(f"Size: {_size(f['size'])}")
    if meta:
        lines.append("PDF metadata: " + "; ".join(f"{k}={v}" for k, v in meta.items()))
    hint = conn.execute(
        "SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND field='filename_hint'"
        " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if hint:
        lines.append(f"Filename suggests: {hint['value']}")
    header = "\n".join(lines)
    budget = max(500, max_chars - len(header))
    pages = conn.execute("SELECT page, text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page",
                         (file_id, TRIAGE_PAGES)).fetchall()
    body = []
    for p in pages:
        if budget <= 0:
            break
        t = p["text"][:budget]
        budget -= len(t)
        body.append(f"--- page {p['page']} ---\n{t}")
    if not body:
        body.append("--- (no extractable text: probably a scanned or image-only PDF) ---")
    return header + "\n\n" + "\n".join(body)


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


def triage_file(conn: sqlite3.Connection, file_id: int, backend: Backend, classifier: Classifier | None,
                run_id: int | None = None, max_chars: int = STATE_CHARS, screen: Any = None) -> dict[str, Any]:
    """Ask the models about one file and record their claims. With cached models
    (inference.cached_backend / cached_classifier), each claim carries the call it came from, and
    the calls carry the file's sha256, so a purge can find them.

    With a `screen` (screen.py), a file it sets aside as personal paperwork stops there:
    result["screened"] is True and no language model is asked."""
    from . import inference

    result: dict[str, Any] = {"file_id": file_id}
    if screen is not None:
        from .screen import screen_file

        s = screen_file(conn, file_id, screen, run_id=run_id)
        if s is not None:
            result["screen"] = s
            if s["rejected"]:
                result["screened"] = True
                return result
    from .purge import PURGE_THRESHOLD, user_says

    state = build_state(conn, file_id, max_chars=max_chars)
    sha = conn.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()[0]
    btag = "llm:" + backend.info().tag
    # The classifier first (outside the write transaction: model calls can take seconds). With one,
    # kind and library-ness are its answers: P(kind=personal), as works.is_library reads it.
    answers, dcalls = {}, {}
    if classifier is not None:
        t1 = time.monotonic()
        with inference.subject(sha):
            answers = classifier.classify(state, CLASSIFICATIONS)
        dcalls = inference.calls_of(classifier)
        result["classify_s"] = round(time.monotonic() - t1, 2)
    p_personal = answers["kind"].probabilities.get("personal", 0.0) if "kind" in answers else None
    user = user_says(conn, file_id)  # the user's own judgement outranks the classifier (`papyrus keep`)
    extracted: dict[str, Any] = {"abstract": None}
    abstract_source, ecall = None, None
    if user is not True and p_personal is not None and p_personal >= PURGE_THRESHOLD:
        result["extract_skipped"] = True  # promote purges it: a title or summary would be wasted work
    else:
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
        extracted["abstract"] = None
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
        if answers:
            dtag = "classify:" + classifier.info().tag
            for key, ans in answers.items():
                value = ans.best if key == "kind" else ans.best == "yes"
                db.assert_(conn, "file", file_id, key, value, source=dtag, confidence=ans.confidence,
                           run_id=run_id, call_id=dcalls.get(key))
                db.assert_(conn, "file", file_id, key + "_probs", ans.probabilities, source=dtag, run_id=run_id,
                           call_id=dcalls.get(key))
                result[key] = (value, round(ans.confidence, 3))
        ids = detect_ids(conn, file_id)
        if ids:
            db.assert_(conn, "file", file_id, "ids", ids, source="heuristic:regex", run_id=run_id)
            result["ids"] = ids
        conn.execute("UPDATE files SET triage_status='done' WHERE id=?", (file_id,))
    result["extracted"] = extracted
    return result
