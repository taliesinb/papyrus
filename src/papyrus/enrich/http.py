"""Shared polite HTTP client for the enrichment sources.

* User-Agent ``papyrus/<version> (mailto:<PAPYRUS_MAILTO>)`` so APIs can contact us
  (Crossref and OpenAlex route such requests to their "polite" pools).
* Per-host minimum interval between requests (arXiv asks for 1 request / 3 s).
* Retries with exponential backoff (+jitter) on 429, 5xx and transport errors,
  honouring ``Retry-After``.
* Optional on-disk JSON cache under ``~/.cache/papyrus/http`` keyed by the request
  URL (+ POST body). Secrets are never part of the key or the stored entry: pass API
  keys as headers (or list query params in ``SECRET_PARAMS``).
* Every attempt (and every cache hit) is appended to the permanent API log
  (`papyrus.apilog`): the cache expires, the log doesn't. New API clients go through
  this class so that no response is ever lost.

Environment:
    PAPYRUS_MAILTO        contact address put into the User-Agent / mailto params
    PAPYRUS_HTTP_CACHE    '0' disables the cache, or a directory path to use instead
    PAPYRUS_HTTP_CACHE_TTL_DAYS  cache freshness (default 30)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .. import apilog

log = logging.getLogger(__name__)

VERSION = "0.1.0"
DEFAULT_CACHE_DIR = Path("~/.cache/papyrus/http").expanduser()
SECRET_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token"})

# Minimum seconds between requests, per host.
DEFAULT_INTERVALS: dict[str, float] = {
    "export.arxiv.org": 3.0,  # https://info.arxiv.org/help/api/tou.html
    "api.semanticscholar.org": 1.05,  # 1 rps with a key; shared pool without
    "api.openalex.org": 0.11,  # 10 rps keyless, 30 rps with a key
    "api.crossref.org": 0.35,  # polite pool advertises 3 rps (x-rate-limit-*)
    "arxiv.org": 1.0,  # PDF downloads (papyrus download)
}

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {redact_url(url)}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


def mailto() -> str | None:
    v = os.environ.get("PAPYRUS_MAILTO", "").strip()
    return v or None


def user_agent() -> str:
    m = mailto()
    base = f"papyrus/{VERSION} (local-first PDF library"
    return base + (f"; mailto:{m})" if m else ")")


def redact_url(url: str) -> str:
    """Drop secret query params (used for cache keys and log messages)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SECRET_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(sorted(q))))


def full_url(url: str, params: Mapping[str, Any] | None) -> str:
    """URL with params merged in (sorted, so equivalent requests share a cache key)."""
    if not params:
        return url
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    q += [(k, str(v)) for k, v in params.items() if v is not None]
    return urlunsplit(parts._replace(query=urlencode(sorted(q))))


@dataclass
class Response:
    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class Download:
    status: int
    url: str  # final URL, after redirects
    headers: dict[str, str]  # lower-case names
    size: int


class _HostLimiter:
    def __init__(self, interval: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_ok = 0.0
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> None:
        with self.lock:
            now = self._clock()
            if now < self.next_ok:
                self._sleep(self.next_ok - now)
                now = self._clock()
            self.next_ok = now + self.interval

    def penalise(self, seconds: float) -> None:
        with self.lock:
            self.next_ok = max(self.next_ok, self._clock() + seconds)


class HttpClient:
    """Thread-safe polite client; one instance is shared by all sources (see `default_client`)."""

    def __init__(
        self,
        *,
        cache_dir: Path | str | None | bool = None,
        cache_ttl: float | None = None,
        intervals: Mapping[str, float] | None = None,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if cache_dir is None:
            env = os.environ.get("PAPYRUS_HTTP_CACHE", "").strip()
            cache_dir = False if env in {"0", "false", "no", "off"} else (Path(env) if env else DEFAULT_CACHE_DIR)
        elif cache_dir is True:
            cache_dir = DEFAULT_CACHE_DIR
        self.cache_dir: Path | None = Path(cache_dir).expanduser() if cache_dir else None
        if cache_ttl is None:
            cache_ttl = float(os.environ.get("PAPYRUS_HTTP_CACHE_TTL_DAYS", "30")) * 86400
        self.cache_ttl = cache_ttl
        self.intervals = dict(DEFAULT_INTERVALS)
        if intervals:
            self.intervals.update(intervals)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._clock = clock
        self._sleep = sleep
        self._limiters: dict[str, _HostLimiter] = {}
        self._limiters_lock = threading.Lock()
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"},
            transport=transport,
        )

    # -- rate limiting -------------------------------------------------------
    def _limiter(self, host: str) -> _HostLimiter:
        with self._limiters_lock:
            lim = self._limiters.get(host)
            if lim is None:
                lim = _HostLimiter(self.intervals.get(host, 0.0), self._clock, self._sleep)
                self._limiters[host] = lim
            return lim

    # -- cache ---------------------------------------------------------------
    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_get(self, key: str) -> Response | None:
        p = self._cache_path(key)
        if p is None or not p.exists():
            return None
        try:
            entry = json.loads(p.read_text())
        except OSError, ValueError:
            return None
        if self.cache_ttl and time.time() - entry.get("fetched_at", 0) > self.cache_ttl:
            return None
        return Response(entry["status"], entry["body"], entry["url"], entry.get("headers", {}), from_cache=True)

    def _cache_put(self, key: str, resp: Response) -> None:
        p = self._cache_path(key)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "key": key,
                "url": resp.url,
                "status": resp.status,
                "headers": {k: v for k, v in resp.headers.items() if k.lower() == "content-type"},
                "body": resp.text,
                "fetched_at": time.time(),
            }
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry))
            tmp.replace(p)
        except OSError as e:  # cache is best-effort
            log.debug("cache write failed: %s", e)

    # -- requests ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
        cache_statuses: frozenset[int] = frozenset({200, 404}),
        ok_statuses: frozenset[int] = frozenset({200, 404}),
        max_retries: int | None = None,
        backoff_cap: float | None = None,
    ) -> Response:
        """Perform a request; returns statuses in `ok_statuses`, raises HttpError otherwise."""
        url = full_url(url, params)
        body = json.dumps(json_body, sort_keys=True) if json_body is not None else None
        key = f"{method.upper()} {redact_url(url)}" + (f"\n{body}" if body else "")
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None and hit.status in ok_statuses:
                apilog.record({"t": apilog.now_iso(), "method": method.upper(), "url": apilog.redact_url(url),
                               "from_cache": True, "status": hit.status})
                return hit
        host = urlsplit(url).hostname or ""
        limiter = self._limiter(host)
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            limiter.wait()
            req_headers = {**({"Content-Type": "application/json"} if body else {}), **(headers or {})}
            started = time.time()
            try:
                r = self._client.request(method, url, content=body.encode() if body else None, headers=req_headers)
            except httpx.TransportError as e:
                _log_attempt(method, url, req_headers, body, started, attempt, error=f"{type(e).__name__}: {e}")
                if attempt >= retries:
                    raise HttpError(0, url, f"{type(e).__name__}: {e}") from e
                delay = self._backoff(attempt, None, backoff_cap)
                log.info("%s %s: %s; retrying in %.1fs", method, redact_url(url), e, delay)
                self._sleep(delay)
                attempt += 1
                continue
            _log_attempt(method, url, req_headers, body, started, attempt, response=r, resp_body=r.text)
            if r.status_code in RETRY_STATUSES and attempt < retries:
                delay = self._backoff(attempt, r.headers.get("Retry-After"), backoff_cap)
                log.info("%s %s -> %d; retrying in %.1fs", method, redact_url(url), r.status_code, delay)
                limiter.penalise(delay)
                self._sleep(delay)
                attempt += 1
                continue
            resp = Response(r.status_code, r.text, redact_url(str(r.request.url)), dict(r.headers))
            if r.status_code not in ok_statuses:
                raise HttpError(r.status_code, url, r.text)
            if use_cache and r.status_code in cache_statuses:
                self._cache_put(key, resp)
            return resp

    def download(
        self,
        url: str,
        dest: Path,
        *,
        headers: Mapping[str, str] | None = None,
        max_retries: int | None = None,
        max_bytes: int | None = None,
    ) -> Download:
        """Stream a GET into `dest` (overwritten), with the rate limiting and retries of `request`
        but no cache. Raises HttpError unless the final status is 200."""
        host = urlsplit(url).hostname or ""
        limiter = self._limiter(host)
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            limiter.wait()
            delay = None
            started = time.time()
            hdrs = dict(headers or {})
            r = None
            try:
                with self._client.stream("GET", url, headers=hdrs) as r:
                    if r.status_code in RETRY_STATUSES and attempt < retries:
                        _log_attempt("GET", url, hdrs, None, started, attempt, response=r)
                        delay = self._backoff(attempt, r.headers.get("Retry-After"))
                        log.info("GET %s -> %d; retrying in %.1fs", redact_url(url), r.status_code, delay)
                        limiter.penalise(delay)
                    elif r.status_code != 200:
                        raw = r.read()
                        _log_attempt("GET", url, hdrs, None, started, attempt, response=r,
                                     resp_body=raw.decode("utf-8", "replace"))
                        raise HttpError(r.status_code, url, raw[:2000].decode("utf-8", "replace"))
                    else:
                        size = 0
                        digest = hashlib.sha256()
                        with open(dest, "wb") as fh:
                            for chunk in r.iter_bytes(1 << 16):
                                fh.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                                if max_bytes is not None and size > max_bytes:
                                    _log_attempt("GET", url, hdrs, None, started, attempt, response=r,
                                                 error=f"larger than {max_bytes} bytes (stopped)")
                                    raise HttpError(200, url, f"larger than {max_bytes} bytes")
                        _log_attempt("GET", url, hdrs, None, started, attempt, response=r,
                                     resp_size=size, resp_sha256=digest.hexdigest())
                        return Download(r.status_code, redact_url(str(r.url)),
                                        {k.lower(): v for k, v in r.headers.items()}, size)
            except httpx.TransportError as e:  # includes connections dropped mid-body
                _log_attempt("GET", url, hdrs, None, started, attempt, response=r,
                             error=f"{type(e).__name__}: {e}")
                if attempt >= retries:
                    raise HttpError(0, url, f"{type(e).__name__}: {e}") from e
                delay = self._backoff(attempt, None)
                log.info("GET %s: %s; retrying in %.1fs", redact_url(url), e, delay)
            self._sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int, retry_after: str | None, cap: float | None = None) -> float:
        cap = self.backoff_cap if cap is None else cap
        if retry_after:
            try:
                return min(cap, max(0.0, float(retry_after)))
            except ValueError:
                pass
        d = self.backoff_base * (2**attempt)
        return min(cap, d * (0.75 + 0.5 * random.random()))

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any | None:
        """GET and decode JSON; None on 404."""
        r = self.request("GET", url, **kw)
        return None if r.status == 404 else r.json()

    def post_json(self, url: str, body: Any, **kw: Any) -> Any | None:
        r = self.request("POST", url, json_body=body, **kw)
        return None if r.status == 404 else r.json()

    def close(self) -> None:
        self._client.close()


def _log_attempt(method: str, url: str, req_headers: Mapping[str, str], req_body: str | None, started: float,
                 attempt: int, *, response: httpx.Response | None = None, resp_body: str | None = None,
                 resp_size: int | None = None, resp_sha256: str | None = None, error: str | None = None) -> None:
    """One `apilog` event for one attempt."""
    ev: dict[str, Any] = {
        "t": apilog.now_iso(started),
        "ms": round((time.time() - started) * 1000),
        "attempt": attempt,
        "method": method.upper(),
        "url": apilog.redact_url(str(response.url) if response is not None else url),
        "req_headers": apilog.request_headers(req_headers),
    }
    if req_body is not None:
        ev["req_body"] = apilog.redact_body(req_body, req_headers.get("Content-Type"))
    if response is not None:
        ev["status"] = response.status_code
        ev["resp_headers"] = apilog.redact_headers(response.headers)
        if response.history:
            ev["redirects"] = [[h.status_code, apilog.redact_url(str(h.url))] for h in response.history]
        if response.url != httpx.URL(url):
            ev["requested_url"] = apilog.redact_url(url)
    if resp_body is not None:
        ev["resp_body"] = apilog.redact_body(resp_body, response.headers.get("content-type") if response else None)
    if resp_size is not None:
        ev["resp_size"] = resp_size
        ev["resp_sha256"] = resp_sha256
    if error:
        ev["error"] = error
    apilog.record(ev)


_default: HttpClient | None = None
_default_lock = threading.Lock()


def default_client() -> HttpClient:
    global _default
    with _default_lock:
        if _default is None:
            _default = HttpClient()
        return _default


def set_default_client(client: HttpClient | None) -> None:
    global _default
    with _default_lock:
        _default = client
