"""Slack importer: link classification, Markdown, filters. Synthetic messages only."""

import json
import time

import httpx
import pytest
from click.testing import CliRunner

from papyrus import apilog
from papyrus.contexts import links as L
from papyrus.contexts import slack as S
from papyrus.enrich import http as h


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
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))
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
    # every call is in the API log, token redacted
    evs = list(apilog.events())
    assert len(evs) == len(calls) and "xoxp-test" not in json.dumps(evs)

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
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))
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
    assert {e["run_id"] for e in apilog.events()} != {None}  # tagged with the slack-links run


def test_manifest_scopes():
    m = S.manifest()
    assert m["oauth_config"]["scopes"]["user"] == list(S.USER_SCOPES)
    assert "bot" not in m["oauth_config"]["scopes"]
