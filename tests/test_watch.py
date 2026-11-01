"""`papyrus watch` (watch/): debounce, ingestion through the scan path, back-off, model ledger.

A fake watcher (events injected), a fake clock, a fake memory probe and the FakeBackend /
FakeClassifier of test_triage_promote; enrichment's network is stubbed. Synthetic documents only."""

import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from papyrus import db
from papyrus.cli import main
from papyrus.enrich import match
from papyrus.llm.base import BackendUnavailable, ModelBusy, ModelInfo
from papyrus.watch import EXIT_LOCKED, Backoff, Watch, read_status
from papyrus.watch import daemon as daemon_mod
from papyrus.watch.debounce import Debouncer, is_candidate
from papyrus.watch.errors import IDLE, LMSTUDIO_UNAVAILABLE, STOPPED, WAITING_MEMORY
from papyrus.watch.models import ModelLedger
from papyrus.watch.sources import FSEventsWatcher, InotifyWatcher, PollingWatcher, parse_inotify
from papyrus.watch.system import InstanceLock, linux_pressure

from test_core import _make_pdf
from test_triage_promote import PAPER, FakeBackend, FakeClassifier

MARK = "Quuxperson"  # in a payslip's filename and text: must never reach the log
SLIP = dict(PAPER, kind="personal", is_library_material=False, title=f"Payslip {MARK}", topics=[],
            has_abstract=False)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeWatcher:
    name = "fake"

    def __init__(self):
        self.events, self.started, self.closed = [], False, False

    def inject(self, *paths):
        self.events += [Path(p) for p in paths]

    def start(self):
        self.started = True

    def get(self, timeout):
        out, self.events = self.events, []
        return out

    def close(self):
        self.closed = True


class Memory:
    def __init__(self, level="normal"):
        self.level, self.calls = level, 0

    def __call__(self):
        self.calls += 1
        return self.level


class LMBackend(FakeBackend):
    """A FakeBackend that says it is LM Studio (so the model ledger applies), and loads its model
    just in time, as LM Studio does."""

    model = "qwen-test"

    def __init__(self, out, lm):
        super().__init__(out)
        self.lm = lm

    def info(self):
        return ModelInfo("lmstudio", self.model)

    def extract(self, **kw):
        if self.model not in self.lm.loaded:
            self.lm.loaded.append(self.model)
        return super().extract(**kw)


class LMClassifier(FakeClassifier):
    model = "qwen-test"

    def info(self):
        return ModelInfo("lmstudio", self.model)


class LMStudio:
    """What a fake LM Studio has loaded, and what was unloaded."""

    def __init__(self, loaded=()):
        self.loaded, self.unloaded = list(loaded), []

    def list_loaded(self):
        return list(self.loaded)

    def unload(self, m):
        self.unloaded.append(m)
        self.loaded.remove(m)
        return True


@pytest.fixture(autouse=True)
def _no_network(no_enrich_network):
    pass


def _pdf(path: Path, text: str, old: bool = True) -> Path:
    """A one-page PDF; `old`: last changed an hour ago (the catch-up scan takes it at once)."""
    _make_pdf(path, text)
    if old:
        t = time.time() - 3600
        os.utime(path, (t, t))
    return path


def _watch(tmp_path, *, backend=None, classifier=None, memory=None, lm=None, models=None, **kw):
    dl = tmp_path / "dl"
    dl.mkdir(exist_ok=True)
    conn = db.connect(tmp_path / "t.sqlite")
    clock, logs = Clock(), []
    be, dec = backend or FakeBackend(PAPER), classifier or FakeClassifier("paper", True)
    lm = lm or LMStudio()
    w = Watch(conn, [dl], watcher=FakeWatcher(), memory=memory or Memory(), clock=clock, log=logs.append,
              ledger=ModelLedger(lm.list_loaded, lm.unload, idle_s=600, clock=clock, log=logs.append),
              models=models or (lambda: (be, dec)), ocr=False, wall=clock, **kw)
    w._start_watcher()
    return w, conn, dl.resolve(), clock, logs


def _runs(conn):
    return [(r["command"], json.loads(r["stats"] or "{}")) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


# --- debounce ---------------------------------------------------------------------------------


def test_candidates():
    assert is_candidate("paper.pdf") and is_candidate("Paper.PDF")
    for name in [".hidden.pdf", "x.pdf.crdownload", "x.pdf.part", "x.pdf.download", "x.pdf.partial", "notes.txt"]:
        assert not is_candidate(name), name


def test_debounce_waits_until_a_file_stops_changing(tmp_path):
    clock = Clock()
    d = Debouncer([tmp_path], settle_s=3, clock=clock)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 part one")
    d.touch(f)
    clock.advance(2)
    f.write_bytes(b"%PDF-1.4 part one, part two")  # still being written
    assert d.due() == ([], [])
    clock.advance(2)
    assert d.due() == ([], [])  # 2 s since the last change
    clock.advance(2)
    assert d.due() == ([f], [])
    assert d.due() == ([], [])  # handed out once
    d.touch(tmp_path)  # a directory rescan skips what was handed out unchanged
    assert d.pending == {}


def test_debounce_ignores_partial_and_hidden_files_and_catches_the_rename(tmp_path):
    clock = Clock()
    d = Debouncer([tmp_path], settle_s=3, clock=clock)
    partial = tmp_path / "x.pdf.crdownload"
    partial.write_bytes(b"%PDF-1.4 downloading")
    (tmp_path / ".hidden.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "y.pdf.download").mkdir()  # Safari's bundle, with the PDF inside
    (tmp_path / "y.pdf.download" / "y.pdf").write_bytes(b"%PDF-1.4")
    for p in [partial, tmp_path / ".hidden.pdf", tmp_path / "y.pdf.download", tmp_path / "y.pdf.download" / "y.pdf"]:
        d.touch(p)
    clock.advance(10)
    assert d.due() == ([], [])
    final = tmp_path / "x.pdf"
    partial.rename(final)
    d.touch(partial)  # the old name's event: gone, but nothing was recorded for it
    d.touch(final)
    clock.advance(3)
    ready, _ = d.due()
    assert ready == [final]


def test_debounce_waits_for_firefoxs_part_file(tmp_path):
    clock = Clock()
    d = Debouncer([tmp_path], settle_s=3, clock=clock)
    final, part = tmp_path / "z.pdf", tmp_path / "z.pdf.part"
    final.write_bytes(b"")  # Firefox's empty placeholder under the final name
    part.write_bytes(b"%PDF-1.4 growing")
    d.touch(final)
    clock.advance(5)
    assert d.due() == ([], [])  # .part beside it
    part.unlink()
    clock.advance(5)
    assert d.due() == ([], [])  # empty and settled: dropped
    final.write_bytes(b"%PDF-1.4 complete")
    d.touch(final)
    clock.advance(3)
    assert d.due()[0] == [final]


def test_debounce_ignores_events_that_change_nothing(tmp_path):
    """Hashing a file fires events on it. A purged file has no context to say 'unchanged', so
    each event used to re-hash it: a run a second (2026-11-01, runs 86-120)."""
    clock = Clock()
    d = Debouncer([tmp_path], settle_s=3, clock=clock)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 done")
    d.touch(f)
    clock.advance(3)
    assert d.due()[0] == [f]
    for _ in range(3):
        d.touch(f)  # events while it is read
        clock.advance(3)
        assert d.due() == ([], [])
    f.write_bytes(b"%PDF-1.4 done, and changed")
    d.touch(f)
    clock.advance(3)
    assert d.due()[0] == [f]


def test_debounce_reports_gone_paths_and_ignores_other_directories(tmp_path):
    clock = Clock()
    (tmp_path / "sub").mkdir()
    d = Debouncer([tmp_path], settle_s=3, clock=clock)
    d.touch(tmp_path / "sub" / "deep.pdf")
    d.touch(tmp_path / "deleted.pdf")
    assert d.pending == {} and d.due() == ([], [tmp_path / "deleted.pdf"])


# --- ingestion through the scan path ----------------------------------------------------------


def test_new_file_is_ingested_once_settled_and_processed(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path)
    w.catch_up()
    f = dl / "2412.06464v3.pdf"
    _make_pdf(f, "A Study of Things. Jane Doe. arXiv:2412.06464v3 [cs.LG] Abstract We study things in great detail.")
    w.watcher.inject(f)
    w.pump()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0  # not settled yet
    clock.advance(3)
    w.pump()
    f1 = conn.execute("SELECT * FROM files").fetchone()
    assert f1["triage_status"] == "done" and f1["stored_path"] and f1["fulltext_status"] == "done"
    work = conn.execute("SELECT * FROM works").fetchone()
    assert work["title"] == "A Study of Things"
    ctx = conn.execute("SELECT * FROM contexts").fetchone()
    assert ctx["kind"] == "filesystem" and ctx["locator"] == str(f)
    # one recorded run per batch, all `watch`, closed as completed
    runs = _runs(conn)
    assert [c for c, _ in runs] == ["watch", "watch"]
    assert runs[1][1]["status"] == "completed" and runs[1][1]["new_files"] == 1 and runs[1][1]["triaged"] == 1
    assert runs[1][1]["promote_created"] == 1 and runs[1][1]["extracted"] == 1 and runs[1][1]["enrich_not_found"] == 1
    assert json.loads((tmp_path / "papyrus-home" / "watch.json").read_text())["state"] == IDLE
    # a copy of the same bytes: a second context for the same file, nothing to triage
    copy = dl / "copy.pdf"
    shutil.copy(f, copy)
    w.watcher.inject(copy)
    w.pump()
    clock.advance(3)
    w.pump()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM contexts").fetchone()[0] == 2
    assert _runs(conn)[-1][1]["known_files"] == 1 and "triaged" not in _runs(conn)[-1][1]


def test_purged_bytes_are_skipped_and_never_triaged_again(tmp_path):
    be = FakeBackend(SLIP)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=FakeClassifier("personal", False, p=0.98))
    slip = _pdf(dl / f"Payslip_{MARK}.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1.00")
    shutil.copy2(slip, dl / f"Payslip_{MARK} (1).pdf")  # the same bytes twice: one file
    w.catch_up()
    stats = _runs(conn)[-1][1]
    assert (stats["new_files"], stats["known_files"], stats["classified"], stats["promote_purged"]) == (1, 1, 1, 1)
    assert conn.execute("SELECT count(*) FROM files WHERE purged_at IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0  # both paths erased
    # the same bytes again, under another name: nothing recorded, not even the path
    again = dl / "scan0001.pdf"
    shutil.copy(slip, again)
    w.watcher.inject(again)
    w.pump()
    clock.advance(3)
    w.pump()
    assert conn.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    assert _runs(conn)[-1][1]["purged_skipped"] == 1
    assert be.prompts == []  # the classifier was sure: no extraction (triage-4), and never triaged again
    # the log holds ids and counts, never names, paths or text
    assert logs and not any(MARK in m or str(dl) in m or "Zebedee" in m or "Payslip" in m for m in logs)


def test_catch_up_leaves_files_still_being_written_to_the_debouncer(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path)
    _make_pdf(dl / "fresh.pdf", "Just written, maybe still downloading")
    w.catch_up()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    clock.advance(3)
    w.pump()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1


def test_deleted_file_marks_its_context_missing(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path)
    f = _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    f.unlink()
    w.watcher.inject(f)
    w.pump()
    assert conn.execute("SELECT missing_at FROM contexts").fetchone()[0]


def test_an_unreadable_folder_is_skipped_not_emptied(tmp_path):
    """macOS refuses the app access until the user allows it: nothing in the folder may be marked
    missing meanwhile, the status says why, and access granted later starts a scan."""
    w, conn, dl, clock, logs = _watch(tmp_path)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert conn.execute("SELECT count(*) FROM contexts WHERE missing_at IS NULL").fetchone()[0] == 1
    _pdf(dl / "b.pdf", "More lecture notes, on other things")
    dl.chmod(0)
    try:
        w.catch_up()
        assert conn.execute("SELECT count(*) FROM contexts WHERE missing_at IS NOT NULL").fetchone()[0] == 0
        status = json.loads(w.status_path.read_text())
        assert status["state"] == IDLE and "cannot read 1 watched folder" in status["detail"]
        assert any("cannot read" in m for m in logs)
    finally:
        dl.chmod(0o755)
    clock.advance(61)
    w.pump()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2 and not w.unreadable
    assert json.loads(w.status_path.read_text())["detail"] == ""


def test_a_database_replaced_under_the_watcher_is_rescued(tmp_path, monkeypatch):
    """What SQLPro for SQLite did on 2026-11-01: a safe-save swapped a copy in over papyrus.sqlite
    while the watcher held it open. The watcher's writes then went to the nameless old file."""
    import shutil
    import sqlite3

    from papyrus.watch import daemon

    exits = []
    monkeypatch.setattr(daemon.os, "_exit", lambda code: (exits.append(code), (_ for _ in ()).throw(SystemExit(code))))
    w, conn, dl, clock, logs = _watch(tmp_path)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    dbfile = tmp_path / "t.sqlite"
    shutil.copy(dbfile, tmp_path / "copy.sqlite")  # an editor's copy, saved back over the original
    (tmp_path / "copy.sqlite").replace(dbfile)
    conn.execute("INSERT INTO meta(key, value) VALUES ('written-after-the-swap', '1')")
    with pytest.raises(SystemExit):
        w.pump()
    assert exits == [daemon.EXIT_REPLACED]
    [rescued] = (tmp_path / "papyrus-home").glob("rescued-*")
    assert {p.name for p in rescued.iterdir()} == {"papyrus.sqlite", "inference.sqlite", "queue.sqlite"}
    r = sqlite3.connect(rescued / "papyrus.sqlite")
    assert r.execute("SELECT value FROM meta WHERE key='written-after-the-swap'").fetchone() == ("1",)
    assert r.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    assert json.loads(w.status_path.read_text())["state"] == "error"
    assert any("replaced while the watcher had them open" in m for m in logs)
    # a new watcher won't build on the damaged files while the rescued copy waits
    r = CliRunner().invoke(main, ["watch", "--once", "--dir", str(dl)])
    assert r.exit_code == daemon.EXIT_RESCUED and "rescued-" in r.output


# --- back-off -----------------------------------------------------------------------------------


def test_memory_pressure_skips_model_calls_and_backs_off(tmp_path):
    mem = Memory("warn")
    be = FakeBackend(PAPER)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, memory=mem)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert be.prompts == [] and mem.calls == 1
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "pending"  # not an error
    status = json.loads(w.status_path.read_text())
    assert status["state"] == WAITING_MEMORY and status["pending"]["triage"] == 1 and status["retry_at"]
    assert _runs(conn)[-1][1]["backoff_waiting-memory"] == 1
    w.pump()  # still backing off: no batch, no model call
    assert mem.calls == 1 and len(_runs(conn)) == 1
    clock.advance(31)
    w.pump()  # still under pressure: backs off for longer
    assert mem.calls == 2 and w.backoffs["classify"][0] - clock() == 60
    mem.level = "normal"
    clock.advance(61)
    w.pump()
    assert len(be.prompts) == 1 and conn.execute("SELECT triage_status FROM files").fetchone()[0] == "done"
    assert json.loads(w.status_path.read_text())["state"] == IDLE


def test_pausing_model_work_keeps_adding_files(tmp_path):
    be = FakeBackend(PAPER)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be)
    r = CliRunner().invoke(main, ["watch", "--pause"])
    assert r.exit_code == 0 and w.paused
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1  # added
    assert be.prompts == [] and conn.execute("SELECT triage_status FROM files").fetchone()[0] == "pending"
    status = json.loads(w.status_path.read_text())
    assert status["state"] == "paused" and status["paused"] is True and status["pending"]["triage"] == 1
    CliRunner().invoke(main, ["watch", "--resume"])
    w.pump()  # notices the resume, triages
    assert len(be.prompts) == 1 and conn.execute("SELECT triage_status FROM files").fetchone()[0] == "done"
    assert json.loads(w.status_path.read_text())["state"] == IDLE
    assert any(m == "model work resumed" for m in logs)


def test_an_ocr_back_off_does_not_hold_triage(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.backoffs["ocr"] = (clock() + 600, WAITING_MEMORY, "memory pressure (warn)")
    w.catch_up()
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "done"
    w.backoffs["classify"] = (clock() + 600, WAITING_MEMORY, "memory pressure (warn)")
    conn.execute("UPDATE files SET triage_status='pending'")
    w.dirty = True
    w.pump()
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "pending"
    assert json.loads(w.status_path.read_text())["state"] == WAITING_MEMORY


def test_apple_refusal_under_memory_pressure_is_a_back_off(tmp_path):
    class Busy(FakeBackend):
        def extract(self, **kw):
            raise ModelBusy("Apple Foundation Model busy: ModelManagerError 1013")

    w, conn, dl, clock, logs = _watch(tmp_path, backend=Busy(PAPER))
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "classified"  # extraction waits
    assert w.state == WAITING_MEMORY


def test_extract_and_enrich_go_on_while_model_work_waits(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path, memory=Memory("critical"))
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    # a work promoted by an earlier run, still to be extracted and enriched
    conn.execute("INSERT INTO works(kind, title) VALUES ('paper', 'Lecture notes on things')")
    conn.execute("INSERT INTO file_works(file_id, work_id, relation, source) VALUES (1, 1, 'full', 'test')")
    w.dirty = True
    w.pump()
    assert conn.execute("SELECT fulltext_status FROM files").fetchone()[0] == "done"
    assert conn.execute("SELECT count(*) FROM assertions WHERE field='enrich_candidates'").fetchone()[0] == 1


def test_no_classifier_means_no_triage(tmp_path):
    def models():
        raise Backoff(LMSTUDIO_UNAVAILABLE, "no classifier")

    w, conn, dl, clock, logs = _watch(tmp_path, models=models)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "pending"
    assert w.state == LMSTUDIO_UNAVAILABLE


def test_resolve_models_refuses_to_triage_without_a_classifier_unless_none(monkeypatch):
    import papyrus.llm as llm
    import papyrus.llm.classify as classify

    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(PAPER))

    def no_classifier(spec=None):
        raise BackendUnavailable("LM Studio not reachable")

    monkeypatch.setattr(classify, "get_classifier", no_classifier)
    with pytest.raises(Backoff) as e:
        daemon_mod.resolve_models()
    assert e.value.state == LMSTUDIO_UNAVAILABLE
    monkeypatch.setenv("PAPYRUS_CLASSIFIER", "none")
    be, dec = daemon_mod.resolve_models()
    assert dec is None


def test_sigterm_closes_the_run_as_interrupted(tmp_path):
    class Killed(FakeBackend):
        def extract(self, **kw):
            raise KeyboardInterrupt  # what cli._sigterm turns SIGTERM into

    w, conn, dl, clock, logs = _watch(tmp_path, backend=Killed(PAPER))
    _pdf(dl / "a.pdf", "Lecture notes on things")
    with pytest.raises(KeyboardInterrupt):
        w.run()
    assert _runs(conn)[-1][1]["status"] == "interrupted"
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "classified"  # extraction interrupted
    assert json.loads(w.status_path.read_text())["state"] == STOPPED and w.watcher.closed


# --- triage in phases: classify everything, then extract everything ---------------------------


class Recording:
    """A model that loads just in time in a fake LM Studio and records the order of its calls."""

    def __init__(self, inner, model, lm, log):
        self.inner, self.model, self.lm, self.calls = inner, model, lm, log

    def info(self):
        return ModelInfo("lmstudio", self.model)

    def available(self):
        return True

    def _use(self, what):
        if self.model not in self.lm.loaded:
            self.lm.loaded.append(self.model)
        self.calls.append(what)

    def extract(self, **kw):
        self._use("extract")
        return self.inner.extract(**kw)

    def classify(self, state, questions):
        self._use("classify")
        return self.inner.classify(state, questions)


def _two_models(tmp_path, memory="normal", loaded=()):
    lm, order = LMStudio(loaded), []
    be = Recording(FakeBackend(PAPER), "small-extractor", lm, order)
    clf = Recording(FakeClassifier("paper", True), "big-classifier", lm, order)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=clf, lm=lm, memory=Memory(memory))
    for i in range(3):
        _pdf(dl / f"{i}.pdf", f"Lecture notes number {i}, on things")
    return w, conn, lm, order, logs


def test_triage_classifies_everything_before_extracting(tmp_path):
    w, conn, lm, order, logs = _two_models(tmp_path)
    w.catch_up()
    assert order == ["classify"] * 6 + ["extract"] * 3  # two questions a file, then extraction
    assert [r[0] for r in conn.execute("SELECT DISTINCT triage_status FROM files")] == ["done"]
    assert conn.execute("SELECT count(*) FROM file_works").fetchone()[0] == 3  # (one work: the same fake title)
    # normal memory pressure: the second model loads beside the first, nothing is unloaded
    assert lm.unloaded == [] and sorted(lm.loaded) == ["big-classifier", "small-extractor"]
    assert "triage phase: metadata" in logs


def test_under_pressure_the_watcher_swaps_its_own_models(tmp_path):
    w, conn, lm, order, logs = _two_models(tmp_path, memory="warn")
    w.ledger.ours.add("big-classifier")  # the watcher loaded the classifier earlier
    lm.loaded.append("big-classifier")
    w.catch_up()
    assert order == ["classify"] * 6 + ["extract"] * 3
    assert lm.unloaded == ["big-classifier"] and lm.loaded == ["small-extractor"]  # one swap, at the phase change
    assert any("swap" in m for m in logs)


def test_a_model_the_user_loaded_is_not_swapped_out(tmp_path):
    w, conn, lm, order, logs = _two_models(tmp_path, memory="warn", loaded=["big-classifier"])  # theirs
    w.catch_up()
    assert order == ["classify"] * 6 and lm.unloaded == []  # extraction waits for its model
    assert [r[0] for r in conn.execute("SELECT DISTINCT triage_status FROM files")] == ["classified"]
    assert json.loads(w.status_path.read_text())["state"] == LMSTUDIO_UNAVAILABLE


def test_files_that_arrive_during_extraction_wait_for_the_next_classify_phase(tmp_path):
    w, conn, lm, order, logs = _two_models(tmp_path)
    late = []

    def extract_and_drop_a_file(**kw):
        if not late:
            late.append(_pdf(w.dirs[0] / "late.pdf", "Lecture notes arriving late, on things", old=False))
            w.watcher.inject(late[0])
        return FakeBackend(PAPER).extract(**kw)

    w.resolve_models()[0].inner.extract = extract_and_drop_a_file
    w.catch_up()
    assert order == ["classify"] * 6 + ["extract"] * 3  # the late file isn't slipped into this phase
    w.clock.advance(5)  # it settles
    w.pump()
    assert order == ["classify"] * 6 + ["extract"] * 3 + ["classify"] * 2 + ["extract"]


# --- LM Studio: never a second model, unload only our own -------------------------------------


def test_ledger_unloads_only_what_it_loaded():
    clock, lm = Clock(), LMStudio()
    ledger = ModelLedger(lm.list_loaded, lm.unload, idle_s=600, clock=clock)
    ledger.claim("qwen")  # nothing loaded: the request loads it, so it is ours
    lm.loaded.append("qwen")
    assert ledger.ours == {"qwen"}
    clock.advance(599)
    assert ledger.release_idle() == []
    clock.advance(2)
    assert ledger.release_idle() == ["qwen"] and lm.unloaded == ["qwen"]

    user = LMStudio(["qwen"])  # the user loaded it
    ledger = ModelLedger(user.list_loaded, user.unload, idle_s=600, clock=clock)
    ledger.claim("qwen")
    clock.advance(10_000)
    assert ledger.release_idle(force=True) == [] and user.unloaded == []

    other = LMStudio(["gemma"])  # another model is loaded: asking would load a second one
    ledger = ModelLedger(other.list_loaded, other.unload, clock=clock)
    with pytest.raises(Backoff) as e:
        ledger.claim("qwen")
    assert e.value.state == LMSTUDIO_UNAVAILABLE and not ledger.ours

    ledger = ModelLedger(lambda: [], lambda m: True, clock=clock)
    with pytest.raises(Backoff) as e:
        ledger.claim("qwen", may_load=False)  # under pressure: don't load anything
    assert e.value.state == WAITING_MEMORY


def test_ledger_forgets_a_model_that_went_away():
    clock, lm = Clock(), LMStudio()
    ledger = ModelLedger(lm.list_loaded, lm.unload, idle_s=600, clock=clock)
    ledger.claim("qwen")
    lm.loaded.append("qwen")
    lm.loaded.remove("qwen")  # LM Studio's own idle timeout unloaded it
    ledger.refresh()
    lm.loaded.append("qwen")  # … then the user loads it again: theirs now
    clock.advance(601)
    assert ledger.release_idle(force=True) == [] and lm.unloaded == []


def test_watch_with_lmstudio_models(tmp_path):
    lm = LMStudio(["gemma"])
    be, dec = LMBackend(PAPER, lm), LMClassifier("paper", True)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=dec, lm=lm)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert be.prompts == [] and w.state == LMSTUDIO_UNAVAILABLE  # the user's gemma is loaded
    lm.loaded.clear()  # the user unloaded it
    clock.advance(121)
    w.pump()
    assert len(be.prompts) == 1 and w.ledger.ours == {"qwen-test"} and lm.loaded == ["qwen-test"]
    assert json.loads(w.status_path.read_text())["loaded_by_watch"] == ["qwen-test"]
    clock.advance(601)
    w.pump()
    assert lm.unloaded == ["qwen-test"] and not w.ledger.ours


def test_memory_warn_allows_a_loaded_lmstudio_model_but_not_loading_one(tmp_path):
    lm = LMStudio(["qwen-test"])  # loaded by the user
    be, dec = LMBackend(PAPER, lm), LMClassifier("paper", True)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=dec, lm=lm, memory=Memory("warn"))
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert len(be.prompts) == 1 and not w.ledger.ours
    w.close()
    assert lm.unloaded == []


# --- operation ----------------------------------------------------------------------------------


def test_single_instance_lock_and_status(tmp_path):
    home = tmp_path / "home"
    a, b = InstanceLock(home / "watch.lock"), InstanceLock(home / "watch.lock")
    assert a.acquire()
    assert not b.acquire() and b.holder == os.getpid()
    (home / "watch.json").write_text(json.dumps({"state": "idle", "pid": os.getpid()}))
    assert read_status(home)["state"] == "idle"
    a.release()
    assert read_status(home)["state"] == STOPPED
    assert b.acquire()
    b.release()


def test_cli_watch_once_and_lock(tmp_path, monkeypatch):
    from papyrus.watch import system

    dl = tmp_path / "dl"
    dl.mkdir()
    _pdf(dl / "a.pdf", "A Study of Things. Jane Doe.")
    monkeypatch.setattr(system, "lower_priority", lambda: [])
    monkeypatch.setattr(daemon_mod, "resolve_models", lambda: (FakeBackend(PAPER), FakeClassifier("paper", True)))
    monkeypatch.setattr(daemon_mod, "memory_pressure", lambda: "normal")
    home = tmp_path / "papyrus-home"
    r = CliRunner().invoke(main, ["watch", "--once", "--dir", str(dl)])
    assert r.exit_code == 0, r.output
    conn = db.connect(home / "papyrus.sqlite")
    assert conn.execute("SELECT count(*) FROM works").fetchone()[0] == 1
    assert [c for c, _ in _runs(conn)] == ["watch"]
    lock = InstanceLock(home / "watch.lock")
    assert lock.acquire()
    r = CliRunner().invoke(main, ["watch", "--once", "--dir", str(dl)])
    assert r.exit_code == EXIT_LOCKED and "another papyrus watch" in r.output
    lock.release()
    r = CliRunner().invoke(main, ["watch", "--status"])
    assert json.loads(r.output)["state"] == STOPPED


def test_config_watch_section(tmp_path, monkeypatch):
    from papyrus import config

    assert [str(d) for d in config.watch().dirs] == [str(Path("~/Downloads").expanduser()),
                                                      str(Path("~/Desktop").expanduser())]
    cfg = tmp_path / "c.toml"
    cfg.write_text('[watch]\ndirs = ["/tmp/a"]\nsettle = 5\n')
    monkeypatch.setenv("PAPYRUS_CONFIG", str(cfg))
    s = config.watch()
    assert s.dirs == (Path("/tmp/a"),) and s.settle_s == 5 and s.unload_after_s == 600
    cfg.write_text('[watch]\nsettle = "soon"\n')
    with pytest.raises(config.ConfigError):
        config.watch()


# --- sources and system probes ----------------------------------------------------------------


def test_parse_inotify():
    def ev(wd, mask, name):
        raw = name.encode() + b"\0" * (16 - len(name))
        return struct.pack("iIII", wd, mask, 0, len(raw)) + raw

    buf = ev(1, 0x80, "paper.pdf") + ev(1, 0x8, "x.pdf.part") + struct.pack("iIII", -1, 0x4000, 0, 0)
    assert parse_inotify(buf) == [(1, 0x80, "paper.pdf"), (1, 0x8, "x.pdf.part"), (-1, 0x4000, "")]


def test_linux_pressure():
    psi = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    assert linux_pressure(psi, None) == "normal"
    assert linux_pressure(psi.replace("some avg10=0.00", "some avg10=12.50"), None) == "warn"
    assert linux_pressure(psi.replace("full avg10=0.00", "full avg10=7.00"), None) == "critical"
    meminfo = "MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: {} kB\n"
    assert linux_pressure(None, meminfo.format(500)) == "normal"
    assert linux_pressure(None, meminfo.format(80)) == "warn"
    assert linux_pressure(None, meminfo.format(30)) == "critical"


def _wait_for(watcher, path, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path in watcher.get(0.5):
            return True
    return False


def test_polling_watcher(tmp_path):
    w = PollingWatcher([tmp_path], interval=0.1)
    w.start()
    try:
        (tmp_path / "a.pdf").write_bytes(b"%PDF")
        assert _wait_for(w, tmp_path / "a.pdf")
    finally:
        w.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="FSEvents is macOS-only")
def test_fsevents_watcher(tmp_path):
    d = tmp_path.resolve()
    w = FSEventsWatcher([d], latency=0.1)
    w.start()
    try:
        (d / "a.pdf.crdownload").write_bytes(b"%PDF")
        (d / "a.pdf.crdownload").rename(d / "a.pdf")
        assert _wait_for(w, d / "a.pdf")
    finally:
        w.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")
def test_inotify_watcher(tmp_path):
    w = InotifyWatcher([tmp_path])
    w.start()
    try:
        (tmp_path / "a.pdf.part").write_bytes(b"%PDF")
        (tmp_path / "a.pdf.part").rename(tmp_path / "a.pdf")
        assert _wait_for(w, tmp_path / "a.pdf")
    finally:
        w.close()
