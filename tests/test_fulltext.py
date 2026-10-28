"""Full text: `papyrus extract`, page kinds, OCR, and search over every page (no model, no network)."""

from pathlib import Path

import pytest

from papyrus import db, fulltext, mcp_server
from papyrus.scan import ScanOptions, scan
from papyrus.search import fts_query, rank_files, search_works
from papyrus.triage import build_state, triage_file
from papyrus.works import promote_file

from test_triage_promote import PAPER, FakeBackend, FakeDecider


def make_pdf(path: Path, pages: list[list[str]], outline: list[tuple[str, int]] = ()) -> None:
    """A PDF with one Helvetica text line per string, and optionally an outline (title, 1-based page)."""
    objs: dict[int, bytes] = {}
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    outline_root = 4 + 2 * n
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R" + (f" /Outlines {outline_root} 0 R".encode() if outline else b"") + b" >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n} >>".encode()
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for pid, lines in zip(page_ids, pages):
        esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
        stream = ("BT /F1 10 Tf 12 TL 72 740 Td " + " T* ".join(f"({ln}) Tj" for ln in esc) + " ET").encode("latin-1")
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {pid + 1} 0 R "
                     f"/Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objs[pid + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    if outline:
        items = [outline_root + 1 + i for i in range(len(outline))]
        objs[outline_root] = f"<< /Type /Outlines /First {items[0]} 0 R /Last {items[-1]} 0 R /Count {len(items)} >>".encode()
        for i, (title, page) in enumerate(outline):
            links = (f" /Prev {items[i - 1]} 0 R" if i else "") + (f" /Next {items[i + 1]} 0 R" if i + 1 < len(items) else "")
            objs[items[i]] = (f"<< /Title ({title}) /Parent {outline_root} 0 R /Dest [{page_ids[page - 1]} 0 R /Fit]"
                              f"{links} >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + objs[i] + b"\nendobj\n"
    size = max(objs) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[i]:010d} 00000 n \n".encode() if i in offsets else b"0000000000 65535 f \n"
                    for i in range(1, size))
    out += f"trailer << /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


PROSE = ["We study things in great detail, as is customary in the literature of things.",
         "This line is about the method; in-text citations (Doe et al., 2020; Roe, 2021) are prose."] * 5
REFERENCES = ["References"] + [f"[{i}] A. Author and B. Writer. A paper about things. In Proceedings of Things, "
                               f"pp. {i}0-{i}9, 20{i:02d}." for i in range(1, 12)]
INDEX = [f"term number {i}, {i * 3}, {i * 7}" for i in range(1, 15)]
CONTENTS = [f"{i} Chapter about things {i * 10}" for i in range(1, 12)]


def test_page_kind():
    assert fulltext.page_kind("\n".join(PROSE), 3, 10) == "body"
    assert fulltext.page_kind("\n".join(REFERENCES), 9, 10) == "references"
    assert fulltext.page_kind("\n".join(INDEX), 95, 100) == "index"
    assert fulltext.page_kind("\n".join(INDEX), 50, 100) == "body"  # an index is at the back
    assert fulltext.page_kind("\n".join(CONTENTS), 3, 100) == "contents"
    assert fulltext.page_kind("\n".join(CONTENTS), 50, 100) == "body"  # a table of numbers mid-book
    assert fulltext.page_kind("References\n[1] too short", 9, 10) == "body"


def _paper_library(tmp_path: Path):
    """A promoted 4-page paper (pages: abstract, method with 'zygomorphic', more, references) and a
    3-page personal document that also says 'zygomorphic' on its last page."""
    d = tmp_path / "lib"
    d.mkdir()
    make_pdf(d / "paper.pdf",
             [["A Study of Things. Jane Doe.", "Abstract We study things in great detail."] + PROSE,
              PROSE,
              ["3 Method"] + PROSE[:4] + ["Our zygomorphic transform is the key idea of this paper."] + PROSE[4:],
              REFERENCES],
             outline=[("1 Introduction", 1), ("3 Method", 3), ("References", 4)])
    make_pdf(d / "payslip.pdf", [["Payslip October, for the employee named below", "Net pay 1234.00"], ["Deductions"],
                                 ["zygomorphic payroll code 77"]])
    conn = db.connect(tmp_path / "t.sqlite")
    scan(conn, [d], ScanOptions())
    ids = {r["title"]: r["file_id"] for r in conn.execute(
        "SELECT c.title, r.file_id FROM contexts c JOIN context_refs r ON r.context_id=c.id")}
    paper, slip = ids["paper.pdf"], ids["payslip.pdf"]
    triage_file(conn, paper, FakeBackend(PAPER), FakeDecider("paper", True))
    triage_file(conn, slip, FakeBackend(dict(PAPER, kind="personal", is_library_material=False, title="Payslip")),
                FakeDecider("personal", False))
    assert promote_file(conn, paper)[0] == "created"
    assert promote_file(conn, slip) == ("not_library", None)
    return conn, paper, slip


def test_extract_reads_every_page_of_live_works_only(tmp_path):
    conn, paper, slip = _paper_library(tmp_path)
    assert conn.execute("SELECT count(*) FROM file_pages WHERE file_id=?", (paper,)).fetchone()[0] == 2  # scan
    stats = fulltext.run(conn, ocr=False)
    assert stats["files"] == 1 and stats["pages"] == 4
    pages = {r["page"]: r for r in conn.execute("SELECT * FROM file_pages WHERE file_id=?", (paper,))}
    assert set(pages) == {1, 2, 3, 4} and pages[4]["kind"] == "references" and pages[3]["kind"] == "body"
    assert [tuple(r) for r in conn.execute("SELECT title, page FROM file_outline WHERE file_id=? ORDER BY position",
                                           (paper,))] == [("1 Introduction", 1), ("3 Method", 3), ("References", 4)]
    f = conn.execute("SELECT fulltext_status FROM files WHERE id=?", (paper,)).fetchone()
    assert f[0] == "done"
    # personal paperwork stays at the pages scanning read
    assert conn.execute("SELECT max(page) FROM file_pages WHERE file_id=?", (slip,)).fetchone()[0] == 2
    # triage still sees only the first pages
    assert "--- page 3 ---" not in build_state(conn, paper)
    assert fulltext.run(conn, ocr=False)["files"] == 0  # idempotent


def test_search_ranks_pages_with_sections_and_hides_personal_files(tmp_path):
    conn, paper, slip = _paper_library(tmp_path)
    fulltext.run(conn, ocr=False)
    fulltext.run(conn, (slip,), ocr=False)  # extracted on request: still not searchable as a work
    (w,) = search_works(conn, "zygomorphic")
    assert w["title"] == "A Study of Things" and w["matching_pages"] == 1
    (h,) = w["page_hits"]
    assert (h.page, h.section, h.kind) == (3, "3 Method", "body") and "[zygomorphic]" in h.snippet
    assert [f.file_id for f in rank_files(conn, "zygomorphic")] == [paper]
    assert {f.file_id for f in rank_files(conn, "zygomorphic", live_only=False)} == {paper, slip}
    # a references page ranks below a body page that matches as well
    hits = search_works(conn, "things")[0]["page_hits"]
    assert all(h.kind == "body" for h in hits)


def test_search_query_fallbacks(tmp_path):
    conn, _, _ = _paper_library(tmp_path)
    fulltext.run(conn, ocr=False)
    assert fts_query("zygo-morphic transform") == '"zygo" "morphic" "transform"'
    assert search_works(conn, "zygomorphic-transform")  # FTS5 syntax error -> tokenised
    assert search_works(conn, '"zygomorphic transform"')  # a phrase
    assert search_works(conn, "NEAR(zygomorphic idea, 6)")
    assert not search_works(conn, "zygomorphic NOT transform")


def test_mcp_fulltext_search(tmp_path, monkeypatch):
    conn, paper, _ = _paper_library(tmp_path)
    fulltext.run(conn, ocr=False)
    monkeypatch.setenv("PAPYRUS_DB", str(tmp_path / "t.sqlite"))
    (r,) = mcp_server.fulltext_search("zygomorphic")
    assert r["title"] == "A Study of Things" and r["file_id"] == paper
    assert r["hits"][0]["page"] == 3 and r["hits"][0]["section"] == "3 Method"
    f = mcp_server.get_file(paper, include_text=True, page_from=3, page_to=3)
    assert [p["page"] for p in f["pages"]] == [3] and len(f["outline"]) == 3


class FakeOCR:
    def __init__(self):
        self.calls = []
        self.reason = ""

    def available(self):
        return True

    def pages(self, path, pages):
        self.calls.append(list(pages))
        return {p: f"Scanned page {p} of a gloriously legible document about things." for p in pages}

    def close(self):
        pass


def test_ocr_first_pages_of_image_only_file_and_retriage(tmp_path, monkeypatch):
    d = tmp_path / "lib"
    d.mkdir()
    make_pdf(d / "scan.pdf", [[], [], []])  # no text layer at all
    conn = db.connect(tmp_path / "t.sqlite")
    scan(conn, [d], ScanOptions())
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    conn.execute("UPDATE files SET triage_status='done' WHERE id=?", (fid,))  # triaged blind
    fake = FakeOCR()
    monkeypatch.setattr(fulltext, "VisionOCR", lambda: fake)
    stats = fulltext.run(conn)
    assert fake.calls == [[1, 2]] and stats["retriage"] == 1  # first pages only: not a work
    rows = conn.execute("SELECT page, source, text FROM file_pages WHERE file_id=? ORDER BY page", (fid,)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "ocr:vision"), (2, "ocr:vision")] and "legible" in rows[0][2]
    assert conn.execute("SELECT triage_status FROM files WHERE id=?", (fid,)).fetchone()[0] == "pending"
    assert conn.execute("SELECT count(*) FROM file_pages_fts WHERE file_pages_fts MATCH 'legible'").fetchone()[0] == 2
    fulltext.run(conn)
    assert fake.calls == [[1, 2]]  # nothing left to OCR


def test_ocr_pages_survive_reextraction(tmp_path, monkeypatch):
    conn, paper, _ = _paper_library(tmp_path)
    conn.execute("UPDATE file_pages SET text='' WHERE file_id=? AND page=2", (paper,))
    fulltext.run(conn, ocr=False)  # page 2 comes back from the text layer
    conn.execute("UPDATE file_pages SET text='' WHERE file_id=? AND page=2", (paper,))
    fake = FakeOCR()
    monkeypatch.setattr(fulltext, "VisionOCR", lambda: fake)
    fulltext.run(conn)
    assert fake.calls == [[2]]
    fulltext.run(conn, redo=True)  # the text layer is read again but doesn't replace OCR'd text
    assert conn.execute("SELECT source FROM file_pages WHERE file_id=? AND page=2", (paper,)).fetchone()[0] == "ocr:vision"
    assert fake.calls == [[2]]


def test_older_schema_is_refused(tmp_path):
    p = tmp_path / "old.sqlite"
    db.connect(p).execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    with pytest.raises(RuntimeError, match="older"):
        db.connect(p)
