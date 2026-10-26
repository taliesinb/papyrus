"""LLM triage: from filename + path + PDF info + first pages, decide what a document is.

Produces assertions on the file (never overwrites anything):
  kind, is_library_material      - Decider probabilities (Jev-style) if available,
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
from .llm.base import Backend, Decider, Question
from .textspan import best_abstract

# triage-1: model copied the abstract verbatim (slow: hundreds of output tokens per paper).
# triage-2: model gives start/end anchors; the exact span is cut from the page text.
PROMPT_VERSION = "triage-2"
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

DECISIONS = [
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
        "SELECT c.locator, c.url, c.extra, c.occurred_at FROM context_refs r JOIN contexts c ON c.id=r.context_id"
        " WHERE r.file_id=? AND c.kind='filesystem' ORDER BY c.missing_at IS NOT NULL, c.occurred_at",
        (file_id,),
    ).fetchall()
    for c in ctxs[:3]:
        extra = json.loads(c["extra"] or "{}")
        lines.append(f"Filename: {extra.get('filename')}")
        lines.append(f"Found at: {c['locator']}")
        froms = extra.get("where_froms") or []
        if froms:
            lines.append(f"Downloaded from: {froms[0]}" + (f" (via page {froms[1]})" if len(froms) > 1 else ""))
    meta = {k: f[f"pdf_{k}"] for k in ["title", "author", "subject", "keywords", "creator", "producer", "created"]}
    meta = {k: v for k, v in meta.items() if v}
    lines.append(f"Pages: {f['page_count']}")
    if meta:
        lines.append("PDF metadata: " + "; ".join(f"{k}={v}" for k, v in meta.items()))
    hint = conn.execute(
        "SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND field='filename_hint'"
        " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if hint:
        lines.append(f"Filename suggests: {hint['value']}")
    header = "\n".join(lines)
    budget = max(500, max_chars - len(header))
    pages = conn.execute("SELECT page, text FROM file_pages WHERE file_id=? ORDER BY page", (file_id,)).fetchall()
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


def detect_ids(conn: sqlite3.Connection, file_id: int) -> dict[str, Any]:
    """Identifiers of *this* document, not of papers it cites.

    Trusted, in order: filename and download URLs; then page 1 (arXiv stamp in
    the margin, DOI/ISBN in the header or footer). Later pages are ignored
    because they are full of references.
    """
    from .enrich import ids as idmod

    names_urls = []
    for r in conn.execute(
        "SELECT c.extra FROM context_refs r JOIN contexts c ON c.id=r.context_id"
        " WHERE r.file_id=? AND c.kind='filesystem'", (file_id,)):
        extra = json.loads(r["extra"] or "{}")
        names_urls += [extra.get("filename") or "", *(extra.get("where_froms") or [])]
    page1 = conn.execute("SELECT text FROM file_pages WHERE file_id=? AND page=1", (file_id,)).fetchone()
    page1 = page1["text"] if page1 else ""

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
    dois = [d for d in meta.dois + p1.dois if not idmod.is_arxiv_doi(d)]
    if dois:
        found["doi"] = dois[0]
    isbns = meta.isbns + p1.isbns
    if isbns:
        found["isbn"] = isbns[0]
        if len(isbns) > 1:
            found["isbns"] = isbns
    return {k: v for k, v in found.items() if v}


def triage_file(conn: sqlite3.Connection, file_id: int, backend: Backend, decider: Decider | None,
                run_id: int | None = None, max_chars: int = STATE_CHARS) -> dict[str, Any]:
    state = build_state(conn, file_id, max_chars=max_chars)
    result: dict[str, Any] = {"file_id": file_id}
    t0 = time.monotonic()
    btag = "llm:" + backend.info().tag
    extracted = backend.extract(instructions=INSTRUCTIONS, prompt=state, schema=EXTRACT_SCHEMA, max_tokens=1500)
    result["extract_s"] = round(time.monotonic() - t0, 2)
    # Abstract: cut the exact span out of the page text using the model's anchors, falling back
    # to an 'Abstract ... Introduction' heuristic. Only for library material, so that personal
    # paperwork never gets copied into the database as an "abstract".
    extracted["abstract"], abstract_source = None, None
    if extracted.get("has_abstract") and extracted.get("is_library_material") is not False:
        pages = "\n".join(r[0] for r in conn.execute(
            "SELECT text FROM file_pages WHERE file_id=? ORDER BY page", (file_id,)))
        text, method = best_abstract(pages, extracted.get("abstract_start"), extracted.get("abstract_end"))
        if text:
            extracted["abstract"] = text
            abstract_source = "heuristic:abstract" if method == "heuristic" else f"{btag}#{method}"
        result["abstract_method"] = method
    if extracted.get("is_library_material") is False:
        extracted["topics"] = []  # don't index personal paperwork by topic
    answers = {}
    if decider is not None:  # outside the write transaction: model calls can take seconds
        t1 = time.monotonic()
        answers = decider.decide(state, DECISIONS)
        result["decide_s"] = round(time.monotonic() - t1, 2)
    with db.tx(conn):
        for field in ["title", "authors", "year", "venue", "summary", "topics", "language",
                      "kind", "is_library_material"]:
            v = extracted.get(field)
            if v not in (None, "", []):
                db.assert_(conn, "file", file_id, field, v, source=btag, run_id=run_id)
        if extracted["abstract"]:
            db.assert_(conn, "file", file_id, "abstract", extracted["abstract"], source=abstract_source,
                       run_id=run_id)
        if answers:
            dtag = "decide:" + decider.info().tag
            for key, ans in answers.items():
                value = ans.best if key == "kind" else ans.best == "yes"
                db.assert_(conn, "file", file_id, key, value, source=dtag, confidence=ans.confidence,
                           run_id=run_id)
                db.assert_(conn, "file", file_id, key + "_probs", ans.probabilities, source=dtag, run_id=run_id)
                result[key] = (value, round(ans.confidence, 3))
        ids = detect_ids(conn, file_id)
        if ids:
            db.assert_(conn, "file", file_id, "ids", ids, source="heuristic:regex", run_id=run_id)
            result["ids"] = ids
        conn.execute("UPDATE files SET triage_status='done' WHERE id=?", (file_id,))
    result["extracted"] = extracted
    return result
