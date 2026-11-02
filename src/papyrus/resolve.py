"""Resolve `context_refs` (links found in Slack messages and other contexts) to works.

Today: links that carry an identifier, i.e. arXiv ids and DOIs (`contexts.links.classify`).
For each distinct identifier:

1. a live work already has it → link the refs to that work;
2. else look it up (`match.candidates.resolve`, network, outside any transaction). Nothing
   good enough → the refs stay ``unresolved`` and the next run tries again;
3. a live work already has one of the record's other ids, or its title and year (the
   published DOI of an arXiv link, say) → link to that work;
4. else create a ``candidate`` work holding the identifier and match it
   (`match.pipeline.match_work`, which repeats the lookup from the web services store's answers), so its
   title, authors, abstract and topics come with API provenance.

Refs become ``resolved`` with ``work_id``, ``confidence`` 1.0 (identifier match) and
``source`` ``resolve:<scheme>``. PDF links and uploads are left for later (they need a
download and ingesting).
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from . import db, works
from .contexts import links as linkmod
from .match import candidates
from .match.pipeline import THRESHOLD, match_work

ID_SOURCE = "heuristic:link"


def identifier(url: str | None) -> tuple[str, str] | None:
    """(scheme, value) for a link that names a work: ('arxiv', '2401.01234') or ('doi', ...)."""
    c = linkmod.classify(url or "", ())
    if c and c.kind in ("arxiv", "doi") and c.id:
        return c.kind, c.id
    return None


def pending(conn: sqlite3.Connection, context_kind: str | None = None) -> dict[tuple[str, str], list[int]]:
    """Unresolved refs with an identifier, grouped by identifier (oldest context first)."""
    q = ("SELECT r.id, r.url FROM context_refs r JOIN contexts c ON c.id=r.context_id"
         " WHERE r.status='unresolved' AND r.url IS NOT NULL" + (" AND c.kind=?" if context_kind else "")
         + " ORDER BY c.occurred_at, r.id")
    out: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in conn.execute(q, (context_kind,) if context_kind else ()):
        ident = identifier(r["url"])
        if ident:
            out[ident].append(r["id"])
    return dict(out)


def _link(conn: sqlite3.Connection, ref_ids: list[int], work_id: int, scheme: str) -> None:
    conn.execute(f"UPDATE context_refs SET work_id=?, status='resolved', confidence=1.0, source=?"
                 f" WHERE id IN ({','.join('?' * len(ref_ids))})", (work_id, f"resolve:{scheme}", *ref_ids))


def resolve_one(conn: sqlite3.Connection, scheme: str, value: str, ref_ids: list[int], *,
                run_id: int | None = None, **resolve_kw: Any) -> dict[str, Any]:
    wid = works.find_work(conn, {scheme: value}, None, None)
    if wid:
        with db.tx(conn):
            _link(conn, ref_ids, wid, scheme)
        return {"status": "linked", "work_id": wid}
    records = candidates.resolve(candidates.Hint(**{"arxiv_id" if scheme == "arxiv" else "doi": value}), **resolve_kw)
    good = [r for r in records if (r.match_score or 0) >= THRESHOLD]
    if not good:
        return {"status": "not_found", "best_score": records[0].match_score if records else None}
    rec = candidates.merge(candidates.cluster(good)[0])
    other_ids = {k: v for k, v in rec.ids.items() if k in ("arxiv", "doi", "isbn") and v}
    wid = works.find_work(conn, other_ids, rec.title, rec.year)
    if wid:
        with db.tx(conn):
            _link(conn, ref_ids, wid, scheme)
        return {"status": "linked_other_id", "work_id": wid, "title": rec.title}
    with db.tx(conn):
        wid = conn.execute("INSERT INTO works(status) VALUES ('candidate')").lastrowid
        conn.execute("INSERT INTO work_ids(work_id, scheme, value, source) VALUES (?,?,?,?)",
                     (wid, scheme, value, ID_SOURCE))
        db.assert_(conn, "work", wid, "ids", {scheme: value}, source=ID_SOURCE, confidence=1.0, run_id=run_id)
        _link(conn, ref_ids, wid, scheme)
        works.reindex_work(conn, wid)
    r = match_work(conn, wid, run_id=run_id, **resolve_kw)
    return {"status": "created", "work_id": wid, "match": r["status"], "title": rec.title}


def resolve_refs(conn: sqlite3.Connection, *, context_kind: str | None = None, run_id: int | None = None,
                 limit: int | None = None, progress: Callable[[tuple[str, str], dict], None] | None = None,
                 **resolve_kw: Any) -> Counter:
    stats: Counter = Counter()
    todo = list(pending(conn, context_kind).items())[:limit]
    for (scheme, value), ref_ids in todo:
        try:
            r = resolve_one(conn, scheme, value, ref_ids, run_id=run_id, **resolve_kw)
        except Exception as e:  # one bad record must not stop the rest
            r = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        stats[r["status"]] += 1
        if progress:
            progress((scheme, value), r)
    return stats
