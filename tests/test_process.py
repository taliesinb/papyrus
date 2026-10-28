from click.testing import CliRunner

import papyrus.enrich.pipeline as pipeline
import papyrus.llm as llm
import papyrus.llm.decide as decide
from papyrus.cli import main
from papyrus.llm.base import BackendUnavailable

from test_triage_promote import PAPER, FakeBackend, _lib


def test_process_runs_triage_promote_enrich_and_is_idempotent(tmp_path, monkeypatch):
    conn = _lib(tmp_path, "paper.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(PAPER))
    monkeypatch.setenv("PAPYRUS_DECIDER", "none")
    enriched = []

    def fake_enrich(conn, wid, run_id=None):
        enriched.append(wid)
        conn.execute("INSERT INTO assertions(subject_type, subject_id, field, value, source)"
                     " VALUES ('work', ?, 'title', '\"x\"', 'api:test')", (wid,))
        return {"status": "matched"}

    monkeypatch.setattr(pipeline, "enrich_work", fake_enrich)
    runner, db_arg = CliRunner(), ["--db", str(tmp_path / "t.sqlite")]

    r = runner.invoke(main, [*db_arg, "process"])
    assert r.exit_code == 0, r.output
    assert "triaging 1 files" in r.output and "== enrich (1 works)" in r.output
    assert enriched == [1]
    assert conn.execute("SELECT title FROM works").fetchone()[0] == "A Study of Things"

    r = runner.invoke(main, [*db_arg, "process"])  # nothing pending any more
    assert r.exit_code == 0 and "triaging 0 files" in r.output and "== enrich (0 works)" in r.output
    assert enriched == [1]


def test_process_stops_when_triage_cannot_run(tmp_path, monkeypatch):
    _lib(tmp_path, "paper.pdf", "A Study of Things.")
    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(PAPER))

    def no_decider(spec):
        raise BackendUnavailable("LM Studio not reachable")

    monkeypatch.setattr(decide, "get_decider", no_decider)
    r = CliRunner().invoke(main, ["--db", str(tmp_path / "t.sqlite"), "process"])
    assert r.exit_code == 1 and "no decider" in r.output and "== promote" not in r.output
