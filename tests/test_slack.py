"""Slack importer: link classification, Markdown, filters. Synthetic messages only."""

import json
import time

import httpx
import pytest
from click.testing import CliRunner

from papyrus import services
from papyrus.contexts import links as L
from papyrus.contexts import slack as S
from papyrus import http as h


def _recorded() -> list[dict]:
    """Every call in the web services store, with its body."""
    st = services.store()
    return [{**dict(r), "body": (st.read_body(r["resp_hash"]) or b"").decode()}
            for r in st.conn.execute("SELECT * FROM calls ORDER BY started_at")]


@pytest.mark.parametrize("url,kind,id_", [
    ("https://arxiv.org/abs/2401.01234v2", "arxiv", "2401.01234"),
    ("https://www.alphaxiv.org/abs/2401.01234", "arxiv", "2401.01234"),
    ("https://huggingface.co/papers/2401.01234", "arxiv", "2401.01234"),
    ("https://doi.org/10.48550/arXiv.2401.01234", "arxiv", "2401.01234"),
    ("https://dl.acm.org/doi/10.1145/3591280", "doi", "10.1145/3591280"),
    ("https://example.edu/~me/thesis.PDF", "pdf", None),
    ("https://openreview.net/pdf?id=abc", "pdf", None),
    ("https://openreview.net/forum?id=abc", "domain", None),
    ("https://www.nature.com/articles/s41586-024-00001-x", "domain", None),
    ("https://huggingface.co/Qwen/Qwen3-8B", None, None),  # a model, not a paper
    ("https://github.com/org/repo", None, None),
    ("mailto:someone@example.org", None, None),
])
def test_classify(url, kind, id_):
    c = L.classify(url)
    assert (c.kind if c else None) == kind
    assert (c.id if c else None) == id_


def test_domain_entries_with_paths():
    assert L.matches("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1", "ssrn.com")
    assert L.matches("https://www.jstor.org/stable/123", "jstor.org/stable")
    assert not L.matches("https://www.jstor.org/action/search", "jstor.org/stable")
    assert not L.matches("https://notnature.com/x", "nature.com")
    assert L.classify("https://blog.example.org/post", ["blog.example.org"]).kind == "domain"


NAMES = S.Names(users={"U1": "Ada", "U2": "Bob"}, channels={"C1": "papers"})


def _rt(*elements):
    return [{"type": "rich_text", "elements": list(elements)}]


def test_blocks_markdown():
    blocks = _rt(
        {"type": "rich_text_section", "elements": [
            {"type": "text", "text": "Read "}, {"type": "text", "text": "this", "style": {"bold": True}},
            {"type": "text", "text": " "}, {"type": "link", "url": "https://arxiv.org/abs/2401.01234", "text": "paper"},
            {"type": "text", "text": ", "}, {"type": "user", "user_id": "U2"}, {"type": "text", "text": " in "},
            {"type": "channel", "channel_id": "C1"}, {"type": "text", "text": " "},
            {"type": "emoji", "name": "thumbsup", "unicode": "1f44d"}, {"type": "text", "text": " "},
            {"type": "emoji", "name": "custom"}, {"type": "text", "text": " "},
            {"type": "text", "text": "x = 1", "style": {"code": True}}, {"type": "text", "text": "\n"}]},
        {"type": "rich_text_list", "style": "bullet", "indent": 0, "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": "one"}]},
            {"type": "rich_text_section", "elements": [
                {"type": "link", "url": "https://www.example.org/a.pdf", "text": "example.org/a.pdf"}]}]},
        {"type": "rich_text_list", "style": "ordered", "indent": 1, "offset": 2, "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": "three"}]}]},
        {"type": "rich_text_quote", "elements": [{"type": "text", "text": "quoted\nlines"}]},
        {"type": "rich_text_preformatted", "elements": [
            {"type": "text", "text": "code *not bold*\n"}, {"type": "link", "url": "https://x.org"}]},
    )
    assert S.blocks_markdown(blocks, NAMES) == (
        "Read **this** [paper](https://arxiv.org/abs/2401.01234), @Bob in #papers 👍 :custom: `x = 1`\n"
        "- one\n- https://www.example.org/a.pdf\n"
        "    3. three\n"
        "> quoted\n> lines\n"
        "```\ncode *not bold*\nhttps://x.org\n```")


def test_mrkdwn_markdown():
    text = "*bold* _it_ ~gone~ <https://arxiv.org/abs/2401.01234|the paper> <https://x.org> <@U1> <#C9|general> <!here> a &amp; b"
    assert S.mrkdwn_markdown(text, NAMES) == (
        "**bold** *it* ~~gone~~ [the paper](https://arxiv.org/abs/2401.01234) https://x.org @Ada #general @here a & b")


def test_message_links_sources_and_dedupe():
    m = {
        "blocks": _rt({"type": "rich_text_section", "elements": [
            {"type": "link", "url": "https://arxiv.org/abs/2401.01234"},
            {"type": "link", "url": "mailto:a@b.org"}]}),
        "attachments": [
            {"original_url": "https://arxiv.org/abs/2401.01234", "title": "A Paper", "service_name": "arXiv.org"},
            {"is_msg_unfurl": True, "is_share": True, "text": "see <https://doi.org/10.1000/abc>"}],
        "files": [{"id": "F1", "filetype": "pdf", "name": "notes.pdf", "title": "Notes",
                   "permalink": "https://ws.slack.com/files/U1/F1/notes.pdf"},
                  {"id": "F2", "filetype": "png", "name": "plot.png", "permalink": "https://ws.slack.com/files/U1/F2"}],
    }
    found = S.classify_links(S.message_links(m), L.DEFAULT_DOMAINS)
    assert [(f["source"], f.get("kind")) for f in found] == [
        ("message", "arxiv"), ("shared_message", "doi"), ("file", "pdf_upload"), ("file", None)]
    assert found[0]["title"] == "A Paper" and found[0]["site"] == "arXiv.org" and found[0]["id"] == "2401.01234"
    assert found[2]["label"] == "Notes" and found[2]["file_id"] == "F1"


def _match(ts, channel, text_url, **kw):
    return {"ts": ts, "user": "U1", "username": "ada", "team": "T1", "channel": channel,
            "permalink": f"https://ws.slack.com/archives/{channel['id']}/p{ts.replace('.', '')}" + kw.pop("q", ""),
            "text": f"<{text_url}>", "blocks": _rt({"type": "rich_text_section", "elements": [
                {"type": "link", "url": text_url}]}), **kw}


def _fake_slack(now):
    recent, old = f"{now - 3600:.6f}", f"{now - 40 * 86400:.6f}"
    pub = {"id": "C1", "name": "papers", "is_private": False}
    other = {"id": "C2", "name": "random", "is_private": False}
    dm = {"id": "D1", "name": "U2", "is_im": True, "user": "U2"}
    haslink = [
        _match(recent, pub, "https://arxiv.org/abs/2401.01234", q=f"?thread_ts={old}"),
        _match(f"{now - 7200:.6f}", other, "https://github.com/org/repo"),
        _match(f"{now - 7300:.6f}", other, "https://example.org/paper.pdf"),
        _match(f"{now - 7400:.6f}", dm, "https://arxiv.org/abs/2402.00001"),
        _match(old, pub, "https://arxiv.org/abs/2301.00001"),
    ]
    pdfs = [dict(_match(f"{now - 8000:.6f}", pub, "https://github.com/x"),
                 files=[{"id": "F1", "filetype": "pdf", "name": "a.pdf", "permalink": "https://ws.slack.com/files/F1"}])]
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[1]
        calls.append(method)
        assert req.headers["authorization"] == "Bearer xoxp-test"
        if method == "search.messages":
            q = req.url.params["query"]
            ms = haslink if q.startswith("has:link") else pdfs
            return httpx.Response(200, json={"ok": True, "messages": {
                "matches": ms, "paging": {"total": len(ms), "pages": 1, "page": 1}}})
        if method == "users.list":
            return httpx.Response(200, json={"ok": True, "members": [
                {"id": "U1", "name": "ada", "profile": {"display_name": "Ada L"}},
                {"id": "U2", "name": "bob", "profile": {"real_name": "Bob B"}}]})
        if method == "users.conversations":
            return httpx.Response(200, json={"ok": True, "channels": [pub, other]})
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user": "ada", "team": "WS", "url": "https://ws.slack.com/"},
                                  headers={"x-oauth-scopes": "search:read,users:read"})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    return h.HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None), calls


def test_find_filters_and_records(tmp_path, monkeypatch):
    now = time.time()
    client, calls = _fake_slack(now)
    slack = S.Slack("xoxp-test", client)
    stats = {}
    recs = S.find(slack, S.Filters(since=now - 30 * 86400), workspace="ws", stats=stats)
    assert [r["links"][0]["url"] for r in recs] == [
        "https://arxiv.org/abs/2401.01234", "https://example.org/paper.pdf", "https://ws.slack.com/files/F1"]
    r = recs[0]
    assert r["source"] == "ws/#papers" and r["author"] == {"id": "U1", "handle": "ada", "name": "Ada L"}
    assert r["extra"]["thread_ts"] and r["permalink"].startswith("https://ws.slack.com/archives/C1/p")
    assert r["markdown"] == "https://arxiv.org/abs/2401.01234"
    assert r["occurred_at"].endswith("Z")
    assert stats["skipped_dm"] == 1 and stats["messages"] == 3
    assert calls.count("search.messages") == 2
    # every call is in the services store, token redacted (headers and bodies)
    evs = _recorded()
    assert len(evs) == len(calls) and "xoxp-test" not in json.dumps(evs)
    assert {e["status"] for e in evs} == {"ok"} and json.loads(evs[0]["req_headers"])["authorization"] == "<redacted>"

    with_dms = S.find(S.Slack("xoxp-test", _fake_slack(now)[0]), S.Filters(since=now - 30 * 86400, dms=True),
                      workspace="ws")
    assert any(r["source"] == "ws/@Bob B" for r in with_dms)
    only = S.find(S.Slack("xoxp-test", _fake_slack(now)[0]),
                  S.Filters(since=now - 30 * 86400, channels=("#random",)), workspace="ws")
    assert [r["extra"]["channel_name"] for r in only] == ["random"]
    excl = S.find(S.Slack("xoxp-test", _fake_slack(now)[0]),
                  S.Filters(since=now - 30 * 86400, exclude_channels=("C1",)), workspace="ws")
    assert {r["extra"]["channel_id"] for r in excl} == {"C2"}
    alll = S.find(S.Slack("xoxp-test", _fake_slack(now)[0]), S.Filters(since=now - 30 * 86400, all_links=True),
                  workspace="ws")
    assert any(lk["url"] == "https://github.com/org/repo" for r in alll for lk in r["links"])


def test_cli_links_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_SLACK_TOKEN", "xoxp-test")
    monkeypatch.setenv("PAPYRUS_SLACK_WORKSPACE", "ws")
    client, _ = _fake_slack(time.time())
    h.set_default_client(client)
    try:
        from papyrus.cli import main

        res = CliRunner().invoke(main, ["slack", "links", "--days", "7", "--json"])
    finally:
        h.set_default_client(None)
    assert res.exit_code == 0, res.output
    lines = [json.loads(x) for x in res.stdout.splitlines() if x.startswith("{")]
    assert len(lines) == 3 and lines[0]["links"][0]["kind"] == "arxiv"
    assert "3 messages" in res.stderr
    assert {e["run_id"] for e in _recorded()} != {None}  # tagged with the slack-links run


def test_manifest_scopes():
    m = S.manifest()
    assert m["oauth_config"]["scopes"]["user"] == list(S.USER_SCOPES)
    assert "bot" not in m["oauth_config"]["scopes"]


# --------------------------------------------------------------------------- store + resolve

from papyrus import db as _db  # noqa: E402
from papyrus import resolve as R  # noqa: E402
from papyrus.match import candidates as match_mod  # noqa: E402
from papyrus.models import AuthorRecord, WorkRecord  # noqa: E402


def _rec(permalink, ts, urls, thread_ts=None, text="see"):
    links = S.classify_links([{"url": u, "source": "message"} for u in urls], L.DEFAULT_DOMAINS)
    return {"permalink": permalink, "occurred_at": S._occurred_at(ts), "source": "ws/#papers",
            "author": {"id": "U1", "handle": "ada", "name": "Ada"}, "markdown": text, "links": links,
            "extra": {"team_id": "T1", "channel_id": "C1", "channel_name": "papers", "channel_type": "public",
                      "ts": ts, "thread_ts": thread_ts}}


@pytest.fixture
def conn():
    c = _db.connect(":memory:")
    yield c
    c.close()


def test_store_is_idempotent_and_links_threads(conn):
    root = _rec("https://ws.slack.com/archives/C1/p100", "100.000001", ["https://arxiv.org/abs/2401.01234"])
    reply = _rec("https://ws.slack.com/archives/C1/p200?thread_ts=100.000001", "200.000002",
                 ["https://arxiv.org/abs/2401.01234", "https://example.org/x.pdf"], thread_ts="100.000001")
    assert S.store(conn, [reply, root]) == {"contexts_new": 2, "contexts_updated": 0, "refs_new": 3}
    reply["markdown"] = "edited"
    assert S.store(conn, [reply, root]) == {"contexts_new": 0, "contexts_updated": 2, "refs_new": 0}
    rows = {r["locator"]: r for r in conn.execute("SELECT * FROM contexts WHERE kind='slack'")}
    r, p = rows[reply["permalink"]], rows[root["permalink"]]
    assert r["parent_id"] == p["id"] and p["parent_id"] is None
    assert r["text"] == "edited" and r["actor"] == "Ada" and r["actor_id"] == "U1" and r["title"] == "#papers"
    assert r["url"] == reply["permalink"] and r["occurred_at"].startswith("1970-01-01T00:03:20")
    assert json.loads(r["extra"])["links"][1]["kind"] == "pdf"


def _fake_records(arxiv_id, doi=None, title="A Paper About Things"):
    ids = {"arxiv": arxiv_id, **({"doi": doi} if doi else {})}
    return [WorkRecord(source="arxiv", title=title, year=2024, ids=ids, kind="paper",
                       authors=[AuthorRecord(name="Ada Lovelace")], abstract="We study things.", match_score=0.99)]


def test_resolve_creates_links_and_reuses(conn, monkeypatch):
    calls = []

    def fake(hint, **kw):
        calls.append(hint)
        if hint.arxiv_id == "2401.01234":
            return _fake_records("2401.01234", doi="10.1000/pub.1")
        return []

    monkeypatch.setattr(match_mod, "resolve", fake)
    S.store(conn, [
        _rec("https://ws.slack.com/archives/C1/p1", "1.0", ["https://arxiv.org/abs/2401.01234",
                                                           "https://doi.org/10.5555/missing"]),
        _rec("https://ws.slack.com/archives/C1/p2", "2.0", ["https://www.alphaxiv.org/abs/2401.01234v2",
                                                           "https://example.org/x.pdf"]),
    ])
    with _db.recorded_run(conn, "test") as (run_id, _):
        stats = R.resolve_refs(conn, context_kind="slack", run_id=run_id)
    assert stats == {"created": 1, "not_found": 1}
    w = conn.execute("SELECT * FROM works").fetchall()
    assert len(w) == 1 and w[0]["title"] == "A Paper About Things" and w[0]["status"] == "candidate"
    ids = {r["scheme"]: r["value"] for r in conn.execute("SELECT * FROM work_ids")}
    assert ids["arxiv"] == "2401.01234" and ids["doi"] == "10.1000/pub.1"
    refs = conn.execute("SELECT url, status, work_id, source FROM context_refs ORDER BY id").fetchall()
    assert [(r["status"], r["source"]) for r in refs] == [
        ("resolved", "resolve:arxiv"), ("unresolved", "slack"), ("resolved", "resolve:arxiv"), ("unresolved", "slack")]
    # provenance: the id claim and the API's claims
    srcs = {r[0] for r in conn.execute("SELECT source FROM assertions WHERE subject_type='work'")}
    assert R.ID_SOURCE in srcs and any(s.startswith("api:") for s in srcs)
    # a DOI link to the published version of the same paper reuses the work, with no lookup
    S.store(conn, [_rec("https://ws.slack.com/archives/C1/p3", "3.0", ["https://doi.org/10.1000/PUB.1"])])
    n = len(calls)
    assert R.resolve_refs(conn, context_kind="slack") == {"linked": 1, "not_found": 1}
    assert len(calls) == n + 1  # only the unknown DOI was looked up again
    assert conn.execute("SELECT count(*) FROM works").fetchone()[0] == 1


def test_resolve_links_to_existing_work_by_other_id(conn, monkeypatch):
    wid = conn.execute("INSERT INTO works(title, year) VALUES ('A Paper About Things', 2024)").lastrowid
    conn.execute("INSERT INTO work_ids(work_id, scheme, value) VALUES (?, 'doi', '10.1000/pub.1')", (wid,))
    monkeypatch.setattr(match_mod, "resolve", lambda hint, **kw: _fake_records("2401.01234", doi="10.1000/pub.1"))
    S.store(conn, [_rec("https://ws.slack.com/archives/C1/p1", "1.0", ["https://arxiv.org/pdf/2401.01234"])])
    assert R.resolve_refs(conn) == {"linked_other_id": 1}
    assert conn.execute("SELECT work_id FROM context_refs").fetchone()[0] == wid
    assert conn.execute("SELECT count(*) FROM works").fetchone()[0] == 1


def test_identifier():
    assert R.identifier("https://arxiv.org/abs/2401.01234v3") == ("arxiv", "2401.01234")
    assert R.identifier("https://doi.org/10.48550/arXiv.2401.01234") == ("arxiv", "2401.01234")
    assert R.identifier("https://link.springer.com/article/10.1007/s00453-020-00001-1") == (
        "doi", "10.1007/s00453-020-00001-1")
    assert R.identifier("https://example.org/x.pdf") is None and R.identifier(None) is None
