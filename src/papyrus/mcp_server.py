"""MCP server exposing the papyrus library (read-only for now).

Run:  papyrus mcp            (stdio; add to Claude Code / Desktop config)
"""

from __future__ import annotations

import json
import sqlite3
from importlib import resources
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import db
from .search import LIVE_FILE
from .works import work_json

server = MCPServer(
    name="papyrus",
    instructions=(
        "Personal library of papers, books, slides and notes, with where each was found "
        "(disk paths, download URLs; later Slack, Safari Reading List, Twitter). Use search_library "
        "for titles/authors/abstracts/topics, fulltext_search for words anywhere inside the documents "
        "(every page), get_file with include_text to read pages, get_work for a "
        "full record with discovery contexts, and sql for anything else (schema in resource "
        "papyrus://schema; database is SQLite, opened read-only)."
    ),
)


def _conn() -> sqlite3.Connection:
    return db.connect()


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.default_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@server.tool()
def search_library(query: str, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Search works by title, authors, abstract and topics (SQLite FTS5 syntax: words, "phrases",
    OR, prefix*). kind filters by paper|book|chapter|thesis|slides|tutorial|lecture_notes|article|report|manual."""
    conn = _conn()
    sql = """SELECT w.id, w.kind, w.year, w.title,
                    (SELECT group_concat(raw_name, ', ') FROM work_authors WHERE work_id=w.id) AS authors,
                    snippet(works_fts, 2, '[', ']', '…', 20) AS abstract_snippet
             FROM works_fts JOIN works w ON w.id = works_fts.rowid
             WHERE works_fts MATCH ? AND w.status NOT IN ('merged', 'rejected')""" + (" AND w.kind = ?" if kind else "") + \
          " ORDER BY bm25(works_fts) LIMIT ?"
    args = (query, kind, limit) if kind else (query, limit)
    return [dict(r) for r in conn.execute(sql, args)]


@server.tool()
def fulltext_search(query: str, limit: int = 20, pages_per_work: int = 3) -> list[dict[str, Any]]:
    """Search inside the documents: the text of every page of every work (free text, or FTS5
    syntax: "phrases", OR, NOT, prefix*, NEAR(a b, 5)). Returns works ranked by their best
    matching pages, each with how many pages match and the best few (page number, section from
    the PDF's outline, page kind, snippet with [matches]). References, index and contents pages
    rank lower. Read a page in full with get_file(file_id, page_from=..., page_to=...)."""
    from dataclasses import asdict

    from .search import fill_snippets, rank_files

    conn = _conn()
    files = rank_files(conn, query, limit=limit, per_file=pages_per_work)
    fill_snippets(conn, query, [h for f in files for h in f.hits])
    out = []
    for f in files:
        w = conn.execute("SELECT w.id, w.kind, w.year, w.title FROM file_works fw JOIN works w ON w.id=fw.work_id"
                         " WHERE fw.file_id=? AND w.status IN ('candidate', 'confirmed') LIMIT 1",
                         (f.file_id,)).fetchone()
        out.append({"work_id": w["id"], "title": w["title"], "kind": w["kind"], "year": w["year"],
                    "file_id": f.file_id, "matching_pages": f.pages,
                    "hits": [{k: v for k, v in asdict(h).items() if k != "file_id" and v is not None}
                             for h in f.hits]})
    return out


@server.tool()
def get_work(work_id: int) -> dict[str, Any]:
    """Full record of a work: identifiers, authors (with ORCID/OpenAlex ids when known), topics,
    files, and every context in which it was discovered (paths, download URLs, messages...)."""
    conn = _conn()
    w = conn.execute("SELECT status FROM works WHERE id=?", (work_id,)).fetchone()
    if not w or w["status"] == "rejected":  # rejected = not library material (e.g. a CV)
        raise ValueError(f"no work {work_id}")
    return work_json(conn, work_id)


@server.tool()
def get_file(file_id: int, include_text: bool = False, page_from: int = 1, page_to: int | None = None
             ) -> dict[str, Any]:
    """A PDF file: PDF metadata, locations on disk, model/heuristic assertions, linked works;
    with include_text, the text of pages page_from..page_to (default: the first 2; at most 30)."""
    conn = _conn()
    # Only files of live works: the others are personal paperwork the classifier kept out of the library.
    f = conn.execute(f"SELECT * FROM files WHERE id=? AND {LIVE_FILE.format(f='files.id')}", (file_id,)).fetchone()
    if not f:
        raise ValueError(f"no file {file_id}")
    out: dict[str, Any] = {k: f[k] for k in f.keys() if f[k] is not None}
    out["contexts"] = [dict(r) for r in conn.execute(
        "SELECT c.kind, c.locator, c.occurred_at, c.url, c.missing_at FROM context_refs r"
        " JOIN contexts c ON c.id=r.context_id WHERE r.file_id=?", (file_id,))]
    out["assertions"] = db.current_assertions(conn, "file", file_id)
    out["works"] = [r[0] for r in conn.execute("SELECT work_id FROM file_works WHERE file_id=?", (file_id,))]
    if include_text:
        page_to = min(page_to or page_from + 1, page_from + 29)
        out["pages"] = [dict(r) for r in conn.execute(
            "SELECT page, kind, source, text FROM file_pages WHERE file_id=? AND page BETWEEN ? AND ?"
            " ORDER BY page", (file_id, page_from, page_to))]
        out["outline"] = [dict(r) for r in conn.execute(
            "SELECT level, title, page FROM file_outline WHERE file_id=? ORDER BY position LIMIT 300", (file_id,))]
    return out


@server.tool()
def recent_discoveries(limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
    """Most recently encountered documents (by context time: download time, message time...)."""
    conn = _conn()
    # Only refs to live works and their files (or to nothing yet, e.g. a Slack link being resolved):
    # paths and URLs of personal paperwork stay out.
    live = (f"(r.file_id IS NULL OR {LIVE_FILE.format(f='r.file_id')})"
            " AND (r.work_id IS NULL OR r.work_id IN (SELECT id FROM works WHERE status IN ('candidate', 'confirmed')))")
    rows = conn.execute(
        f"""SELECT c.kind, c.occurred_at, c.locator, c.actor, c.url, r.file_id, r.work_id,
                  (SELECT title FROM works WHERE id=r.work_id) AS title
           FROM contexts c JOIN context_refs r ON r.context_id=c.id
           WHERE c.missing_at IS NULL AND {live}""" + (" AND c.kind=?" if kind else "") +
        " ORDER BY c.occurred_at DESC LIMIT ?", ((kind, limit) if kind else (limit,)))
    return [dict(r) for r in rows]


@server.tool()
def sql(query: str, limit: int = 200) -> list[dict[str, Any]]:
    """Run a read-only SQL query against the library database (see resource papyrus://schema)."""
    conn = _ro_conn()
    cur = conn.execute(query)
    return [dict(r) for r in cur.fetchmany(limit)]


@server.tool()
def library_stats() -> dict[str, Any]:
    """Counts of files, works, people, contexts; breakdown of works by kind."""
    conn = _conn()
    out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
           for t in ["files", "works", "people", "contexts", "assertions"]}
    out["works_by_kind"] = {r[0] or "unknown": r[1] for r in conn.execute(
        "SELECT kind, count(*) FROM works WHERE status NOT IN ('merged', 'rejected') GROUP BY kind")}
    return out


@server.resource("papyrus://schema")
def schema() -> str:
    """The SQLite DDL of the library database, with comments."""
    return resources.files("papyrus").joinpath("schema.sql").read_text()


def run() -> None:
    server.run("stdio")


if __name__ == "__main__":
    run()
