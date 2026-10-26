"""Turn triaged files into works: dedupe by identifiers / title, link files, index.

Resolution order for a field: user > api:* > decide:* > llm:* > heuristic:*.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any

from . import db

SOURCE_RANK = [("user", 0), ("api:", 1), ("decide:", 2), ("llm:", 3), ("heuristic:", 4), ("pdf:", 5)]


def _rank(source: str) -> int:
    return next((r for p, r in SOURCE_RANK if source.startswith(p)), 9)


def best(claims: dict[str, list[dict]], field: str) -> Any:
    xs = claims.get(field) or []
    if not xs:
        return None
    return sorted(xs, key=lambda c: (_rank(c["source"]), -(c["confidence"] or 0)))[0]["value"]


def norm_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def find_work(conn: sqlite3.Connection, ids: dict[str, str], title: str | None, year: int | None) -> int | None:
    for scheme, value in ids.items():
        if value:
            r = conn.execute("SELECT wi.work_id FROM work_ids wi JOIN works w ON w.id=wi.work_id"
                             " WHERE wi.scheme=? AND wi.value=? AND w.status NOT IN ('rejected', 'merged')",
                             (scheme, value)).fetchone()
            if r:
                return r[0]
    nt = norm_title(title)
    if len(nt) >= 12:
        for r in conn.execute("SELECT id, title, year FROM works WHERE status NOT IN ('merged', 'rejected')"):
            if norm_title(r["title"]) == nt and (year is None or r["year"] is None or abs(r["year"] - year) <= 1):
                return r["id"]
    return None


def topic_id(conn: sqlite3.Connection, scheme: str, name: str, level: str, parent: int | None) -> int:
    r = conn.execute("SELECT id FROM topics WHERE scheme=? AND name=? AND parent_id IS ?",
                     (scheme, name, parent)).fetchone()
    if r:
        return r[0]
    return conn.execute("INSERT INTO topics(scheme, name, level, parent_id) VALUES (?,?,?,?)",
                        (scheme, name, level, parent)).lastrowid


def reindex_work(conn: sqlite3.Connection, work_id: int) -> None:
    w = conn.execute("SELECT title, subtitle, abstract FROM works WHERE id=?", (work_id,)).fetchone()
    authors = " ; ".join(r[0] for r in conn.execute(
        "SELECT raw_name FROM work_authors WHERE work_id=? ORDER BY role, position", (work_id,)))
    topics = " ; ".join(r[0] for r in conn.execute(
        "SELECT t.name FROM work_topics wt JOIN topics t ON t.id=wt.topic_id WHERE wt.work_id=?", (work_id,)))
    conn.execute("DELETE FROM works_fts WHERE rowid=?", (work_id,))
    conn.execute("INSERT INTO works_fts(rowid, title, authors, abstract, topics) VALUES (?,?,?,?,?)",
                 (work_id, " ".join(x for x in [w["title"], w["subtitle"]] if x), authors, w["abstract"] or "",
                  topics))


def is_library(claims: dict[str, list[dict]]) -> bool:
    """Library material vs personal paperwork.

    The decider's *kind* distribution is well calibrated, but its yes/no 'is this library
    material' answer is biased towards yes (0.7 for an exam paper it calls personal with p=1.0).
    So: P(kind=personal) from the decider when present, else the extraction model's flag.
    """
    for c in claims.get("kind_probs") or []:
        if c["source"].startswith("decide:") and isinstance(c["value"], dict):
            return c["value"].get("personal", 0.0) < 0.5
    llm = [c["value"] for c in claims.get("is_library_material") or [] if c["source"].startswith("llm:")]
    return llm[0] is not False if llm else best(claims, "is_library_material") is not False


def reject_work(conn: sqlite3.Connection, work_id: int) -> int:
    """Mark a candidate work rejected and release its identifiers (recorded as an assertion),
    so that a real document with the same arXiv id / DOI can claim them later."""
    n = conn.execute("UPDATE works SET status='rejected', updated_at=? WHERE id=? AND status='candidate'",
                     (db.now_iso(), work_id)).rowcount
    release_ids(conn, work_id)
    return n


def release_ids(conn: sqlite3.Connection, work_id: int) -> None:
    ids = {r[0]: r[1] for r in conn.execute("SELECT scheme, value FROM work_ids WHERE work_id=?", (work_id,))}
    if ids:
        db.assert_(conn, "work", work_id, "released_ids", ids, source="papyrus:reject", supersede=False)
        conn.execute("DELETE FROM work_ids WHERE work_id=?", (work_id,))


def promote_file(conn: sqlite3.Connection, file_id: int) -> tuple[str, int | None]:
    """Create or link a work for a triaged file. Returns (action, work_id)."""
    claims = db.current_assertions(conn, "file", file_id)
    linked = conn.execute("SELECT work_id FROM file_works WHERE file_id=?", (file_id,)).fetchall()
    if linked:
        if not is_library(claims):
            # created by an earlier, laxer rule: reject (never delete) works whose files are all personal
            n = 0
            for (wid,) in linked:
                others = [r[0] for r in conn.execute(
                    "SELECT file_id FROM file_works WHERE work_id=? AND file_id<>?", (wid, file_id))]
                if all(not is_library(db.current_assertions(conn, "file", f)) for f in others):
                    n += reject_work(conn, wid)
            return ("rejected" if n else "already_linked"), None
        return "already_linked", None
    if not is_library(claims):
        return "not_library", None
    title = best(claims, "title")
    if not title:
        return "no_title", None
    ids_claim = best(claims, "ids") or {}
    ids = {k: v for k, v in ids_claim.items() if k in {"arxiv", "doi", "isbn"} and isinstance(v, str) and v}
    year = best(claims, "year")
    kind = best(claims, "kind")
    work_id = find_work(conn, ids, title, year)
    action = "linked"
    with db.tx(conn):
        if work_id is None:
            action = "created"
            work_id = conn.execute(
                "INSERT INTO works(kind, title, abstract, year, venue, language) VALUES (?,?,?,?,?,?)",
                (kind, title, best(claims, "abstract"), year, best(claims, "venue"), best(claims, "language")),
            ).lastrowid
            for pos, name in enumerate(best(claims, "authors") or []):
                conn.execute("INSERT INTO work_authors(work_id, position, raw_name, source) VALUES (?,?,?,?)",
                             (work_id, pos, name, "triage"))
            for t in best(claims, "topics") or []:
                field = (t.get("field") or "").strip().lower() or None
                parent = topic_id(conn, "llm", field, "field", None) if field else None
                tid = topic_id(conn, "llm", t["name"].strip().lower(), "topic", parent)
                conn.execute("INSERT OR IGNORE INTO work_topics(work_id, topic_id, source) VALUES (?,?,'triage')",
                             (work_id, tid))
            summary = best(claims, "summary")
            if summary:
                db.assert_(conn, "work", work_id, "summary", summary, source="triage")
        for scheme, value in ids.items():
            conn.execute("INSERT OR IGNORE INTO work_ids(work_id, scheme, value, source) VALUES (?,?,?,?)",
                         (work_id, scheme, value, "heuristic:regex"))
        relation = "preprint" if "arxiv" in ids and kind == "paper" else "full"
        conn.execute("INSERT INTO file_works(file_id, work_id, relation, version, confidence, source)"
                     " VALUES (?,?,?,?,?,?)",
                     (file_id, work_id, relation, ids_claim.get("arxiv_version"), None, "promote"))
        # contexts that point at this file now also point at the work
        conn.execute("UPDATE context_refs SET work_id=? WHERE file_id=? AND work_id IS NULL", (work_id, file_id))
        reindex_work(conn, work_id)
    return action, work_id


def promote_all(conn: sqlite3.Connection) -> dict[str, int]:
    stats: dict[str, int] = {}
    for (fid,) in conn.execute("SELECT id FROM files WHERE triage_status='done' ORDER BY id").fetchall():
        action, _ = promote_file(conn, fid)
        stats[action] = stats.get(action, 0) + 1
    return stats


def work_json(conn: sqlite3.Connection, work_id: int) -> dict[str, Any]:
    w = dict(conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone())
    w["ids"] = {r["scheme"]: r["value"] for r in conn.execute("SELECT scheme, value FROM work_ids WHERE work_id=?",
                                                              (work_id,))}
    w["authors"] = [dict(r) for r in conn.execute(
        "SELECT wa.raw_name, wa.role, p.orcid, p.openalex_id FROM work_authors wa LEFT JOIN people p"
        " ON p.id=wa.person_id WHERE wa.work_id=? ORDER BY wa.role, wa.position", (work_id,))]
    w["topics"] = [f"{r['field']} › {r['name']}" if r["field"] else r["name"] for r in conn.execute(
        "SELECT t.name, p.name AS field FROM work_topics wt JOIN topics t ON t.id=wt.topic_id"
        " LEFT JOIN topics p ON p.id=t.parent_id WHERE wt.work_id=?", (work_id,))]
    w["files"] = [dict(r) for r in conn.execute(
        "SELECT fw.file_id, fw.relation, fw.version, f.page_count FROM file_works fw JOIN files f ON f.id=fw.file_id"
        " WHERE fw.work_id=?", (work_id,))]
    w["contexts"] = [
        {k: (json.loads(r[k]) if k == "extra" and r[k] else r[k]) for k in r.keys() if r[k] is not None}
        for r in conn.execute(
            "SELECT DISTINCT c.kind, c.locator, c.occurred_at, c.actor, c.url, c.text, c.missing_at"
            " FROM context_refs r JOIN contexts c ON c.id=r.context_id"
            " WHERE r.work_id=? OR r.file_id IN (SELECT file_id FROM file_works WHERE work_id=?)"
            " ORDER BY c.occurred_at", (work_id, work_id))
    ]
    return {k: v for k, v in w.items() if v not in (None, [], {})}
