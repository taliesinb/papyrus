import json
from collections import Counter

import httpx
from click.testing import CliRunner

from papyrus import db, download as dl, store
from papyrus.cli import main
from papyrus.enrich import http as h
from papyrus.triage import build_state, detect_ids

from test_core import _make_pdf

# What Safari puts on the clipboard (as plain text) for a selection of copied tabs:
# 'title\nURL\n\n' per tab; PDF tabs are titled by their own URL.
SAFARI_TEXT = """\
https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf
https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf

[2312.00429] Polygraphs: From Rewriting to Higher Categories
https://arxiv.org/abs/2312.00429

https://higher-structures.math.cas.cz/api/files/issues/Vol6Iss1/CurHoTMim
https://higher-structures.math.cas.cz/api/files/issues/Vol6Iss1/CurHoTMim

[1903.05848] Syntactic approaches to opetopes
https://arxiv.org/abs/1903.05848
"""

# ... and the same selection as RTF (abridged), one HYPERLINK field per tab; one title has
# a cp1252 escape, a \\u escape and escaped braces.
SAFARI_RTF = r"""{\rtf1\ansi\ansicpg1252\cocoartf2907
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\*\listtable{\list\listtemplateid1\listhybrid{\listlevel\levelnfc23{\*\levelmarker \{disc\}}{\leveltext\leveltemplateid1\'01\uc0\u8226 ;}{\levelnumbers;}\fi-360\li720\lin720 }{\listname ;}\listid1}}
\pard\tx560\tx1120\pardirnatural\partightenfactor0
\ls1\ilvl0{\field{\*\fldinst{HYPERLINK "https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf"}}{\fldrslt 
\f0\fs24 \cf0 {\listtext	\'95	}https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf}}
\f0\fs24 \
\pard\tx560\tx1120\pardirnatural\partightenfactor0
\ls3\ilvl0{\field{\*\fldinst{HYPERLINK "https://arxiv.org/abs/2312.00429"}}{\fldrslt \cf0 {\listtext	\'95	}[2312.00429] Polygraphs: From Rewriting to Higher Categories}}\
\pard\tx560\tx1120\pardirnatural\partightenfactor0
\ls4\ilvl0{\field{\*\fldinst{HYPERLINK "https://example.org/cat"}}{\fldrslt \cf0 {\listtext	\'95	}Cat\'e9gories \uc0\u8212  a \{note\}}}\
\cf0 \
}"""


def test_parse_safari_text():
    links = dl.parse_text(SAFARI_TEXT)
    assert [link.url for link in links] == [
        "https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf",
        "https://arxiv.org/abs/2312.00429",
        "https://higher-structures.math.cas.cz/api/files/issues/Vol6Iss1/CurHoTMim",
        "https://arxiv.org/abs/1903.05848",
    ]
    assert [link.title for link in links] == [
        None, "[2312.00429] Polygraphs: From Rewriting to Higher Categories", None,
        "[1903.05848] Syntactic approaches to opetopes"]


def test_parse_text_other_shapes():
    text = ("see https://a.example/x.pdf, https://b.example/y.pdf;https://c.example/z.pdf.\n"
            "[A paper](https://d.example/p.pdf) and <https://e.example/q.pdf#page=3>\n"
            "https://en.wikipedia.org/wiki/Topos_(disambiguation)\n"
            "arXiv:2312.00429v2 1903.05848\n"
            "https://a.example/x.pdf#again\n")
    links = dl.parse_text(text)
    assert [link.url for link in links] == [
        "https://a.example/x.pdf", "https://b.example/y.pdf", "https://c.example/z.pdf",
        "https://d.example/p.pdf", "https://e.example/q.pdf",
        "https://en.wikipedia.org/wiki/Topos_(disambiguation)",
        "https://arxiv.org/abs/2312.00429v2", "https://arxiv.org/abs/1903.05848"]
    assert links[3].title == "A paper"
    assert dl.parse_text("just some words\n\n") == []


def test_parse_rtf_and_html():
    links = dl.parse_rtf(SAFARI_RTF)
    assert [(link.url, link.title) for link in links] == [
        ("https://www.lix.polytechnique.fr/Labo/Samuel.Mimram/docs/mimram_automata.pdf", None),
        ("https://arxiv.org/abs/2312.00429", "[2312.00429] Polygraphs: From Rewriting to Higher Categories"),
        ("https://example.org/cat", "Catégories — a {note}"),
    ]
    html = '<ul><li><a href="https://x.example/a.pdf">A &amp; B</a></li><li><a href="/rel">r</a></li></ul>'
    assert [(link.url, link.title) for link in dl.parse_html(html)] == [("https://x.example/a.pdf", "A & B")]


def test_read_clipboard_prefers_rich_links_and_adds_plain_urls(monkeypatch):
    monkeypatch.setattr(dl, "_clipboard_data", lambda cls: SAFARI_RTF.encode("latin-1") if cls == "RTF " else None)
    monkeypatch.setattr(dl, "_clipboard_text", lambda: SAFARI_TEXT)
    links, flavour = dl.read_clipboard()
    assert flavour == "rtf+text"
    assert len(links) == 5 and links[2].title == "Catégories — a {note}"
    assert links[4].title == "[1903.05848] Syntactic approaches to opetopes"


def test_resolve_and_filename():
    assert dl.resolve("https://arxiv.org/abs/2312.00429") == (
        "https://arxiv.org/pdf/2312.00429", "https://arxiv.org/abs/2312.00429")
    assert dl.resolve("https://arxiv.org/pdf/math/0601001v2.pdf") == ("https://arxiv.org/pdf/math/0601001v2", None)
    assert dl.resolve("https://openreview.net/forum?id=abc123") == (
        "https://openreview.net/pdf?id=abc123", "https://openreview.net/forum?id=abc123")
    assert dl.resolve("https://x.example/a.pdf") == ("https://x.example/a.pdf", None)
    assert dl.filename_from({"content-disposition": 'inline; filename="2312.00429v2.pdf"'}, "u") == "2312.00429v2.pdf"
    assert dl.filename_from({"content-disposition": "attachment; filename*=UTF-8''Cat%C3%A9.pdf"}, "u") == "Caté.pdf"
    assert dl.filename_from({}, "https://x.example/docs/My%20Paper.pdf?dl=1") == "My Paper.pdf"


# --- end-to-end against a mocked web -------------------------------------------------------

def _pdf_bytes(tmp_path, name, text):
    p = tmp_path / name
    _make_pdf(p, text)
    return p.read_bytes()


def _web(tmp_path):
    a = _pdf_bytes(tmp_path, "a.pdf", "Polygraphs: From Rewriting to Higher Categories. arXiv:2312.00429v2")
    b = _pdf_bytes(tmp_path, "b.pdf", "Cubical higher structures, a journal article served oddly")
    c = _pdf_bytes(tmp_path, "c.pdf", "A journal article reached through its landing page")
    calls: Counter = Counter()

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls[url] += 1
        if url == "https://arxiv.org/pdf/2312.00429":
            return httpx.Response(200, content=a, headers={
                "content-type": "application/pdf", "content-disposition": 'inline; filename="2312.00429v2.pdf"'})
        if url == "https://mirror.example/polygraphs.pdf":
            return httpx.Response(200, content=a, headers={"content-type": "application/pdf"})
        if url == "https://hs.example/api/files/CurHoTMim":
            return httpx.Response(200, content=b, headers={"content-type": "application/json"})
        if url == "https://journal.example/article/1":
            return httpx.Response(200, text='<html><head><meta name="citation_pdf_url" content="/article/1.pdf">'
                                            "</head><body>landing</body></html>",
                                  headers={"content-type": "text/html"})
        if url == "https://journal.example/article/1.pdf":
            return httpx.Response(302, headers={"location": "https://cdn.example/1.pdf"})
        if url == "https://cdn.example/1.pdf":
            return httpx.Response(200, content=c, headers={"content-type": "application/pdf"})
        if url == "https://blog.example/post":
            return httpx.Response(200, text="<!doctype html><html><body>no pdf here</body></html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    client = h.HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return client, calls


LINKS = [
    dl.Link("https://arxiv.org/abs/2312.00429", "[2312.00429] Polygraphs: From Rewriting to Higher Categories"),
    dl.Link("https://hs.example/api/files/CurHoTMim"),
    dl.Link("https://journal.example/article/1", "Some Journal Article"),
    dl.Link("https://mirror.example/polygraphs.pdf"),
    dl.Link("https://nope.example/missing.pdf"),
    dl.Link("https://blog.example/post"),
]


def test_download_end_to_end(tmp_path):
    client, calls = _web(tmp_path)
    conn = db.connect(tmp_path / "t.sqlite")
    stats, outcomes = dl.download(conn, LINKS, client=client, jobs=3)
    states = {o.link.url: o.state for o in outcomes}
    # the arXiv PDF and the mirror's are the same bytes: one file, two contexts (which one is
    # "new" depends on which host answers first)
    same = {states.pop("https://arxiv.org/abs/2312.00429"), states.pop("https://mirror.example/polygraphs.pdf")}
    assert same == {"new", "dup"}
    assert states == {
        "https://hs.example/api/files/CurHoTMim": "new",  # a PDF despite Content-Type: application/json
        "https://journal.example/article/1": "new",  # via citation_pdf_url and a redirect
        "https://nope.example/missing.pdf": "failed",
        "https://blog.example/post": "failed",
    }
    assert stats["new_files"] == 3 and stats["known_files"] == 1 and stats["failed"] == 2
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 3
    for f in conn.execute("SELECT stored_path, size FROM files"):
        assert store.is_stored(f["stored_path"], f["size"])
    assert not list((store.root() / "tmp").glob("*.part"))

    ctx = conn.execute("SELECT * FROM contexts WHERE kind='download' AND locator=?",
                       ("https://arxiv.org/abs/2312.00429",)).fetchone()
    extra = json.loads(ctx["extra"])
    assert ctx["title"].startswith("[2312.00429] Polygraphs") and ctx["url"] == "https://arxiv.org/pdf/2312.00429"
    assert ctx["source"] == "arxiv.org" and ctx["occurred_at"]
    assert extra["where_froms"] == ["https://arxiv.org/pdf/2312.00429", "https://arxiv.org/abs/2312.00429"]
    assert extra["filename"] == "2312.00429v2.pdf"
    j = conn.execute("SELECT extra FROM contexts WHERE locator='https://journal.example/article/1'").fetchone()[0]
    assert json.loads(j)["where_froms"] == ["https://cdn.example/1.pdf", "https://journal.example/article/1"]

    failed = conn.execute(
        "SELECT c.locator, c.extra, r.status, r.file_id FROM contexts c JOIN context_refs r ON r.context_id=c.id"
        " WHERE r.status='unresolved' ORDER BY c.locator").fetchall()
    assert [f["locator"] for f in failed] == ["https://blog.example/post", "https://nope.example/missing.pdf"]
    assert json.loads(failed[1]["extra"])["status"] == 404
    assert "citation_pdf_url" in json.loads(failed[0]["extra"])["error"]
    run = json.loads(conn.execute("SELECT stats FROM runs WHERE command='download'").fetchone()[0])
    assert run["status"] == "completed" and run["failed"] == 2

    # triage sees the link title and download URLs, and takes the arXiv id (with version) from them
    fid = conn.execute("SELECT file_id FROM context_refs r JOIN contexts c ON c.id=r.context_id"
                       " WHERE c.locator='https://arxiv.org/abs/2312.00429'").fetchone()[0]
    state = build_state(conn, fid)
    assert "Link title: [2312.00429] Polygraphs" in state
    assert "Downloaded from: https://arxiv.org/pdf/2312.00429 (via page https://arxiv.org/abs/2312.00429)" in state
    assert "Filename: 2312.00429v2.pdf" in state
    assert detect_ids(conn, fid) == {"arxiv": "2312.00429", "arxiv_version": "v2"}

    # a second run fetches only what failed
    calls.clear()
    stats2, outcomes2 = dl.download(conn, LINKS, client=client)
    assert stats2["known"] == 4 and stats2["failed"] == 2 and not stats2["new_files"]
    assert set(calls) == {"https://nope.example/missing.pdf", "https://blog.example/post"}
    assert conn.execute("SELECT count(*) FROM contexts WHERE kind='download'").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM context_refs WHERE status='unresolved'").fetchone()[0] == 2

    # --refetch fetches again but changes nothing: same bytes, same rows
    stats3, _ = dl.download(conn, LINKS[:1], client=client, refetch=True)
    assert stats3["known_files"] == 1 and conn.execute("SELECT count(*) FROM files").fetchone()[0] == 3


def test_failed_refetch_keeps_earlier_success(tmp_path):
    client, _ = _web(tmp_path)
    conn = db.connect(tmp_path / "t.sqlite")
    dl.download(conn, LINKS[:1], client=client)
    broken = h.HttpClient(cache_dir=False, transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                          sleep=lambda s: None)
    stats, _ = dl.download(conn, LINKS[:1], client=broken, refetch=True)
    assert stats["failed"] == 1
    r = conn.execute("SELECT c.extra, r.status FROM contexts c JOIN context_refs r ON r.context_id=c.id").fetchall()
    assert len(r) == 1 and r[0]["status"] == "resolved"
    extra = json.loads(r[0]["extra"])
    assert extra["filename"] == "2312.00429v2.pdf" and extra["last_status"] == 404


def test_cli_dry_run_and_usage(tmp_path):
    runner = CliRunner()
    dbp = str(tmp_path / "t.sqlite")
    r = runner.invoke(main, ["--db", dbp, "download"])
    assert r.exit_code == 2 and "--clipboard" in r.output
    r = runner.invoke(main, ["--db", dbp, "download", "-n", "1903.05848", "-"], input=SAFARI_TEXT)
    assert r.exit_code == 0, r.output
    assert r.output.count("fetch ") == 4  # 1903.05848 given twice: once as an id, once in the text
    assert "-> https://arxiv.org/pdf/2312.00429" in r.output and "“[2312.00429] Polygraphs" in r.output
    links_file = tmp_path / "links.txt"
    links_file.write_text("https://a.example/x.pdf https://b.example/y.pdf")
    r = runner.invoke(main, ["--db", dbp, "download", "--dry-run", "-f", str(links_file), "--stdin"], input="")
    assert r.exit_code == 0 and r.output.count("fetch ") == 2


def test_cli_process_triages_promotes_and_enriches_new_files(tmp_path, monkeypatch):
    import papyrus.enrich.pipeline as pipeline
    import papyrus.llm as llm
    import papyrus.llm.decide as decide
    from papyrus.llm.base import BackendUnavailable

    from test_triage_promote import PAPER, FakeBackend

    client, _ = _web(tmp_path)
    h.set_default_client(client)
    monkeypatch.setattr(llm, "get_backend", lambda spec: FakeBackend(dict(PAPER, title="Polygraphs")))

    def no_decider(spec):
        raise BackendUnavailable("none in tests")

    monkeypatch.setattr(decide, "get_decider", no_decider)
    enriched = []
    monkeypatch.setattr(pipeline, "enrich_work", lambda conn, wid, run_id=None: enriched.append(wid) or
                        {"status": "nomatch"})
    try:
        r = CliRunner().invoke(main, ["--db", str(tmp_path / "t.sqlite"), "download", "--process",
                                      "https://arxiv.org/abs/2312.00429", "https://nope.example/x.pdf"])
    finally:
        h.set_default_client(None)
    assert r.exit_code == 0, r.output
    assert "triaging 1 files" in r.output and "failed=1" in r.output
    assert enriched == [1]
    conn = db.connect(tmp_path / "t.sqlite")
    assert conn.execute("SELECT title FROM works").fetchone()[0] == "Polygraphs"
    assert conn.execute("SELECT value FROM work_ids WHERE scheme='arxiv'").fetchone()[0] == "2312.00429"
