import json

import httpx

from papyrus import apilog, db
from papyrus.enrich import http as h


def _client(handler):
    return h.HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def _events():
    return list(apilog.events())


def test_logs_request_and_response_with_secrets_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))

    def handler(req):
        return httpx.Response(200, json={"ok": True, "access_token": "xoxp-secret", "key": "ref1",
                                         "messages": [{"text": "see arxiv.org/abs/2401.00001"}]},
                              headers={"X-RateLimit-Remaining": "7"})

    c = _client(handler)
    r = c.get_json("https://slack.com/api/search.messages", params={"query": "has:link", "token": "xoxp-q"},
                   headers={"Authorization": "Bearer xoxp-secret", "X-Trace": "1"}, use_cache=False)
    assert r["access_token"] == "xoxp-secret"  # the caller still gets the real response
    (ev,) = _events()
    line = json.dumps(ev)
    assert "xoxp-secret" not in line and "xoxp-q" not in line
    assert ev["method"] == "GET" and ev["status"] == 200 and ev["attempt"] == 0
    assert "query=has%3Alink" in ev["url"] and "token=<redacted>" in ev["url"]
    assert ev["req_headers"] == {"authorization": "<redacted>", "x-trace": "1"}
    assert ev["resp_headers"]["x-ratelimit-remaining"] == "7"
    body = json.loads(ev["resp_body"])
    assert body["access_token"] == "<redacted>"
    assert body["key"] == "ref1"  # data, not a credential: kept
    assert body["messages"][0]["text"] == "see arxiv.org/abs/2401.00001"
    assert ev["run_id"] is None and ev["ms"] >= 0


def test_every_retry_attempt_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "10"}, text="slow down")
        return httpx.Response(200, text="fine")

    _client(handler).get("https://api.example.org/x", use_cache=False)
    evs = _events()
    assert [(e["attempt"], e["status"], e["resp_body"]) for e in evs] == [(0, 429, "slow down"), (1, 200, "fine")]
    assert evs[0]["resp_headers"]["retry-after"] == "10"


def test_cache_hits_are_logged_without_body(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))
    c = h.HttpClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")))
    c.get("https://api.example.org/y")
    c.get("https://api.example.org/y")
    first, second = _events()
    assert first["resp_body"] == "x" and "from_cache" not in first
    assert second["from_cache"] is True and "resp_body" not in second


def test_download_logs_hash_not_bytes_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))
    pdf = b"%PDF-1.4 fake"

    def handler(req):
        if req.url.path == "/abs":
            return httpx.Response(302, headers={"Location": "https://example.org/file.pdf"})
        return httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"})

    _client(handler).download("https://example.org/abs", tmp_path / "out.pdf")
    (ev,) = _events()
    import hashlib

    assert ev["resp_sha256"] == hashlib.sha256(pdf).hexdigest() and ev["resp_size"] == len(pdf)
    assert "resp_body" not in ev
    assert ev["url"] == "https://example.org/file.pdf"
    assert ev["requested_url"] == "https://example.org/abs"
    assert ev["redirects"] == [[302, "https://example.org/abs"]]


def test_transport_errors_are_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", str(tmp_path / "log"))

    def handler(req):
        raise httpx.ConnectError("refused")

    c = h.HttpClient(cache_dir=False, transport=httpx.MockTransport(handler), sleep=lambda s: None, max_retries=1)
    try:
        c.get("https://api.example.org/z", use_cache=False)
    except h.HttpError:
        pass
    evs = _events()
    assert len(evs) == 2 and all("ConnectError" in e["error"] and "status" not in e for e in evs)


def test_events_name_the_run_and_default_to_the_library_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPYRUS_API_LOG", raising=False)
    conn = db.connect(db.default_db_path())
    db.init(conn)
    c = _client(lambda r: httpx.Response(200, text="ok"))
    with db.recorded_run(conn, "test") as (run_id, _):
        c.get("https://api.example.org/a", use_cache=False)
    c.get("https://api.example.org/b", use_cache=False)
    a, b = _events()
    assert a["run_id"] == run_id and b["run_id"] is None
    d = db.library_home() / "api-log"
    assert apilog.log_dir() == d
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in d.glob("*.jsonl"))


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_API_LOG", "0")
    _client(lambda r: httpx.Response(200, text="ok")).get("https://api.example.org/a", use_cache=False)
    assert apilog.log_dir() is None and _events() == []
