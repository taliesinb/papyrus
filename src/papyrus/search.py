"""Search over work metadata (works_fts) and page text (file_pages_fts), ranked per work.

Used by `papyrus search` and the MCP server; web/src/lib/server/query.ts implements the same
ranking in TypeScript (keep the two in step).

* **Pages:** bm25 per page, times a weight for the page's kind (`KIND_WEIGHT`: references,
  index and contents pages match almost anything). A file scores its best three pages with
  weights 1, 1/2, 1/4, so one strong page beats many weak ones, while breadth still counts.
* **Works:** Reciprocal Rank Fusion (k=60) of the metadata ranking and the page ranking.
  bm25 values from two indexes aren't on one scale; ranks are.
* **Privacy:** only files of live works (candidate or confirmed) are searched, unless asked
  otherwise. Personal paperwork has its first pages in file_pages (the classifier needs them) and
  must not turn up in search results.

Gotchas:

* FTS5's bm25() and snippet() can't be used in an aggregate query, not even inside a subquery,
  because SQLite flattens the subquery into it. The `WITH h AS MATERIALIZED (...)` CTE computes
  them first.
* `MATCH ? AND rowid IN (...)` makes FTS5 run the whole MATCH once per listed rowid. For a
  prefix like "t"* (thousands of terms, ~9 ms each time) 180 snippets took 1.7 s. `+rowid`
  keeps the constraint away from FTS5: one MATCH, filtered afterwards, 24 ms.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

KIND_WEIGHT = {"body": 1.0, "references": 0.2, "index": 0.1, "contents": 0.1}
TOP_PAGE_WEIGHTS = (1.0, 0.5, 0.25)
RRF_K = 60
META_WEIGHTS = "10.0, 4.0, 2.0, 3.0"  # bm25 weights of works_fts: title, authors, abstract, topics
LIVE = "('candidate', 'confirmed')"
LIVE_FILE = ("EXISTS (SELECT 1 FROM file_works fw JOIN works w ON w.id = fw.work_id"
             f" WHERE fw.file_id = {{f}} AND w.status IN {LIVE})")


def fts_query(q: str, prefix_last: bool = False) -> str:
    """Free text as a safe FTS5 query: every word must match (the last one as a prefix if asked)."""
    tokens = re.findall(r"[^\W_]+", q)
    return " ".join(f'"{t}"' + ("*" if prefix_last and i == len(tokens) - 1 else "") for i, t in enumerate(tokens))


def looks_advanced(q: str) -> bool:
    """Queries that look like deliberate FTS5 syntax are tried verbatim first."""
    return bool(re.search(r'["*:()^]|\b(AND|OR|NOT|NEAR)\b', q))


def _run(conn: sqlite3.Connection, sql: str, q: str, params: tuple = (), prefix_last: bool = False) -> list:
    """Run an FTS query whose MATCH argument comes first; FTS5 syntax errors fall back to the
    tokenised form of `q`."""
    attempts = [q, fts_query(q, prefix_last)] if looks_advanced(q) else [fts_query(q, prefix_last)]
    for a in attempts:
        if not a:
            continue
        try:
            return conn.execute(sql, (a, *params)).fetchall()
        except sqlite3.OperationalError as e:
            if "fts5" not in str(e) and "syntax" not in str(e) and "no such column" not in str(e):
                raise
    return []


def _kind_case(col: str) -> str:
    return "CASE " + col + "".join(f" WHEN '{k}' THEN {w}" for k, w in KIND_WEIGHT.items()) + " ELSE 1.0 END"


def page_sql(live_only: bool) -> str:
    """Ranked files for a MATCH (param 1); params 2, 3 = pages kept per file, files returned."""
    weights = " ".join(f"WHEN {i + 1} THEN {w}" for i, w in enumerate(TOP_PAGE_WEIGHTS))
    return f"""
    WITH h AS MATERIALIZED (
        SELECT rowid AS id, bm25(file_pages_fts) AS r FROM file_pages_fts WHERE file_pages_fts MATCH ?),
    s AS (SELECT p.id, p.file_id, p.page, p.kind, -h.r * ({_kind_case('p.kind')}) AS score
          FROM h JOIN file_pages p ON p.id = h.id
          {'WHERE ' + LIVE_FILE.format(f='p.file_id') if live_only else ''}),
    k AS (SELECT *, row_number() OVER (PARTITION BY file_id ORDER BY score DESC, page) AS n FROM s)
    SELECT file_id, sum(score * (CASE n {weights} ELSE 0 END)) AS score, count(*) AS pages,
           json_group_array(json_object('id', id, 'page', page, 'kind', kind)) FILTER (WHERE n <= ?) AS top
    FROM k GROUP BY file_id ORDER BY score DESC LIMIT ?"""


@dataclass
class PageHit:
    file_id: int
    page: int
    kind: str
    section: str | None = None
    snippet: str = ""


@dataclass
class FileHit:
    file_id: int
    score: float
    pages: int  # matching pages in all
    hits: list[PageHit] = field(default_factory=list)  # the best few


def rank_files(conn: sqlite3.Connection, q: str, *, live_only: bool = True, limit: int = 1000,
               per_file: int = 3) -> list[FileHit]:
    """Files ranked by their matching pages, best pages first (without snippets)."""
    out = []
    for r in _run(conn, page_sql(live_only), q, (per_file, limit)):
        top = sorted(json.loads(r["top"]), key=lambda h: h["page"])
        out.append(FileHit(r["file_id"], r["score"], r["pages"],
                           [PageHit(r["file_id"], h["page"], h["kind"]) for h in top]))
    return out


def section(conn: sqlite3.Connection, file_id: int, page: int) -> str | None:
    """The outline entry a page belongs to: the last one starting on or before it."""
    r = conn.execute("SELECT title FROM file_outline WHERE file_id=? AND page<=? ORDER BY page DESC, position DESC"
                     " LIMIT 1", (file_id, page)).fetchone()
    return r[0] if r else None


def fill_snippets(conn: sqlite3.Connection, q: str, hits: list[PageHit], marks: tuple[str, str] = ("[", "]"),
                  tokens: int = 16) -> None:
    """Snippets and sections for these hits (only those shown: snippets cost a page scan each)."""
    if not hits:
        return
    ids = {r[0]: r[1] for r in conn.execute(
        "SELECT p.id, p.file_id || ':' || p.page FROM file_pages p JOIN json_each(?) j"
        " ON p.file_id = j.value ->> '$[0]' AND p.page = j.value ->> '$[1]'",
        (json.dumps([[h.file_id, h.page] for h in hits]),))}
    snips = {ids[r[0]]: r[1] for r in _run(
        conn, "SELECT rowid, snippet(file_pages_fts, 0, ?2, ?3, '…', ?4) FROM file_pages_fts"
              " WHERE file_pages_fts MATCH ?1 AND +rowid IN (SELECT value FROM json_each(?5))",  # see below
        q, (marks[0], marks[1], tokens, json.dumps(list(ids))))}
    adjacent = re.compile(re.escape(marks[1]) + r"(\s+)" + re.escape(marks[0]))  # "[Monte] [Carlo]" -> "[Monte Carlo]"
    for h in hits:
        h.snippet = adjacent.sub(r"\1", " ".join((snips.get(f"{h.file_id}:{h.page}") or "").split()))
        h.section = section(conn, h.file_id, h.page)


def _works_of_files(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for fid, wid in conn.execute(
            f"SELECT fw.file_id, fw.work_id FROM file_works fw JOIN works w ON w.id = fw.work_id"
            f" WHERE w.status IN {LIVE} AND fw.file_id IN (SELECT value FROM json_each(?))", (json.dumps(file_ids),)):
        out.setdefault(fid, []).append(wid)
    return out


def search_works(conn: sqlite3.Connection, q: str, *, limit: int = 20, kind: str | None = None,
                 snippets: bool = True, marks: tuple[str, str] = ("[", "]")) -> list[dict[str, Any]]:
    """Live works matching `q` in their metadata or their pages, best first (RRF of the two)."""
    meta = _run(conn, f"""SELECT w.id FROM works_fts JOIN works w ON w.id = works_fts.rowid
                          WHERE works_fts MATCH ? AND w.status IN {LIVE}{' AND w.kind = ?' if kind else ''}
                          ORDER BY bm25(works_fts, {META_WEIGHTS}) LIMIT 1000""", q, (kind,) if kind else ())
    fused: dict[int, float] = {}
    for rank, r in enumerate(meta, 1):
        fused[r[0]] = 1 / (RRF_K + rank)
    files = rank_files(conn, q)
    owners = _works_of_files(conn, [f.file_id for f in files])
    page_hits: dict[int, FileHit] = {}
    rank = 0
    for f in files:
        for wid in owners.get(f.file_id, []):
            if wid in page_hits:  # a second file of the same work: its best file already counted
                continue
            rank += 1
            page_hits[wid] = f
            fused[wid] = fused.get(wid, 0) + 1 / (RRF_K + rank)
    ids = sorted(fused, key=lambda w: -fused[w])
    rows = {r["id"]: r for r in conn.execute(
        """SELECT w.id, w.kind, w.year, w.title,
                  (SELECT group_concat(raw_name, ', ') FROM (SELECT raw_name FROM work_authors
                     WHERE work_id = w.id ORDER BY position LIMIT 4)) AS authors
           FROM works w WHERE w.id IN (SELECT value FROM json_each(?))""" + (" AND w.kind = ?" if kind else ""),
        (json.dumps(ids), kind) if kind else (json.dumps(ids),))}
    out = []
    for wid in ids:
        if wid not in rows:  # filtered out by kind
            continue
        f = page_hits.get(wid)
        out.append({**dict(rows[wid]), "score": round(fused[wid], 5),
                    "matching_pages": f.pages if f else 0, "page_hits": f.hits if f else []})
        if len(out) >= limit:
            break
    if snippets:
        fill_snippets(conn, q, [h for w in out for h in w["page_hits"]], marks)
    return out
