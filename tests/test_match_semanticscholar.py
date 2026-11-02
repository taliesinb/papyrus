import json
from pathlib import Path

import httpx
import pytest

from papyrus.match import semanticscholar as s2
from papyrus.http import HttpClient, HttpError

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def client(handler):
    return HttpClient(cache_ttl=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)


@pytest.mark.parametrize(
    "s,expected",
    [
        ("2412.06464", "arXiv:2412.06464"),
        ("arXiv:2412.06464v3", "arXiv:2412.06464"),
        ("10.48550/arXiv.2412.06464", "arXiv:2412.06464"),
        ("https://doi.org/10.1007/s10801-013-0456-7", "DOI:10.1007/s10801-013-0456-7"),
        ("DOI:10.1007/S10801-013-0456-7", "DOI:10.1007/s10801-013-0456-7"),
        ("CorpusId:689286", "CorpusId:689286"),
        ("7ee76cc2109ce7842a5c768f60bdc0ece61fbdfb", "7ee76cc2109ce7842a5c768f60bdc0ece61fbdfb"),
        ("not an id", None),
    ],
)
def test_to_paper_id(s, expected):
    assert s2.to_paper_id(s) == expected


def test_parse_with_embedding():
    r = s2.parse_paper(load("s2_hopf.json"))
    assert r.source == "s2"
    assert r.ids["arxiv"] == "1206.3620" and r.ids["doi"] == "10.1007/s10801-013-0456-7"
    assert r.ids["s2"] == "7ee76cc2109ce7842a5c768f60bdc0ece61fbdfb"
    assert r.venue == "Journal of Algebraic Combinatorics"
    assert [a.s2_id for a in r.authors] == ["2796350", None, "143795757"]
    assert r.abstract.startswith("The operation of squaring")
    assert r.raw["embedding"]["model"] == "specter_v2"
    assert len(s2.embedding_of(r)) == 8  # fixture truncated from 768
    assert {"name": "Mathematics", "scheme": "s2-fos", "source": "external"} in r.topics


def test_parse_venue_year_from_dblp_and_tldr():
    r = s2.parse_paper(load("s2_gdn.json"))
    assert r.venue == "International Conference on Learning Representations"
    assert r.ids["dblp"] == "conf/iclr/YangKH25"
    assert "doi" not in r.ids  # arXiv DataCite DOI is not a publisher DOI
    assert r.year == 2024 and r.raw["venue_year"] == 2025
    assert r.raw["tldr"].startswith("This work introduces the gated delta rule")


def test_corr_dblp_key_gives_no_venue_year():
    p = {"paperId": "x", "title": "t", "externalIds": {"DBLP": "journals/corr/abs-2412-06464"}}
    assert "venue_year" not in s2.parse_paper(p).raw


def test_get_sends_key_and_fields(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "abc")
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("x-api-key")
        seen["fields"] = req.url.params["fields"]
        seen["path"] = req.url.path
        return httpx.Response(200, json=load("s2_hopf.json"))

    r = s2.get("1206.3620", http=client(handler))
    assert r.title.startswith("Hopf algebras")
    assert seen["key"] == "abc"
    assert seen["path"].endswith("/paper/arXiv:1206.3620")
    assert "embedding.specter_v2" in seen["fields"] and "tldr" in seen["fields"]


def test_batch_aligned_with_input():
    def handler(req):
        body = json.loads(req.content)
        assert body == {"ids": ["arXiv:1206.3620", "DOI:10.1234/missing"]}
        return httpx.Response(200, json=[load("s2_hopf.json"), None])

    out = s2.batch(["1206.3620", "nonsense", "10.1234/missing"], http=client(handler))
    assert out[0].ids["arxiv"] == "1206.3620" and out[1] is None and out[2] is None


def test_match_title_404_is_none():
    def handler(req):
        return httpx.Response(404, json={"error": "Title match not found"})

    assert s2.match_title("no such paper", http=client(handler)) is None


def test_429_triggers_cooldown():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"message": "Too Many Requests"})

    c = client(handler)
    with pytest.raises(HttpError):
        s2.get("1206.3620", http=c)
    n = len(calls)
    assert n == 5  # 1 + 4 retries without a key
    with pytest.raises(s2.S2RateLimited):
        s2.get("1206.3620", http=c)
    assert len(calls) == n  # skipped without a request
