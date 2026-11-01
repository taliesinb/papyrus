"""Regressions from the first library review (CV arXiv id, thesis/paper conflation, open runs)."""

import pytest

from papyrus import db
from papyrus.enrich import pipeline
from papyrus.enrich.match import Hint
from papyrus.models import AuthorRecord, WorkRecord
from papyrus.triage import detect_ids, triage_file
from papyrus.works import find_work, promote_file

from test_core import _make_pdf
from test_triage_promote import PAPER, FakeBackend, FakeDecider, _lib


def test_cv_citing_arxiv_url_gets_no_arxiv_id(tmp_path):
    conn = _lib(tmp_path, "cv.pdf", "Jane Doe CV. Deep GPs, https://arxiv.org/abs/2411.00161 First-authored paper")
    assert "arxiv" not in detect_ids(conn, 1)


def test_arxiv_margin_stamp_is_trusted(tmp_path):
    conn = _lib(tmp_path, "x.pdf", "Some Title arXiv:2412.06464v3 [cs.CL] 6 Mar 2025 Abstract")
    assert detect_ids(conn, 1) == {"arxiv": "2412.06464", "arxiv_version": "v3"}


def test_rejecting_a_work_releases_its_ids(tmp_path):
    conn = _lib(tmp_path, "2411.00161.pdf", "Jane Doe CV")  # filename id -> trusted
    triage_file(conn, 1, FakeBackend(dict(PAPER, title="Jane Doe CV")), FakeDecider("paper", True))
    action, wid = promote_file(conn, 1)
    assert action == "created" and find_work(conn, {"arxiv": "2411.00161"}, None, None) == wid
    # re-triage says personal (held, below the purge threshold) -> promote rejects the work, frees the id
    triage_file(conn, 1, FakeBackend(dict(PAPER, title="Jane Doe CV")), FakeDecider("personal", False, p=0.7))
    assert promote_file(conn, 1)[0] == "rejected"
    assert conn.execute("SELECT count(*) FROM work_ids").fetchone()[0] == 0
    assert find_work(conn, {"arxiv": "2411.00161"}, None, None) is None
    assert db.current_assertions(conn, "work", wid)["released_ids"][0]["value"] == {"arxiv": "2411.00161"}


def _rec(source, title, ids, authors, score=0.93):
    return WorkRecord(source=source, title=title, ids=ids, authors=[AuthorRecord(name=a) for a in authors],
                      year=2014, match_score=score)


def test_candidate_owned_by_other_work_is_not_merged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.sqlite")
    paper = conn.execute("INSERT INTO works(kind, title) VALUES ('paper', 'Hopf algebras and Markov chains: two examples')").lastrowid
    conn.execute("INSERT INTO work_ids VALUES (?, 'doi', '10.1007/x', 'test')", (paper,))
    thesis = conn.execute("INSERT INTO works(kind, title) VALUES ('thesis', 'Hopf Algebras and Markov Chains')").lastrowid
    conn.execute("INSERT INTO work_ids VALUES (?, 'arxiv', '1412.8221', 'test')", (thesis,))
    conn.execute("INSERT INTO work_authors(work_id, position, raw_name, source) VALUES (?, 0, 'Amy Pang', 'triage')",
                 (thesis,))
    good = _rec("arxiv", "Hopf Algebras and Markov Chains", {"arxiv": "1412.8221"}, ["Amy Pang"], 1.0)
    other_doi = _rec("openalex", "Hopf algebras and Markov chains: two examples", {"doi": "10.1007/x"},
                     ["Persi Diaconis", "Amy Pang", "Arun Ram"])
    other_arxiv = _rec("s2", "Hopf algebras and Markov chains", {"arxiv": "1206.3620"}, ["Persi Diaconis"])
    monkeypatch.setattr(pipeline.match, "resolve", lambda hint, **kw: [good, other_doi, other_arxiv])
    r = pipeline.enrich_work(conn, thesis)
    assert r["status"] == "enriched" and r["sources"] == ["arxiv"]
    names = [x[0] for x in conn.execute("SELECT raw_name FROM work_authors WHERE work_id=?", (thesis,))]
    assert names == ["Amy Pang"]
    excl = db.current_assertions(conn, "work", thesis)["excluded_candidate"]
    assert {e["value"]["source"] for e in excl} == {"openalex", "s2"}


def test_conflict_reasons(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    w = conn.execute("INSERT INTO works(title) VALUES ('A')").lastrowid
    assert "different arXiv id" in pipeline._conflict(conn, w, Hint(arxiv_id="1"), _rec("s2", "A", {"arxiv": "2"}, []))
    assert pipeline._conflict(conn, w, Hint(arxiv_id="1"), _rec("s2", "A", {"arxiv": "1"}, [])) is None


def test_recorded_run_closes_on_interrupt_and_crash(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    with pytest.raises(KeyboardInterrupt):
        with db.recorded_run(conn, "triage") as (rid, stats):
            stats["ok"] = 2
            raise KeyboardInterrupt
    with pytest.raises(ValueError):
        with db.recorded_run(conn, "triage"):
            raise ValueError("boom")
    with db.recorded_run(conn, "scan"):
        pass
    rows = conn.execute("SELECT finished_at, stats FROM runs ORDER BY id").fetchall()
    assert all(r[0] for r in rows)
    assert ['"interrupted"' in rows[0][1], "crashed: ValueError: boom" in rows[1][1], '"completed"' in rows[2][1]] \
        == [True, True, True]
