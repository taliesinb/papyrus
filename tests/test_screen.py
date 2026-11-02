"""The screen (screen.py): the classify stage's fast first step. A fake embedder and two-dimensional
weights stand in for nomic-embed and the trained weights. Synthetic documents only."""

import math

from papyrus import db, inference, purge, screen
from papyrus.classify import KINDS
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


WEIGHTS = screen.Weights(((10.0, -10.0),), (0.0,), {"threshold": 0.9})


def _screen():
    return screen.Screen(inference.cached_embedder(FakeEmbedder()), WEIGHTS, 0.9)


def _payslip(tmp_path):
    return _lib(tmp_path, "Payslip_2026-10.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1234.00")


def test_weights_round_trip_and_the_shipped_weights(tmp_path):
    p = tmp_path / "w.bin"
    screen.write_weights(p, [0.5, -0.25, 2.0], -1.5, {"threshold": 0.8, "model": "m"})
    w = screen.read_weights(p)
    assert w.w == ((0.5, -0.25, 2.0),) and w.b == (-1.5,) and w.threshold == 0.8 and w.meta["dim"] == 3
    assert not w.kinds and w.accept_threshold is None and w.classes == ["personal"]
    z = (0.5 * 3 + -0.25 * 4) / 5 - 1.5  # the embedding is L2-normalised first
    assert math.isclose(w.p([3.0, 4.0, 0.0]), 1 / (1 + math.exp(-z)), rel_tol=1e-6)
    shipped = screen.read_weights()
    assert shipped.threshold == 0.9 and shipped.input_version == 3 and shipped.meta["dim"] == len(shipped.w[0])
    assert set(shipped.classes) <= set(KINDS) and "personal" in shipped.classes
    assert shipped.prefix == shipped.meta["prefix"]
    assert isinstance(FakeEmbedder(), Embedder)


def test_per_kind_weights_round_trip(tmp_path):
    p = tmp_path / "k.bin"
    screen.write_weights(p, [[1.0, 0.0], [0.0, 2.0], [-1.0, -1.0]], [0.0, 0.5, -0.5],
                         {"classes": ["paper", "personal", "slides"], "accept_threshold": 0.8, "prefix": "X: "})
    assert p.read_bytes()[:8] == screen.MAGIC_KINDS
    w = screen.read_weights(p)
    assert w.kinds and w.classes == ["paper", "personal", "slides"] and w.b == (0.0, 0.5, -0.5)
    assert w.accept_threshold == 0.8 and w.prefix == "X: "
    z = [0.6, 0.8 * 2 + 0.5, -0.6 - 0.8 - 0.5]  # the embedding [3, 4] is L2-normalised first
    e = [math.exp(v) for v in z]
    probs = w.probs([3.0, 4.0])
    assert all(math.isclose(probs[c], v / sum(e), rel_tol=1e-6) for c, v in zip(w.classes, e))
    assert math.isclose(w.p([3.0, 4.0]), probs["personal"])


class KindEmbedder(FakeEmbedder):
    """Payslip -> personal, Study -> paper, Slides -> slides; both of the last two: undecided."""

    def embed(self, texts):
        self.texts += texts
        return [[1.0 if "Payslip" in t else 0.0, 1.0 if "Study" in t else 0.0, 1.0 if "Slides" in t else 0.0]
                for t in texts]


KIND_WEIGHTS = screen.Weights(((10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 10.0)), (0.0, 0.0, 0.0),
                              {"classes": ["personal", "paper", "slides"], "threshold": 0.9, "accept_threshold": 0.9,
                               "input_version": 3, "prefix": "Instruct: kind\nQuery: "})


class CountingClassifier(FakeClassifier):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0

    def classify(self, state, questions):
        self.calls += 1
        return super().classify(state, questions)


def _kind_screen(accept=0.9):
    return screen.Screen(inference.cached_embedder(KindEmbedder()), KIND_WEIGHTS, 0.9, accept)


def test_the_screen_settles_a_kind_it_is_sure_of_without_the_classifier(tmp_path):
    from papyrus.classify import classified_p_personal

    conn = _lib(tmp_path, "things.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    be, clf, scr = FakeBackend(PAPER), CountingClassifier("slides", True), _kind_screen()
    r = classify_and_extract(conn, 1, be, clf, screen=scr)
    assert r["accepted"] and clf.calls == 0 and len(be.prompts) == 1 and r["kind"][0] == "paper"
    (text,) = scr.embedder.inner.texts  # the weights' own prefix
    assert text.startswith("Instruct: kind\nQuery: --- document ---\n")
    claims = db.current_assertions(conn, "file", 1)
    assert claims["kind"][0]["source"] == "screen:fake:embed-test" and claims["kind"][0]["value"] == "paper"
    kp = claims["kind_probs"][0]
    assert set(kp["value"]) == set(KINDS) and kp["value"]["paper"] > 0.99 and kp["value"]["book"] == 0.0
    sc = claims["screen"][0]["value"]
    assert sc["accepted"] and sc["kind"] == "paper" and not sc["rejected"] and sc["accept"] == 0.9
    # one model call, recorded once, behind both claims
    calls = {r[0] for r in conn.execute("SELECT call_id FROM assertions WHERE field IN ('screen', 'kind')")}
    assert len(calls) == 1 and None not in calls
    assert is_library(claims) and classified_p_personal(conn, 1) < 0.01
    assert promote_file(conn, 1)[0] == "created"
    assert conn.execute("SELECT kind FROM works").fetchone()[0] == "paper"


def test_the_classifier_decides_what_the_screen_is_unsure_of(tmp_path):
    conn = _lib(tmp_path, "talk.pdf", "A Study of Things. Slides for a talk by Jane Doe.")
    clf = CountingClassifier("slides", True)
    r = classify_and_extract(conn, 1, FakeBackend(PAPER), clf, screen=_kind_screen())
    assert not r.get("accepted") and clf.calls == 1 and r["kind"][0] == "slides"
    claims = db.current_assertions(conn, "file", 1)
    assert claims["screen"][0]["value"]["accepted"] is False and claims["kind"][0]["source"].startswith("classify:")


def test_accepting_kinds_can_be_turned_off(tmp_path):
    conn = _lib(tmp_path, "things.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    clf = CountingClassifier("paper", True)
    r = classify_and_extract(conn, 1, FakeBackend(PAPER), clf, screen=_kind_screen(accept=None))
    assert not r.get("accepted") and clf.calls == 1


def test_per_kind_weights_still_set_personal_paperwork_aside(tmp_path):
    conn = _payslip(tmp_path)
    be, clf = FakeBackend(PAPER), CountingClassifier("paper", True)
    r = classify_and_extract(conn, 1, be, clf, screen=_kind_screen())
    assert r["screened"] and clf.calls == 0 and be.prompts == []
    claims = db.current_assertions(conn, "file", 1)
    assert "kind_probs" not in claims and not is_library(claims) and purge.verdict(conn, 1) is None


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
    w3 = screen.Weights(WEIGHTS.w, WEIGHTS.b, {**WEIGHTS.meta, "input_version": 3})  # no prefix: nomic's
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


class LoadedEmbedder(FakeEmbedder):
    def model_loaded(self):
        return True


def test_the_watcher_screens_before_asking_the_language_model(tmp_path, no_match_network):
    from test_watch import LMBackend, LMClassifier, LMStudio, Memory, _pdf, _watch

    lm = LMStudio(["gemma"])  # the user's model: the language model may not be asked now
    be, dec = LMBackend(PAPER, lm), LMClassifier("paper", True)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, classifier=dec, lm=lm, memory=Memory("warn"),
                                      screen=lambda: screen.Screen(LoadedEmbedder(), WEIGHTS, 0.9))
    _pdf(dl / f"Payslip_{MARK}.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1.00")
    w.catch_up()
    assert be.prompts == [] and lm.loaded == ["gemma"]
    assert conn.execute("SELECT classify_status FROM files").fetchone()[0] == "done"
    assert any("set aside by the screen" in m for m in logs) and not any(MARK in m for m in logs)
    assert purge.candidates(conn, held=True) == [(1, None)]
    w.close()


def test_under_warn_the_watcher_loads_the_screens_model_only_if_it_fits(tmp_path, no_match_network):
    from test_watch import LMBackend, LMClassifier, LMStudio, Memory, _pdf, _watch

    lm = LMStudio(["gemma"])
    w, conn, dl, clock, logs = _watch(tmp_path, backend=LMBackend(PAPER, lm), classifier=LMClassifier("paper", True),
                                      lm=lm, memory=Memory("warn"),
                                      screen=lambda: screen.Screen(FakeEmbedder(), WEIGHTS, 0.9))
    w.ledger.room = lambda model: False  # not loaded, and no room: wait
    _pdf(dl / f"Payslip_{MARK}.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1.00")
    w.catch_up()
    assert conn.execute("SELECT classify_status FROM files").fetchone()[0] == "pending"
    assert any("no room for the screen's model" in m for m in logs)
    w.ledger.room = lambda model: True  # it fits: go on
    clock.t += 3600
    w.catch_up()
    assert conn.execute("SELECT classify_status FROM files").fetchone()[0] == "done"
    w.close()
