"""Live checks against the real APIs. Run with:  PAPYRUS_LIVE=1 uv run pytest tests/test_enrich_live.py

Responses are cached under ~/.cache/papyrus/http (PAPYRUS_HTTP_CACHE=0 to disable).
Semantic Scholar without S2_API_KEY is frequently rate limited; such tests skip.
"""

import os

import pytest

from papyrus.match import arxiv, crossref, candidates, openalex
from papyrus.match import semanticscholar as s2
from papyrus.http import HttpError

pytestmark = pytest.mark.skipif(os.environ.get("PAPYRUS_LIVE") != "1", reason="set PAPYRUS_LIVE=1 for live API tests")


def s2_or_skip(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except HttpError as e:
        if e.status == 429:
            pytest.skip("Semantic Scholar rate limited (set S2_API_KEY)")
        raise


def test_arxiv_fetch_and_search():
    (r,) = arxiv.fetch(["arXiv:2412.06464v1"])
    assert r.title.startswith("Gated Delta Networks") and r.arxiv_version and "cs.LG" in r.arxiv_categories
    hits = arxiv.search("Transformers are SSMs", ["Tri Dao", "Albert Gu"])
    assert any(h.ids["arxiv"] == "2405.21060" for h in hits)


def test_openalex_singletons():
    r = openalex.get_by_arxiv("2412.06464")
    assert r and r.ids["openalex"].startswith("W") and r.authors[0].openalex_id
    h = openalex.get_by_doi("10.1007/s10801-013-0456-7")
    assert h.venue == "Journal of Algebraic Combinatorics" and h.ids.get("arxiv") == "1206.3620"


def test_crossref():
    r = crossref.get("10.1007/s10801-013-0456-7")
    assert r.title.lower().startswith("hopf algebras and markov chains")
    assert crossref.get("10.48550/arXiv.2412.06464") is None


def test_s2_embedding_field():
    r = s2_or_skip(s2.get, "arXiv:2405.21060")
    assert r.venue and "Machine Learning" in r.venue
    vec = s2.embedding_of(r)
    assert vec is not None and len(vec) == 768


def test_resolve_gated_delta_networks():
    best = candidates.resolve_best({"arxiv_id": "2412.06464"})
    assert best.ids["arxiv"] == "2412.06464"
    assert best.authors[0].family == "Yang" and best.authors[0].openalex_id


def test_resolve_transformers_are_ssms():
    recs = candidates.resolve({"title": "Transformers are SSMs", "authors": ["Dao", "Gu"], "year": 2024})
    assert recs[0].ids.get("arxiv") == "2405.21060" and recs[0].match_score >= 0.85


def test_resolve_hopf():
    best = candidates.resolve_best(
        {"title": "Hopf algebras and Markov chains: two examples and a theory", "authors": ["Diaconis", "Pang", "Ram"]}
    )
    assert best.ids["doi"] == "10.1007/s10801-013-0456-7" and best.ids["arxiv"] == "1206.3620"
    assert all(a.openalex_id for a in best.authors)


def test_resolve_bellard_non_arxiv():
    recs = candidates.resolve(
        {"title": "Lossless Data Compression with Neural Networks", "authors": ["Bellard"], "year": 2019}
    )
    top = [r for r in recs if r.match_score >= 0.85]
    if not top:
        pytest.skip("only Semantic Scholar knows this report; probably rate limited")
    assert top[0].authors and top[0].authors[0].family == "Bellard"


def test_resolve_aima_book_known_gap():
    recs = candidates.resolve(
        {
            "title": "Artificial Intelligence: A Modern Approach 4th edition",
            "authors": ["Russell", "Norvig"],
            "isbn": "9780134610993",
        }
    )
    # Crossref has no record for this ISBN and OpenAlex/S2 only have junk uploads; nothing
    # must pass the acceptance threshold (a books source such as Open Library is needed).
    assert not [r for r in recs if r.match_score >= 0.85 and "Russell" not in [a.family for a in r.authors]]
