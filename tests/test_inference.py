"""The inference cache (inference.py): keys, seeds, hits, failures, the call ids triage stores,
and purge leaving no text behind. Fake models only, except the live Apple seed test."""

import json
import math
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from papyrus import db, inference, purge
from papyrus.cli import main
from papyrus.llm.base import Answer, BackendUnavailable, ModelBusy, ModelInfo, Question
from papyrus.llm.decide import OpenAICompatDecider
from papyrus.llm.lmstudio import LMStudioBackend
from papyrus.triage import triage_file
from papyrus.works import promote_file

from test_decide import chat_response, oa_transport, top
from test_triage_promote import PAPER, FakeBackend, FakeDecider, _lib

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
MARK = "Quuxperson"  # in a payslip: must survive nowhere after a purge


@pytest.fixture(autouse=True)
def _no_network(no_enrich_network):  # the watcher enriches what it promotes
    pass


class SeededBackend(FakeBackend):
    """Takes a seed, has a build and settings, and reports usage, like the real backends."""

    def __init__(self, out, build=None, settings=None):
        super().__init__(out)
        self.seeds, self.build, self.settings = [], build or {"quantization": "4bit"}, settings or {"impl": "x-1"}

    def model_build(self):
        return self.build

    def seed_honoured(self, build):
        return True

    def cache_params(self):
        return self.settings

    def extract(self, *, seed=0, **kw):
        self.seeds.append(seed)
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
        return super().extract(**kw)


def _extract(be, prompt="the first pages", instructions="Catalogue it.", temperature=0.0):
    return be.extract(instructions=instructions, prompt=prompt, schema=SCHEMA, temperature=temperature)


def _calls():
    return [dict(r) for r in inference.store().conn.execute("SELECT * FROM calls ORDER BY started_at")]


def test_a_repeated_question_is_answered_from_the_cache():
    inner = SeededBackend({"title": "A Study of Things"})
    be = inference.cached_backend(inner)
    assert _extract(be) == {"title": "A Study of Things"} and not be.last_hit
    first = be.last_call
    assert _extract(be) == {"title": "A Study of Things"} and be.last_hit and be.last_call == first
    assert len(inner.prompts) == 1  # the model was asked once
    [c] = _calls()
    assert c["status"] == "ok" and c["hits"] == 1 and c["input_chars"] == len("the first pages")
    assert (c["input_tokens"], c["output_tokens"]) == (100, 20) and c["duration_ms"] is not None
    s = inference.store()
    assert s.blob("input", c["input_hash"]).read_text() == "the first pages"
    assert json.loads(s.blob("output", c["output_hash"]).read_text()) == {"title": "A Study of Things"}
    m = s.conn.execute("SELECT * FROM models").fetchone()
    assert (m["backend"], m["model"], json.loads(m["build"]), m["seed_honoured"]) == ("fake", "m", {"quantization": "4bit"}, 1)
    q = s.conn.execute("SELECT * FROM queries").fetchone()
    assert q["kind"] == "extract" and q["instructions"] == "Catalogue it." and json.loads(q["output_schema"]) == SCHEMA
    assert json.loads(q["params"]) == {"temperature": 0.0, "max_tokens": 1024, "seed": 0, "impl": "x-1"}
    # the database holds hashes, not the text
    assert b"the first pages" not in s.path.read_bytes() + Path(f"{s.path}-wal").read_bytes()


def test_ids_are_16_digits_and_a_taken_name_falls_back_to_the_full_hash(monkeypatch):
    _extract(inference.cached_backend(SeededBackend({"title": "T"})))
    [c] = _calls()
    for v in (c["id"], c["model_id"], c["query_id"], c["input_hash"], c["output_hash"]):
        assert len(v) == 16
    s = inference.store()
    assert s.blob("input", c["input_hash"]).parent == s.root / "inputs"  # flat
    # another text whose short hash is already taken: named by its full sha256
    monkeypatch.setattr(inference, "short_hash", lambda data: c["input_hash"])
    name = s._write("input", b"different text")
    assert len(name) == 64 and s.blob("input", name).read_bytes() == b"different text"
    assert s.blob("input", c["input_hash"]).read_text() == "the first pages"


def test_everything_that_shapes_the_answer_is_in_the_key():
    inner = SeededBackend({"title": "T"})
    _extract(inference.cached_backend(inner))
    for be, kw in [
        (inference.cached_backend(inner, seed=1), {}),  # another seed: another answer
        (inference.cached_backend(inner), {"instructions": "Other instructions."}),
        (inference.cached_backend(inner), {"temperature": 0.7}),
        (inference.cached_backend(inner), {"prompt": "other pages"}),
        (inference.cached_backend(SeededBackend({"title": "T"}, build={"quantization": "8bit"})), {}),
        (inference.cached_backend(SeededBackend({"title": "T"}, settings={"impl": "x-2"})), {}),
    ]:
        _extract(be, **kw)
        assert not be.last_hit, kw
    assert inner.seeds == [0, 1, 0, 0, 0]  # the seed reaches backends that take one
    # the endpoint is not part of the model: same weights elsewhere still hit
    moved = SeededBackend({"title": "T"})
    moved.base_url = "http://127.0.0.1:9999/v1"
    be = inference.cached_backend(moved)
    _extract(be)
    assert be.last_hit and moved.prompts == []


def test_fresh_asks_again_and_before_call_runs_only_on_a_miss():
    inner, asked = SeededBackend({"title": "T"}), []
    be = inference.cached_backend(inner, before_call=lambda: asked.append(1))
    _extract(be)
    _extract(be)
    assert asked == [1]
    fresh = inference.cached_backend(inner, fresh=True)
    _extract(fresh)
    assert not fresh.last_hit and len(inner.prompts) == 2 and len(_calls()) == 2


def test_failures_are_recorded_and_never_served():
    class Flaky(SeededBackend):
        def __init__(self, errors):
            super().__init__({"title": "T"})
            self.errors = list(errors)

        def extract(self, **kw):
            if self.errors:
                raise self.errors.pop(0)
            return super().extract(**kw)

    be = inference.cached_backend(Flaky([ModelBusy("1013"), ValueError(f"bad output about {MARK}")]))
    for _ in range(2):
        with pytest.raises((ModelBusy, ValueError)):
            _extract(be)
    assert _extract(be) == {"title": "T"} and not be.last_hit
    assert [(c["status"], c["error"]) for c in _calls()] == [("busy", "ModelBusy"), ("error", "ValueError"), ("ok", None)]
    assert MARK.encode() not in inference.store().path.read_bytes() + Path(f"{inference.store().path}-wal").read_bytes()


def test_unknown_build_means_no_caching():
    class Down(SeededBackend):
        def model_build(self):
            return None  # e.g. LM Studio not answering

    inner = Down({"title": "T"})
    be = inference.cached_backend(inner)
    _extract(be)
    _extract(be)
    assert len(inner.prompts) == 2 and be.last_call is None and _calls() == []


def test_decider_is_cached_per_question():
    kind = Question("kind", "choice", "What kind?", {"paper": "a paper", "personal": "paperwork"})
    lib = Question("is_library_material", "yesno", "Library material?")
    new = Question("language", "choice", "Language?", {"en": "English", "de": "German"})

    class Counting(FakeDecider):
        def __init__(self):
            super().__init__("paper", True)
            self.asked = []

        def decide(self, state, questions):
            self.asked += [q.key for q in questions]
            out = super().decide(state, questions)
            out["language"] = Answer("language", {"en": 0.9, "de": 0.1})
            return {q.key: out[q.key] for q in questions}

    inner = Counting()
    dec = inference.cached_decider(inner)
    a = dec.decide("STATE", [kind, lib])
    assert a["kind"].best == "paper" and set(dec.last_calls) == {"kind", "is_library_material"}
    first = dict(dec.last_calls)
    b = dec.decide("STATE", [kind, lib, new])
    assert inner.asked == ["kind", "is_library_material", "language"]  # only the new question was asked
    assert b["kind"].probabilities == a["kind"].probabilities
    assert dec.last_calls["kind"] == first["kind"] and dec.last_calls["language"] not in first.values()


def test_triage_records_call_ids_and_a_rerun_reuses_them(tmp_path):
    conn = _lib(tmp_path, "notes.pdf", "Lecture notes on things, with exercises.")
    be, dec = inference.cached_backend(FakeBackend(PAPER)), inference.cached_decider(FakeDecider("paper", True))
    r = triage_file(conn, 1, be, dec)
    assert not r["cached"]
    rows = conn.execute("SELECT field, call_id FROM assertions WHERE subject_id=1 AND superseded_at IS NULL"
                        " AND source NOT LIKE 'heuristic:%'").fetchall()
    calls = {c["id"]: c for c in _calls()}
    assert rows and all(row["call_id"] in calls for row in rows)
    sha = conn.execute("SELECT sha256 FROM files WHERE id=1").fetchone()[0]
    assert {c["subject"] for c in calls.values()} == {sha}
    title_call = dict(rows)["title"]
    r = triage_file(conn, 1, be, dec)  # e.g. `papyrus triage --all` with nothing changed
    assert r["cached"] and len(_calls()) == 3  # extract + 2 questions, asked once
    again = dict(conn.execute("SELECT field, call_id FROM assertions WHERE subject_id=1 AND superseded_at IS NULL"
                              " AND field='title'").fetchall())
    assert again["title"] == title_call


def test_purge_leaves_no_inference_text(tmp_path):
    conn = _lib(tmp_path, "Payslip_2026-10.pdf", f"Payslip October. Employee Zebedee {MARK}. Net pay 1234.00")
    slip = dict(PAPER, kind="personal", is_library_material=False, title=f"Payslip {MARK}", topics=[])
    triage_file(conn, 1, inference.cached_backend(FakeBackend(slip)),
                inference.cached_decider(FakeDecider("personal", False, p=0.98)))
    root = tmp_path / "papyrus-home" / "inference"
    assert any(MARK.encode() in p.read_bytes() for p in root.rglob("*") if p.is_file())
    assert promote_file(conn, 1) == ("purged", None)
    assert [p for p in root.rglob("*") if p.is_file()] == []
    s = inference.store()
    assert len(_calls()) == 2  # the decider's two questions (sure: no extraction); the rows stay
    on_disk = b"".join(p.read_bytes() for p in s.path.parent.glob("inference.sqlite*"))
    assert MARK.encode() not in on_disk and b"Payslip" not in on_disk


def test_prune_and_stats(tmp_path):
    inner = SeededBackend({"title": "T"})
    be = inference.cached_backend(inner)
    _extract(be)
    s = inference.store()
    assert s.prune(inputs=True, outputs=False, older_than_days=0) == 1
    _extract(be)
    assert be.last_hit  # the answer is still there
    assert s.prune(inputs=False, outputs=True, older_than_days=0) == 1
    _extract(be)
    assert not be.last_hit and len(inner.prompts) == 2
    [row] = s.stats()
    assert (row["calls"], row["hits"], row["failed"], row["kind"]) == (2, 1, 0, "extract")
    c = _calls()[0]
    assert s.estimate_s(c["model_id"], c["query_id"], 1000) is not None
    assert s.show(c["id"][:8])["input"] == "the first pages"  # the rerun wrote it again (same content, same name)
    s.prune(inputs=True, outputs=False, older_than_days=0)
    shown = s.show(c["id"][:8])
    assert shown["input"] is None and shown["model"] == "m"
    r = CliRunner().invoke(main, ["inference", "stats"])
    assert r.exit_code == 0 and "fake:m" in r.output
    r = CliRunner().invoke(main, ["inference", "show", c["id"][:8]])
    assert r.exit_code == 0 and "--- input (deleted)" in r.output


def test_lmstudio_backend_sends_the_seed_and_reports_usage():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"title": "T"}'}}],
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 5}})

    be = LMStudioBackend(model="qwen3.5-4b-mlx")
    be._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert be.extract(instructions="i", prompt="p", schema=SCHEMA, seed=7) == {"title": "T"}
    assert bodies[0]["seed"] == 7 and be.last_usage == {"prompt_tokens": 50, "completion_tokens": 5}
    assert be.last_params == {"temperature": 0.0, "attempts": 1}
    assert be.cache_params()["impl"] and be.seed_honoured({"format": "mlx"}) is False


def test_openai_decider_seeds_every_request_and_sums_usage():
    rec = []
    seq = iter(["Yes", "No", "Yes", "Yes", "Yes"])

    def respond(body):
        if body.get("logprobs") or body.get("max_tokens") == 1:  # the probe, or the prefix prime
            return chat_response(None, "Yes")
        return {**chat_response(None, next(seq)), "usage": {"prompt_tokens": 10, "completion_tokens": 1}}

    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", vote_samples=5, transport=oa_transport(rec, respond))
    d.decide("s", [Question("lib", "yesno", "Library?")], seed=3)
    # the prime and the probe, then seed * 5 + i per vote
    assert [b["seed"] for b in rec] == [3, 3, 15, 16, 17, 18, 19]
    assert d.last_usage == {"prompt_tokens": 50, "completion_tokens": 5}
    assert d.cache_params()["method"] == "auto" and d.last_params["method"] == "vote"


def test_watch_answers_from_the_cache_without_asking_for_memory(tmp_path):
    from test_watch import Memory, _pdf, _watch

    mem = Memory("normal")
    be = FakeBackend(PAPER)
    w, conn, dl, clock, logs = _watch(tmp_path, backend=be, memory=mem)
    _pdf(dl / "a.pdf", "Lecture notes on things")
    w.catch_up()
    assert len(be.prompts) == 1 and mem.calls == 3  # extract + 2 questions, each gated
    conn.execute("UPDATE files SET triage_status='pending'")  # asked to triage again
    mem.level = "critical"
    w.dirty = True
    w.pump()
    assert conn.execute("SELECT triage_status FROM files").fetchone()[0] == "done"
    assert len(be.prompts) == 1 and mem.calls == 3 and w.state != "waiting-memory"
    assert conn.execute("SELECT count(*) FROM assertions WHERE call_id IS NOT NULL").fetchone()[0] > 0


def test_live_apple_honours_the_seed():
    from papyrus.llm.apple import AppleFMBackend

    be = AppleFMBackend()
    if not be.available():
        pytest.skip("Apple Foundation Models unavailable")
    schema = {"type": "object", "properties": {"words": {"type": "array", "items": {"type": "string"}}},
              "required": ["words"]}
    try:
        runs = [be.extract(instructions="You invent words.", prompt="Invent five unusual made-up words.",
                           schema=schema, temperature=1.0, seed=s, max_tokens=60)["words"] for s in (7, 7, 8)]
    except ModelBusy:
        pytest.skip("refused under memory pressure")
    finally:
        be.close()
    assert runs[0] == runs[1] and runs[0] != runs[2]
