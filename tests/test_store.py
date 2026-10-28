import os
import stat
import sys

import pytest

from papyrus import db, macos, store
from papyrus.pdf import sha256_file
from papyrus.scan import ScanOptions, scan

from test_core import _make_pdf


def test_snapshot_commit_layout_and_readonly(tmp_path):
    src = tmp_path / "a.pdf"
    _make_pdf(src, "Hello store")
    tmp, method = store.snapshot(src)
    assert method in ("clone", "copy") and tmp.parent == store.root() / "tmp"
    sha = sha256_file(tmp)
    rel, created = store.commit(tmp, sha)
    assert created and rel == f"store/{sha[:2]}/{sha}.pdf"
    p = store.abs_path(rel)
    assert p.read_bytes() == src.read_bytes() and not tmp.exists()
    assert not (stat.S_IMODE(p.stat().st_mode) & 0o222)  # read-only
    # committing the same bytes again drops the temp file and keeps the stored one
    tmp2, _ = store.snapshot(src)
    assert store.commit(tmp2, sha) == (rel, False) and not tmp2.exists()
    assert store.is_stored(rel, p.stat().st_size) and not store.is_stored(rel, 1)


@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile is macOS-only")
def test_clonefile_is_used_on_apfs(tmp_path):
    src = tmp_path / "a.pdf"
    _make_pdf(src, "Hello clone")
    dst = tmp_path / "b.pdf"
    macos.clonefile(src, dst)
    assert dst.read_bytes() == src.read_bytes()
    with pytest.raises(OSError):
        macos.clonefile(src, dst)  # EEXIST
    _tmp, method = store.snapshot(src)
    assert method == "clone"  # tmp_path and PAPYRUS_HOME are on the same volume


def test_scan_stores_snapshot_and_original_edits_dont_leak(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _make_pdf(lib / "x.pdf", "Original text of x")
    original = (lib / "x.pdf").read_bytes()
    conn = db.connect(tmp_path / "t.sqlite")
    s = scan(conn, [lib], ScanOptions())
    assert s["new_files"] == 1 and s["stored_clone"] + s["stored_copy"] == 1
    f = conn.execute("SELECT sha256, stored_path FROM files").fetchone()
    stored = store.abs_path(f["stored_path"])
    assert sha256_file(stored) == f["sha256"]
    (lib / "x.pdf").write_bytes(original + b"\n% edited\n")  # the user edits the original
    assert stored.read_bytes() == original  # the store keeps the hashed bytes


def test_scan_no_store_then_backfill_and_check(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _make_pdf(lib / "x.pdf", "Backfill me")
    _make_pdf(lib / "y.pdf", "And me too")
    conn = db.connect(tmp_path / "t.sqlite")
    scan(conn, [lib], ScanOptions(store=False))
    assert conn.execute("SELECT count(*) FROM files WHERE stored_path IS NULL").fetchone()[0] == 2
    os.remove(lib / "y.pdf")  # gone before it was stored: nothing to copy from
    s = store.backfill(conn)
    assert s["stored_clone"] + s["stored_copy"] == 1 and s["no_source"] == 1
    assert store.backfill(conn)["already"] == 1
    assert store.check(conn) == {"ok": 1}
    rel = conn.execute("SELECT stored_path FROM files WHERE stored_path IS NOT NULL").fetchone()[0]
    store.abs_path(rel).unlink()
    assert store.check(conn) == {"missing": 1}
    run = conn.execute("SELECT stats FROM runs WHERE command='store' ORDER BY id LIMIT 1").fetchone()[0]
    assert '"status": "completed"' in run
