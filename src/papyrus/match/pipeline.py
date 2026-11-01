"""Enrich a work in the database from external bibliographic APIs.

    match_work(conn, work_id) -> {'status': 'matched' | 'ambiguous' | 'not_found', ...}

Network calls (candidates.resolve) happen before the write transaction. Writes:
* assertions (subject 'work') per contributing record, source 'api:<source>':
  title, abstract, year, venue, authors (names), author_ids, ids, topics, citation_count
* works: abstract (if empty), title (if empty), venue, date, year, kind (if NULL)
* work_ids for every identifier (an id owned by another work becomes a
  'possible_duplicate' assertion instead)
* people upserted by orcid / openalex_id / s2_id / dblp_pid; work_authors.person_id
* topics: 'openalex' tree (domain > field > subfield > topic) and 'arxiv' categories
The S2 SPECTER embedding is not stored yet.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from papyrus import db, works
from papyrus import ids as _ids
from papyrus.match import candidates, semanticscholar
from papyrus.models import AuthorRecord, WorkRecord

THRESHOLD = 0.85
KINDS = {
    "paper",
    "book",
    "chapter",
    "thesis",
    "slides",
    "tutorial",
    "lecture_notes",
    "article",
    "report",
    "manual",
    "other",
}
# identifiers that are not unique to a work (journal ISSNs) are not written to work_ids
SKIP_ID_SCHEMES = {"issn"}
MATCH_SOURCE = "api:match"


def build_hint(conn: sqlite3.Connection, work_id: int) -> candidates.Hint:
    w = conn.execute("SELECT title, year FROM works WHERE id=?", (work_id,)).fetchone()
    if w is None:
        raise KeyError(f"no work {work_id}")
    wids = {
        r["scheme"]: r["value"] for r in conn.execute("SELECT scheme, value FROM work_ids WHERE work_id=?", (work_id,))
    }
    authors = [
        r["raw_name"]
        for r in conn.execute(
            "SELECT raw_name FROM work_authors WHERE work_id=? AND role='author' ORDER BY position", (work_id,)
        )
    ]
    return candidates.Hint(
        arxiv_id=wids.get("arxiv"),
        doi=wids.get("doi"),
        isbn=wids.get("isbn"),
        title=w["title"],
        authors=authors,
        year=w["year"],
    )


def _candidate_summary(r: WorkRecord) -> dict[str, Any]:
    return {
        "source": r.source,
        "score": r.match_score,
        "title": r.title,
        "year": r.year,
        "venue": r.venue,
        "kind": r.kind,
        "ids": r.ids,
        "authors": [a.name for a in r.authors[:8]],
    }


def _author_dict(a: AuthorRecord) -> dict[str, Any]:
    d = {
        "name": a.name,
        "given": a.given,
        "family": a.family,
        "orcid": a.orcid,
        "openalex_id": a.openalex_id,
        "s2_id": a.s2_id,
        "dblp_pid": a.dblp_pid,
        "affiliations": a.affiliations or None,
    }
    return {k: v for k, v in d.items() if v}


def _record_assertions(conn: sqlite3.Connection, work_id: int, records: list[WorkRecord], run_id: int | None) -> None:
    seen_sources: set[str] = set()
    for r in records:
        source = f"api:{r.source}"
        first = source not in seen_sources
        seen_sources.add(source)
        conf = r.match_score
        fields: dict[str, Any] = {
            "title": r.title or None,
            "abstract": r.abstract,
            "year": r.year,
            "venue": r.venue,
            "authors": [a.name for a in r.authors] or None,
            "author_ids": [_author_dict(a) for a in r.authors if a.orcid or a.openalex_id or a.s2_id or a.dblp_pid]
            or None,
            "ids": r.ids or None,
            "topics": r.topics or None,
            "citation_count": r.citation_count,
        }
        for field, value in fields.items():
            if value is None:
                continue
            # several records from one source (OpenAlex preprint + published work) are all kept
            db.assert_(
                conn, "work", work_id, field, value, source=source, confidence=conf, run_id=run_id, supersede=first
            )


def _id_source(records: list[WorkRecord], scheme: str, value: str) -> str:
    for r in records:
        if r.ids.get(scheme) == value:
            return f"api:{r.source}"
    return MATCH_SOURCE


def _write_ids(
    conn: sqlite3.Connection, work_id: int, merged: WorkRecord, records: list[WorkRecord], run_id: int | None
) -> list[dict[str, Any]]:
    pairs: list[tuple[str, str]] = list(merged.ids.items())
    for scheme, alts in (merged.raw.get("id_conflicts") or {}).items():
        pairs += [(scheme, v) for v in alts]
    dups = []
    for scheme, value in pairs:
        if scheme in SKIP_ID_SCHEMES or not value:
            continue
        row = conn.execute("SELECT work_id FROM work_ids WHERE scheme=? AND value=?", (scheme, value)).fetchone()
        if row and row[0] != work_id:
            dup = {"other_work_id": row[0], "scheme": scheme, "value": value}
            dups.append(dup)
            db.assert_(
                conn, "work", work_id, "possible_duplicate", dup, source=MATCH_SOURCE, run_id=run_id, supersede=False
            )
            continue
        conn.execute(
            "INSERT OR IGNORE INTO work_ids(work_id, scheme, value, source) VALUES (?,?,?,?)",
            (work_id, scheme, value, _id_source(records, scheme, value)),
        )
    return dups


_PERSON_KEYS = ("orcid", "openalex_id", "s2_id", "dblp_pid")


def _upsert_person(conn: sqlite3.Connection, a: AuthorRecord) -> int | None:
    vals = {k: getattr(a, k) for k in _PERSON_KEYS if getattr(a, k)}
    if not vals:
        return None  # cannot disambiguate a bare name
    pid = None
    for k, v in vals.items():
        row = conn.execute(f"SELECT id FROM people WHERE {k}=?", (v,)).fetchone()
        if row:
            pid = row[0]
            break
    if pid is None:
        cols = ["display_name", "given_name", "family_name", *vals]
        pid = conn.execute(
            f"INSERT INTO people({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (a.name, a.given, a.family, *vals.values()),
        ).lastrowid
        return pid
    for k, v in vals.items():
        owner = conn.execute(f"SELECT id FROM people WHERE {k}=?", (v,)).fetchone()
        if owner is None:
            conn.execute(f"UPDATE people SET {k}=? WHERE id=? AND {k} IS NULL", (v, pid))
    conn.execute(
        "UPDATE people SET given_name=COALESCE(given_name, ?), family_name=COALESCE(family_name, ?) WHERE id=?",
        (a.given, a.family, pid),
    )
    return pid


def _same_author(raw_name: str, a: AuthorRecord) -> bool:
    s1 = _ids.surname_key(raw_name)
    s2 = _ids.surname_key(a.family, is_family=True) if a.family else _ids.surname_key(a.name)
    if s1 and s2 and candidates._same_surname(s1, s2):
        return True
    # raw names sometimes arrive as 'Dao T.' or in caps; compare against every word
    words = set(_ids.normalize_title(raw_name).split())
    return bool(s2) and s2 in words


def _link_authors(conn: sqlite3.Connection, work_id: int, merged: WorkRecord) -> int:
    rows = conn.execute(
        "SELECT position, raw_name, person_id, affiliation FROM work_authors WHERE work_id=? AND role='author'"
        " ORDER BY position",
        (work_id,),
    ).fetchall()
    authors = merged.authors
    cache: dict[int, int | None] = {}

    def person(j: int) -> int | None:  # upsert lazily so unmatched authors create no people
        if j not in cache:
            cache[j] = _upsert_person(conn, authors[j])
        return cache[j]

    linked = 0
    if not rows:
        for pos, a in enumerate(authors):
            pid = person(pos)
            conn.execute(
                "INSERT INTO work_authors(work_id, position, raw_name, person_id, role, affiliation, source)"
                " VALUES (?,?,?,?, 'author', ?, ?)",
                (work_id, pos, a.name, pid, (a.affiliations or [None])[0], MATCH_SOURCE),
            )
            linked += pid is not None
        return linked
    used: set[int] = set()
    assigned: dict[int, int] = {}  # row index -> author index
    for i, r in enumerate(rows):
        j = next((j for j, a in enumerate(authors) if j not in used and _same_author(r["raw_name"], a)), None)
        if j is None and len(rows) == len(authors) and i not in used and not _ids.surname_key(r["raw_name"]):
            j = i  # unreadable raw name: fall back to position
        if j is not None:
            used.add(j)
            assigned[i] = j
    for i, j in assigned.items():
        r, a, pid = rows[i], authors[j], person(j)
        conn.execute(
            "UPDATE work_authors SET person_id=COALESCE(?, person_id), affiliation=COALESCE(affiliation, ?)"
            " WHERE work_id=? AND role='author' AND position=?",
            (pid, (a.affiliations or [None])[0], work_id, r["position"]),
        )
        linked += pid is not None
    # triage often truncates long author lists: append the rest when all existing rows matched
    if len(assigned) == len(rows) and len(authors) > len(rows):
        pos = max(r["position"] for r in rows) + 1
        for j, a in enumerate(authors):
            if j in used:
                continue
            conn.execute(
                "INSERT INTO work_authors(work_id, position, raw_name, person_id, role, affiliation, source)"
                " VALUES (?,?,?,?, 'author', ?, ?)",
                (work_id, pos, a.name, person(j), (a.affiliations or [None])[0], MATCH_SOURCE),
            )
            linked += cache[j] is not None
            pos += 1
    return linked


def _topic(conn: sqlite3.Connection, scheme: str, name: str, level: str, parent: int | None, ext: str | None) -> int:
    tid = works.topic_id(conn, scheme, name, level, parent)
    if ext:
        conn.execute("UPDATE topics SET external_id=? WHERE id=? AND external_id IS NULL", (ext, tid))
    return tid


def _write_topics(conn: sqlite3.Connection, work_id: int, merged: WorkRecord) -> int:
    n = 0
    for t in merged.topics:
        if t.get("scheme") != "openalex" or not t.get("name"):
            continue
        parent = None
        for level in ("domain", "field", "subfield"):
            if t.get(level):
                parent = _topic(conn, "openalex", t[level], level, parent, None)
        tid = _topic(conn, "openalex", t["name"], "topic", parent, t.get("id"))
        conn.execute(
            "INSERT OR IGNORE INTO work_topics(work_id, topic_id, score, source) VALUES (?,?,?, 'api:openalex')",
            (work_id, tid, t.get("score")),
        )
        n += 1
    for i, cat in enumerate(merged.arxiv_categories):
        archive = cat.split(".", 1)[0] if "." in cat else None
        parent = _topic(conn, "arxiv", archive, "field", None, archive) if archive else None
        tid = _topic(conn, "arxiv", cat, "topic", parent, cat)
        conn.execute(
            "INSERT OR IGNORE INTO work_topics(work_id, topic_id, score, source) VALUES (?,?,?, 'api:arxiv')",
            (work_id, tid, 1.0 if i == 0 else 0.5),
        )
        n += 1
    return n


def _update_work(conn: sqlite3.Connection, work_id: int, merged: WorkRecord) -> None:
    kind = merged.kind if merged.kind in KINDS else ("other" if merged.kind else None)
    date = merged.date
    if not date and merged.year and str(merged.raw.get("first_seen") or "").startswith(str(merged.year)):
        date = merged.raw["first_seen"]
    conn.execute(
        """UPDATE works SET
             title    = CASE WHEN title IS NULL OR title = '' THEN ? ELSE title END,
             abstract = CASE WHEN abstract IS NULL OR abstract = '' THEN ? ELSE abstract END,
             venue    = COALESCE(?, venue),
             date     = COALESCE(?, date),
             year     = COALESCE(?, year),
             kind     = COALESCE(kind, ?),
             updated_at = ?
           WHERE id = ?""",
        (merged.title or None, merged.abstract, merged.venue, date, merged.year, kind, db.now_iso(), work_id),
    )


_STRONG_IDS = ("arxiv", "doi", "openalex", "s2", "isbn")


def _conflict(conn: sqlite3.Connection, work_id: int, hint: candidates.Hint, rec: WorkRecord) -> str | None:
    """Why `rec` cannot be the same work as `work_id`, or None."""
    if hint.arxiv_id and rec.ids.get("arxiv") and rec.ids["arxiv"] != hint.arxiv_id:
        return f"different arXiv id {rec.ids['arxiv']} (work has {hint.arxiv_id})"
    for scheme in _STRONG_IDS:
        value = rec.ids.get(scheme)
        if not value:
            continue
        row = conn.execute(
            "SELECT wi.work_id FROM work_ids wi JOIN works w ON w.id = wi.work_id"
            " WHERE wi.scheme=? AND wi.value=? AND wi.work_id<>? AND w.status NOT IN ('rejected', 'merged')",
            (scheme, value, work_id)).fetchone()
        if row:
            return f"{scheme}:{value} belongs to work {row[0]}"
    return None


def reset_match(conn: sqlite3.Connection, work_id: int) -> None:
    """Undo what enrichment wrote for a work, keeping triage data and the claims as history."""
    now = db.now_iso()
    with db.tx(conn):
        conn.execute("UPDATE assertions SET superseded_at=? WHERE subject_type='work' AND subject_id=?"
                     " AND superseded_at IS NULL AND (source LIKE 'api:%')", (now, work_id))
        conn.execute("DELETE FROM work_authors WHERE work_id=? AND source=?", (work_id, MATCH_SOURCE))
        conn.execute("UPDATE work_authors SET person_id=NULL WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM work_ids WHERE work_id=? AND source LIKE 'api:%'", (work_id,))
        conn.execute("DELETE FROM work_topics WHERE work_id=? AND source<>'triage'", (work_id,))
        conn.execute("UPDATE works SET venue=NULL, date=NULL, updated_at=? WHERE id=?", (now, work_id))
        works.reindex_work(conn, work_id)


def unmatched_works(conn: sqlite3.Connection, file_ids: tuple[int, ...] = ()) -> list[int]:
    """Live works with no API claims or candidates yet; only those of `file_ids` if given."""
    q = ("SELECT DISTINCT w.id FROM works w" + (" JOIN file_works fw ON fw.work_id=w.id" if file_ids else "")
         + " WHERE w.status IN ('candidate','confirmed')"
         + (f" AND fw.file_id IN ({','.join('?' * len(file_ids))})" if file_ids else "")
         + " AND NOT EXISTS (SELECT 1 FROM assertions a WHERE a.subject_type='work' AND a.subject_id=w.id"
           " AND (a.source LIKE 'api:%' OR a.field='match_candidates')) ORDER BY w.id")
    return [r[0] for r in conn.execute(q, file_ids)]


def match_work(
    conn: sqlite3.Connection,
    work_id: int,
    *,
    threshold: float = THRESHOLD,
    run_id: int | None = None,
    **resolve_kw: Any,
) -> dict[str, Any]:
    """Resolve work `work_id` against external APIs and write the results back."""
    hint = build_hint(conn, work_id)
    if not (hint.arxiv_id or hint.doi or hint.isbn or hint.title):
        return {"status": "not_found", "work_id": work_id, "reason": "no identifiers or title"}
    report: dict[str, Any] = {}
    records = candidates.resolve(hint, report=report, **resolve_kw)
    good = [r for r in records if (r.match_score or 0) >= threshold]
    # Nothing matched while a source we rely on refused us: that is "not asked", not "not found".
    # Record no verdict (the work stays pending); try again later. Semantic Scholar without a key
    # refuses almost always, so it doesn't count.
    limited = {s: w for s, w in (report.get("rate_limited") or {}).items()
               if s != "s2" or semanticscholar.api_key()}
    if not good and limited:
        return {"status": "rate_limited", "work_id": work_id, "sources": sorted(limited),
                "retry_after": max(limited.values())}
    # A candidate that is demonstrably a *different* work must not be merged in, however well
    # its title matches (e.g. a thesis vs. the journal paper with the same title prefix).
    excluded = [(r, why) for r in good if (why := _conflict(conn, work_id, hint, r))]
    if excluded:
        good = [r for r in good if all(r is not x for x, _ in excluded)]
        with db.tx(conn):
            for r, why in excluded:
                db.assert_(conn, "work", work_id, "excluded_candidate",
                           {"source": r.source, "title": r.title, "ids": r.ids, "reason": why},
                           source=MATCH_SOURCE, confidence=r.match_score, run_id=run_id, supersede=False)
    if not good:
        cands = [_candidate_summary(r) for r in records[:10]]
        with db.tx(conn):
            db.assert_(
                conn,
                "work",
                work_id,
                "match_candidates",
                cands,
                source=MATCH_SOURCE,
                run_id=run_id,
                confidence=records[0].match_score if records else None,
            )
        return {
            "status": "ambiguous" if records else "not_found",
            "work_id": work_id,
            "best_score": records[0].match_score if records else None,
            "candidates": len(cands),
        }
    group = candidates.cluster(good)[0]
    merged = candidates.merge(group)
    with db.tx(conn):
        _record_assertions(conn, work_id, group, run_id)
        _update_work(conn, work_id, merged)
        dups = _write_ids(conn, work_id, merged, group, run_id)
        linked = _link_authors(conn, work_id, merged)
        n_topics = _write_topics(conn, work_id, merged)
        works.reindex_work(conn, work_id)
    return {
        "status": "matched",
        "work_id": work_id,
        "score": merged.match_score,
        "sources": sorted({r.source for r in group}),
        "ids": merged.ids,
        "authors_linked": linked,
        "topics": n_topics,
        "possible_duplicates": dups,
    }
