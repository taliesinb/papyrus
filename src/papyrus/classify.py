"""Stage 3, classify: what kind of document a file is, and so whether it is library material.

First the screen (screen.py: an embedding model, ~30 ms), which sets obvious personal paperwork
aside; then the classifier (llm/classify.py: probabilities over KINDS). Library material
(P(kind=personal) < 0.5, or the user's `keep`) goes on to extraction (extract.py); anything else is
done, and promote purges or holds it under its file name: personal paperwork gets no metadata.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import db
from .describe import build_state, detect_ids
from .llm.base import Classifier, Question

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

# One question: library-ness is P(kind=personal) (works.is_library). The yes/no "is this library
# material?" question was dropped in triage-6: biased towards yes, and nothing read its answer.
CLASSIFICATIONS = [
    Question("kind", "choice", "What kind of document is this?", KINDS),
]


def classify_file(conn: sqlite3.Connection, file_id: int, classifier: Classifier | None,
                  run_id: int | None = None, max_tokens: int | None = None, screen: Any = None) -> dict[str, Any]:
    """Triage's first stage: the screen, then the classifier (kind, library or not).

    Sets classify_status to `done`, and extract_status to `pending` when the file goes on to
    metadata extraction (library material, or no classifier to say otherwise; result status
    `classified`), else to `skipped` (result status `done`): set aside by the screen, or
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
        conn.execute("UPDATE files SET classify_status='done', extract_status=? WHERE id=?",
                     ("pending" if status == "classified" else "skipped", file_id))
    result["status"], result["p_personal"], result["library"] = status, p_personal, library
    return result


def classified_p_personal(conn: sqlite3.Connection, file_id: int) -> float | None:
    """P(kind=personal) from the file's current classifier claim, if it has one."""
    r = conn.execute("SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND field='kind_probs'"
                     " AND source LIKE 'classify:%' AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
                     (file_id,)).fetchone()
    return None if r is None else float(json.loads(r[0]).get("personal", 0.0))
