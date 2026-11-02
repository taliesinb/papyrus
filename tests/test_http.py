import json

import httpx
import pytest

from papyrus import http as h


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def make(handler, tmp_path=None, **kw):
    fc = FakeClock()
    client = h.HttpClient(
        transport=httpx.MockTransport(handler),
        clock=fc.clock,
        sleep=fc.sleep,
        **{"cache_ttl": None if tmp_path else 0, **kw},
    )
    return client, fc


def test_user_agent_mailto(monkeypatch):
    monkeypatch.setenv("PAPYRUS_MAILTO", "me@example.org")
    assert "mailto:me@example.org" in h.user_agent()
    monkeypatch.delenv("PAPYRUS_MAILTO")
    assert "mailto" not in h.user_agent()
    seen = {}

    def handler(req):
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200, json={})

    c, _ = make(handler)
    c.get_json("https://example.org/x")
    assert seen["ua"].startswith("papyrus/")


def test_retry_on_429_honours_retry_after():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    c, fc = make(handler)
    assert c.get_json("https://api.example.org/a") == {"ok": True}
    assert len(calls) == 3
    assert fc.sleeps.count(7.0) >= 2


def test_retry_exhausted_raises():
    c, _ = make(lambda req: httpx.Response(503, text="down"), max_retries=2)
    with pytest.raises(h.HttpError) as e:
        c.get("https://api.example.org/a")
    assert e.value.status == 503


def test_404_returns_none_and_400_raises():
    c, _ = make(lambda req: httpx.Response(404 if "missing" in str(req.url) else 400, text="bad"))
    assert c.get_json("https://api.example.org/missing") is None
    with pytest.raises(h.HttpError):
        c.get_json("https://api.example.org/bad")


def test_transport_error_retried():
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json=[1])

    c, _ = make(handler)
    assert c.get_json("https://api.example.org/a") == [1]


def test_per_host_rate_limit():
    c, fc = make(lambda req: httpx.Response(200, json={}), intervals={"slow.example.org": 3.0})
    for _ in range(3):
        c.get_json("https://slow.example.org/q")
    c.get_json("https://fast.example.org/q")
    assert fc.sleeps == [3.0, 3.0]
    assert c.interval("export.arxiv.org") == 3.0 and c.interval("slow.example.org") == 3.0


def test_cache_hits_and_excludes_secrets(tmp_path):
    n = []

    def handler(req):
        n.append(str(req.url))
        return httpx.Response(200, json={"n": len(n)})

    c, _ = make(handler, tmp_path)
    a = c.get_json("https://api.example.org/w", params={"q": "x", "api_key": "SECRET"})
    b = c.get_json("https://api.example.org/w", params={"api_key": "OTHER", "q": "x"})
    assert a == b == {"n": 1}
    assert len(n) == 1
    st = h.services.store()
    (call,) = st.conn.execute("SELECT * FROM calls").fetchall()
    assert call["http_status"] == 200 and call["hits"] == 1
    everything = json.dumps([dict(r) for t in ("calls", "requests", "credentials") for r in st.conn.execute(f"SELECT * FROM {t}")])
    assert "SECRET" not in everything and "OTHER" not in everything
    assert st.conn.execute("SELECT count(*) FROM credentials WHERE kind='key'").fetchone()[0] == 2  # two keys, one request
    # bypass
    c.get_json("https://api.example.org/w", params={"q": "x"}, cache_ttl=0)
    assert len(n) == 2


def test_cache_post_body_in_key(tmp_path):
    bodies = []

    def handler(req):
        bodies.append(req.content)
        return httpx.Response(200, json=json.loads(req.content))

    c, _ = make(handler, tmp_path)
    assert c.post_json("https://api.example.org/batch", {"ids": [1]}) == {"ids": [1]}
    assert c.post_json("https://api.example.org/batch", {"ids": [2]}) == {"ids": [2]}
    assert c.post_json("https://api.example.org/batch", {"ids": [1]}) == {"ids": [1]}
    assert len(bodies) == 2


def test_cache_ttl(tmp_path, monkeypatch):
    n = []
    c, _ = make(lambda req: (n.append(1), httpx.Response(200, json={}))[1], tmp_path, cache_ttl=10)
    c.get_json("https://api.example.org/t")
    real = h.time.time
    monkeypatch.setattr(h.time, "time", lambda: real() + 100)
    c.get_json("https://api.example.org/t")
    assert len(n) == 2


def test_env_disables_reuse(monkeypatch):
    monkeypatch.setenv("PAPYRUS_HTTP_CACHE", "0")
    p = h.services.Policy(cache_ttl=100)
    assert h.services.resolve_ttl(h.AUTO, h.AUTO, policy=p) == 0  # the environment says no
    assert h.services.resolve_ttl(h.AUTO, 5, policy=p) == 5  # a client's own TTL beats it
    assert h.services.resolve_ttl(None, 5, policy=p) == float("inf")  # and a request's beats the client's
    monkeypatch.delenv("PAPYRUS_HTTP_CACHE")
    assert h.services.resolve_ttl(h.AUTO, h.AUTO, policy=p) == 100  # the policy's
    assert h.services.resolve_ttl(0, None, policy=p) == 0
    for bad in (-1, True, "1"):
        with pytest.raises(ValueError):
            h.services.resolve_ttl(bad, policy=p)
    with pytest.raises(ValueError):
        h.HttpClient(cache_ttl=-5)


def test_redact_url():
    assert h.redact_url("https://x.org/a?b=1&api_key=s&a=2") == "https://x.org/a?a=2&b=1"


def test_patience_gives_up_on_a_long_rate_limit_instead_of_waiting():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "120"})

    client, fc = make(handler)
    with h.patience(15):
        with pytest.raises(h.RateLimited) as e:
            client.get("https://api.example.org/x")
    assert e.value.retry_after == 120 and e.value.status == 429
    assert fc.sleeps == [] and len(calls) == 1  # no waiting: the caller has other work


def test_patience_still_allows_short_retries():
    seq = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": 1})])
    client, fc = make(lambda req: next(seq))
    with h.patience(15):
        assert client.get_json("https://api.example.org/x") == {"ok": 1}
    assert fc.sleeps == [2.0]
