"""The watcher's work queue (watch/queue.py): sync rules, order, retries, estimates; and that the
watcher's retries survive a restart."""

import json

import pytest
from click.testing import CliRunner

from papyrus.cli import main
from papyrus.watch.queue import MAX_ATTEMPTS, RETRY_S, Queue

from test_ingest import PAPER, FakeBackend
from test_watch import Clock, _pdf, _watch


@pytest.fixture(autouse=True)
def _no_network(no_match_network):  # the watcher enriches what it promotes
    pass


@pytest.fixture
def q(tmp_path):
    clock = Clock()
    return Queue(tmp_path / "queue.sqlite", clock), clock


def _rows(queue):
    return {(r["stage"], r["item_id"]): r["state"] for r in queue.conn.execute("SELECT * FROM items")}


def test_sync_mirrors_the_library(q):
    queue, clock = q
    queue.sync({"classify": [1, 2], "extract": [5]})
    assert _rows(queue) == {("classify", 1): "pending", ("classify", 2): "pending", ("extract", 5): "pending"}
    queue.done("classify", 1, 900)
    queue.sync({"classify": [2, 3], "extract": [5]})  # 1 done, 3 new
    assert _rows(queue)[("classify", 3)] == "pending" and _rows(queue)[("classify", 1)] == "done"
    queue.sync({"classify": [1, 3], "extract": []})  # 1 pending again (e.g. OCR found text), 2 and 5 done elsewhere
    rows = _rows(queue)
    assert rows[("classify", 1)] == "pending" and rows[("classify", 2)] == "done" and rows[("extract", 5)] == "done"
    queue.sync({"enrich": [7]})  # stages not given are left alone
    assert _rows(queue)[("classify", 1)] == "pending"


def test_next_takes_stages_in_order_and_waits_for_retries(q):
    queue, clock = q
    queue.sync({"classify": [1], "enrich": [4], "extract": [9]})
    assert queue.next() == ("extract", 9)
    assert queue.next(["classify"]) == ("classify", 1)  # e.g. model work allowed only
    assert queue.next(["enrich", "classify"]) == ("enrich", 4)
    assert queue.retry("extract", 9, "PdfiumError") == RETRY_S[0]
    assert queue.next() == ("enrich", 4)
    clock.advance(RETRY_S[0])
    assert queue.next() == ("extract", 9)


def test_retries_back_off_then_give_up(q):
    queue, clock = q
    queue.sync({"enrich": [4]})
    delays = [queue.retry("enrich", 4, "no_progress") for _ in range(MAX_ATTEMPTS)]
    assert delays == [*RETRY_S, None]
    assert _rows(queue)[("enrich", 4)] == "failed" and queue.counts()["failed"] == 1
    queue.sync({"enrich": [4]})  # still pending in the library: stays failed
    assert queue.next() is None
    assert queue.retry_failed() == 1 and queue.next() == ("enrich", 4)
    queue.sync({"enrich": []})  # done elsewhere: a failed item is done too
    assert _rows(queue)[("enrich", 4)] == "done"


def test_counts_and_eta(q):
    queue, clock = q
    queue.sync({"classify": [1, 2, 3], "extract": [8, 9]})
    assert queue.eta_s() is None  # no history yet
    queue.done("extract", 8, 2000)
    assert queue.eta_s() is None  # classify has none yet: no partial sum
    queue.done("classify", 1, 20_000)
    assert queue.counts() == {"extract": 1, "ocr": 0, "enrich": 0, "classify": 2, "metadata": 0, "failed": 0}
    assert queue.eta_s() == 2 * 20 + 1 * 2
    assert queue.eta_s({"classify": 10.0}) == 2 * 10 + 2  # e.g. the inference cache's estimate


def test_watch_retries_survive_a_restart(tmp_path, monkeypatch):
    from papyrus.match import pipeline

    attempts = []

    def finds_nothing(conn, wid, **kw):  # writes nothing: the work stays unenriched
        attempts.append(wid)
        return {"status": "not_found", "work_id": wid}

    monkeypatch.setattr(pipeline, "match_work", finds_nothing)
    w, conn, dl, clock, logs = _watch(tmp_path)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert attempts == [1] and any("no_progress, trying again in 1 h" in m for m in logs)
    status = json.loads(w.status_path.read_text())
    # enrichment has no finished step to learn from yet: no estimate rather than a wrong one
    assert status["pending"]["enrich"] == 1 and status["failed"] == 0 and status["eta_s"] is None
    w.close()

    w2, conn, dl, clock2, logs2 = _watch(tmp_path)  # a restart: same library, same queue
    clock2.t = clock.t
    w2.catch_up()
    assert attempts == [1]  # not retried at once
    clock2.advance(RETRY_S[0])
    w2.dirty = True
    w2.pump()
    assert attempts == [1, 1]


def test_eta_uses_the_inference_cache_for_triage(tmp_path):
    w, conn, dl, clock, logs = _watch(tmp_path, backend=FakeBackend(PAPER))
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()  # one document triaged through the cache: its three calls have timings
    from papyrus import inference

    per_doc = inference.store().document_estimate_s()
    assert per_doc is not None and per_doc >= 0
    conn.execute("UPDATE files SET triage_status='pending'")
    w.queue.sync(w._library_pending())
    assert w.eta_s() == pytest.approx(per_doc, abs=0.1)


def test_cli_retry_failed(tmp_path):
    queue = Queue(tmp_path / "papyrus-home" / "queue.sqlite")
    queue.sync({"classify": [1]})
    for _ in range(MAX_ATTEMPTS):
        queue.retry("classify", 1, "ValueError")
    queue.close()
    r = CliRunner().invoke(main, ["watch", "--retry-failed"])
    assert r.exit_code == 0 and "1 failed items are pending again" in r.output
