import json
from pathlib import Path

import httpx
import pytest

from papyrus.enrich import openalex
from papyrus.enrich.http import HttpClient

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def client(handler):
    return HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None)


@pytest.fixture(autouse=True)
def reset_key(monkeypatch):
    monkeypatch.setattr(openalex, "_key_disabled", False)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("PAPYRUS_MAILTO", raising=False)


def test_reconstruct_abstract():
    inv = {"Hello": [0], "world": [1, 3], "again": [2]}
    assert openalex.reconstruct_abstract(inv) == "Hello world again world"
    assert openalex.reconstruct_abstract(None) is None
    assert openalex.reconstruct_abstract({}) is None


def test_parse_arxiv_preprint():
    r = openalex.parse_work(load("openalex_gdn.json"))
    assert r.source == "openalex"
    # the arXiv DataCite DOI becomes the arxiv id, not 'doi'
    assert r.ids == {"openalex": "W4405254248", "arxiv": "2412.06464"}
    assert r.venue is None  # arXiv repository is not a venue
    assert r.kind == "paper"
    assert r.abstract.startswith("Linear Transformers have gained attention")
    a = r.authors[1]
    assert (a.name, a.openalex_id, a.orcid) == ("Jan Kautz", "A5056503617", "0000-0002-8830-429X")
    assert a.affiliations  # institution display names
    t = next(t for t in r.topics if t["scheme"] == "openalex")
    assert set(t) >= {"name", "id", "score", "subfield", "field", "domain"}
    assert t["id"].startswith("T") and t["field"] == "Computer Science"


def test_parse_published_journal_article():
    r = openalex.parse_work(load("openalex_hopf.json"))
    assert r.ids["doi"] == "10.1007/s10801-013-0456-7"
    assert r.ids["arxiv"] == "1206.3620"  # from the arxiv.org location
    assert r.venue == "Journal of Algebraic Combinatorics"
    assert [a.openalex_id for a in r.authors] == ["A5050449040", "A5107747320", "A5001599475"]
    assert r.authors[0].orcid == "0000-0003-0837-6662"
    assert r.citation_count and r.year == 2013


def test_get_by_arxiv_uses_free_singleton_and_bearer_key(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k123")
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("authorization")
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=load("openalex_gdn.json"))

    r = openalex.get_by_arxiv("arXiv:2412.06464v3", http=client(handler))
    assert r.ids["openalex"] == "W4405254248"
    assert seen["path"] == "/works/doi:10.48550/arxiv.2412.06464"
    assert seen["auth"] == "Bearer k123"
    assert "api_key" not in seen["params"]


def test_rejected_key_falls_back_to_keyless(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "pending")
    auths = []

    def handler(req):
        auths.append(req.headers.get("authorization"))
        if req.headers.get("authorization"):
            return httpx.Response(403, json={"error": "key_not_activated"})
        return httpx.Response(200, json=load("openalex_hopf.json"))

    r = openalex.get_by_doi("10.1007/s10801-013-0456-7", http=client(handler))
    assert r is not None and auths == ["Bearer pending", None]
    assert openalex.api_key() is None  # disabled for the rest of the process


def test_not_found_is_none():
    assert openalex.get_by_doi("10.1234/nope", http=client(lambda req: httpx.Response(404, json={}))) is None


def test_search_filter_is_sanitised():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={"results": [load("openalex_hopf.json")]})

    res = openalex.search("Hopf algebras, and Markov chains: two examples|a theory", year=2013, http=client(handler))
    assert len(res) == 1
    assert seen["filter"] == (
        "title.search:hopf algebras and markov chains two examples a theory,"
        "from_publication_date:2012-01-01,to_publication_date:2014-12-31"
    )
