"""Shared polite HTTP client for the matching sources (match/), `papyrus download`, Slack and tools.

Every request goes through the web services store (services.py, `<PAPYRUS_ROOT>/services.sqlite`):

* **reuse**: an answer the store has (same request, status in `cache_statuses`, no older than
  the cache TTL) is returned without asking (`Response.from_cache`). `cache_ttl`, per request and
  per client: seconds; 0 never reuses; None reuses any age; AUTO (the default) takes the service
  policy's `cache_ttl` from config (30 days; 0 for Slack and PDFs);
* **one rate limit and one pause per service, shared by every process**: each attempt reserves
  the scope's next slot (the policy's `interval_s`; arXiv 3 s), then sleeps outside any lock.
  A request that fails for good with one of the policy's `pause_statuses` (429, 503, no answer)
  pauses the service for `pause_s` (arXiv 5 min, Semantic Scholar 2 min); while paused, requests
  raise `Cooling` without being sent;
* **retries** with exponential backoff (+jitter) on 429, 5xx and no answer, honouring
  ``Retry-After``, up to the policy's `max_retries`; `patience()` caps the total wait;
* **the record**: every attempt, its status, timing, headers and body (secrets redacted, apilog.py's
  rules), the run in progress and the subject (`services.subject`). No response is ever lost.

User-Agent ``papyrus/<version> (mailto:<PAPYRUS_MAILTO>)`` so APIs can contact us (Crossref and
OpenAlex route such requests to their "polite" pools). Pass API keys as headers (or query params in
`apilog.SECRET_PARAMS`): they are never part of a request's identity or the record, only their
fingerprint is (`services.credentials`).

Environment:
    PAPYRUS_MAILTO        contact address put into the User-Agent / mailto params
    PAPYRUS_HTTP_CACHE    '0': never reuse an answer where cache_ttl is AUTO (requests are still recorded)
    PAPYRUS_SERVICES      directory of the services store (default PAPYRUS_ROOT, ~/.papyrus)
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import services

log = logging.getLogger(__name__)

VERSION = "0.1.0"
SECRET_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token"})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
AUTO = services.AUTO  # cache_ttl: the service policy's (config)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {redact_url(url)}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class RateLimited(HttpError):
    """A retryable refusal (429, 503) whose wait would exceed the caller's `patience`. `retry_after`:
    the seconds the server asked for, or the backoff we would have slept."""

    def __init__(self, status: int, url: str, retry_after: float):
        super().__init__(status, url, f"rate limited; retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class Cooling(HttpError):
    """Not sent: the service is paused (it refused or didn't answer recently; services.state).
    Matching treats it as "not asked", not as "not found". `service`, `until` (unix seconds), `reason`."""

    def __init__(self, status: int, url: str, body: str = "", *, service: str | None = None,
                 until: float | None = None, reason: str | None = None):
        super().__init__(status, url, body)
        self.service, self.until, self.reason = service, until, reason


_patience: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("papyrus_http_patience", default=None)


@contextmanager
def patience(seconds: float):
    """Within the block, retries sleep at most `seconds` in all: a 429/503 whose backoff would go
    beyond raises `RateLimited` instead of waiting, and a timeout or dropped connection its HttpError
    (status 0). For callers that have other work to do (the watcher); without it a request waits
    through all its retries (up to minutes)."""
    token = _patience.set([seconds])
    try:
        yield
    finally:
        _patience.reset(token)


def mailto() -> str | None:
    v = os.environ.get("PAPYRUS_MAILTO", "").strip()
    return v or None


def user_agent() -> str:
    m = mailto()
    base = f"papyrus/{VERSION} (local-first PDF library"
    return base + (f"; mailto:{m})" if m else ")")


def redact_url(url: str) -> str:
    """Drop secret query params (for messages)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SECRET_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(sorted(q))))


def full_url(url: str, params: Mapping[str, Any] | None) -> str:
    """URL with params merged in (sorted, so equivalent requests are the same request)."""
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


def _retry_after(v: str | None) -> float | None:
    try:
        return max(0.0, float(v)) if v else None
    except ValueError:
        return None


class HttpClient:
    """Thread-safe polite client; one instance is shared by all sources (see `default_client`).

    The policy of each request is its service's (config: [services] and [services.<name>]);
    `max_retries`, `backoff_base`, `backoff_cap` and `timeout` given here override it for every
    service, `intervals` per host. `cache_ttl` is every request's unless the request gives its own
    (seconds, 0 never, None forever, AUTO: the policy's); requests are recorded either way. `clock`
    (unix seconds, shared with other processes through the store) and `sleep` are for tests."""

    def __init__(
        self,
        *,
        cache_ttl: float | None | Any = services.AUTO,
        intervals: Mapping[str, float] | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        backoff_cap: float | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        store: services.Store | None = None,
    ):
        services.resolve_ttl(cache_ttl, policy=services.Policy())  # (checks it now)
        self.cache_ttl = cache_ttl
        self._given_intervals = dict(intervals or {})
        self._policy_kw = {"max_retries": max_retries, "backoff_base": backoff_base, "backoff_cap": backoff_cap,
                           "timeout_s": timeout}
        self._clock = clock
        self._sleep = sleep
        self._store = store
        self._client = httpx.Client(
            timeout=30.0 if timeout is None else timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"},
            transport=transport,
        )

    @property
    def store(self) -> services.Store:
        return self._store or services.store()

    # -- the policy, the slot, the outcome -------------------------------------
    def _ids(self, method: str, url: str, headers: Mapping[str, str] | None, body: str | None,
             **call_kw: Any) -> services.Ids:
        host = (urlsplit(url).hostname or "").lower()
        kw = dict(self._policy_kw)
        if host in self._given_intervals:
            kw["interval_s"] = self._given_intervals[host]
        kw.update({k: v for k, v in call_kw.items() if v is not None})
        return self.store.ids(method, url, headers, body, layers=services.layers(), **kw)

    def interval(self, host: str) -> float:
        """Seconds between two requests to `host` (its service's policy, or `intervals`)."""
        if host in self._given_intervals:
            return self._given_intervals[host]
        return services.policy_for(services.service_for(f"https://{host}/"), services.layers()).interval_s

    def _wait_slot(self, ids: services.Ids) -> None:
        try:
            wait = self.store.claim(ids, self._clock())
        except services.Paused as p:
            raise Cooling(429, ids.url, f"not sent: {p}", service=p.service, until=p.until, reason=p.reason) from None
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, ids: services.Ids, attempt: int, retry_after: str | None) -> float:
        p = ids.policy
        if (asked := _retry_after(retry_after)) is not None:
            return min(p.backoff_cap, asked)
        d = p.backoff_base * (2**attempt)
        return min(p.backoff_cap, d * (0.75 + 0.5 * random.random()))

    def _spend(self, delay: float) -> bool:
        """Take `delay` from the caller's patience; False if it doesn't have that much left."""
        budget = _patience.get()
        if budget is None:
            return True
        if delay > budget[0]:
            return False
        budget[0] -= delay
        return True

    def _give_up(self, ids: services.Ids, status: int | None, error: str | None, call_id: str) -> None:
        until = self.store.failed(ids, status, error, call_id, self._clock())
        if until is not None:
            log.info("%s paused for %.0f s (%s)", ids.service.name, until - self._clock(),
                     f"HTTP {status}" if status else error)

    # -- requests ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float | None | Any = services.AUTO,
        cache_statuses: frozenset[int] = frozenset({200, 404}),
        ok_statuses: frozenset[int] = frozenset({200, 404}),
        max_retries: int | None = None,
        backoff_cap: float | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Perform a request; returns statuses in `ok_statuses`, raises HttpError otherwise
        (Cooling while the service is paused, RateLimited when `patience` runs out).
        `cache_ttl`: reuse a recorded answer this many seconds old (0: never, ask; None: any age;
        AUTO: the client's, else the service policy's). `timeout`: seconds per attempt."""
        url = full_url(url, params)
        body = json.dumps(json_body, sort_keys=True) if json_body is not None else None
        ids = self._ids(method, url, headers, body, max_retries=max_retries, backoff_cap=backoff_cap, timeout_s=timeout)
        st = self.store
        ttl = services.resolve_ttl(cache_ttl, self.cache_ttl, policy=ids.policy)
        if ttl > 0:
            hit = st.lookup(ids.request_id, cache_statuses & ok_statuses, ttl)
            if hit is not None:
                return Response(hit.status, hit.text, hit.url, hit.headers, from_cache=True)
        retries = ids.policy.max_retries
        attempt = 0
        while True:
            self._wait_slot(ids)
            req_headers = {**({"Content-Type": "application/json"} if body else {}), **(headers or {})}
            started, t0 = time.time(), time.monotonic()
            try:
                r = self._client.request(method, url, content=body.encode() if body else None, headers=req_headers,
                                         timeout=ids.policy.timeout_s)
            except httpx.TransportError as e:
                what = type(e).__name__
                cid = st.record(ids, attempt=attempt, status="no_answer", started=started,
                                finished=started + time.monotonic() - t0, error=what, req_headers=req_headers)
                delay = self._backoff(ids, attempt, None) if attempt < retries else None
                if delay is None or not self._spend(delay):  # a server that times out is slow to recover
                    self._give_up(ids, None, what, cid)
                    raise HttpError(0, url, f"{what}: {e}") from e
                log.info("%s %s: %s; retrying in %.1fs", method, redact_url(url), what, delay)
                self._sleep(delay)
                attempt += 1
                continue
            except KeyboardInterrupt:
                st.record(ids, attempt=attempt, status="interrupted", started=started,
                          finished=started + time.monotonic() - t0, req_headers=req_headers)
                raise
            ok = r.status_code in ok_statuses
            cid = st.record(ids, attempt=attempt, status="ok" if ok else "http_error", started=started,
                            finished=started + time.monotonic() - t0, http_status=r.status_code,
                            req_headers=req_headers, resp_headers=dict(r.headers), body=r.text, resp_size=len(r.content),
                            final_url=str(r.url), redirects=[[h.status_code, str(h.url)] for h in r.history],
                            retry_after=_retry_after(r.headers.get("Retry-After")))
            if r.status_code in RETRY_STATUSES and attempt < retries:
                delay = self._backoff(ids, attempt, r.headers.get("Retry-After"))
                if r.status_code in (429, 503) and not self._spend(delay):
                    self._give_up(ids, r.status_code, None, cid)
                    raise RateLimited(r.status_code, url, _retry_after(r.headers.get("Retry-After")) or delay)
                log.info("%s %s -> %d; retrying in %.1fs", method, redact_url(url), r.status_code, delay)
                st.penalise(ids, self._clock() + delay)
                self._sleep(delay)
                attempt += 1
                continue
            if r.status_code in RETRY_STATUSES:
                self._give_up(ids, r.status_code, None, cid)
            else:
                st.succeeded(ids)  # it answered (a 400 is ours to fix, not a sign of trouble)
            if not ok:
                raise HttpError(r.status_code, url, r.text)
            return Response(r.status_code, r.text, redact_url(str(r.request.url)), dict(r.headers))

    def download(
        self,
        url: str,
        dest: Path,
        *,
        headers: Mapping[str, str] | None = None,
        max_retries: int | None = None,
        max_bytes: int | None = None,
    ) -> Download:
        """Stream a GET into `dest` (overwritten), with the rate limit, pauses and retries of
        `request` but no reuse. The record keeps the bytes' sha256 and size, not the bytes (they go
        to the library's store). Raises HttpError unless the final status is 200."""
        ids = self._ids("GET", url, headers, None, max_retries=max_retries)
        st = self.store
        retries = ids.policy.max_retries
        attempt = 0
        while True:
            self._wait_slot(ids)
            delay = None
            hdrs = dict(headers or {})
            started, t0 = time.time(), time.monotonic()

            def rec(status: str, r: httpx.Response | None = None, **kw: Any) -> str:
                extra = {}
                if r is not None:
                    extra = {"http_status": r.status_code, "resp_headers": dict(r.headers), "final_url": str(r.url),
                             "redirects": [[h.status_code, str(h.url)] for h in r.history],
                             "retry_after": _retry_after(r.headers.get("Retry-After"))}
                return st.record(ids, attempt=attempt, status=status, started=started,
                                 finished=started + time.monotonic() - t0, req_headers=hdrs, **extra, **kw)

            r = None
            try:
                with self._client.stream("GET", url, headers=hdrs, timeout=ids.policy.timeout_s) as r:
                    if r.status_code in RETRY_STATUSES and attempt < retries:
                        rec("http_error", r)
                        delay = self._backoff(ids, attempt, r.headers.get("Retry-After"))
                        log.info("GET %s -> %d; retrying in %.1fs", redact_url(url), r.status_code, delay)
                        st.penalise(ids, self._clock() + delay)
                    elif r.status_code != 200:
                        raw = r.read()
                        cid = rec("http_error", r, body=raw, resp_size=len(raw))
                        if r.status_code in RETRY_STATUSES:
                            self._give_up(ids, r.status_code, None, cid)
                        else:
                            st.succeeded(ids)
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
                                    rec("too_large", r, resp_size=size)
                                    raise HttpError(200, url, f"larger than {max_bytes} bytes")
                        rec("ok", r, resp_size=size, resp_sha256=digest.hexdigest())
                        st.succeeded(ids)
                        return Download(r.status_code, redact_url(str(r.url)),
                                        {k.lower(): v for k, v in r.headers.items()}, size)
            except httpx.TransportError as e:  # includes connections dropped mid-body
                what = type(e).__name__
                cid = rec("no_answer", r, error=what)
                if attempt >= retries:
                    self._give_up(ids, None, what, cid)
                    raise HttpError(0, url, f"{what}: {e}") from e
                delay = self._backoff(ids, attempt, None)
                log.info("GET %s: %s; retrying in %.1fs", redact_url(url), what, delay)
            except KeyboardInterrupt:
                rec("interrupted", r)
                raise
            self._sleep(delay)
            attempt += 1

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
