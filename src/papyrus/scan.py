"""Scan directories for PDFs: hash, read first pages, record filesystem contexts.

Idempotent: a path whose (size, mtime) hasn't changed since the last scan is
only touched (last_seen_at); files are deduplicated by sha256; contexts under a
scanned root that no longer exist get `missing_at`.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import db, macos
from .filenames import parse_filename
from .pdf import looks_like_pdf, read_pdf, sha256_file

TRIAGE_PAGES = 2
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".Trash", "Library"}


@dataclass
class ScanOptions:
    recursive: bool = False
    max_depth: int = 4
    follow_symlinks: bool = False


def iter_pdfs(root: Path, opts: ScanOptions) -> Iterable[Path]:
    root = root.expanduser().resolve()
    if root.is_file():
        yield root
        return
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=opts.follow_symlinks):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = sorted(
            d for d in dirnames
            if opts.recursive and depth < opts.max_depth and d not in SKIP_DIRS and not d.startswith(".")
            and not d.endswith((".app", ".photoslibrary", ".bundle"))
        )
        for fn in sorted(filenames):
            if fn.lower().endswith(".pdf") and not fn.startswith("."):
                yield Path(dirpath) / fn


def _upsert_file(conn: sqlite3.Connection, path: Path, sha: str, size: int) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
    if row:
        return row["id"], False
    cur = conn.execute("INSERT INTO files(sha256, size) VALUES (?, ?)", (sha, size))
    return cur.lastrowid, True


def extract_first_pages(conn: sqlite3.Connection, file_id: int, path: Path, pages: int = TRIAGE_PAGES) -> str:
    try:
        info = read_pdf(path, max_pages=pages)
    except Exception as e:  # pdfium raises PdfiumError on broken/encrypted files
        conn.execute("UPDATE files SET text_status='error', text_error=? WHERE id=?", (str(e)[:500], file_id))
        return "error"
    m = info.meta
    has_text = any(len(p) > 40 for p in info.pages)
    conn.execute(
        """UPDATE files SET page_count=?, pdf_title=?, pdf_author=?, pdf_subject=?, pdf_keywords=?,
               pdf_creator=?, pdf_producer=?, pdf_created=?, pdf_modified=?, text_status=?, text_error=NULL
           WHERE id=?""",
        (info.page_count, m["title"], m["author"], m["subject"], m["keywords"], m["creator"],
         m["producer"], m["created"], m["modified"], "ok" if has_text else "no_text", file_id),
    )
    for i, text in enumerate(info.pages, start=1):
        conn.execute(
            "INSERT INTO file_pages(file_id, page, text) VALUES (?,?,?)"
            " ON CONFLICT(file_id, page) DO UPDATE SET text=excluded.text",
            (file_id, i, text),
        )
    return "ok" if has_text else "no_text"


def _record_context(conn: sqlite3.Connection, path: Path, st: os.stat_result, file_id: int,
                    host: str, now: str) -> None:
    froms = macos.where_froms(path)
    quarantine = macos.quarantine(path)
    extra = {
        "filename": path.name,
        "dir": str(path.parent),
        "size": st.st_size,
        "mtime": db.to_iso(st.st_mtime),
        "where_froms": froms,  # [download URL, referrer page] when set by a browser
        "download_agent": (quarantine or {}).get("agent"),
    }
    birth = getattr(st, "st_birthtime", None)
    url = froms[0] if froms else None
    row = conn.execute("SELECT id FROM contexts WHERE kind='filesystem' AND locator=?", (str(path),)).fetchone()
    if row:
        ctx_id = row["id"]
        conn.execute(
            "UPDATE contexts SET last_seen_at=?, missing_at=NULL, extra=?, url=?, occurred_at=? WHERE id=?",
            (now, json.dumps(extra), url, db.to_iso(birth), ctx_id),
        )
        # file content at this path may have changed: repoint the ref
        conn.execute("DELETE FROM context_refs WHERE context_id=? AND (file_id IS NULL OR file_id<>?)",
                     (ctx_id, file_id))
    else:
        ctx_id = conn.execute(
            """INSERT INTO contexts(kind, locator, source, occurred_at, title, url, extra, first_seen_at, last_seen_at)
               VALUES ('filesystem', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(path), host, db.to_iso(birth), path.name, url, json.dumps(extra), now, now),
        ).lastrowid
    if not conn.execute("SELECT 1 FROM context_refs WHERE context_id=? AND file_id=?", (ctx_id, file_id)).fetchone():
        conn.execute(
            "INSERT INTO context_refs(context_id, url, file_id, status, confidence, source)"
            " VALUES (?,?,?, 'resolved', 1.0, 'scan')",
            (ctx_id, url, file_id),
        )


def scan(conn: sqlite3.Connection, roots: list[Path], opts: ScanOptions | None = None,
         progress: Callable[[str, Path], None] | None = None) -> Counter:
    opts = opts or ScanOptions()
    host = socket.gethostname()
    stats: Counter = Counter()
    run_id = db.start_run(conn, "scan", {"roots": [str(r) for r in roots], "recursive": opts.recursive})
    for root in roots:
        root = root.expanduser().resolve()
        now = db.now_iso()
        for path in iter_pdfs(root, opts):
            stats["seen"] += 1
            try:
                st = path.stat()
            except OSError:
                stats["unreadable"] += 1
                continue
            row = conn.execute(
                "SELECT c.id, c.extra, r.file_id FROM contexts c LEFT JOIN context_refs r ON r.context_id=c.id"
                " WHERE c.kind='filesystem' AND c.locator=?", (str(path),),
            ).fetchone()
            if row and row["file_id"]:
                extra = json.loads(row["extra"] or "{}")
                if extra.get("size") == st.st_size and extra.get("mtime") == db.to_iso(st.st_mtime):
                    conn.execute("UPDATE contexts SET last_seen_at=?, missing_at=NULL WHERE id=?", (now, row["id"]))
                    stats["unchanged"] += 1
                    continue
            if not looks_like_pdf(path):
                stats["not_pdf"] += 1
                continue
            with db.tx(conn):
                sha = sha256_file(path)
                file_id, new = _upsert_file(conn, path, sha, st.st_size)
                stats["new_files" if new else "known_files"] += 1
                if new:
                    stats["text_" + extract_first_pages(conn, file_id, path)] += 1
                    hint = parse_filename(path.stem)
                    if hint:
                        db.assert_(conn, "file", file_id, "filename_hint",
                                   {"title": hint.title, "surnames": hint.surnames, "year": hint.year},
                                   source="heuristic:filename", run_id=run_id)
                _record_context(conn, path, st, file_id, host, now)
            if progress:
                progress("new" if new else "dup", path)
        # anything under this root we didn't see this time has gone missing
        prefix = str(root).rstrip("/") + "/"
        cur = conn.execute(
            "UPDATE contexts SET missing_at=? WHERE kind='filesystem' AND missing_at IS NULL"
            " AND last_seen_at < ? AND (locator = ? OR substr(locator, 1, ?) = ?)"
            + ("" if opts.recursive else " AND instr(substr(locator, ?), '/') = 0"),
            (now, now, str(root), len(prefix), prefix) + (() if opts.recursive else (len(prefix) + 1,)),
        )
        stats["went_missing"] += cur.rowcount
    db.finish_run(conn, run_id, dict(stats))
    return stats
