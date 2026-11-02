"""The web services store (services.py) through HttpClient: the record, redaction, reuse, the shared
rate limit and pauses, the old stores' import, the CLI. Mock transports only."""

import hashlib
import json
import os

import httpx
import pytest
from click.testing import CliRunner

from papyrus import config, db, services
from papyrus import http as h
from papyrus.cli import main


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t, self.sleeps = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def _client(handler, *, clock=None, store=None, **kw):
    clock = clock or Clock()
    return h.HttpClient(transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep, store=store, **kw)


def _calls(st=None):
    st = st or services.store()
    return [dict(r) for r in st.conn.execute("SELECT * FROM calls ORDER BY started_at, attempt")]


def test_records_request_and_response_with_secrets_redacted():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "access_token": "xoxp-secret", "key": "ref1",
                                         "messages": [{"text": "see arxiv.org/abs/2401.00001"}]},
                              headers={"X-RateLimit-Remaining": "7"})

    r = _client(handler).get_json("https://slack.com/api/search.messages", params={"query": "has:link", "token": "xoxp-q"},
                                  headers={"Authorization": "Bearer xoxp-secret", "X-Trace": "1"}, use_cache=False)
    assert r["access_token"] == "xoxp-secret"  # the caller still gets the real response
    st = services.store()
    (c,) = _calls()
    everything = json.dumps([dict(x) for t in ("calls", "requests", "credentials", "state")
                             for x in st.conn.execute(f"SELECT * FROM {t}")])
    assert "xoxp-secret" not in everything and "xoxp-q" not in everything
    assert c["status"] == "ok" and c["http_status"] == 200 and c["attempt"] == 0
    assert "query=has%3Alink" in c["url"] and "token=%3Credacted%3E" in c["url"] or "token=<redacted>" in c["url"]
    assert json.loads(c["req_headers"]) == {"authorization": "<redacted>", "x-trace": "1"}
    assert json.loads(c["resp_headers"])["x-ratelimit-remaining"] == "7"
    body = json.loads(st.read_body(c["resp_hash"]))
    assert body["access_token"] == "<redacted>" and body["key"] == "ref1"  # a `key` field is data: kept
    assert body["messages"][0]["text"] == "see arxiv.org/abs/2401.00001"
    svc, = st.conn.execute("SELECT name FROM services").fetchall()
    cred, = st.conn.execute("SELECT kind, location, fingerprint FROM credentials").fetchall()
    assert svc["name"] == "slack" and cred["kind"] == "key" and cred["location"] == "header:authorization"
    assert len(cred["fingerprint"]) == 16 and cred["fingerprint"] not in ("xoxp-secret",)


def test_every_attempt_is_a_call():
    seq = iter([httpx.Response(429, headers={"Retry-After": "10"}, text="slow down"), httpx.Response(200, text="fine")])
    clock = Clock()
    _client(lambda req: next(seq), clock=clock).get("https://api.example.org/x", use_cache=False)
    cs = _calls()
    assert [(c["attempt"], c["status"], c["http_status"]) for c in cs] == [(0, "http_error", 429), (1, "ok", 200)]
    assert cs[0]["retry_after_s"] == 10 and clock.sleeps == [10.0]
    st = services.store()
    assert [st.read_body(c["resp_hash"]) for c in cs] == [b"slow down", b"fine"]


def test_an_answer_is_reused_across_keys_and_mailto_and_counted():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json={"n": len(n)})

    c = _client(handler, cache_dir=True)
    a = c.get_json("https://api.openalex.org/works/W1", params={"mailto": "a@b"}, headers={"Authorization": "Bearer K"})
    r = c.request("GET", "https://api.openalex.org/works/W1", params={"mailto": "c@d"})
    assert a == r.json() == {"n": 1} and r.from_cache and len(n) == 1
    (call,) = _calls()
    assert call["hits"] == 1 and call["last_hit_at"]
    # not reused: use_cache=False, an answer older than reuse_s, a status the caller doesn't accept
    assert c.get_json("https://api.openalex.org/works/W1", use_cache=False) == {"n": 2}
    old = _client(handler, cache_dir=True, cache_ttl=0)
    assert old.get_json("https://api.openalex.org/works/W1") == {"n": 3}
    assert _client(handler, cache_dir=False).get_json("https://api.openalex.org/works/W1") == {"n": 4}


def test_slack_and_downloads_are_never_reused():
    n = []
    c = _client(lambda req: (n.append(1), httpx.Response(200, json={"ok": True}))[1], cache_dir=True)
    c.get_json("https://slack.com/api/auth.test")
    c.get_json("https://slack.com/api/auth.test")
    assert len(n) == 2  # services.SERVICES: slack reuse_s = 0


def test_download_records_hash_not_bytes_and_redirects(tmp_path):
    pdf = b"%PDF-1.4 fake"

    def handler(req):
        if req.url.path == "/abs":
            return httpx.Response(302, headers={"Location": "https://example.org/file.pdf"})
        return httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"})

    _client(handler).download("https://example.org/abs", tmp_path / "out.pdf")
    (c,) = _calls()
    assert c["resp_sha256"] == hashlib.sha256(pdf).hexdigest() and c["resp_size"] == len(pdf)
    assert c["resp_hash"] is None  # the bytes go to the library's store, not here
    assert c["url"] == "https://example.org/abs" and c["final_url"] == "https://example.org/file.pdf"
    assert json.loads(c["redirects"]) == [[302, "https://example.org/abs"]]


def test_no_answer_is_recorded_as_such():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(h.HttpError) as e:
        _client(handler, max_retries=1).get("https://api.example.org/z", use_cache=False)
    assert e.value.status == 0
    cs = _calls()
    assert [(c["status"], c["error"], c["http_status"]) for c in cs] == [("no_answer", "ConnectError", None)] * 2


def test_calls_name_the_run_and_library_and_the_files_are_private():
    conn = db.connect(db.default_db_path())
    db.init(conn)
    c = _client(lambda r: httpx.Response(200, text="ok"))
    with db.recorded_run(conn, "test") as (run_id, _):
        with services.subject("work:7"):
            c.get("https://api.example.org/a", use_cache=False)
    c.get("https://api.example.org/b", use_cache=False)
    a, b = _calls()
    assert (a["run_id"], a["library"], a["subject"]) == (run_id, str(db.library_home()), "work:7")
    assert (b["run_id"], b["subject"]) == (None, None)
    st = services.store()
    assert st.path.parent == db.papyrus_root()  # one per machine, not per library
    assert oct(st.path.stat().st_mode & 0o777) == "0o600"
    assert oct(st.bodies.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in st.bodies.iterdir())


def test_the_rate_limit_is_shared_by_every_process():
    """Two clients with their own connections to the same store (two processes): the second waits
    out the first's interval, which a per-process limiter wouldn't see."""
    clock = Clock()
    a = _client(lambda r: httpx.Response(200, text="<feed/>"), clock=clock, store=services.Store(services.root()))
    b = _client(lambda r: httpx.Response(200, text="<feed/>"), clock=clock, store=services.Store(services.root()))
    a.get("https://export.arxiv.org/api/query", params={"id_list": "1"}, use_cache=False)
    b.get("https://export.arxiv.org/api/query", params={"id_list": "2"}, use_cache=False)
    a.get("https://api.crossref.org/works/x", use_cache=False)  # another service: its own limit
    assert clock.sleeps == [3.0]


def test_a_pause_survives_a_restart_and_can_be_ended():
    n = []

    def refuse(req):
        n.append(1)
        return httpx.Response(503, text="down")

    with pytest.raises(h.HttpError):
        _client(refuse).get("https://export.arxiv.org/api/query", params={"id_list": "1"})
    sent = len(n)
    restarted = _client(refuse, store=services.Store(services.root()))  # a new process
    with pytest.raises(h.Cooling) as e:
        restarted.get("https://export.arxiv.org/api/query", params={"id_list": "2"})
    assert len(n) == sent and e.value.reason == "HTTP 503" and e.value.until > Clock()()
    (s,) = services.store().states()
    assert s["name"] == "arxiv" and s["failures"] == 1 and s["paused_by"]
    assert services.store().resume("arxiv") == 1
    with pytest.raises(h.HttpError) as e:
        restarted.get("https://export.arxiv.org/api/query", params={"id_list": "2"})
    assert e.value.status == 503 and len(n) > sent


def test_a_400_is_not_trouble():
    with pytest.raises(h.HttpError):
        _client(lambda r: httpx.Response(400, text="bad DOI")).get("https://export.arxiv.org/api/query")
    (s,) = services.store().states()
    assert s["failures"] == 0 and s["paused_until"] is None and s["last_ok_at"]


def test_config_overrides_a_policy(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[services.arxiv]\npause_s = 0\nmax_retries = 0\n")
    monkeypatch.setenv("PAPYRUS_CONFIG", str(cfg))
    n = []
    c = _client(lambda r: (n.append(1), httpx.Response(429))[1])
    for _ in range(2):
        with pytest.raises(h.HttpError) as e:
            c.get("https://export.arxiv.org/api/query")
        assert not isinstance(e.value, h.Cooling)
    assert len(n) == 2  # no retries, no pause
    assert services.effective_policy("arxiv").pause_s == 0 and services.effective_policy("arxiv").interval_s == 3
    cfg.write_text("[services.arxiv]\npause = 5\n")
    with pytest.raises(config.ConfigError, match="not a policy setting"):
        config.services_overrides()


def test_import_of_the_old_api_log_and_cache(tmp_path):
    log = tmp_path / "home" / "api-log"
    log.mkdir(parents=True)
    events = [
        {"v": 1, "id": "a" * 32, "t": "2026-11-01T10:00:00.000Z", "ms": 120, "pid": 1, "run_id": 5, "attempt": 0,
         "method": "GET", "url": "https://api.openalex.org/works/W1?mailto=x%40y", "req_headers": {},
         "status": 200, "resp_headers": {"content-type": "application/json"}, "resp_body": '{"id": "W1"}'},
        {"v": 1, "id": "b" * 32, "t": "2026-11-01T10:01:00.000Z", "method": "GET",
         "url": "https://api.openalex.org/works/W1?mailto=x%40y", "from_cache": True, "status": 200},
        {"v": 1, "id": "c" * 32, "t": "2026-11-01T10:02:00.000Z", "ms": 15000, "attempt": 1, "method": "GET",
         "url": "https://export.arxiv.org/api/query?id_list=1", "req_headers": {}, "error": "ReadTimeout: slow"},
        {"v": 1, "id": "d" * 32, "t": "2026-11-01T10:03:00.000Z", "ms": 50, "attempt": 0, "method": "GET",
         "url": "https://api.semanticscholar.org/graph/v1/paper/x", "req_headers": {"x-api-key": "<redacted>"},
         "status": 404, "resp_body": "{}"},
    ]
    (log / "2026-11-01.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "1.json").write_text(json.dumps({"key": "GET https://api.openalex.org/works/W1?mailto=x%40y",
                                              "url": "u", "status": 200, "body": '{"id": "W1"}', "fetched_at": 1.0}))
    (cache / "2.json").write_text(json.dumps({"key": "GET https://api.crossref.org/works/10.1/x", "url": "u",
                                              "status": 404, "headers": {}, "body": "Resource not found.",
                                              "fetched_at": 1_790_000_000.0}))
    st = services.store()
    assert st.import_api_log(log) == {"calls": 3, "hits": 1, "skipped": 0}
    assert st.import_api_log(log)["calls"] == 0  # idempotent
    assert st.import_http_cache(cache) == {"calls": 1, "known": 1, "skipped": 0}
    rows = {r["id"][:1]: r for r in _calls()}
    assert rows["a"]["hits"] == 1 and rows["a"]["run_id"] == 5 and rows["a"]["library"] == str(log.parent)
    assert rows["c"]["status"] == "no_answer" and rows["c"]["error"] == "ReadTimeout"
    assert rows["d"]["status"] == "ok"  # an API's 404: "no such record", which the client accepted
    kinds = {r["kind"] for r in st.conn.execute("SELECT kind FROM credentials")}
    assert kinds == {"public", "unknown"}
    assert st.conn.execute("SELECT count(*) FROM policies").fetchone()[0] == 0  # imported: policy unknown
    # an imported answer is reused like any other (if young enough)
    hit = st.lookup(rows["a"]["request_id"], {200}, 1e12)
    assert hit is not None and json.loads(hit.text) == {"id": "W1"}


def test_cli(monkeypatch):
    import time

    seq = iter([httpx.Response(503, text="down")] * 3)
    c = _client(lambda r: next(seq), clock=Clock(time.time()))  # `services pause` pauses in real time
    h.set_default_client(c)
    try:
        with pytest.raises(h.HttpError):
            c.get("https://export.arxiv.org/api/query", params={"id_list": "1"})
        r = CliRunner().invoke(main, ["services", "stats"])
        assert r.exit_code == 0 and "arxiv" in r.output and "HTTP 503: 3" in r.output
        r = CliRunner().invoke(main, ["services", "state"])
        assert "PAUSED" in r.output and "HTTP 503" in r.output
        cid = _calls()[0]["id"]
        r = CliRunner().invoke(main, ["services", "show", cid[:6]])
        assert r.exit_code == 0 and '"service": "arxiv"' in r.output and "down" in r.output
        assert "1 paused scope" in CliRunner().invoke(main, ["services", "resume", "arxiv"]).output
        r = CliRunner().invoke(main, ["services", "pause", "crossref", "--minutes", "5"])
        assert r.exit_code == 0 and "paused crossref" in r.output
        with pytest.raises(h.Cooling):
            c.get("https://api.crossref.org/works/10.1/x")
    finally:
        h.set_default_client(None)
