import copy
import json
from pathlib import Path

import pytest

from papyrus import db
from papyrus.match import arxiv, candidates, openalex, pipeline
from papyrus.match import semanticscholar as s2
from papyrus.models import WorkRecord

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def gdn_records():
    a = next(r for r in arxiv.parse_feed((FIX / "arxiv_idlist.atom").read_text()) if r.ids["arxiv"] == "2412.06464")
    o = openalex.parse_work(load("openalex_gdn.json"))
    s = s2.parse_paper(load("s2_gdn.json"))
    for r in (a, o, s):
        r.match_score = 0.99
    return [a, o, s]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def add_work(conn, title, year=None, ids=(), authors=()):
    wid = conn.execute("INSERT INTO works(title, year) VALUES (?,?)", (title, year)).lastrowid
    for scheme, value in ids:
        conn.execute(
            "INSERT INTO work_ids(work_id, scheme, value, source) VALUES (?,?,?, 'heuristic:regex')",
            (wid, scheme, value),
        )
    for i, name in enumerate(authors):
        conn.execute(
            "INSERT INTO work_authors(work_id, position, raw_name, source) VALUES (?,?,?, 'triage')", (wid, i, name)
        )
    return wid


@pytest.fixture
def fake_resolve(monkeypatch):
    state = {"records": gdn_records(), "hints": []}

    def resolve(hint, **kw):
        state["hints"].append(hint)
        return copy.deepcopy(state["records"])

    monkeypatch.setattr(candidates, "resolve", resolve)
    return state


def test_build_hint(conn):
    wid = add_work(
        conn,
        "Gated Delta Networks",
        2024,
        [("arxiv", "2412.06464"), ("doi", "10.1234/x1")],
        ["Songlin Yang", "Jan Kautz"],
    )
    h = pipeline.build_hint(conn, wid)
    assert (h.arxiv_id, h.doi, h.isbn, h.title, h.authors, h.year) == (
        "2412.06464",
        "10.1234/x1",
        None,
        "Gated Delta Networks",
        ["Songlin Yang", "Jan Kautz"],
        2024,
    )


def test_enrich_writes_everything(conn, fake_resolve):
    # triage only got two of three authors, one in 'Family, Given' order
    wid = add_work(
        conn,
        "Gated Delta Networks: Improving Mamba2 with Delta Rule",
        2024,
        [("arxiv", "2412.06464")],
        ["Yang, Songlin", "Jan Kautz"],
    )
    res = pipeline.match_work(conn, wid)
    assert res["status"] == "matched"
    assert res["sources"] == ["arxiv", "openalex", "s2"]
    assert fake_resolve["hints"][0].arxiv_id == "2412.06464"

    w = conn.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
    assert w["abstract"].startswith("Linear Transformers")
    assert w["venue"] == "International Conference on Learning Representations"
    assert w["year"] == 2025 and w["kind"] == "paper"

    wids = dict(conn.execute("SELECT scheme, value FROM work_ids WHERE work_id=?", (wid,)).fetchall())
    assert wids["openalex"] == "W4405254248" and wids["dblp"] == "conf/iclr/YangKH25"
    assert wids["s2"] == "968ea1c7f681592ce6b0830eac0b7fda8db71792"
    src = conn.execute("SELECT source FROM work_ids WHERE scheme='openalex'").fetchone()[0]
    assert src == "api:openalex"

    rows = conn.execute(
        "SELECT wa.position, wa.raw_name, p.openalex_id, p.orcid, p.s2_id, wa.affiliation FROM work_authors wa"
        " LEFT JOIN people p ON p.id = wa.person_id WHERE wa.work_id=? ORDER BY position",
        (wid,),
    ).fetchall()
    assert [r["raw_name"] for r in rows] == ["Yang, Songlin", "Jan Kautz", "Ali Hatamizadeh"]  # 3rd appended
    assert rows[0]["openalex_id"] == "A5100548266" and rows[0]["s2_id"] == "2327294933"
    assert rows[1]["orcid"] == "0000-0002-8830-429X"
    assert rows[2]["openalex_id"] == "A5028525720"
    assert res["authors_linked"] == 3

    topics = conn.execute(
        "SELECT t.scheme, t.name, t.level, t.external_id, p.name AS parent FROM work_topics wt"
        " JOIN topics t ON t.id = wt.topic_id LEFT JOIN topics p ON p.id = t.parent_id WHERE wt.work_id=?",
        (wid,),
    ).fetchall()
    arx = {r["name"]: r for r in topics if r["scheme"] == "arxiv"}
    assert arx["cs.CL"]["parent"] == "cs" and arx["cs.CL"]["external_id"] == "cs.CL"
    oa = [r for r in topics if r["scheme"] == "openalex"]
    assert oa and all(r["level"] == "topic" and r["external_id"].startswith("T") for r in oa)
    # full chain domain > field > subfield > topic
    t = oa[0]
    chain = []
    tid = conn.execute("SELECT id FROM topics WHERE scheme='openalex' AND name=?", (t["name"],)).fetchone()[0]
    while tid is not None:
        r = conn.execute("SELECT level, parent_id FROM topics WHERE id=?", (tid,)).fetchone()
        chain.append(r["level"])
        tid = r["parent_id"]
    assert chain == ["topic", "subfield", "field", "domain"]

    claims = db.current_assertions(conn, "work", wid)
    assert {c["source"] for c in claims["title"]} == {"api:arxiv", "api:openalex", "api:s2"}
    assert any(c["source"] == "api:openalex" for c in claims["author_ids"])
    assert claims["venue"][0]["confidence"] == 0.99
    assert "embedding" not in claims

    fts = conn.execute("SELECT rowid FROM works_fts WHERE works_fts MATCH 'hatamizadeh'").fetchall()
    assert [r[0] for r in fts] == [wid]


def test_enrich_is_idempotent_and_reuses_people(conn, fake_resolve):
    wid = add_work(conn, "Gated Delta Networks: Improving Mamba2 with Delta Rule", 2024, [("arxiv", "2412.06464")])
    pipeline.match_work(conn, wid)
    n_people = conn.execute("SELECT count(*) FROM people").fetchone()[0]
    res = pipeline.match_work(conn, wid)
    assert res["status"] == "matched" and not res["possible_duplicates"]
    assert conn.execute("SELECT count(*) FROM people").fetchone()[0] == n_people
    assert conn.execute("SELECT count(*) FROM work_authors WHERE work_id=?", (wid,)).fetchone()[0] == 3
    # live claims are not duplicated by the re-run
    live = conn.execute("SELECT count(*) FROM assertions WHERE field='title' AND superseded_at IS NULL").fetchone()[0]
    assert live == 3


def test_id_owned_by_other_work_is_flagged(conn, fake_resolve):
    other = add_work(conn, "Some other record", None, [("dblp", "conf/iclr/YangKH25")])
    wid = add_work(conn, "Gated Delta Networks: Improving Mamba2 with Delta Rule", 2024, [("arxiv", "2412.06464")])
    res = pipeline.match_work(conn, wid)
    assert res["possible_duplicates"] == [{"other_work_id": other, "scheme": "dblp", "value": "conf/iclr/YangKH25"}]
    assert conn.execute("SELECT work_id FROM work_ids WHERE scheme='dblp'").fetchone()[0] == other
    claims = db.current_assertions(conn, "work", wid)
    assert claims["possible_duplicate"][0]["value"]["other_work_id"] == other


def test_existing_values_kept(conn, fake_resolve):
    wid = add_work(conn, "Gated Delta Networks: Improving Mamba2 with Delta Rule", 2024, [("arxiv", "2412.06464")])
    conn.execute("UPDATE works SET abstract='mine', kind='slides' WHERE id=?", (wid,))
    pipeline.match_work(conn, wid)
    w = conn.execute("SELECT abstract, kind FROM works WHERE id=?", (wid,)).fetchone()
    assert (w["abstract"], w["kind"]) == ("mine", "slides")


def test_below_threshold_is_ambiguous(conn, fake_resolve):
    weak = WorkRecord(
        source="crossref", title="Something similar", year=2020, ids={"doi": "10.1234/abc"}, match_score=0.6
    )
    fake_resolve["records"] = [weak]
    wid = add_work(conn, "Something", 2020)
    res = pipeline.match_work(conn, wid)
    assert res["status"] == "ambiguous" and res["best_score"] == 0.6
    claims = db.current_assertions(conn, "work", wid)
    cands = claims["match_candidates"][0]["value"]
    assert cands[0]["ids"] == {"doi": "10.1234/abc"} and cands[0]["source"] == "crossref"
    assert conn.execute("SELECT count(*) FROM work_ids WHERE work_id=?", (wid,)).fetchone()[0] == 0


def test_nothing_found(conn, fake_resolve):
    fake_resolve["records"] = []
    wid = add_work(conn, "Nothing", None)
    assert pipeline.match_work(conn, wid)["status"] == "not_found"
    empty = conn.execute("INSERT INTO works(title) VALUES (NULL)").lastrowid
    assert pipeline.match_work(conn, empty)["status"] == "not_found"
