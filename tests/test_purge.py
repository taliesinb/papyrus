"""Purging personal paperwork down to a tombstone (purge.py), and keeping it out of the
browser and MCP meanwhile. Synthetic documents only."""

import shutil
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from papyrus import db, mcp_server, purge, store
from papyrus.cli import main
from papyrus.download import Link, download
from papyrus.enrich import http as h
from papyrus.scan import ScanOptions, scan
from papyrus.triage import triage_file
from papyrus.works import promote_all, promote_file

from test_triage_promote import PAPER, FakeBackend, FakeDecider, _lib

MARK = "Quuxperson"  # the employee's surname, which must not survive a purge
SLIP = dict(PAPER, kind="personal", is_library_material=False, title=f"Payslip October {MARK}",
            authors=["ACME Payroll"], summary=f"A payslip for Zebedee {MARK}.", topics=[], has_abstract=False)


def _payslip(tmp_path, p=0.98):
    conn = _lib(tmp_path, "Payslip_2026-10.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1234.00")
    triage_file(conn, 1, FakeBackend(SLIP), FakeDecider("personal", False, p=p))
    return conn


def _bytes_on_disk(dbfile: Path) -> bytes:
    return b"".join(p.read_bytes() for p in [dbfile, Path(f"{dbfile}-wal")] if p.exists())


def test_sure_personal_file_is_purged_to_a_tombstone(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_WEB_CACHE", str(tmp_path / "web-cache"))
    conn = _payslip(tmp_path)
    f = conn.execute("SELECT sha256, size, stored_path FROM files WHERE id=1").fetchone()
    stored = store.abs_path(f["stored_path"])
    thumb = purge.thumbs_dir() / f"{f['sha256'][:32]}-p1-w160.png"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"png")
    assert stored.exists() and MARK.encode() in _bytes_on_disk(tmp_path / "t.sqlite")

    assert promote_all(conn) == {"purged": 1}

    claims = db.current_assertions(conn, "file", 1)
    assert list(claims) == ["purged"]
    assert claims["purged"][0]["value"] == {"by": "decide:fakedec:d", "p_personal": 0.98, "prompt_version": None}
    row = conn.execute("SELECT * FROM files WHERE id=1").fetchone()
    assert (row["sha256"], row["size"]) == (f["sha256"], f["size"]) and row["purged_at"]
    assert all(row[k] is None for k in ["page_count", "pdf_title", "pdf_producer", "stored_path"])
    for table in ["file_pages", "file_outline", "contexts", "context_refs", "works"]:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert not conn.execute("SELECT 1 FROM file_pages_fts WHERE file_pages_fts MATCH 'quuxperson'").fetchone()
    assert not stored.exists() and not thumb.exists()
    # erased, not just unlinked: no trace of the text, the name or the path in the database file or WAL
    on_disk = _bytes_on_disk(tmp_path / "t.sqlite")
    assert MARK.encode() not in on_disk and b"Payslip_2026-10" not in on_disk
    assert promote_file(conn, 1) == ("purged", None)  # idempotent


def test_purged_bytes_are_skipped_wherever_they_turn_up(tmp_path):
    conn = _payslip(tmp_path)
    original = tmp_path / "lib" / "Payslip_2026-10.pdf"
    data = original.read_bytes()
    promote_all(conn)
    (tmp_path / "elsewhere").mkdir()
    shutil.copy(original, tmp_path / "elsewhere" / "renamed.pdf")
    stats = scan(conn, [tmp_path / "lib", tmp_path / "elsewhere"], ScanOptions())
    assert stats["purged_skipped"] == 2 and not stats["new_files"]
    assert conn.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0
    assert not any(store.root().glob("??/*.pdf"))

    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=data, headers={"content-type": "application/pdf",
                                                          "content-disposition": 'attachment; filename="slip.pdf"'})

    client = h.HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    url = "https://payroll.example/slips/2026-10.pdf"
    stats, (o,) = download(conn, [Link(url, f"Payslip {MARK}")], client=client)
    assert (o.state, o.file_id, stats["purged_skipped"]) == ("purged", 1, 1)
    ctx = conn.execute("SELECT locator, title, extra FROM contexts WHERE kind='download'").fetchone()
    assert ctx["locator"] == url and ctx["title"] is None and "slip.pdf" not in ctx["extra"]
    assert not any(store.root().glob("??/*.pdf"))
    # asked again: known (the URL is kept), not fetched
    _, (o,) = download(conn, [Link(url, f"Payslip {MARK}")], client=client)
    assert (o.state, len(calls)) == ("known", 1)
    assert conn.execute("SELECT title FROM contexts WHERE kind='download'").fetchone()[0] is None


def test_unsure_personal_file_is_held_and_purge_command(tmp_path, monkeypatch):
    conn = _payslip(tmp_path, p=0.7)
    assert promote_all(conn) == {"held": 1}
    assert conn.execute("SELECT count(*) FROM file_pages WHERE file_id=1").fetchone()[0] == 1
    monkeypatch.setenv("PAPYRUS_DB", str(tmp_path / "t.sqlite"))
    r = CliRunner().invoke(main, ["purge", "--dry-run"])
    assert r.exit_code == 0 and "held" in r.output and "would purge" not in r.output
    r = CliRunner().invoke(main, ["purge", "1"])  # the user's decision
    assert r.exit_code == 0 and "purged=1" in r.output, r.output
    assert db.current_assertions(conn, "file", 1)["purged"][0]["value"] == {"by": "user"}
    assert CliRunner().invoke(main, ["files"]).output.split()[3] == "purged"


def test_purging_a_works_only_file_strips_the_work(tmp_path):
    conn = _lib(tmp_path, "2411.00161.pdf", f"Zebedee {MARK} CV")  # filename id -> trusted
    triage_file(conn, 1, FakeBackend(dict(PAPER, title=f"{MARK} CV", authors=[f"Zebedee {MARK}"])),
                FakeDecider("paper", True))
    action, wid = promote_file(conn, 1)
    assert action == "created"
    triage_file(conn, 1, FakeBackend(SLIP), FakeDecider("personal", False, p=0.95))
    assert promote_file(conn, 1) == ("purged", None)
    w = conn.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
    assert w["status"] == "rejected" and w["title"] is None and w["abstract"] is None
    for table in ["work_authors", "work_topics", "work_ids", "file_works", "topics"]:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert not conn.execute("SELECT 1 FROM works_fts WHERE rowid=?", (wid,)).fetchone()
    assert list(db.current_assertions(conn, "work", wid)) == ["released_ids"]


def test_confirmed_work_and_user_claim_are_never_purged(tmp_path, monkeypatch):
    conn = _payslip(tmp_path, p=0.7)
    monkeypatch.setenv("PAPYRUS_DB", str(tmp_path / "t.sqlite"))
    r = CliRunner().invoke(main, ["keep", "1"])  # the user says it's library material
    assert r.exit_code == 0 and "created" in r.output, r.output
    triage_file(conn, 1, FakeBackend(SLIP), FakeDecider("personal", False, p=1.0))  # the decider disagrees
    assert purge.verdict(conn, 1) is None and promote_file(conn, 1)[0] == "already_linked"
    conn.execute("UPDATE works SET status='confirmed'")
    conn.execute("DELETE FROM assertions WHERE source='user'")
    assert purge.verdict(conn, 1) is None
    assert "confirmed work" in CliRunner().invoke(main, ["purge", "1"]).output


def test_keep_path_brings_back_a_purged_file(tmp_path, monkeypatch):
    conn = _payslip(tmp_path)
    promote_all(conn)
    path = tmp_path / "lib" / "Payslip_2026-10.pdf"
    monkeypatch.setenv("PAPYRUS_DB", str(tmp_path / "t.sqlite"))
    assert "give the path" in CliRunner().invoke(main, ["keep", "1"]).output
    r = CliRunner().invoke(main, ["keep", str(path)])
    assert r.exit_code == 0 and "restored (file 1)" in r.output, r.output
    f = conn.execute("SELECT * FROM files WHERE id=1").fetchone()
    assert f["purged_at"] is None and f["triage_status"] == "pending" and store.is_stored(f["stored_path"], f["size"])
    assert conn.execute("SELECT count(*) FROM file_pages WHERE file_id=1").fetchone()[0] == 1
    assert conn.execute("SELECT locator FROM contexts").fetchone()[0] == str(path.resolve())
    triage_file(conn, 1, FakeBackend(SLIP), FakeDecider("personal", False, p=1.0))
    assert promote_file(conn, 1)[0] == "created"  # the user's claim outranks the decider


def test_mcp_shows_only_live_works_files(tmp_path, monkeypatch):
    d = tmp_path / "lib"
    d.mkdir()
    from test_core import _make_pdf
    _make_pdf(d / "paper.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    _make_pdf(d / "slip.pdf", f"Payslip October. Employee Zebedee {MARK}.")
    monkeypatch.setenv("PAPYRUS_DB", str(tmp_path / "t.sqlite"))
    conn = db.connect()
    scan(conn, [d], ScanOptions())
    ids = {r[0]: r[1] for r in conn.execute("SELECT c.title, r.file_id FROM contexts c JOIN context_refs r"
                                            " ON r.context_id=c.id")}
    triage_file(conn, ids["paper.pdf"], FakeBackend(PAPER), FakeDecider("paper", True))
    triage_file(conn, ids["slip.pdf"], FakeBackend(SLIP), FakeDecider("personal", False, p=0.7))
    assert promote_all(conn) == {"created": 1, "held": 1}
    assert mcp_server.get_file(ids["paper.pdf"])["works"]
    with pytest.raises(ValueError):
        mcp_server.get_file(ids["slip.pdf"], include_text=True)
    assert [r["locator"] for r in mcp_server.recent_discoveries()] == [str((d / "paper.pdf").resolve())]
    wid = conn.execute("SELECT id FROM works").fetchone()[0]
    conn.execute("UPDATE works SET status='rejected'")
    with pytest.raises(ValueError):
        mcp_server.get_work(wid)
