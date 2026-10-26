"""Tests for papyrus.llm.decide: mocked HTTP plus one live LM Studio test."""

from __future__ import annotations

import json
import math
import os
import time

import httpx
import pytest

from papyrus.llm.base import Answer, BackendUnavailable, Decider, Question
from papyrus.llm.decide import (
    QWEN_NO_THINK_PREFILL,
    JevHTTPDecider,
    OpenAICompatDecider,
    get_decider,
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
    d = JevHTTPDecider("http://jev.local:8000/v1/", api_key="sk-x", transport=jev_transport(rec, answers))
    assert isinstance(d, Decider)
    out = d.decide("STATE", [KIND, LIB, QUALITY, PAPERISH])

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
    d = JevHTTPDecider("http://h", transport=jev_transport(rec, answers))
    a = d.decide("s", [KIND])["kind"]
    assert a.probabilities == {"paper": 0.0, "book": 1.0, "slides": 0.0}


def test_jev_errors():
    def down(request):
        raise httpx.ConnectError("refused")

    d = JevHTTPDecider("http://h", transport=httpx.MockTransport(down))
    assert not d.available()
    with pytest.raises(BackendUnavailable):
        d.decide("s", [LIB])

    rec: list = []
    with pytest.raises(BackendUnavailable):
        JevHTTPDecider("http://h", transport=jev_transport(rec, {}, status=401)).decide("s", [LIB])
    with pytest.raises(ValueError):
        JevHTTPDecider("http://h", transport=jev_transport(rec, {}, status=422)).decide("s", [LIB])


def test_jev_retries_on_overload(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(200, json={"model": "m", "answers": {"library": {"type": "noul", "noul": 0.2}}, "usage": {}})

    d = JevHTTPDecider("http://h", transport=httpx.MockTransport(handler))
    assert d.decide("s", [LIB])["library"].best == "no"
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


def test_openai_choice_logprobs_readout_and_prompt():
    rec: list[dict] = []
    tops = top({"B": math.log(0.6), " B": math.log(0.1), "A": math.log(0.2), "b": math.log(0.05), "The": -3.0})
    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", top_logprobs=5,
                            transport=oa_transport(rec, lambda b: chat_response(tops, "B")))
    assert isinstance(d, Decider) and d.available()
    a = d.decide("FILE: x.pdf", [KIND])["kind"]
    # B and " B" pool (0.7); "b" ignored for letters; C ("slides") unseen -> floor exp(-3)
    floor = math.exp(-3.0)
    z = 0.7 + 0.2 + floor
    assert a.probabilities == pytest.approx({"paper": 0.2 / z, "book": 0.7 / z, "slides": floor / z})
    assert a.best == "book"

    body = rec[0]
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

    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", transport=oa_transport(rec, respond))
    a = d.decide("s", [LIB])["library"]
    pyes = math.exp(-0.1) / (math.exp(-0.1) + math.exp(-2.4))
    assert a.probabilities == pytest.approx({"yes": pyes, "no": 1 - pyes})
    assert "Yes means: papers, books\nNo means: payslips, CVs" in rec[0]["messages"][1]["content"]


def test_openai_yesno_case_insensitive_and_shared_prefix():
    rec: list = []
    tops = top({"yes": math.log(0.5), "Yes": math.log(0.3), " No": math.log(0.2)})
    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", transport=oa_transport(rec, lambda b: chat_response(tops)))
    out = d.decide("same state", [LIB, PAPERISH])
    assert out["library"].probabilities == pytest.approx({"yes": 0.8, "no": 0.2})
    assert out["paperish"].probabilities == pytest.approx({"score": 0.8})
    # every question about one state shares the system + STATE prefix
    p0, p1 = (b["messages"][1]["content"] for b in rec)
    assert p0.split("---")[0] == p1.split("---")[0]
    assert rec[0]["messages"][0] == rec[1]["messages"][0]


def test_openai_score_with_levels():
    rec: list = []
    tops = top({"2": math.log(0.5), "1": math.log(0.5), "x": -9.0})
    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", transport=oa_transport(rec, lambda b: chat_response(tops)))
    s = d.decide("s", [QUALITY])["quality"].probabilities["score"]
    assert s == pytest.approx(0.75, abs=1e-3)  # expected level 1.5 of 0..2
    assert "0) garbled\n1) partly\n2) clean" in rec[0]["messages"][1]["content"]


def test_openai_permutations_average_reverse_order():
    rec: list = []

    def respond(body):
        # model always prefers the first letter: position bias cancels out over two orders
        return chat_response(top({"A": math.log(0.9), "B": math.log(0.05), "C": math.log(0.05)}))

    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", permutations=2, transport=oa_transport(rec, respond))
    a = d.decide("s", [KIND])["kind"]
    assert len(rec) == 2
    assert "A) slides" in rec[1]["messages"][1]["content"]
    assert a.probabilities["paper"] == pytest.approx(a.probabilities["slides"])
    assert a.probabilities["book"] == pytest.approx(0.05)


def test_openai_temperature_flattens():
    rec: list = []
    tops = top({"Yes": math.log(0.9), "No": math.log(0.1)})
    d = OpenAICompatDecider(model="m", temperature=2.0, assistant_prefill=None,
                            transport=oa_transport(rec, lambda b: chat_response(tops)))
    p = d.decide("s", [LIB])["library"].probabilities["yes"]
    assert p == pytest.approx(3 / 4)  # sqrt(.9)/(sqrt(.9)+sqrt(.1))
    assert rec[0]["messages"][-1]["role"] == "user"  # no prefill for non-Qwen3 models


def test_openai_falls_back_to_voting_without_logprobs():
    rec: list = []
    seq = iter(["No", "Yes", "Yes.", "**Yes**", "yes"] * 3)

    def respond(body):
        if body.get("logprobs"):
            return chat_response(None, "Yes")
        return chat_response(None, next(seq))

    d = OpenAICompatDecider(model="qwen3.5-4b-mlx", vote_samples=5, transport=oa_transport(rec, respond))
    a = d.decide("s", [LIB])["library"]
    assert d.method == "vote" and d.info().model == "qwen3.5-4b-mlx#vote"
    assert a.probabilities == pytest.approx({"yes": 4.1 / 5.2, "no": 1.1 / 5.2})
    assert len(rec) == 6  # one logprob probe + 5 samples
    d.decide("s", [LIB])
    assert len(rec) == 11  # no more probing once switched


def test_openai_strict_logprobs_raises():
    d = OpenAICompatDecider(model="m", method="logprobs",
                            transport=oa_transport([], lambda b: chat_response(None)))
    with pytest.raises(BackendUnavailable):
        d.decide("s", [LIB])


def test_openai_unreachable():
    def down(request):
        raise httpx.ConnectError("refused")

    d = OpenAICompatDecider(model="m", transport=httpx.MockTransport(down))
    assert not d.available()
    with pytest.raises(BackendUnavailable):
        d.decide("s", [LIB])


# ------------------------------------------------------------------------- factory


def test_get_decider_specs():
    env = {"PAPYRUS_JEV_API_KEY": "k", "PAPYRUS_LMSTUDIO_URL": "http://lm:1234/v1"}
    j = get_decider("jev:http://localhost:8765", env=env)
    assert isinstance(j, JevHTTPDecider) and j.base_url == "http://localhost:8765" and j.api_key == "k"
    assert get_decider("jev", env=env).base_url == "https://api.typesafe.ai"
    lm = get_decider("lmstudio:qwen3.5-9b-mlx", env=env)
    assert isinstance(lm, OpenAICompatDecider) and lm.model == "qwen3.5-9b-mlx" and lm.base_url == "http://lm:1234/v1"
    oa = get_decider("openai:gemma@http://box:8000/v1", env=env)
    assert oa.base_url == "http://box:8000/v1" and oa.model == "gemma" and oa.info().backend == "openai"
    with pytest.raises(ValueError):
        get_decider("bogus:x", env=env)


def test_get_decider_auto():
    assert isinstance(get_decider(None, env={"PAPYRUS_JEV_URL": "http://j:1"}, probe=lambda u: None), JevHTTPDecider)
    d = get_decider(None, env={}, probe=lambda u: ["text-embedding", "qwen3.5-9b-mlx", "qwen3.5-4b-mlx"])
    assert d.model == "qwen3.5-4b-mlx"
    d = get_decider(None, env={"PAPYRUS_DECIDE_MODEL": "qwen3.5-9b-mlx"}, probe=lambda u: ["qwen3.5-9b-mlx", "qwen3.5-4b-mlx"])
    assert d.model == "qwen3.5-9b-mlx"
    assert get_decider(None, env={"PAPYRUS_DECIDER": "jev:http://x"}, probe=lambda u: None).base_url == "http://x"
    with pytest.raises(BackendUnavailable):
        get_decider(None, env={}, probe=lambda u: None)
    with pytest.raises(BackendUnavailable):
        get_decider(None, env={}, probe=lambda u: ["something-else"])


# ---------------------------------------------------------------------------- live


def _lmstudio_up() -> bool:
    try:
        return httpx.get(os.environ.get("PAPYRUS_LMSTUDIO_URL", "http://localhost:1234/v1") + "/models", timeout=1.0).is_success
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _lmstudio_up(), reason="LM Studio not reachable")
def test_live_lmstudio_decides():
    d = get_decider(os.environ.get("PAPYRUS_DECIDER_TEST_SPEC"))
    if not d.available():
        pytest.skip(f"{d.info().tag} not served")
    state = (
        "filename: 2410.01234v2.pdf\npdf metadata: title='Sparse MoE Transformers'; producer='pdfTeX'\n"
        "first page text:\narXiv:2410.01234v2 [cs.LG] 3 Nov 2024\nSparse Mixture-of-Experts Transformers\n"
        "Abstract. We study sparse mixture-of-experts transformers ... 1 Introduction ..."
    )
    out = d.decide(state, [KIND, LIB])
    assert set(out) == {"kind", "library"}
    for a in out.values():
        assert isinstance(a, Answer)
        assert sum(a.probabilities.values()) == pytest.approx(1.0)
    assert out["kind"].best == "paper"
    assert out["library"].best == "yes"


@pytest.mark.skipif(not os.environ.get("PAPYRUS_JEV_URL"), reason="PAPYRUS_JEV_URL not set")
def test_live_jev_endpoint():
    d = JevHTTPDecider(
        os.environ["PAPYRUS_JEV_URL"],
        api_key=os.environ.get("PAPYRUS_JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY"),
        model=os.environ.get("PAPYRUS_JEV_MODEL", "jev-latest"),
    )
    if not d.available():
        pytest.skip("jev endpoint not reachable")
    out = d.decide("Payslip. Employee: Jane Doe. Period: March. Gross pay 2,000.00. Net pay 1,600.00.", [KIND, LIB, QUALITY])
    assert out["library"].best == "no"
    assert sum(out["kind"].probabilities.values()) == pytest.approx(1.0)
    assert 0.0 <= out["quality"].probabilities["score"] <= 1.0
