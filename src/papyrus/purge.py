"""Purge: erase what papyrus knows about a document that is not library material.

Scanning and triage keep, for every PDF, its first pages, paths, download URLs, PDF metadata,
a stored copy and the model's claims (title, "authors", a summary). For a payslip, a diary or
a CV none of that belongs in the library, so once triage is sure a file is personal paperwork,
`promote` purges it:

* **kept** (a tombstone): the `files` row with `sha256`, `size` and `purged_at`, so the same
  bytes are recognised and skipped wherever they turn up again without being triaged again; one
  `purged` assertion saying why (`{by, p_personal, prompt_version}`); download contexts keep
  their URL (the link given to `papyrus download`, and `where_froms`); the API log is untouched.
* **erased**: page text and its full-text index entries, the outline, PDF metadata, page
  count, every claim about the file, filesystem contexts (path, filename, dates), download
  contexts' titles and filenames, the stored copy, cached page thumbnails, and the inference
  cache's input and output files of every model call about it (`inference.Store.forget_subject`;
  the call rows, hashes and timings, stay). A work whose files are all purged is rejected and
  stripped to its released ids.

Deleted rows are erased, not just unlinked: the purging connection turns on
`PRAGMA secure_delete` (freed pages are zeroed), the full-text index's `secure-delete` option
(entries are removed rather than masked by delete markers), and the WAL is checkpointed
afterwards. (Dated `.backup` copies of the database are not touched.)

Purging is the one exception to "never delete a stored file" and "never delete assertions".
It can't be undone from the database; the original file, which papyrus never touches, can be
brought back with `papyrus keep PATH`.

When: `verdict` says purge when the user said so (an `is_library_material` claim from `user`),
or when the classifier's P(kind=personal) >= PURGE_THRESHOLD. Between 0.5 (where `works.is_library`
stops promoting) and the threshold, or without a classifier, a personal-looking file is *held*:
no work, not served, its first pages kept for review (`papyrus purge --held`). So is a file
the screen (screen.py) set aside: the screen alone never purges.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import db, inference, store
from .search import LIVE_FILE

PURGE_THRESHOLD = 0.9


def is_purged(conn: sqlite3.Connection, sha: str) -> int | None:
    """The id of the purged file with these bytes, if any."""
    r = conn.execute("SELECT id FROM files WHERE sha256=? AND purged_at IS NOT NULL", (sha,)).fetchone()
    return r[0] if r else None


def user_says(conn: sqlite3.Connection, file_id: int) -> bool | None:
    """The user's own is_library_material claim for this file, if any."""
    r = conn.execute("SELECT value FROM assertions WHERE subject_type='file' AND subject_id=? AND"
                     " field='is_library_material' AND source='user' AND superseded_at IS NULL"
                     " ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    return None if r is None else json.loads(r[0]) is not False


def verdict(conn: sqlite3.Connection, file_id: int) -> dict[str, Any] | None:
    """Why this triaged file should be purged, or None (keep it, or hold it for review)."""
    if conn.execute("SELECT 1 FROM file_works fw JOIN works w ON w.id=fw.work_id"
                    " WHERE fw.file_id=? AND w.status='confirmed'", (file_id,)).fetchone():
        return None  # the user confirmed its work
    user = user_says(conn, file_id)
    if user is not None:
        return None if user else {"by": "user"}
    r = conn.execute(
        "SELECT a.value, a.source, r.prompt_version FROM assertions a LEFT JOIN runs r ON r.id=a.run_id"
        " WHERE a.subject_type='file' AND a.subject_id=? AND a.field='kind_probs' AND a.source LIKE 'classify:%'"
        " AND a.superseded_at IS NULL ORDER BY a.id DESC LIMIT 1", (file_id,)).fetchone()
    if r is None:
        return None  # no classifier: the extraction model's own yes/no is too unreliable to act on
    p = (json.loads(r["value"]) or {}).get("personal", 0.0)
    if p < PURGE_THRESHOLD:
        return None
    return {"by": r["source"], "p_personal": round(p, 4), "prompt_version": r["prompt_version"]}


def thumbs_dir() -> Path:
    """Where the web app caches page thumbnails (web/src/lib/server/files.ts, cacheDir)."""
    return Path(os.environ.get("PAPYRUS_WEB_CACHE") or Path.home() / ".cache" / "papyrus-web") / "thumbs"


def _strip_work(conn: sqlite3.Connection, work_id: int) -> None:
    """Reject a work whose files were all purged, keeping only its row and its released ids."""
    from .works import release_ids

    conn.execute("UPDATE works SET status='rejected', title=NULL, subtitle=NULL, abstract=NULL, year=NULL,"
                 " date=NULL, venue=NULL, language=NULL, updated_at=? WHERE id=?", (db.now_iso(), work_id))
    release_ids(conn, work_id)  # public identifiers (often a cited paper's), kept so lookups stay right
    conn.execute("DELETE FROM work_authors WHERE work_id=?", (work_id,))
    conn.execute("DELETE FROM work_topics WHERE work_id=?", (work_id,))
    conn.execute("DELETE FROM works_fts WHERE rowid=?", (work_id,))
    conn.execute("DELETE FROM assertions WHERE subject_type='work' AND subject_id=? AND field<>'released_ids'",
                 (work_id,))
    conn.execute("UPDATE context_refs SET work_id=NULL WHERE work_id=?", (work_id,))


def _drop_orphan_topics(conn: sqlite3.Connection) -> None:
    """Model-made topics (scheme llm) no work uses any more, e.g. 'payroll deductions'."""
    for _ in range(2):  # topics, then the fields they hung from
        conn.execute("DELETE FROM topics WHERE scheme='llm' AND id NOT IN (SELECT topic_id FROM work_topics)"
                     " AND id NOT IN (SELECT parent_id FROM topics WHERE parent_id IS NOT NULL)")


def purge_file(conn: sqlite3.Connection, file_id: int, why: dict[str, Any], run_id: int | None = None) -> bool:
    """Erase everything about a file but its tombstone (see the module docstring). Returns False
    if it was already purged. Call outside a transaction: this opens its own."""
    f = conn.execute("SELECT sha256, stored_path, purged_at FROM files WHERE id=?", (file_id,)).fetchone()
    if f is None or f["purged_at"]:
        return False
    conn.execute("PRAGMA secure_delete = ON")  # zero freed pages (this connection, from now on)
    with db.tx(conn):
        conn.execute("INSERT INTO file_pages_fts(file_pages_fts, rank) VALUES ('secure-delete', 1)")
        stripped = False
        works = [r[0] for r in conn.execute("SELECT work_id FROM file_works WHERE file_id=?", (file_id,))]
        conn.execute("DELETE FROM file_works WHERE file_id=?", (file_id,))
        for wid in works:
            if not conn.execute("SELECT 1 FROM file_works WHERE work_id=?", (wid,)).fetchone():
                _strip_work(conn, wid)
                stripped = True
        conn.execute("DELETE FROM file_pages WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM file_outline WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM assertions WHERE subject_type='file' AND subject_id=?", (file_id,))
        for c in conn.execute("SELECT DISTINCT c.id, c.kind, c.extra FROM context_refs r"
                              " JOIN contexts c ON c.id=r.context_id WHERE r.file_id=?", (file_id,)).fetchall():
            if c["kind"] == "filesystem":  # path, filename, dates: all of it goes (refs cascade)
                conn.execute("DELETE FROM contexts WHERE id=?", (c["id"],))
            elif c["kind"] == "download":  # the URL stays, so `papyrus download` knows it
                froms = json.loads(c["extra"] or "{}").get("where_froms") or []
                conn.execute("UPDATE contexts SET title=NULL, text=NULL, occurred_at=NULL, extra=? WHERE id=?",
                             (json.dumps({"where_froms": froms, "purged": True}), c["id"]))
            # other contexts (a Slack message linking the file) are about the message: left as they are
        conn.execute(
            "UPDATE files SET page_count=NULL, pdf_title=NULL, pdf_author=NULL, pdf_subject=NULL, pdf_keywords=NULL,"
            " pdf_creator=NULL, pdf_producer=NULL, pdf_created=NULL, pdf_modified=NULL, text_error=NULL,"
            " stored_path=NULL, purged_at=? WHERE id=?", (db.now_iso(), file_id))
        if stripped:
            _drop_orphan_topics(conn)
        db.assert_(conn, "file", file_id, "purged", why, source="papyrus:purge", run_id=run_id)
    # the bytes, once the database no longer points at them
    rel = f["stored_path"] or store.rel_path(f["sha256"])
    store.abs_path(rel).unlink(missing_ok=True)
    # what the models were asked about it and answered (inference.py): the files go, the rows
    # (hashes, timings) stay
    inference.store().forget_subject(f["sha256"])
    for p in thumbs_dir().glob(f"{f['sha256'][:32]}-p*-w*.png"):
        p.unlink(missing_ok=True)
    return True


def checkpoint(conn: sqlite3.Connection) -> None:
    """Move purged pages out of the WAL into the (zeroed) database file, and truncate the WAL."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def keep(conn: sqlite3.Connection, file_id: int, run_id: int | None = None) -> tuple[str, int | None]:
    """The user says this (unpurged) file is library material: record it, then promote it. A file
    with no title yet was never given metadata: one the screen set aside goes back to triage
    (which skips the screen for it from now on), one the classifier judged personal goes on to
    metadata extraction (triage_status `classified`). Action `retriage` either way."""
    from .screen import rejected
    from .works import promote_file

    claims = db.current_assertions(conn, "file", file_id)
    retriage = not claims.get("title")
    status = "pending" if rejected(claims) is not None else "classified"
    with db.tx(conn):
        db.assert_(conn, "file", file_id, "is_library_material", True, source="user", run_id=run_id)
        if retriage:
            conn.execute("UPDATE files SET triage_status=? WHERE id=?", (status, file_id))
    if retriage:
        return "requeued", None
    return promote_file(conn, file_id, run_id=run_id)


def restore(conn: sqlite3.Connection, path: Path, run_id: int | None = None) -> tuple[str, int | None]:
    """Bring back a purged file from its original at `path`, as the user's library material:
    the stored copy, first pages and the path come back, triage is pending again (a `user`
    claim makes promote keep it whatever the classifier says). Returns (action, file_id), action
    one of restored | unknown (never scanned) | or what `keep` returns for an unpurged file."""
    import socket

    from . import scan
    from .pdf import sha256_file

    path = path.expanduser().resolve()
    st = path.stat()
    tmp, _ = store.snapshot(path)
    sha = sha256_file(tmp)
    r = conn.execute("SELECT id, purged_at FROM files WHERE sha256=?", (sha,)).fetchone()
    if r is None or not r["purged_at"]:
        tmp.unlink(missing_ok=True)
        return ("unknown", None) if r is None else keep(conn, r["id"], run_id)
    fid = r["id"]
    rel, _ = store.commit(tmp, sha)
    with db.tx(conn):
        conn.execute("UPDATE files SET purged_at=NULL, triage_status='pending', fulltext_status='pending' WHERE id=?",
                     (fid,))
        store.set_stored_path(conn, fid, rel)
        scan.first_read(conn, fid, store.abs_path(rel), path.name, run_id)
        scan._record_context(conn, path, st, fid, socket.gethostname(), db.now_iso())
        conn.execute("UPDATE assertions SET superseded_at=? WHERE subject_type='file' AND subject_id=?"
                     " AND field='purged' AND superseded_at IS NULL", (db.now_iso(), fid))
        db.assert_(conn, "file", fid, "is_library_material", True, source="user", run_id=run_id)
    return "restored", fid


def candidates(conn: sqlite3.Connection, held: bool = False) -> list[tuple[int, dict[str, Any] | None]]:
    """Triaged, unpurged files that are not library material: (file_id, verdict). With `held`,
    also those below the threshold (verdict None), for review."""
    from .works import is_library

    out = []
    for (fid,) in conn.execute("SELECT id FROM files WHERE triage_status='done' AND purged_at IS NULL ORDER BY id"
                               ).fetchall():
        why = verdict(conn, fid)
        if why or (held and not is_library(db.current_assertions(conn, "file", fid))
                   and not conn.execute(f"SELECT 1 WHERE {LIVE_FILE.format(f='?')}", (fid,)).fetchone()):
            out.append((fid, why))
    return out
