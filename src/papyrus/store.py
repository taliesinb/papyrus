"""papyrus's managed file store: a content-addressed copy of every document.

Layout under the library root (`PAPYRUS_HOME`, default the variant's home `~/.papyrus/<variant>`):

    store/ab/abcdef0123….pdf    named by the sha256 of the bytes, sharded by its first two hex digits
    store/tmp/                  snapshots and partial downloads, renamed into place when complete

`files.stored_path` holds the path relative to the library root
(`store/ab/abcdef….pdf`). Stored files are read-only (0444) and never rewritten:
the same name always means the same bytes, so writing one twice is harmless.

Copies are clones where possible: APFS `clonefile(2)` on macOS, a `FICLONE`
reflink on Linux (btrfs, XFS, …). They cost no space while the original is
unchanged, and keep the old bytes if the original is later edited or deleted.
Across volumes, and on filesystems without clones, we fall back to a real copy.
Note that `du`/Finder count a clone at full size.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from . import db, linux, macos
from .pdf import sha256_file

STORE_DIR = "store"
TMP_SUFFIX = ".part"


def root() -> Path:
    return db.library_home() / STORE_DIR


def rel_path(sha: str, suffix: str = ".pdf") -> str:
    return f"{STORE_DIR}/{sha[:2]}/{sha}{suffix}"


def abs_path(rel: str) -> Path:
    return db.library_home() / rel


def is_stored(rel: str | None, size: int | None = None) -> bool:
    if not rel:
        return False
    try:
        st = abs_path(rel).stat()
    except OSError:
        return False
    return size is None or st.st_size == size


def new_tmp() -> Path:
    """A fresh (not yet existing) path in store/tmp, on the same volume as the store."""
    d = root() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}{TMP_SUFFIX}"


def snapshot(src: Path) -> tuple[Path, str]:
    """Clone (or, failing that, copy) `src` into store/tmp. Returns (temp path, 'clone' | 'copy').

    Hashing and reading the snapshot rather than `src` means the stored bytes are exactly
    the hashed bytes, even if `src` changes meanwhile.
    """
    tmp = new_tmp()
    for clone in (macos.clonefile, linux.reflink):  # each raises NotImplementedError on the other system
        try:
            clone(src, tmp)
            return tmp, "clone"
        except (OSError, NotImplementedError):
            tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(src, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, "copy"


def commit(tmp: Path, sha: str, suffix: str = ".pdf") -> tuple[str, bool]:
    """Move a complete temp file to its content address. Returns (relative path, created).

    If the address already holds the file, the temp file is dropped instead.
    """
    rel = rel_path(sha, suffix)
    dst = abs_path(rel)
    if dst.exists():
        tmp.unlink(missing_ok=True)
        return rel, False
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(tmp, 0o444)
    os.replace(tmp, dst)
    return rel, True


def clean_tmp(max_age_s: float = 3600) -> int:
    """Remove leftovers of interrupted runs from store/tmp."""
    d = root() / "tmp"
    n = 0
    if d.is_dir():
        cutoff = time.time() - max_age_s
        for p in d.glob(f"*{TMP_SUFFIX}"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n


def set_stored_path(conn: sqlite3.Connection, file_id: int, rel: str) -> None:
    conn.execute("UPDATE files SET stored_path=? WHERE id=? AND (stored_path IS NULL OR stored_path<>?)",
                 (rel, file_id, rel))


def backfill(conn: sqlite3.Connection, progress: Callable[[str, int, str], None] | None = None) -> Counter:
    """Copy known files that aren't in the store yet, from any live path whose bytes still match."""
    clean_tmp()
    with db.recorded_run(conn, "store", {}) as (_run_id, stats):
        counts: Counter = Counter()
        rows = conn.execute("SELECT id, sha256, size, stored_path FROM files WHERE purged_at IS NULL ORDER BY id"
                            ).fetchall()
        for f in rows:
            if is_stored(f["stored_path"], f["size"]):
                counts["already"] += 1
                continue
            paths = [r["locator"] for r in conn.execute(
                "SELECT c.locator FROM context_refs r JOIN contexts c ON c.id=r.context_id"
                " WHERE r.file_id=? AND c.kind='filesystem' AND c.missing_at IS NULL ORDER BY c.occurred_at",
                (f["id"],))]
            done = None
            for p in paths:
                try:
                    if os.path.getsize(p) != f["size"]:
                        continue
                    tmp, method = snapshot(Path(p))
                except OSError:
                    continue
                if sha256_file(tmp) != f["sha256"]:  # the file at that path has changed since the scan
                    tmp.unlink(missing_ok=True)
                    continue
                rel, _ = commit(tmp, f["sha256"])
                set_stored_path(conn, f["id"], rel)
                done = method
                break
            counts[f"stored_{done}" if done else "no_source"] += 1
            if progress:
                progress(f"stored_{done}" if done else "no_source", f["id"], paths[0] if paths else "")
        stats.update(counts)
    return counts


def check(conn: sqlite3.Connection, progress: Callable[[str, int, str], None] | None = None) -> Counter:
    """Re-hash every stored file and compare with `files.sha256`."""
    counts: Counter = Counter()
    for f in conn.execute("SELECT id, sha256, stored_path FROM files WHERE stored_path IS NOT NULL ORDER BY id"):
        p = abs_path(f["stored_path"])
        if not p.is_file():
            state = "missing"
        else:
            state = "ok" if sha256_file(p) == f["sha256"] else "corrupt"
        counts[state] += 1
        if progress and state != "ok":
            progress(state, f["id"], str(p))
    return counts
