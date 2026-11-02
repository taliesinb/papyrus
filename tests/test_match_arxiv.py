from pathlib import Path

import httpx

from papyrus.match import arxiv
from papyrus.http import HttpClient

FIX = Path(__file__).parent / "fixtures"


def client(handler):
    return HttpClient(cache_ttl=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_parse_idlist_feed():
    recs = arxiv.parse_feed((FIX / "arxiv_idlist.atom").read_text())
    by_id = {r.ids["arxiv"]: r for r in recs}
    gdn = by_id["2412.06464"]
    assert gdn.source == "arxiv"
    assert gdn.title == "Gated Delta Networks: Improving Mamba2 with Delta Rule"
    assert [a.name for a in gdn.authors] == ["Songlin Yang", "Jan Kautz", "Ali Hatamizadeh"]
    assert gdn.authors[0].family == "Yang" and gdn.authors[0].given == "Songlin"
    assert gdn.arxiv_version == "v3"
    assert gdn.arxiv_categories[0] == "cs.CL"  # primary first
    assert "cs.LG" in gdn.arxiv_categories
    assert gdn.date == "2024-12-09" and gdn.year == 2024
    assert gdn.abstract.startswith("Linear Transformers")
    assert "https://arxiv.org/abs/2412.06464" in gdn.urls
    assert any(u.startswith("https://arxiv.org/pdf/2412.06464") for u in gdn.urls)
    assert gdn.raw["comment"]
    assert gdn.kind == "paper"
    old = by_id["math/0601001"]
    assert old.title == "Codepth Two and Related Topics"
    assert old.arxiv_version == "v3"


def test_parse_search_with_doi_and_journal_ref():
    (r,) = arxiv.parse_feed((FIX / "arxiv_search.atom").read_text())
    assert r.ids == {"arxiv": "1206.3620", "doi": "10.1007/s10801-013-0456-7"}
    assert [a.family for a in r.authors] == ["Diaconis", "Pang", "Ram"]
    assert r.arxiv_categories[:1] == ["math.CO"]


def test_error_feed_is_empty():
    assert arxiv.parse_feed((FIX / "arxiv_error.atom").read_text()) == []


def test_fetch_batches_and_orders():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, text=(FIX / "arxiv_idlist.atom").read_text())

    recs = arxiv.fetch(["arXiv:2412.06464v2", "https://arxiv.org/abs/math/0601001", "junk"], http=client(handler))
    assert [r.ids["arxiv"] for r in recs] == ["2412.06464", "math/0601001"]
    assert seen[0]["id_list"] == "2412.06464,math/0601001"  # versions dropped -> latest version


def test_build_query():
    q = arxiv.build_query(
        "Hopf algebras and Markov chains: two examples and a theory", ["Persi Diaconis", "C. Y. Amy Pang"]
    )
    assert (
        q
        == "ti:hopf AND ti:algebras AND ti:markov AND ti:chains AND ti:two AND ti:examples AND ti:theory AND au:diaconis AND au:pang"
    )
    assert arxiv.build_query("", []) == ""


def test_search_falls_back_to_title_only():
    queries = []
    empty = (FIX / "arxiv_error.atom").read_text()
    hit = (FIX / "arxiv_search.atom").read_text()

    def handler(req):
        q = req.url.params["search_query"]
        queries.append(q)
        return httpx.Response(200, text=empty if "au:" in q else hit)

    recs = arxiv.search("Hopf algebras and Markov chains", ["Nobody Wrong"], http=client(handler))
    assert len(recs) == 1 and len(queries) == 2
    assert "au:wrong" in queries[0] and "au:" not in queries[1]


def test_a_refusal_or_timeout_leaves_arxiv_alone_for_a_while(monkeypatch):
    import pytest

    from papyrus import services
    from papyrus.http import Cooling, HttpError

    retries = services.effective_policy("arxiv").max_retries
    calls = []

    def refuse(req):
        calls.append(1)
        return httpx.Response(429, text="Rate exceeded.")

    with pytest.raises(HttpError) as e:
        arxiv.get("2412.06464", http=client(refuse))
    assert e.value.status == 429 and len(calls) == 1 + retries
    with pytest.raises(Cooling) as c:  # not sent, by another client too: the pause is the machine's
        arxiv.search("Anything at all", http=client(refuse))
    assert len(calls) == 1 + retries and c.value.service == "arxiv" and c.value.reason == "HTTP 429"
    assert services.store().resume("arxiv") == 1  # papyrus services resume arxiv

    def timeout(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(HttpError) as e:
        arxiv.get("2412.06464", http=client(timeout))
    assert e.value.status == 0
    with pytest.raises(Cooling) as c:
        arxiv.get("2412.06464", http=client(timeout))
    assert c.value.reason == "no answer (ReadTimeout)"
    st = services.store()
    counts = dict(st.conn.execute("SELECT status, count(*) FROM calls GROUP BY status").fetchall())
    assert counts == {"http_error": 1 + retries, "no_answer": 1 + retries, "paused": 2}


def test_patience_covers_timeouts_too():
    import pytest

    from papyrus import http as http_mod

    calls = []

    def timeout(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    c = HttpClient(cache_ttl=0, transport=httpx.MockTransport(timeout), sleep=lambda s: None, max_retries=5)
    with http_mod.patience(0.1), pytest.raises(http_mod.HttpError) as e:  # the first backoff (~1 s) is over budget
        c.get("https://example.org/x")
    assert e.value.status == 0 and len(calls) == 1
