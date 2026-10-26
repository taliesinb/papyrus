from pathlib import Path

from papyrus import db
from papyrus.llm.base import Answer, ModelInfo
from papyrus.scan import ScanOptions, scan
from papyrus.triage import triage_file
from papyrus.works import promote_file, work_json

from test_core import _make_pdf


class FakeBackend:
    def __init__(self, out):
        self.out = out
        self.prompts = []

    def info(self):
        return ModelInfo("fake", "m")

    def available(self):
        return True

    def extract(self, *, instructions, prompt, schema, max_tokens=1024, temperature=0.0):
        self.prompts.append(prompt)
        return dict(self.out)


class FakeDecider:
    def __init__(self, kind, lib):
        self.kind, self.lib = kind, lib

    def info(self):
        return ModelInfo("fakedec", "d")

    def available(self):
        return True

    def decide(self, state, questions):
        return {"kind": Answer("kind", {self.kind: 0.9, "other": 0.1}),
                "is_library_material": Answer("is_library_material",
                                              {"yes": 0.8 if self.lib else 0.2, "no": 0.2 if self.lib else 0.8})}


PAPER = {"kind": "paper", "is_library_material": True, "title": "A Study of Things", "authors": ["Jane Doe"],
         "year": 2020, "has_abstract": True, "abstract": "We study things.", "summary": "About things.",
         "topics": [{"name": "Things", "field": "Thingology"}], "language": "en"}


def _lib(tmp_path: Path, name: str, text: str):
    d = tmp_path / "lib"
    d.mkdir(exist_ok=True)
    _make_pdf(d / name, text)
    conn = db.connect(tmp_path / "t.sqlite")
    scan(conn, [d], ScanOptions())
    return conn


def test_triage_and_promote_paper(tmp_path):
    conn = _lib(tmp_path, "2412.06464v3.pdf", "A Study of Things. Jane Doe. arXiv:2412.06464v3 [cs.LG]")
    be = FakeBackend(PAPER)
    r = triage_file(conn, 1, be, FakeDecider("paper", True))
    assert "Found at:" in be.prompts[0] and "--- page 1 ---" in be.prompts[0]
    assert r["ids"]["arxiv"] == "2412.06464" and r["ids"]["arxiv_version"] == "v3"
    claims = db.current_assertions(conn, "file", 1)
    assert {c["source"] for c in claims["kind"]} == {"llm:fake:m", "decide:fakedec:d"}
    action, wid = promote_file(conn, 1)
    assert action == "created"
    w = work_json(conn, wid)
    assert w["ids"]["arxiv"] == "2412.06464" and w["authors"][0]["raw_name"] == "Jane Doe"
    assert w["files"][0]["relation"] == "preprint" and w["contexts"][0]["kind"] == "filesystem"
    assert "thingology › things" in w["topics"]
    assert conn.execute("SELECT count(*) FROM works_fts WHERE works_fts MATCH 'things'").fetchone()[0] == 1
    assert promote_file(conn, 1)[0] == "already_linked"


def test_personal_document_not_promoted_and_abstract_dropped(tmp_path):
    conn = _lib(tmp_path, "payslip.pdf", "Payslip October. Net pay 1234.00")
    out = dict(PAPER, kind="personal", is_library_material=False, abstract="Net pay 1234.00 ...", title="Payslip")
    triage_file(conn, 1, FakeBackend(out), FakeDecider("personal", False))
    claims = db.current_assertions(conn, "file", 1)
    assert "abstract" not in claims and "topics" not in claims
    # decider (rank 2) outranks llm (rank 3); both say not library
    assert promote_file(conn, 1) == ("not_library", None)
