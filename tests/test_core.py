import json
import shutil
from pathlib import Path

import pytest

from papyrus import db
from papyrus.filenames import parse_filename
from papyrus.llm.schema_lite import SchemaError, coerce, extract_json_object
from papyrus.scan import ScanOptions, scan


@pytest.mark.parametrize("stem,title,surnames,year", [
    ('Bellard2019 - "Lossless Data Compression with Neural Networks"',
     "Lossless Data Compression with Neural Networks", ["Bellard"], 2019),
    ('BonchiGadducciKissingerSobocinskiZanasi 2022 - "String Diagram Rewrite Theory II- Rewriting with Symmetric Monoidal Structure"',
     "String Diagram Rewrite Theory II: Rewriting with Symmetric Monoidal Structure",
     ["Bonchi", "Gadducci", "Kissinger", "Sobocinski", "Zanasi"], 2022),
    ('NavaWyart2026 "Hierarchical Concept Geometry in Language Models"',
     "Hierarchical Concept Geometry in Language Models", ["Nava", "Wyart"], 2026),
    ('"Gated Delta Networks"', "Gated Delta Networks", [], None),
    ('ShiZhangCui - "A Programming Paradigm for Spatiotemporal Composability"',
     "A Programming Paradigm for Spatiotemporal Composability", ["Shi", "Zhang", "Cui"], None),
    ('Leonhart 2026 "Sutra - Tensor-Op RNNs as a Compilation Target"',
     "Sutra: Tensor-Op RNNs as a Compilation Target", ["Leonhart"], 2026),
])
def test_parse_filename(stem, title, surnames, year):
    h = parse_filename(stem)
    assert (h.title, h.surnames, h.year) == (title, surnames, year)


def test_parse_filename_no_pattern():
    assert not parse_filename("2412.06464v3-2")
    assert not parse_filename("LastthreemonthPayslip")


SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["paper", "book"]},
    "year": {"type": "integer"}, "authors": {"type": "array", "items": {"type": "string"}},
    "ok": {"type": "boolean"}}, "required": ["kind"]}


def test_extract_json_object_variants():
    assert extract_json_object('Sure! ```json\n{"a": 1}\n``` done') == {"a": 1}
    assert extract_json_object('<think>{"no": 1}</think> {"a": "}"}') == {"a": "}"}
    with pytest.raises(SchemaError):
        extract_json_object("nothing here")


def test_coerce():
    assert coerce({"kind": "Paper", "year": "2024", "authors": "A. B", "ok": "yes", "extra": 1}, SCHEMA) == \
        {"kind": "paper", "year": 2024, "authors": ["A. B"], "ok": True}
    assert coerce({"kind": "book", "year": None}, SCHEMA) == {"kind": "book", "year": None}
    with pytest.raises(SchemaError):
        coerce({"kind": "slides"}, SCHEMA)
    with pytest.raises(SchemaError):
        coerce({"year": 1}, SCHEMA)


def _make_pdf(path: Path, text: str) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw  # noqa: F401  (ensures native lib loads)
    # minimal hand-written PDF with one text line
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    pdfium.PdfDocument(str(path)).close()  # sanity: parseable


def test_scan_idempotent_dedup_and_missing(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _make_pdf(lib / 'Doe2020 - "A Study of Things".pdf', "A Study of Things. Jane Doe. Abstract: equality saturation rocks")
    shutil.copy(lib / 'Doe2020 - "A Study of Things".pdf', lib / "copy.pdf")
    (lib / "fake.pdf").write_text("not a pdf")
    conn = db.connect(tmp_path / "t.sqlite")

    s1 = scan(conn, [lib], ScanOptions())
    assert s1["new_files"] == 1 and s1["known_files"] == 1 and s1["not_pdf"] == 1
    assert conn.execute("SELECT count(*) FROM contexts").fetchone()[0] == 2
    hint = conn.execute("SELECT value FROM assertions WHERE field='filename_hint'").fetchone()[0]
    assert json.loads(hint)["surnames"] == ["Doe"]
    assert conn.execute("SELECT count(*) FROM file_pages_fts WHERE file_pages_fts MATCH 'saturation'").fetchone()[0] == 1

    s2 = scan(conn, [lib], ScanOptions())
    assert s2["unchanged"] == 2 and s2["new_files"] == 0

    (lib / "copy.pdf").unlink()
    s3 = scan(conn, [lib], ScanOptions())
    assert s3["went_missing"] == 1
    assert conn.execute("SELECT count(*) FROM contexts WHERE missing_at IS NOT NULL").fetchone()[0] == 1
