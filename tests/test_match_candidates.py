import copy
import json
from pathlib import Path

import pytest

from papyrus.match import arxiv, crossref, candidates, openalex
from papyrus.match import semanticscholar as s2
from papyrus.http import HttpError
from papyrus.models import AuthorRecord, WorkRecord

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def arxiv_rec(aid):
    recs = arxiv.parse_feed((FIX / "arxiv_idlist.atom").read_text()) + arxiv.parse_feed(
        (FIX / "arxiv_search.atom").read_text()
    )
    return next(r for r in recs if r.ids["arxiv"] == aid)


def oa_gdn():
    return openalex.parse_work(load("openalex_gdn.json"))


def oa_hopf():
    return openalex.parse_work(load("openalex_hopf.json"))


def s2_gdn():
    return s2.parse_paper(load("s2_gdn.json"))


def s2_hopf():
    return s2.parse_paper(load("s2_hopf.json"))


def cr_hopf():
    return crossref.parse_item(load("crossref_hopf.json")["message"])


class Calls:
    def __init__(self, monkeypatch, **impls):
        self.log = []
        targets = {
            "arxiv.get": (arxiv, "get"),
            "arxiv.search": (arxiv, "search"),
            "openalex.get_by_arxiv": (openalex, "get_by_arxiv"),
            "openalex.get_by_doi": (openalex, "get_by_doi"),
            "openalex.search": (openalex, "search"),
            "s2.get": (s2, "get"),
            "s2.match_title": (s2, "match_title"),
            "crossref.get": (crossref, "get"),
            "crossref.search": (crossref, "search"),
            "crossref.by_isbn": (crossref, "by_isbn"),
        }
        for name, (mod, attr) in targets.items():
            impl = impls.get(name.replace(".", "_"))
            monkeypatch.setattr(mod, attr, self._wrap(name, impl))

    def _wrap(self, name, impl):
        def f(*a, **kw):
            self.log.append((name, a))
            if impl is None:
                return [] if name.endswith(("search", "by_isbn")) else None
            out = impl(*a, **kw)
            return copy.deepcopy(out)

        f.__name__ = name
        return f

    def names(self):
        return [n for n, _ in self.log]


# --------------------------------------------------------------------------- scoring


@pytest.mark.parametrize(
    "a,b,lo,hi",
    [
        (
            "Gated Delta Networks: Improving Mamba2 with Delta Rule",
            "Gated delta networks improving mamba2 with delta rule",
            1,
            1,
        ),
        (
            "Transformers are SSMs",
            "Transformers are SSMs: Generalized Models and Efficient Algorithms Through Structured State Space Duality",
            0.88,
            0.95,
        ),
        (
            "Artificial Intelligence: A Modern Approach 4th edition",
            "Artificial Intelligence A Modern Approach (4th Edition)",
            1,
            1,
        ),
        (
            "Artificial Intelligence: A Modern Approach",
            "Artificial Intelligence: A Modern Approach, Global Edition",
            1,
            1,
        ),
        ("Hopf algebras and Markov chains", "Stein's method for Markov chains: first examples", 0, 0.6),
        (
            "Lossless Data Compression with Neural Networks",
            "DeepZip: Lossless Data Compression Using Recurrent Neural Networks",
            0.6,
            0.8,
        ),
        ("", "anything", 0, 0),
    ],
)
def test_title_similarity(a, b, lo, hi):
    assert lo <= candidates.title_similarity(a, b) <= hi


def test_score_signals():
    rec = arxiv_rec("1206.3620")
    full = candidates.Hint(title=rec.title, authors=["Persi Diaconis", "Amy Pang", "Arun Ram"], year=2012)
    assert candidates.score(full, rec) == 1.0
    assert candidates.score(candidates.Hint(title=rec.title, authors=["Smith"], year=2012), rec) < 0.85
    assert 0.9 < candidates.score(candidates.Hint(title=rec.title, year=2013), rec) < 1
    # authors and year cannot rescue a different title
    wrong = candidates.Hint(title="Stein's method for Markov chains", authors=["Diaconis"], year=2012)
    assert candidates.score(wrong, rec) < 0.6
    assert candidates.author_overlap(["P. Diaconis", "Ram"], rec) == 1.0
    assert candidates.author_overlap([], rec) is None
    assert candidates.year_score(2024, 2025) == 0.8


def test_hint_of_mapping():
    h = candidates.Hint.of({"title": "x", "authors": "Tri Dao and Albert Gu", "year": "2024", "arxiv": "2405.21060"})
    assert h.authors == ["Tri Dao", "Albert Gu"] and h.year == 2024 and h.arxiv_id == "2405.21060"


# --------------------------------------------------------------------------- resolve


def test_resolve_arxiv_id_route(monkeypatch):
    calls = Calls(
        monkeypatch,
        arxiv_get=lambda aid, **kw: arxiv_rec("2412.06464"),
        openalex_get_by_arxiv=lambda aid, **kw: oa_gdn(),
        s2_get=lambda pid, **kw: s2_gdn(),
    )
    recs = candidates.resolve({"arxiv_id": "arXiv:2412.06464v3"})
    assert [r.source for r in recs] == ["arxiv", "openalex", "s2"]
    assert all(r.match_score >= 0.95 for r in recs)
    assert ("s2.get", ("arXiv:2412.06464",)) in calls.log
    # no published DOI known -> one OpenAlex title search for a published version
    assert calls.names().count("openalex.search") == 1
    assert "crossref.search" not in calls.names() and "arxiv.search" not in calls.names()

    m = candidates.merge(candidates.cluster(recs)[0])
    assert m.source == "merged"
    assert m.title == "Gated Delta Networks: Improving Mamba2 with Delta Rule"
    assert m.venue == "International Conference on Learning Representations"
    assert m.year == 2025  # venue year from the DBLP key, not S2's preprint year
    assert (
        m.ids["arxiv"] == "2412.06464" and m.ids["openalex"] == "W4405254248" and m.ids["dblp"] == "conf/iclr/YangKH25"
    )
    assert m.arxiv_version == "v3" and m.arxiv_categories[0] == "cs.CL"
    assert m.abstract.startswith("Linear Transformers")  # arXiv abstract preferred
    kautz = m.authors[1]
    assert kautz.name == "Jan Kautz" and kautz.openalex_id == "A5056503617"
    assert kautz.orcid == "0000-0002-8830-429X" and kautz.s2_id == "2248128248"
    assert m.authors[0].s2_id == "2327294933"  # 'Song-Lin Yang' aligned with 'Songlin Yang'
    assert set(m.raw["sources"]) == {"arxiv", "openalex", "s2"}
    assert m.raw["tldr"].startswith("This work introduces")


def test_resolve_flags_corrupt_id_record(monkeypatch):
    bad = oa_gdn()
    bad.title = "Sovereign Byte Firewall: Comparing Transformer and Mamba-2 Backbones"
    Calls(
        monkeypatch,
        arxiv_get=lambda aid, **kw: arxiv_rec("2412.06464"),
        openalex_get_by_arxiv=lambda aid, **kw: bad,
    )
    recs = candidates.resolve({"arxiv_id": "2412.06464"})
    by = {r.source: r for r in recs}
    assert by["arxiv"].match_score == 1.0
    assert by["openalex"].match_score < 0.5
    assert recs[0].source == "arxiv"
    clusters = candidates.cluster(recs)
    assert len(clusters) == 2  # shared arXiv id but different title -> not merged


def test_resolve_doi_route_follows_arxiv(monkeypatch):
    calls = Calls(
        monkeypatch,
        crossref_get=lambda d, **kw: cr_hopf(),
        openalex_get_by_doi=lambda d, **kw: oa_hopf(),
        arxiv_get=lambda aid, **kw: arxiv_rec("1206.3620"),
        openalex_get_by_arxiv=lambda aid, **kw: oa_hopf(),
        s2_get=lambda pid, **kw: s2_hopf(),
    )
    recs = candidates.resolve({"doi": "https://doi.org/10.1007/s10801-013-0456-7"})
    assert {r.source for r in recs} == {"crossref", "openalex", "arxiv", "s2"}
    assert all(r.match_score >= 0.95 for r in recs)
    assert ("arxiv.get", ("1206.3620",)) in calls.log
    # published version already known from OpenAlex -> no paid title search
    assert "openalex.search" not in calls.names()


def test_resolve_title_route_and_merge(monkeypatch):
    other = WorkRecord(
        source="crossref",
        title="Stein's method for Markov chains: first examples",
        year=2004,
        ids={"doi": "10.1214/lnms/1196283798"},
        authors=[AuthorRecord("Persi Diaconis")],
    )
    Calls(
        monkeypatch,
        arxiv_search=lambda t, a, **kw: [arxiv_rec("1206.3620")],
        openalex_search=lambda t, **kw: [oa_hopf()],
        s2_match_title=lambda t, **kw: s2_hopf(),
        crossref_search=lambda t, a, **kw: [cr_hopf(), other],
    )
    hint = {
        "title": "Hopf algebras and Markov chains: two examples and a theory",
        "authors": ["Diaconis", "Pang", "Ram"],
    }
    recs = candidates.resolve(hint)
    assert recs[-1].ids.get("doi") == "10.1214/lnms/1196283798" and recs[-1].match_score < 0.6
    good = [r for r in recs if r.match_score >= 0.9]
    assert {r.source for r in good} == {"arxiv", "openalex", "s2", "crossref"}
    clusters = candidates.cluster(good)
    assert len(clusters) == 1
    m = candidates.merge(clusters[0])
    assert m.ids["doi"] == "10.1007/s10801-013-0456-7" and m.ids["arxiv"] == "1206.3620"
    assert m.venue == "Journal of Algebraic Combinatorics"
    assert m.year == 2014  # published (Crossref print) date
    assert m.raw["first_seen"].startswith("2012")
    assert m.abstract.startswith("The operation of squaring")
    assert [a.family for a in m.authors] == ["Diaconis", "Pang", "Ram"]
    assert m.authors[0].orcid == "0000-0003-0837-6662" and m.authors[0].openalex_id == "A5050449040"
    assert m.authors[0].s2_id == "2796350" and m.authors[0].given == "Persi"
    assert any(t["scheme"] == "openalex" for t in m.topics)
    assert m.citation_count == max(r.citation_count or 0 for r in good)

    best = candidates.resolve_best(hint)
    assert best.ids["doi"] == "10.1007/s10801-013-0456-7"


def test_resolve_survives_source_errors(monkeypatch):
    def boom(*a, **kw):
        raise HttpError(500, "https://api.openalex.org/works", "down")

    Calls(
        monkeypatch,
        arxiv_search=lambda t, a, **kw: [arxiv_rec("1206.3620")],
        openalex_search=boom,
    )
    recs = candidates.resolve({"title": "Hopf algebras and Markov chains: two examples and a theory"})
    assert [r.source for r in recs] == ["arxiv"]


def test_resolve_isbn_route(monkeypatch):
    book = crossref.parse_item(load("crossref_isbn.json")["message"]["items"][0])
    Calls(monkeypatch, crossref_by_isbn=lambda i, **kw: [book])
    recs = candidates.resolve(
        {"isbn": "978-3-030-34372-9", "title": "Computer Vision: Algorithms and Applications 2nd edition"}
    )
    assert recs[0].source == "crossref" and recs[0].kind == "book" and recs[0].match_score >= 0.95


def test_resolve_restricted_sources(monkeypatch):
    calls = Calls(monkeypatch, arxiv_get=lambda aid, **kw: arxiv_rec("2412.06464"))
    recs = candidates.resolve({"arxiv_id": "2412.06464"}, sources=("arxiv",))
    assert [r.source for r in recs] == ["arxiv"]
    assert set(calls.names()) == {"arxiv.get"}


def test_merge_requires_records():
    with pytest.raises(ValueError):
        candidates.merge([])
