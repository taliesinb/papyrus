"""Full text (`papyrus extract`): every page of the files of live works, page kinds, outlines, OCR.

Scanning reads only the first pages of a file (enough for triage). Once a file belongs to a live
work (candidate or confirmed), this stage reads the rest. Files that are only personal paperwork
are never extracted past those first pages.

Two passes, each idempotent:

1. **Text layer** (pypdfium2): for files with `fulltext_status='pending'`, every page's text, its
   kind (`page_kind`) and the PDF outline (`file_outline`), then `fulltext_status='done'`.
2. **OCR** (Apple Vision, `ocr.py`) for pages with (almost) no text layer:
   * every such page of an extracted file;
   * the first pages of image-only files (`text_status='no_text'`) that are not works yet, so
     that triage can read them. Such a file goes back to `triage_status='pending'` if OCR found
     text, since its earlier triage saw none.

   Results are written in batches, so an interrupted run over a long scanned book keeps what it
   did. OCR'd pages get `source='ocr:vision'` and are never overwritten by the text layer.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Callable

from . import db, store
from .ocr import SOURCE as OCR_SOURCE, VisionOCR
from .pdf import TRIAGE_PAGES, clean_text, read_pdf
from .search import LIVE_FILE

MIN_TEXT = 40  # characters: a page with less has no usable text layer (as in scan)
OCR_WRITE_EVERY = 32  # pages

# ---------------------------------------------------------------------------
# Page kinds
#
# References, index and contents pages match almost any query, so search ranks them lower.
# Calibrated on the library's 81 PDFs (6,509 pages): the outline would be the obvious signal, but
# it was wrong as often as right (a book's "Index" entry spanning its references; "2.1.2
# Bibliography" as a manual's section about bibliographies), so kinds come from the text alone.
# Bibliography-entry density is >= 0.4 on 86% of reference pages and 1% of body pages; the body
# pages above it were mostly reference pages the outline missed.

REF_HEADING = re.compile(
    r"(?im)^\s*(\d+\.?\s+)?(references|bibliography|works cited|literature cited|cited literature)\s*$")
# Features of a bibliography *entry* (not of in-text citations like "(Gu & Dao, 2023;").
CITE_LINE = re.compile(
    r"^\s*(\[\d{1,3}\]|\[[A-Z][A-Za-z+-]{0,10}\d{2}[a-z]?\]|\d{1,3}\.\s+[A-Z][a-z]+,)"  # [12] [Wal00] 12. Smith,
    r"|\(\s*(19|20)\d{2}[a-z]?\)\."  # (2003).
    r"|\b(19|20)\d{2}[a-z]?\.\s*$"  # …, 2015.
    r"|\barXiv:\s?\d|\bdoi\b|\bProc(eedings|\.)\s|\bpp\.\s*\d|\bvol\.\s*\d"
    r"|\d+\s*\(\d+\)\s*:\s*\d+|:\s*\d+[–-]\d+[.,]"  # 4(2):107  :1107–1149.
    r"|\b\d+\s*[–-]\s*\d+\.\s*$",  # …, 2, 293–323.
    re.I)
INDEX_LINE = re.compile(r",\s*\d{1,4}([–-]\d{1,4})?\s*$")  # "step-size parameter, 10, 31–33"
CONTENTS_LINE = re.compile(r"(\.\s*){3,}\s*\d{1,4}\s*$|\s(\d{1,4}|[ivxlc]{1,6})\s*$")  # dot leaders / page no.


def _density(lines: list[str], rx: re.Pattern) -> float:
    return sum(1 for ln in lines if rx.search(ln)) / len(lines)


def page_kind(text: str, page: int, page_count: int) -> str:
    """body | references | index | contents, from the page's text and its position."""
    lines = [ln for ln in text.split("\n") if len(ln.strip()) > 3]
    if len(lines) < 8:
        return "body"
    cite = _density(lines, CITE_LINE)
    if cite >= 0.5 or (cite >= 0.25 and REF_HEADING.search(text)):
        return "references"
    if page_count >= 20:
        if page > 0.8 * page_count and _density(lines, INDEX_LINE) >= 0.5:
            return "index"
        if page <= max(15, page_count // 10) and _density(lines, CONTENTS_LINE) >= 0.6:
            return "contents"
    return "body"


# ---------------------------------------------------------------------------
# The stage


def readable_path(conn: sqlite3.Connection, file_id: int) -> Path | None:
    """papyrus's stored copy, else a path on disk where the file still is."""
    f = conn.execute("SELECT stored_path, size FROM files WHERE id=?", (file_id,)).fetchone()
    if f and store.is_stored(f["stored_path"], f["size"]):
        return store.abs_path(f["stored_path"])
    for (loc,) in conn.execute(
            "SELECT c.locator FROM context_refs r JOIN contexts c ON c.id=r.context_id"
            " WHERE r.file_id=? AND c.kind='filesystem' AND c.missing_at IS NULL", (file_id,)):
        p = Path(loc)
        try:
            if p.stat().st_size == f["size"]:
                return p
        except OSError:
            continue
    return None


def pending_files(conn: sqlite3.Connection, file_ids: tuple[int, ...] = (), redo: bool = False,
                  live_only: bool = False) -> list[int]:
    """Files whose text layer is to be read in full: those of live works, or `file_ids` (which
    needn't belong to a live work, unless `live_only`)."""
    if file_ids:
        q = (f"SELECT id FROM files WHERE id IN ({','.join('?' * len(file_ids))}) AND purged_at IS NULL"
             + ("" if redo else " AND fulltext_status <> 'done'")
             # qualified: a bare `id` inside LIVE_FILE's subquery would be works.id
             + (f" AND {LIVE_FILE.format(f='files.id')}" if live_only else ""))
        return [r[0] for r in conn.execute(q + " ORDER BY id", file_ids)]
    q = ("SELECT f.id FROM files f WHERE f.text_status IN ('ok', 'no_text') AND " + LIVE_FILE.format(f="f.id")
         + ("" if redo else " AND f.fulltext_status = 'pending'") + " ORDER BY f.id")
    return [r[0] for r in conn.execute(q)]


def ocr_targets(conn: sqlite3.Connection, file_ids: tuple[int, ...] = (), live_only: bool = False) -> dict[int, list[int]]:
    """file_id -> pages that have no usable text layer and haven't been OCR'd yet."""
    # Explicit file_ids were extracted on request, so they needn't belong to a live work.
    extracted = "f.fulltext_status = 'done'" + ("" if file_ids and not live_only else f" AND {LIVE_FILE.format(f='f.id')}")
    q = (f"""SELECT p.file_id, p.page FROM file_pages p JOIN files f ON f.id = p.file_id
             WHERE p.source = 'pdfium' AND length(trim(p.text)) < {MIN_TEXT}
               AND (({extracted}) OR (f.text_status = 'no_text' AND p.page <= {TRIAGE_PAGES}))"""
         + (f" AND f.id IN ({','.join('?' * len(file_ids))})" if file_ids else "")
         + " ORDER BY p.file_id, p.page")
    out: dict[int, list[int]] = {}
    for fid, page in conn.execute(q, file_ids):
        out.setdefault(fid, []).append(page)
    return out


def extract_file(conn: sqlite3.Connection, file_id: int, path: Path) -> int:
    """Read every page's text layer and the outline into the database. Returns the page count."""
    info = read_pdf(path, max_pages=None, outline=True)  # outside the transaction
    n = info.page_count
    with db.tx(conn):
        for i, text in enumerate(info.pages, start=1):
            conn.execute(
                """INSERT INTO file_pages(file_id, page, text, kind) VALUES (?,?,?,?)
                   ON CONFLICT(file_id, page) DO UPDATE SET text=excluded.text, kind=excluded.kind
                   WHERE file_pages.source = 'pdfium'
                     AND (file_pages.text IS NOT excluded.text OR file_pages.kind IS NOT excluded.kind)""",
                (file_id, i, text, page_kind(text, i, n)))
        conn.execute("DELETE FROM file_outline WHERE file_id=?", (file_id,))
        conn.executemany("INSERT INTO file_outline(file_id, position, level, title, page) VALUES (?,?,?,?,?)",
                         [(file_id, pos, e.level, e.title, e.page) for pos, e in enumerate(info.outline)])
        conn.execute("UPDATE files SET fulltext_status='done', page_count=?, text_error=NULL WHERE id=?",
                     (n, file_id))
    return n


def ocr_file(conn: sqlite3.Connection, file_id: int, path: Path, pages: list[int], ocr: VisionOCR,
             progress: Callable[[int, int], None] | None = None) -> int:
    """OCR `pages` and store what Vision read. Returns the number of pages that got text."""
    n = conn.execute("SELECT page_count FROM files WHERE id=?", (file_id,)).fetchone()[0] or len(pages)
    got = 0
    for i in range(0, len(pages), OCR_WRITE_EVERY):
        batch = pages[i:i + OCR_WRITE_EVERY]
        texts = {p: clean_text(t) for p, t in ocr.pages(path, batch).items()}  # outside the transaction
        with db.tx(conn):
            for p, t in texts.items():  # pages that failed are not in `texts` and are retried next run
                conn.execute("UPDATE file_pages SET text=?, source=?, kind=? WHERE file_id=? AND page=?",
                             (t, OCR_SOURCE, page_kind(t, p, n), file_id, p))
                got += len(t) >= MIN_TEXT
        if progress:
            progress(min(i + OCR_WRITE_EVERY, len(pages)), len(pages))
    return got


def run(conn: sqlite3.Connection, file_ids: tuple[int, ...] = (), *, redo: bool = False, ocr: bool = True,
        progress: Callable[[str, int, str], None] | None = None) -> Counter:
    """Both passes over whatever is pending (or over `file_ids`), as one recorded run."""
    stats: Counter = Counter()
    with db.recorded_run(conn, "extract", {"files": list(file_ids), "redo": redo, "ocr": ocr}) as (_run_id, rs):
        try:
            process(conn, file_ids, redo=redo, ocr=ocr, progress=progress, stats=stats)
        finally:
            rs.update(stats)
    return stats


def process(conn: sqlite3.Connection, file_ids: tuple[int, ...] = (), *, redo: bool = False, ocr: bool = True,
            progress: Callable[[str, int, str], None] | None = None, stats: Counter | None = None,
            live_only: bool = False) -> Counter:
    """Both passes, inside a run the caller keeps. `live_only`: of `file_ids`, read past the first
    pages only those of live works (`papyrus watch` passes files it hasn't vetted)."""
    say = progress or (lambda state, fid, detail: None)
    stats = Counter() if stats is None else stats
    for fid in pending_files(conn, file_ids, redo, live_only):
        path = readable_path(conn, fid)
        if path is None:
            conn.execute("UPDATE files SET fulltext_status='error', text_error=? WHERE id=?",
                         ("no readable copy (not in the store, and gone from disk)", fid))
            stats["error"] += 1
            say("error", fid, "no readable copy")
            continue
        try:
            n = extract_file(conn, fid, path)
        except Exception as e:  # pdfium raises PdfiumError on broken/encrypted files
            conn.execute("UPDATE files SET fulltext_status='error', text_error=? WHERE id=?",
                         (f"{type(e).__name__}: {e}"[:500], fid))
            stats["error"] += 1
            say("error", fid, str(e)[:200])
            continue
        stats["files"] += 1
        stats["pages"] += n
        say("text", fid, f"{n} pages")

    targets = ocr_targets(conn, file_ids, live_only) if ocr else {}
    if targets:
        engine = VisionOCR()
        if not engine.available():
            stats["ocr_skipped_pages"] = sum(map(len, targets.values()))
            say("no-ocr", 0, engine.reason)
            targets = {}
        try:
            for fid, pages in targets.items():
                path = readable_path(conn, fid)
                if path is None:
                    continue
                say("ocr", fid, f"{len(pages)} pages")
                got = ocr_file(conn, fid, path, pages, engine,
                               lambda done, total, fid=fid: say("ocr-progress", fid, f"{done}/{total}"))
                stats["ocr_pages"] += len(pages)
                stats["ocr_pages_with_text"] += got
                # An image-only file that never became a work was triaged blind: triage it again.
                if got and not conn.execute("SELECT 1 FROM file_works WHERE file_id=?", (fid,)).fetchone():
                    conn.execute("UPDATE files SET triage_status='pending' WHERE id=? AND text_status='no_text'",
                                 (fid,))
                    stats["retriage"] += 1
        finally:
            engine.close()
    return stats
