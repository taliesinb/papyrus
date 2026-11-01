"""Scan directories for PDFs: hash, read first pages, record filesystem contexts.

Idempotent: a path whose (size, mtime) hasn't changed since the last scan is
only touched (last_seen_at); files are deduplicated by sha256; contexts under a
scanned root that no longer exist get `missing_at`. Bytes that were purged as
personal paperwork (`purge.py`) are skipped without recording anything, not even
the path; they have no context, so they are hashed again on each scan.

New or changed files are snapshotted into the managed store (`store.py`, an
APFS clone where possible) and hashed from the snapshot.
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

from . import db, linux, macos, purge, store
from .filenames import parse_filename
from .index import page_kind
from .pdf import FIRST_PAGES, looks_like_pdf, read_pdf, sha256_file

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".Trash", "Library"}


@dataclass
class ScanOptions:
    recursive: bool = False
    max_depth: int = 4
    follow_symlinks: bool = False
    store: bool = True  # keep a copy in the managed store


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


def _upsert_file(conn: sqlite3.Connection, sha: str, size: int) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
    if row:
        return row["id"], False
    cur = conn.execute("INSERT INTO files(sha256, size) VALUES (?, ?)", (sha, size))
    return cur.lastrowid, True


def add_file(conn: sqlite3.Connection, path: Path, sha: str, size: int, *, name: str | None,
             stored_rel: str | None, run_id: int | None, stats: Counter) -> tuple[int, bool]:
    """The `files` row for these bytes (call inside a transaction). A new file gets its first
    pages read from `path` and a filename hint from `name`. Returns (file_id, new)."""
    file_id, new = _upsert_file(conn, sha, size)
    stats["new_files" if new else "known_files"] += 1
    if new:
        stats["text_" + first_read(conn, file_id, path, name, run_id)] += 1
    if stored_rel:
        store.set_stored_path(conn, file_id, stored_rel)
    return file_id, new


def first_read(conn: sqlite3.Connection, file_id: int, path: Path, name: str | None, run_id: int | None) -> str:
    """What scanning learns about new bytes: the first pages, PDF info and a filename hint."""
    status = read_first_pages(conn, file_id, path)
    hint = parse_filename(Path(name).stem) if name else None
    if hint:
        db.assert_(conn, "file", file_id, "filename_hint",
                   {"title": hint.title, "surnames": hint.surnames, "year": hint.year},
                   source="heuristic:filename", run_id=run_id)
    return status


def read_first_pages(conn: sqlite3.Connection, file_id: int, path: Path, pages: int = FIRST_PAGES) -> str:
    try:
        info = read_pdf(path, max_pages=pages)
    except Exception as e:  # pdfium raises PdfiumError on broken/encrypted files
        conn.execute("UPDATE files SET read_status='error', read_error=? WHERE id=?", (str(e)[:500], file_id))
        return "error"
    m = info.meta
    has_text = any(len(p) > 40 for p in info.pages)
    conn.execute(
        """UPDATE files SET page_count=?, pdf_title=?, pdf_author=?, pdf_subject=?, pdf_keywords=?,
               pdf_creator=?, pdf_producer=?, pdf_created=?, pdf_modified=?, read_status=?, read_error=NULL
           WHERE id=?""",
        (info.page_count, m["title"], m["author"], m["subject"], m["keywords"], m["creator"],
         m["producer"], m["created"], m["modified"], "ok" if has_text else "no_text", file_id),
    )
    for i, text in enumerate(info.pages, start=1):
        conn.execute(
            "INSERT INTO file_pages(file_id, page, text, kind) VALUES (?,?,?,?)"
            " ON CONFLICT(file_id, page) DO UPDATE SET text=excluded.text, kind=excluded.kind"
            " WHERE file_pages.source = 'pdfium'",
            (file_id, i, text, page_kind(text, i, info.page_count)),
        )
    return "ok" if has_text else "no_text"


def _record_context(conn: sqlite3.Connection, path: Path, st: os.stat_result, file_id: int,
                    host: str, now: str) -> None:
    froms = macos.where_froms(path) or linux.where_froms(path)
    quarantine = macos.quarantine(path)
    extra = {
        "filename": path.name,
        "dir": str(path.parent),
        "size": st.st_size,
        "mtime": db.to_iso(st.st_mtime),
        "where_froms": froms,  # [download URL, referrer page] when set by a browser
        "download_agent": (quarantine or {}).get("agent"),
    }
    # Linux's stat has no birth time (before os.statx, Python 3.15); a download's mtime is when it
    # was written, which is what occurred_at stands for here.
    birth = getattr(st, "st_birthtime", None) or st.st_mtime
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


def scan_path(conn: sqlite3.Connection, path: Path, opts: ScanOptions, *, host: str, now: str,
                run_id: int | None, stats: Counter) -> tuple[str, int | None]:
    """Scan one PDF path: what `scan` does for each file it finds (also `papyrus watch`'s way in).

    Returns (outcome, file_id); outcome is one of unreadable | unchanged | not_pdf |
    purged_skipped | new | dup. A path whose (size, mtime) match its filesystem context is only
    touched. Otherwise the bytes are snapshotted (opts.store), hashed, skipped if purged, and
    recorded with their first pages and the path's context."""
    stats["seen"] += 1
    try:
        st = path.stat()
    except OSError:
        stats["unreadable"] += 1
        return "unreadable", None
    row = conn.execute(
        "SELECT c.id, c.extra, r.file_id FROM contexts c LEFT JOIN context_refs r ON r.context_id=c.id"
        " WHERE c.kind='filesystem' AND c.locator=?", (str(path),),
    ).fetchone()
    if row and row["file_id"]:
        extra = json.loads(row["extra"] or "{}")
        if extra.get("size") == st.st_size and extra.get("mtime") == db.to_iso(st.st_mtime):
            conn.execute("UPDATE contexts SET last_seen_at=?, missing_at=NULL WHERE id=?", (now, row["id"]))
            stats["unchanged"] += 1
            return "unchanged", row["file_id"]
    try:
        if not looks_like_pdf(path):
            stats["not_pdf"] += 1
            return "not_pdf", None
    except OSError:
        stats["unreadable"] += 1
        return "unreadable", None
    # hash (and store) a snapshot, so the stored bytes are exactly the hashed ones
    rel, src, size = None, path, st.st_size
    try:
        if opts.store:
            tmp, method = store.snapshot(path)
            sha = sha256_file(tmp)
            if purge.is_purged(conn, sha):  # personal paperwork seen before: record nothing
                tmp.unlink(missing_ok=True)
                stats["purged_skipped"] += 1
                return "purged_skipped", None
            rel, created = store.commit(tmp, sha)
            src, size = store.abs_path(rel), store.abs_path(rel).stat().st_size
            if created:
                stats[f"stored_{method}"] += 1
        else:
            sha = sha256_file(path)
            if purge.is_purged(conn, sha):
                stats["purged_skipped"] += 1
                return "purged_skipped", None
    except OSError:
        stats["unreadable"] += 1
        return "unreadable", None
    with db.tx(conn):
        file_id, new = add_file(conn, src, sha, size, name=path.name, stored_rel=rel,
                                run_id=run_id, stats=stats)
        _record_context(conn, path, st, file_id, host, now)
    return ("new" if new else "dup"), file_id


def mark_missing(conn: sqlite3.Connection, root: Path, now: str, stats: Counter) -> None:
    """Known paths under `root` that no longer exist have gone missing (checked on disk, so a
    shallow scan doesn't flag deeper files it didn't visit)."""
    prefix = str(root).rstrip("/") + "/"
    for r in conn.execute(
        "SELECT id, locator FROM contexts WHERE kind='filesystem' AND missing_at IS NULL"
        " AND (locator = ? OR substr(locator, 1, ?) = ?)", (str(root), len(prefix), prefix),
    ).fetchall():
        if not os.path.exists(r["locator"]):
            conn.execute("UPDATE contexts SET missing_at=? WHERE id=?", (now, r["id"]))
            stats["went_missing"] += 1


def scan_roots(conn: sqlite3.Connection, roots: list[Path], opts: ScanOptions, *, run_id: int | None,
               stats: Counter, progress: Callable[[str, Path], None] | None = None) -> Counter:
    """The work of `scan`, inside a run the caller keeps (`papyrus watch` scans in its own runs)."""
    if opts.store:
        store.clean_tmp()
    host = socket.gethostname()
    for root in roots:
        root = root.expanduser().resolve()
        now = db.now_iso()
        for path in iter_pdfs(root, opts):
            outcome, _ = scan_path(conn, path, opts, host=host, now=now, run_id=run_id, stats=stats)
            if progress and outcome in ("new", "dup"):
                progress(outcome, path)
        mark_missing(conn, root, now, stats)
    return stats


def scan(conn: sqlite3.Connection, roots: list[Path], opts: ScanOptions | None = None,
         progress: Callable[[str, Path], None] | None = None) -> Counter:
    opts = opts or ScanOptions()
    stats: Counter = Counter()
    run_id = db.start_run(conn, "scan", {"roots": [str(r) for r in roots], "recursive": opts.recursive})
    scan_roots(conn, roots, opts, run_id=run_id, stats=stats, progress=progress)
    db.finish_run(conn, run_id, dict(stats))
    return stats
