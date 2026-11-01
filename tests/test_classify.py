"""Tests for papyrus.llm.classify: mocked HTTP plus one live LM Studio test."""

from __future__ import annotations

import json
import math
import os
import time

import httpx
import pytest

from papyrus.llm.base import Answer, BackendUnavailable, Classifier, Question
from papyrus.llm.classify import (
    QUESTION_LEAD,
    QWEN_NO_THINK_PREFILL,
    JevHTTPClassifier,
    OpenAICompatClassifier,
    _render,
    get_classifier,
)

KIND = Question(
    "kind",
    "choice",
    "What kind of document is this?",
    {"paper": "research paper", "book": "a whole book", "slides": "slide deck"},
)
LIB = Question("library", "yesno", "Is this library material?", {"yes": "papers, books", "no": "payslips, CVs"})
QUALITY = Question("quality", "score", "How readable is the text?", {"0": "garbled", "1": "partly", "2": "clean"})
PAPERISH = Question("paperish", "score", "How likely is this a research paper?")


# --------------------------------------------------------------------------- Jev wire


def jev_transport(record: list, answers: dict, status: int = 200, model: str = "jev-1.13.0"):
    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "jev-latest"}]})
        return httpx.Response(
            status, json={"model": model, "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 3}}
        )

    return httpx.MockTransport(handler)


def test_jev_request_wire_format_and_parsing():
    rec: list[httpx.Request] = []
    answers = {
        "kind": {"type": "choice", "choice": "paper", "confidence": 0.8,
                 "probabilities": {"paper": 0.9, "book": 0.08, "slides": 0.02}},
        "library": {"type": "noul", "noul": 0.97},
        "quality": {"type": "score", "score": 1.8, "confidence": 0.7, "legend": {"0": "garbled", "1": "partly", "2": "clean"},
                    "probabilities": {"0": 0.0, "1": 0.2, "2": 0.8}},
        "paperish": {"type": "noul", "noul": 0.4},
    }
    d = JevHTTPClassifier("http://jev.local:8000/v1/", api_key="sk-x", transport=jev_transport(rec, answers))
    assert isinstance(d, Classifier)
    out = d.classify("STATE", [KIND, LIB, QUALITY, PAPERISH])

    req = rec[-1]
    assert req.url == "http://jev.local:8000/v1/systemone"
    assert req.headers["authorization"] == "Bearer sk-x"
    body = json.loads(req.content)
    assert body["model"] == "jev-latest" and body["state"] == "STATE"
    assert body["questions"]["kind"] == {"type": "choice", "instructions": KIND.instructions, "criteria": KIND.criteria}
    assert body["questions"]["library"] == {
        "type": "noul", "instructions": LIB.instructions, "criteria": {"true": "papers, books", "false": "payslips, CVs"}}
    assert body["questions"]["quality"] == {"type": "score", "instructions": QUALITY.instructions,
                                            "criteria": ["garbled", "partly", "clean"]}
    assert body["questions"]["paperish"] == {"type": "noul", "instructions": PAPERISH.instructions}

    assert out["kind"].best == "paper" and out["kind"].probabilities["paper"] == pytest.approx(0.9)
    assert out["library"].probabilities == pytest.approx({"yes": 0.97, "no": 0.03})
    assert out["quality"].probabilities["score"] == pytest.approx(0.9)  # 1.8 / 2
    assert out["paperish"].probabilities == {"score": 0.4}
    assert d.info().tag == "jev:jev-1.13.0"  # served model name wins after a call
    assert d.last_usage == {"input_tokens": 10, "output_tokens": 3}


def test_jev_missing_choice_keys_are_zero_and_renormalised():
    rec: list = []
    answers = {"kind": {"type": "choice", "choice": "book", "confidence": 1, "probabilities": {"book": 0.5, "x": 0.5}}}
    d = JevHTTPClassifier("http://h", transport=jev_transport(rec, answers))
    a = d.classify("s", [KIND])["kind"]
    assert a.probabilities == {"paper": 0.0, "book": 1.0, "slides": 0.0}


def test_jev_errors():
    def down(request):
        raise httpx.ConnectError("refused")

    d = JevHTTPClassifier("http://h", transport=httpx.MockTransport(down))
    assert not d.available()
    with pytest.raises(BackendUnavailable):
        d.classify("s", [LIB])

    rec: list = []
    with pytest.raises(BackendUnavailable):
        JevHTTPClassifier("http://h", transport=jev_transport(rec, {}, status=401)).classify("s", [LIB])
    with pytest.raises(ValueError):
        JevHTTPClassifier("http://h", transport=jev_transport(rec, {}, status=422)).classify("s", [LIB])


def test_jev_retries_on_overload(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(200, json={"model": "m", "answers": {"library": {"type": "noul", "noul": 0.2}}, "usage": {}})

    d = JevHTTPClassifier("http://h", transport=httpx.MockTransport(handler))
    assert d.classify("s", [LIB])["library"].best == "no"
    assert len(calls) == 3


# ------------------------------------------------------------------ OpenAI emulation


def top(pairs: dict[str, float]) -> list[dict]:
    return [{"token": t, "logprob": lp, "bytes": []} for t, lp in pairs.items()]


def chat_response(tops: list[dict] | None, content: str = "A") -> dict:
    lp = None if tops is None else {"content": [{"token": tops[0]["token"], "logprob": tops[0]["logprob"], "top_logprobs": tops}]}
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "logprobs": lp}]}


def oa_transport(record: list, respond):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.5-4b-mlx"}, {"id": "other"}]})
        if "/api/v0/models/" in request.url.path:
            return httpx.Response(200, json={"loaded_context_length": 8192})
        body = json.loads(request.content)
        record.append(body)
        return httpx.Response(200, json=respond(body))

    return httpx.MockTransport(handler)


def is_prime(body: dict) -> bool:
    return body.get("max_tokens") == 1 and not body.get("logprobs")


def questions(rec: list[dict]) -> list[dict]:
    """The recorded requests that ask a question (not the prefix primes)."""
    return [b for b in rec if not is_prime(b)]


def test_openai_choice_logprobs_readout_and_prompt():
    rec: list[dict] = []
    tops = top({"B": math.log(0.6), " B": math.log(0.1), "A": math.log(0.2), "b": math.log(0.05), "The": -3.0})
    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", top_logprobs=5,
                            transport=oa_transport(rec, lambda b: chat_response(tops, "B")))
    assert isinstance(d, Classifier) and d.available()
    a = d.classify("FILE: x.pdf", [KIND])["kind"]
    # B and " B" pool (0.7); "b" ignored for letters; C ("slides") unseen -> floor exp(-3)
    floor = math.exp(-3.0)
    z = 0.7 + 0.2 + floor
    assert a.probabilities == pytest.approx({"paper": 0.2 / z, "book": 0.7 / z, "slides": floor / z})
    assert a.best == "book"

    body = questions(rec)[0]
    assert body["logprobs"] is True and body["top_logprobs"] == 5 and body["temperature"] == 0.0
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"].startswith("STATE:\nFILE: x.pdf")
    assert "A) paper: research paper\nB) book: a whole book\nC) slides: slide deck" in msgs[1]["content"]
    assert msgs[-1] == {"role": "assistant", "content": QWEN_NO_THINK_PREFILL}
    assert d.info().tag == "lmstudio:qwen3.5-4b-mlx" and d.info().context_tokens == 8192


def test_openai_skips_leading_whitespace_token():
    rec: list = []

    def respond(body):
        r = chat_response(top({"Yes": -0.1, "No": -2.4}))
        ws = {"token": "\n\n", "logprob": 0.0, "top_logprobs": top({"\n\n": 0.0, "Yes": -5.0})}
        r["choices"][0]["logprobs"]["content"].insert(0, ws)
        return r

    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", transport=oa_transport(rec, respond))
    a = d.classify("s", [LIB])["library"]
    pyes = math.exp(-0.1) / (math.exp(-0.1) + math.exp(-2.4))
    assert a.probabilities == pytest.approx({"yes": pyes, "no": 1 - pyes})
    assert "Yes means: papers, books\nNo means: payslips, CVs" in questions(rec)[0]["messages"][1]["content"]


def test_openai_yesno_case_insensitive_and_shared_prefix():
    rec: list = []
    tops = top({"yes": math.log(0.5), "Yes": math.log(0.3), " No": math.log(0.2)})
    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", transport=oa_transport(rec, lambda b: chat_response(tops)))
    out = d.classify("same state", [LIB, PAPERISH])
    assert out["library"].probabilities == pytest.approx({"yes": 0.8, "no": 0.2})
    assert out["paperish"].probabilities == pytest.approx({"score": 0.8})
    # every question about one state shares the system + STATE prefix
    q0, q1 = questions(rec)
    p0, p1 = (b["messages"][1]["content"] for b in (q0, q1))
    assert p0.split("---")[0] == p1.split("---")[0]
    assert q0["messages"][0] == q1["messages"][0]


def test_openai_score_with_levels():
    rec: list = []
    tops = top({"2": math.log(0.5), "1": math.log(0.5), "x": -9.0})
    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", transport=oa_transport(rec, lambda b: chat_response(tops)))
    s = d.classify("s", [QUALITY])["quality"].probabilities["score"]
    assert s == pytest.approx(0.75, abs=1e-3)  # expected level 1.5 of 0..2
    assert "0) garbled\n1) partly\n2) clean" in questions(rec)[0]["messages"][1]["content"]


def test_openai_permutations_average_reverse_order():
    rec: list = []

    def respond(body):
        # model always prefers the first letter: position bias cancels out over two orders
        return chat_response(top({"A": math.log(0.9), "B": math.log(0.05), "C": math.log(0.05)}))

    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", permutations=2, transport=oa_transport(rec, respond))
    a = d.classify("s", [KIND])["kind"]
    assert len(questions(rec)) == 2
    assert "A) slides" in questions(rec)[1]["messages"][1]["content"]
    assert a.probabilities["paper"] == pytest.approx(a.probabilities["slides"])
    assert a.probabilities["book"] == pytest.approx(0.05)


def test_openai_temperature_flattens():
    rec: list = []
    tops = top({"Yes": math.log(0.9), "No": math.log(0.1)})
    d = OpenAICompatClassifier(model="m", temperature=2.0, assistant_prefill=None,
                            transport=oa_transport(rec, lambda b: chat_response(tops)))
    p = d.classify("s", [LIB])["library"].probabilities["yes"]
    assert p == pytest.approx(3 / 4)  # sqrt(.9)/(sqrt(.9)+sqrt(.1))
    assert questions(rec)[0]["messages"][-1]["role"] == "user"  # no prefill for non-Qwen3 models


def test_openai_falls_back_to_voting_without_logprobs():
    rec: list = []
    seq = iter(["No", "Yes", "Yes.", "**Yes**", "yes"] * 3)

    def respond(body):
        if body.get("logprobs"):
            return chat_response(None, "Yes")
        return chat_response(None, next(seq))

    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", vote_samples=5, transport=oa_transport(rec, respond))
    a = d.classify("s", [LIB])["library"]
    assert d.method == "vote" and d.info().model == "qwen3.5-4b-mlx#vote"
    assert a.probabilities == pytest.approx({"yes": 4.1 / 5.2, "no": 1.1 / 5.2})
    assert len(questions(rec)) == 6  # one logprob probe + 5 samples
    d.classify("s", [LIB])
    assert len(questions(rec)) == 11  # no more probing once switched
    assert len(rec) == 11  # and no prime: one question per state, nothing to reuse


def test_openai_primes_the_shared_prefix_once_per_state():
    rec: list = []
    tops = top({"Yes": -0.1, "No": -2.4})

    def respond(body):
        r = chat_response(tops)
        r["usage"] = {"prompt_tokens": 1 if is_prime(body) else 10, "completion_tokens": 1}
        return r

    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", transport=oa_transport(rec, respond))
    d.questions_ahead = 2  # the inference cache asks one question per call, and says how many are left
    d.classify("state one", [KIND])
    d.questions_ahead = 1
    d.classify("state one", [LIB])
    assert [is_prime(b) for b in rec] == [True, False, False]
    prime, q0, q1 = rec
    # the prime is the shared prefix: system prompt, then STATE up to where the questions start
    assert prime["messages"][0] == q0["messages"][0]
    assert len(prime["messages"]) == 2 and prime["messages"][1]["content"].endswith("---\n" + QUESTION_LEAD)
    for q in (q0, q1):
        assert q["messages"][1]["content"].startswith(prime["messages"][1]["content"] + " ")
    assert prime["temperature"] == 0.0
    assert d.last_params == {"method": "logprobs", "requests": 1}

    d.questions_ahead = None
    d.classify("state two", [KIND, LIB])  # a new state is primed again, once
    assert [is_prime(b) for b in rec[3:]] == [True, False, False]
    assert d.last_params == {"method": "logprobs", "requests": 3, "primed": True}
    assert d.last_usage == {"prompt_tokens": 21, "completion_tokens": 3}  # the prime counts
    d.classify("state three", [KIND])  # one question: no prime (triage-6 asks only `kind`)
    assert [is_prime(b) for b in rec[6:]] == [False]


def test_openai_prime_is_optional_and_never_fails_a_decision():
    rec: list = []
    tops = top({"Yes": -0.1, "No": -2.4})
    oa = OpenAICompatClassifier("http://box:8000/v1", "m", backend_name="openai",
                             transport=oa_transport(rec, lambda b: chat_response(tops)))
    oa.classify("s", [LIB, PAPERISH])
    assert not any(is_prime(b) for b in rec)  # other servers: off unless asked for

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if is_prime(body):
            return httpx.Response(400, json={"error": "max_tokens too small"})
        return httpx.Response(200, json=chat_response(tops))

    d = OpenAICompatClassifier(model="qwen3.5-4b-mlx", transport=httpx.MockTransport(handler))
    assert d.classify("s", [LIB, PAPERISH])["library"].best == "yes"
    d.classify("t", [LIB, PAPERISH])
    assert [is_prime(b) for b in seen] == [True] + [False] * 4  # refused once, then not tried again


def test_every_rendered_question_starts_with_the_shared_lead():
    for q in (KIND, LIB, QUALITY, PAPERISH):
        assert _render(q).user_tail.startswith(QUESTION_LEAD + " ")


def test_openai_strict_logprobs_raises():
    d = OpenAICompatClassifier(model="m", method="logprobs",
                            transport=oa_transport([], lambda b: chat_response(None)))
    with pytest.raises(BackendUnavailable):
        d.classify("s", [LIB])


def test_openai_unreachable():
    def down(request):
        raise httpx.ConnectError("refused")

    d = OpenAICompatClassifier(model="m", transport=httpx.MockTransport(down))
    assert not d.available()
    with pytest.raises(BackendUnavailable):
        d.classify("s", [LIB])


# ------------------------------------------------------------------------- factory


def test_get_classifier_specs():
    env = {"PAPYRUS_JEV_API_KEY": "k", "PAPYRUS_LMSTUDIO_URL": "http://lm:1234/v1"}
    j = get_classifier("jev:http://localhost:8765", env=env)
    assert isinstance(j, JevHTTPClassifier) and j.base_url == "http://localhost:8765" and j.api_key == "k"
    assert get_classifier("jev", env=env).base_url == "https://api.typesafe.ai"
    lm = get_classifier("lmstudio:qwen3.5-9b-mlx", env=env)
    assert isinstance(lm, OpenAICompatClassifier) and lm.model == "qwen3.5-9b-mlx" and lm.base_url == "http://lm:1234/v1"
    oa = get_classifier("openai:gemma@http://box:8000/v1", env=env)
    assert oa.base_url == "http://box:8000/v1" and oa.model == "gemma" and oa.info().backend == "openai"
    with pytest.raises(ValueError):
        get_classifier("bogus:x", env=env)


def test_get_classifier_auto():
    assert isinstance(get_classifier(None, env={"PAPYRUS_JEV_URL": "http://j:1"}, probe=lambda u: None), JevHTTPClassifier)
    d = get_classifier(None, env={}, probe=lambda u: ["text-embedding", "qwen3.5-9b-mlx", "qwen3.5-4b-mlx"])
    assert d.model == "qwen3.5-4b-mlx"
    d = get_classifier(None, env={"PAPYRUS_CLASSIFY_MODEL": "qwen3.5-9b-mlx"}, probe=lambda u: ["qwen3.5-9b-mlx", "qwen3.5-4b-mlx"])
    assert d.model == "qwen3.5-9b-mlx"
    assert get_classifier(None, env={"PAPYRUS_CLASSIFIER": "jev:http://x"}, probe=lambda u: None).base_url == "http://x"
    started = []
    with pytest.raises(BackendUnavailable):
        get_classifier(None, env={}, probe=lambda u: None, start=lambda u: started.append(u) or False)
    assert started == ["http://localhost:1234/v1"]  # tried to start LM Studio's server first
    with pytest.raises(BackendUnavailable):
        get_classifier(None, env={}, probe=lambda u: ["something-else"], start=lambda u: False)
    # the server was off, starting it worked, and the second probe sees the model
    answers = iter([None, ["qwen3.5-4b-mlx"]])
    d = get_classifier(None, env={}, probe=lambda u: next(answers), start=lambda u: True)
    assert d.model == "qwen3.5-4b-mlx"


# ---------------------------------------------------------------------------- live


def _lmstudio_up() -> bool:
    try:
        return httpx.get(os.environ.get("PAPYRUS_LMSTUDIO_URL", "http://localhost:1234/v1") + "/models", timeout=1.0).is_success
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _lmstudio_up(), reason="LM Studio not reachable")
def test_live_lmstudio_decides():
    d = get_classifier(os.environ.get("PAPYRUS_CLASSIFIER_TEST_SPEC"))
    if not d.available():
        pytest.skip(f"{d.info().tag} not served")
    state = (
        "filename: 2410.01234v2.pdf\npdf metadata: title='Sparse MoE Transformers'; producer='pdfTeX'\n"
        "first page text:\narXiv:2410.01234v2 [cs.LG] 3 Nov 2024\nSparse Mixture-of-Experts Transformers\n"
        "Abstract. We study sparse mixture-of-experts transformers ... 1 Introduction ..."
    )
    out = d.classify(state, [KIND, LIB])
    assert set(out) == {"kind", "library"}
    for a in out.values():
        assert isinstance(a, Answer)
        assert sum(a.probabilities.values()) == pytest.approx(1.0)
    assert out["kind"].best == "paper"
    assert out["library"].best == "yes"


@pytest.mark.skipif(not os.environ.get("PAPYRUS_JEV_URL"), reason="PAPYRUS_JEV_URL not set")
def test_live_jev_endpoint():
    d = JevHTTPClassifier(
        os.environ["PAPYRUS_JEV_URL"],
        api_key=os.environ.get("PAPYRUS_JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY"),
        model=os.environ.get("PAPYRUS_JEV_MODEL", "jev-latest"),
    )
    if not d.available():
        pytest.skip("jev endpoint not reachable")
    out = d.classify("Payslip. Employee: Jane Doe. Period: March. Gross pay 2,000.00. Net pay 1,600.00.", [KIND, LIB, QUALITY])
    assert out["library"].best == "no"
    assert sum(out["kind"].probabilities.values()) == pytest.approx(1.0)
    assert 0.0 <= out["quality"].probabilities["score"] <= 1.0
