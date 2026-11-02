import json
from pathlib import Path

import httpx

from papyrus.match import crossref
from papyrus.http import HttpClient

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def client(handler):
    return HttpClient(cache_ttl=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_parse_journal_article():
    r = crossref.parse_item(load("crossref_hopf.json")["message"])
    assert r.source == "crossref"
    assert r.title == "Hopf algebras and Markov chains: two examples and a theory"
    assert r.ids["doi"] == "10.1007/s10801-013-0456-7"
    assert r.venue == "Journal of Algebraic Combinatorics"
    assert r.kind == "paper"
    assert (r.year, r.date) == (2014, "2014-05")  # print date preferred over online-first
    assert [(a.given, a.family) for a in r.authors] == [("Persi", "Diaconis"), ("C. Y. Amy", "Pang"), ("Arun", "Ram")]
    assert r.citation_count is not None


def test_parse_book_with_subtitle_and_isbn():
    r = crossref.parse_item(load("crossref_isbn.json")["message"]["items"][0])
    assert r.kind == "book"
    assert r.title == "Computer Vision: Algorithms and Applications"
    assert r.ids["isbn"] == "9783030343712"
    assert r.authors[0].family == "Szeliski"


def test_strip_jats():
    s = "<jats:title>Abstract</jats:title><jats:p>We show <jats:italic>that</jats:italic> x.</jats:p>"
    assert crossref._strip_jats(s) == "We show that x."


def test_get_skips_arxiv_dois_and_handles_404():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(404, text="Resource not found.")

    c = client(handler)
    assert crossref.get("10.48550/arXiv.2412.06464", http=c) is None
    assert calls == []
    assert crossref.get("10.1234/missing", http=c) is None
    assert calls == ["/works/10.1234/missing"]


def test_by_isbn_and_search_params(monkeypatch):
    monkeypatch.setenv("PAPYRUS_MAILTO", "me@example.org")
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=load("crossref_isbn.json"))

    c = client(handler)
    res = crossref.by_isbn("978-3-030-34372-9", http=c)
    assert res[0].kind == "book"
    assert seen[-1]["filter"] == "isbn:9783030343729" and seen[-1]["mailto"] == "me@example.org"
    assert crossref.by_isbn("123", http=c) == []
    crossref.search("Computer Vision", ["Richard Szeliski"], year=2022, http=c)
    assert seen[-1]["query.bibliographic"] == "Computer Vision szeliski 2022"
