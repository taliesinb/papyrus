"""The screen (screen.py): the classify stage's fast first step. A fake embedder and two-dimensional
weights stand in for nomic-embed and the trained weights. Synthetic documents only."""

import math

from papyrus import db, inference, purge, screen
from papyrus.llm.base import Embedder, ModelInfo
from papyrus.extract import classify_and_extract
from papyrus.works import is_library, promote_file

from test_ingest import PAPER, FakeBackend, FakeClassifier, _lib

MARK = "Quuxperson"


class FakeEmbedder:
    """[1, 0] for anything mentioning a payslip, else [0, 1]; counts its calls."""

    model = "embed-test"

    def __init__(self):
        self.texts = []

    def info(self):
        return ModelInfo("fake", self.model)

    def available(self):
        return True

    def embed(self, texts):
        self.texts += texts
        return [[1.0, 0.0] if "Payslip" in t else [0.0, 1.0] for t in texts]


WEIGHTS = screen.Weights((10.0, -10.0), 0.0, {"threshold": 0.9})


def _screen():
    return screen.Screen(inference.cached_embedder(FakeEmbedder()), WEIGHTS, 0.9)


def _payslip(tmp_path):
    return _lib(tmp_path, "Payslip_2026-10.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1234.00")


def test_weights_round_trip_and_the_shipped_weights(tmp_path):
    p = tmp_path / "w.bin"
    screen.write_weights(p, [0.5, -0.25, 2.0], -1.5, {"threshold": 0.8, "model": "m"})
    w = screen.read_weights(p)
    assert w.w == (0.5, -0.25, 2.0) and w.b == -1.5 and w.threshold == 0.8 and w.meta["dim"] == 3
    z = (0.5 * 3 + -0.25 * 4) / 5 - 1.5  # the embedding is L2-normalised first
    assert math.isclose(w.p([3.0, 4.0, 0.0]), 1 / (1 + math.exp(-z)), rel_tol=1e-6)
    shipped = screen.read_weights()
    assert len(shipped.w) == 768 and shipped.threshold == 0.9 and shipped.input_version == 3
    assert shipped.meta["model"] == "text-embedding-nomic-embed-text-v1.5" and shipped.meta["prefix"] == screen.PREFIX
    assert isinstance(FakeEmbedder(), Embedder)


def test_screen_input():
    assert screen.home_relative("/Users/someone/Downloads") == "~/Downloads"
    assert screen.home_relative("/home/x/Desktop/papers") == "~/Desktop/papers"
    assert screen.home_relative("/tmp/x") == "/tmp/x"
    s = screen.screen_input("a.pdf", "~/Downloads", 2048, "arxiv.org", " ".join(f"w{i}" for i in range(300)))
    assert s.splitlines()[:4] == ["File name: a.pdf", "Folder: ~/Downloads", "Size: 2 KB", "Downloaded from: arxiv.org"]
    assert s.endswith("w127") and "w128" not in s


def test_screen_sets_personal_paperwork_aside_without_the_language_models(tmp_path):
    conn = _payslip(tmp_path)
    be, scr = FakeBackend(PAPER), _screen()
    r = classify_and_extract(conn, 1, be, FakeClassifier("paper", True), screen=scr)
    assert r["screened"] and r["screen"]["rejected"] and be.prompts == []
    claims = db.current_assertions(conn, "file", 1)
    c = claims["screen"][0]
    assert c["source"] == "screen:fake:embed-test" and c["value"]["rejected"] and c["value"]["p_personal"] > 0.99
    assert conn.execute("SELECT classify_status FROM files WHERE id=1").fetchone()[0] == "done"
    assert not is_library(claims)
    # held, never purged by the screen alone; listed for review
    assert purge.verdict(conn, 1) is None
    assert promote_file(conn, 1) == ("held", None)
    assert purge.candidates(conn, held=True) == [(1, None)]
    # the embedder saw the name, folder and first words, behind the task prefix, through the cache
    (text,) = scr.embedder.inner.texts
    assert text.startswith("classification: File name: Payslip_2026-10.pdf\n") and MARK in text
    call = conn.execute("SELECT call_id FROM assertions WHERE field='screen'").fetchone()[0]
    assert inference.store().show(call)["input"] == text
    # purging it erases what the embedder was asked
    purge.purge_file(conn, 1, {"by": "user"})
    assert inference.store().show(call)["input"] is None


def test_input_version_3_is_the_triage_state(tmp_path):
    """Weights trained on input version 3 embed exactly what the classifier and the extraction
    model read."""
    from papyrus.describe import build_state

    conn = _payslip(tmp_path)
    w3 = screen.Weights(WEIGHTS.w, WEIGHTS.b, {**WEIGHTS.meta, "input_version": 3})
    scr = screen.Screen(inference.cached_embedder(FakeEmbedder()), w3, 0.9)
    assert screen.screen_file(conn, 1, scr)["rejected"]
    (text,) = scr.embedder.inner.texts
    assert text == screen.PREFIX + build_state(conn, 1) and text.startswith(screen.PREFIX + "--- document ---\n")


def test_library_material_goes_on_to_full_triage(tmp_path):
    conn = _lib(tmp_path, "things.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    be = FakeBackend(PAPER)
    r = classify_and_extract(conn, 1, be, FakeClassifier("paper", True), screen=_screen())
    assert not r.get("screened") and r["screen"]["p_personal"] < 0.01 and len(be.prompts) == 1
    claims = db.current_assertions(conn, "file", 1)
    assert claims["screen"][0]["value"]["rejected"] is False
    assert promote_file(conn, 1)[0] == "created"


def test_keep_sends_a_screened_file_through_full_triage(tmp_path):
    conn = _payslip(tmp_path)
    scr = _screen()
    classify_and_extract(conn, 1, FakeBackend(PAPER), FakeClassifier("paper", True), screen=scr)
    assert purge.keep(conn, 1) == ("requeued", None)
    assert conn.execute("SELECT classify_status FROM files WHERE id=1").fetchone()[0] == "pending"
    be = FakeBackend(PAPER)
    r = classify_and_extract(conn, 1, be, FakeClassifier("paper", True), screen=scr)
    assert "screen" not in r and len(be.prompts) == 1  # the user has judged it: no screen
    assert promote_file(conn, 1)[0] == "created"


def test_no_screen_without_a_text_layer(tmp_path):
    conn = _payslip(tmp_path)
    conn.execute("DELETE FROM file_pages")
    be, scr = FakeBackend(PAPER), _screen()
    r = classify_and_extract(conn, 1, be, FakeClassifier("paper", True), screen=scr)
    assert "screen" not in r and len(be.prompts) == 1 and scr.embedder.inner.texts == []


def test_the_watcher_screens_before_asking_the_language_model(tmp_path, no_match_network):
    from test_watch import LMBackend, LMClassifier, LMStudio, Memory, _pdf, _watch

    lm = LMStudio(["gemma"])  # the user's model: the language model may not be asked now
    be, dec = LMBackend(PAPER, lm), LMClassifier("paper", True)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=dec, lm=lm, memory=Memory("warn"),
                                      screen=lambda: screen.Screen(FakeEmbedder(), WEIGHTS, 0.9))
    _pdf(dl / f"Payslip_{MARK}.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1.00")
    w.catch_up()
    assert be.prompts == [] and lm.loaded == ["gemma"]
    assert conn.execute("SELECT classify_status FROM files").fetchone()[0] == "done"
    assert any("set aside by the screen" in m for m in logs) and not any(MARK in m for m in logs)
    assert purge.candidates(conn, held=True) == [(1, None)]
    w.close()
