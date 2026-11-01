"""Stage 4, extract: the bibliographic metadata of a file the classifier kept as library material
(title, authors, year, venue, summary, topics, language, the abstract's span), from the
extraction model (`Backend.extract`). Claims `llm:<backend>:<model>`; the abstract is cut out of
the page text from the model's anchors (textspan.py).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import db
from .classify import KINDS, classified_p_personal, classify_file
from .describe import build_state
from .llm.base import Backend, Classifier
from .pdf import FIRST_PAGES
from .textspan import best_abstract

ANCHOR_WORDS = 8

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


def extract_metadata(conn: sqlite3.Connection, file_id: int, backend: Backend, run_id: int | None = None,
                     max_tokens: int | None = None) -> dict[str, Any]:
    """The extract stage, for files classified as library material: title, authors, year, venue, summary,
    topics, language and the abstract, from the extraction model. Sets extract_status to `done`.

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
            "SELECT text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page", (file_id, FIRST_PAGES)))
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
        conn.execute("UPDATE files SET extract_status='done' WHERE id=?", (file_id,))
    result["extracted"] = extracted
    return result


def classify_and_extract(conn: sqlite3.Connection, file_id: int, backend: Backend, classifier: Classifier | None,
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
