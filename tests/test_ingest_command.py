from click.testing import CliRunner

import papyrus.match.pipeline as pipeline
import papyrus.llm as llm
import papyrus.llm.classify as classify
from papyrus.cli import main
from papyrus.llm.base import BackendUnavailable

from test_ingest import PAPER, FakeBackend, _lib


def test_ingest_runs_every_stage_and_is_idempotent(tmp_path, monkeypatch):
    conn = _lib(tmp_path, "paper.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(PAPER))
    monkeypatch.setenv("PAPYRUS_CLASSIFIER", "none")
    enriched = []

    def fake_match(conn, wid, run_id=None):
        enriched.append(wid)
        conn.execute("INSERT INTO assertions(subject_type, subject_id, field, value, source)"
                     " VALUES ('work', ?, 'title', '\"x\"', 'api:test')", (wid,))
        return {"status": "matched"}

    monkeypatch.setattr(pipeline, "match_work", fake_match)
    runner, db_arg = CliRunner(), ["--db", str(tmp_path / "t.sqlite")]

    r = runner.invoke(main, [*db_arg, "ingest"])
    assert r.exit_code == 0, r.output
    assert "classifying 1 files" in r.output and "== match (1 works)" in r.output
    assert enriched == [1]
    assert conn.execute("SELECT title FROM works").fetchone()[0] == "A Study of Things"

    r = runner.invoke(main, [*db_arg, "ingest"])  # nothing pending any more
    assert r.exit_code == 0 and "classifying 0 files" in r.output and "== match (0 works)" in r.output
    assert enriched == [1]


def test_ingest_stops_when_classifying_cannot_run(tmp_path, monkeypatch):
    _lib(tmp_path, "paper.pdf", "A Study of Things.")
    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(PAPER))

    def no_classifier(spec):
        raise BackendUnavailable("LM Studio not reachable")

    monkeypatch.setattr(classify, "get_classifier", no_classifier)
    r = CliRunner().invoke(main, ["--db", str(tmp_path / "t.sqlite"), "ingest"])
    assert r.exit_code == 1 and "no classifier" in r.output and "== promote" not in r.output
