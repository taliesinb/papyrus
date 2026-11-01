"""Regressions from the first library review (CV arXiv id, thesis/paper conflation, open runs)."""

import pytest

from papyrus import db
from papyrus.enrich import pipeline
from papyrus.enrich.match import Hint
from papyrus.models import AuthorRecord, WorkRecord
from papyrus.triage import detect_ids, triage_file
from papyrus.works import find_work, promote_file

from test_core import _make_pdf
from test_triage_promote import PAPER, FakeBackend, FakeClassifier, _lib


def test_cv_citing_arxiv_url_gets_no_arxiv_id(tmp_path):
    conn = _lib(tmp_path, "cv.pdf", "Jane Doe CV. Deep GPs, https://arxiv.org/abs/2411.00161 First-authored paper")
    assert "arxiv" not in detect_ids(conn, 1)


def test_arxiv_margin_stamp_is_trusted(tmp_path):
    conn = _lib(tmp_path, "x.pdf", "Some Title arXiv:2412.06464v3 [cs.CL] 6 Mar 2025 Abstract")
    assert detect_ids(conn, 1) == {"arxiv": "2412.06464", "arxiv_version": "v3"}


def test_rejecting_a_work_releases_its_ids(tmp_path):
    conn = _lib(tmp_path, "2411.00161.pdf", "Jane Doe CV")  # filename id -> trusted
    triage_file(conn, 1, FakeBackend(dict(PAPER, title="Jane Doe CV")), FakeClassifier("paper", True))
    action, wid = promote_file(conn, 1)
    assert action == "created" and find_work(conn, {"arxiv": "2411.00161"}, None, None) == wid
    # re-triage says personal (held, below the purge threshold) -> promote rejects the work, frees the id
    triage_file(conn, 1, FakeBackend(dict(PAPER, title="Jane Doe CV")), FakeClassifier("personal", False, p=0.7))
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


def test_live_only_extraction_when_file_and_work_ids_differ(tmp_path):
    """fulltext.pending_files(live_only=True) formatted LIVE_FILE with a bare `id`, which inside its
    subquery meant works.id: a file was extracted only if its id equalled its work's. Found on the
    first real catch-up (file 6 = work 1: every extraction failed)."""
    from papyrus import db, fulltext
    from papyrus.scan import ScanOptions, scan
    from test_core import _make_pdf

    d = tmp_path / "lib"
    d.mkdir()
    for name in ("a.pdf", "b.pdf"):
        _make_pdf(d / name, f"Lecture notes {name}, on things, with exercises and solutions.")
    conn = db.connect(tmp_path / "t.sqlite")
    scan(conn, [d], ScanOptions())
    wid = conn.execute("INSERT INTO works(kind, title) VALUES ('lecture_notes', 'Notes')").lastrowid
    conn.execute("INSERT INTO file_works(file_id, work_id, relation, source) VALUES (2, ?, 'full', 'test')", (wid,))
    assert wid == 1
    assert fulltext.pending_files(conn, (2,), live_only=True) == [2]
    assert fulltext.pending_files(conn, (1,), live_only=True) == []  # file 1 is no live work's


def test_doi_and_isbn_from_the_pdfs_own_subject_and_keywords(tmp_path):
    """Publishers write the document's DOI into the PDF's subject (Elsevier: 'Journal, vol (year)
    pages. doi:...'); page 1 often doesn't show it."""
    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe. Abstract We study things.")
    conn.execute("UPDATE files SET pdf_subject=?, pdf_keywords=? WHERE id=1",
                 ("Journal of Things, 12 (2019) 1-20. doi:10.1016/J.THINGS.2019.01.002", "ISBN 978-0-262-03384-8"))
    assert detect_ids(conn, 1) == {"doi": "10.1016/j.things.2019.01.002", "isbn": "9780262033848"}
    # the filename and download URLs still come first
    conn.execute("UPDATE contexts SET extra=json_set(extra, '$.where_froms', json_array('https://doi.org/10.1145/3290345'))")
    assert detect_ids(conn, 1)["doi"] == "10.1145/3290345"


def test_state_gives_size_and_caps_long_metadata(tmp_path):
    from papyrus.triage import META_CHARS, build_state

    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    conn.execute("UPDATE files SET size=2500000, pdf_keywords=? WHERE id=1", ("topic; " * 500,))
    state = build_state(conn, 1)
    assert "\nsize: 2.5 MB\n" in state and "We study things in great detail" in state
    meta = next(line for line in state.splitlines() if line.startswith("keywords: "))
    assert len(meta) < META_CHARS + 20 and meta.endswith("…")


def test_state_metadata_on_one_line(tmp_path):
    """A book's keywords held Amazon's product details, one per line with '#' bullets; a creator
    or producer can carry tool noise after a newline."""
    from papyrus.triage import build_state

    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    conn.execute("UPDATE files SET pdf_keywords=?, pdf_creator=? WHERE id=1",
                 ("# Hardcover: 635 pages\r\n# ISBN-10: 1558608567\r\n", "Word\r\n  build 17.2 x64"))
    state = build_state(conn, 1)
    assert "\nkeywords: Hardcover: 635 pages; ISBN-10: 1558608567\n" in state
    assert "\ncreator: Word\n" in state


class _SchemaBackend(FakeBackend):
    def extract(self, *, schema=None, **kw):
        self.schemas = getattr(self, "schemas", []) + [schema]
        return super().extract(schema=schema, **kw)


def _claims(conn, field):
    return [r[0] for r in conn.execute("SELECT source FROM assertions WHERE subject_id=1 AND field=? AND"
                                       " superseded_at IS NULL", (field,))]


def test_the_classifier_settles_library_ness_not_the_extraction_model(tmp_path):
    """triage-4. A small extraction model called slides and manuals 'not library material', which
    dropped their topics and abstract though the classifier kept them (2026-11-01, Qwen3.5-2B)."""
    conn = _lib(tmp_path, "slides.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    be = _SchemaBackend(dict(PAPER, is_library_material=False, kind="other"))
    triage_file(conn, 1, be, FakeClassifier("slides", True))
    assert "kind" not in be.schemas[0]["properties"] and "is_library_material" not in be.schemas[0]["properties"]
    assert _claims(conn, "topics") and _claims(conn, "abstract")  # kept: the classifier says library
    assert all(s.startswith("classify:") for s in _claims(conn, "kind") + _claims(conn, "is_library_material"))


def test_extraction_is_skipped_when_the_classifier_is_sure_it_is_personal(tmp_path):
    conn = _lib(tmp_path, "payslip.pdf", "Payslip October. Net pay 1234.00")
    be = FakeBackend(PAPER)
    r = triage_file(conn, 1, be, FakeClassifier("personal", False, p=0.98))
    assert r["extract_skipped"] and be.prompts == [] and _claims(conn, "title") == []
    assert promote_file(conn, 1) == ("purged", None)


def test_held_files_get_no_metadata_from_a_model(tmp_path):
    """Not library material (0.5-0.9: held): no extraction at all; it is known by its file name."""
    conn = _lib(tmp_path, "letter.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    be = FakeBackend(PAPER)
    triage_file(conn, 1, be, FakeClassifier("personal", False, p=0.7))
    assert be.prompts == [] and not _claims(conn, "title") and not _claims(conn, "topics")
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "done"
    assert promote_file(conn, 1)[0] == "held"


def test_without_a_classifier_the_extraction_model_still_judges(tmp_path):
    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    be = _SchemaBackend(dict(PAPER, is_library_material=False))
    triage_file(conn, 1, be, None)
    assert "is_library_material" in be.schemas[0]["properties"] and not _claims(conn, "topics")


def test_a_null_author_from_the_model_is_dropped():
    """2026-11-01: Qwen3.5-2B answered "authors": [null]; promote then failed on work_authors'
    NOT NULL, at every watcher start (the catch-up promotes outside the per-step error handling)."""
    from papyrus.llm.schema_lite import coerce

    schema = {"type": "object", "properties": {"authors": {"type": "array", "items": {"type": "string"}},
                                               "topics": {"type": "array", "items": {"type": "object", "properties": {
                                                   "name": {"type": "string"}}, "required": ["name"]}}},
              "required": ["authors"]}
    assert coerce({"authors": [None, "", "Jane Doe"], "topics": [None]}, schema) == {"authors": ["Jane Doe"], "topics": []}


def test_promote_ignores_null_author_claims_already_stored(tmp_path):
    from papyrus import db

    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe.")
    triage_file(conn, 1, FakeBackend(PAPER), FakeClassifier("paper", True))
    db.assert_(conn, "file", 1, "authors", [None, "Jane Doe"], source="llm:fake:m")
    action, wid = promote_file(conn, 1)
    assert action == "created"
    assert [r[0] for r in conn.execute("SELECT raw_name FROM work_authors WHERE work_id=?", (wid,))] == ["Jane Doe"]


def test_a_rate_limited_lookup_records_no_verdict(tmp_path, monkeypatch):
    """2026-11-01 (test variant): OpenAlex answered 429, and enrichment recorded the work as
    'ambiguous' after waiting 5 minutes. Not asked is not found: no claim, the work stays pending."""
    from papyrus.enrich import match, pipeline

    conn = _lib(tmp_path, "x.pdf", "A Study of Things. Jane Doe.")
    triage_file(conn, 1, FakeBackend(PAPER), FakeClassifier("paper", True))
    _, wid = promote_file(conn, 1)

    def resolve(hint, report=None, **kw):
        report["rate_limited"] = {"openalex": 120.0, "s2": 0.0}
        return []

    monkeypatch.setattr(match, "resolve", resolve)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    r = pipeline.enrich_work(conn, wid)
    assert r["status"] == "rate_limited" and r["sources"] == ["openalex"] and r["retry_after"] == 120.0
    assert wid in pipeline.unenriched_works(conn)  # still pending

    def resolve_s2_only(hint, report=None, **kw):  # Semantic Scholar without a key refuses almost always
        report["rate_limited"] = {"s2": 0.0}
        return []

    monkeypatch.setattr(match, "resolve", resolve_s2_only)
    assert pipeline.enrich_work(conn, wid)["status"] == "not_found"
