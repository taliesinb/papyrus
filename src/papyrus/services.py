"""The web services store: every HTTP exchange papyrus makes, in a database of its own.

    ask this service (services)  with this credential (credentials)  under this policy (policies)
    this request (requests)  →  calls,  and where each rate limit and pause stands  →  state

`<PAPYRUS_ROOT>/services.sqlite` (DDL: services.sql), one per machine, with the response bodies as
zstd files beside it (`services.bodies/<hash>.zst`; a dot, so no variant can be called that). It replaces the JSONL API log
(`<home>/api-log/`, apilog.py) and the 30-day response cache (`~/.cache/papyrus/http`); `papyrus
services import` brings those in. It is what `http.HttpClient` goes through, and it gives it:

* **reuse**: a request answered before, no older than its cache TTL, with a status the caller
  reuses (200 and 404 by default), is answered from the store, and counts in that call's `hits`.
  The TTL is `cache_ttl`: seconds, None (forever), 0 (never), or AUTO (the default: the service
  policy's `cache_ttl`, from config.toml), per request and per client (`resolve_ttl`). Nothing
  is ever deleted; an older answer is just not reused;
* **one rate limit and one pause per scope, for every process**: before each attempt the request
  reserves the next slot in `state` (a short transaction), then sleeps outside it. A request that
  fails for good with one of the policy's `pause_statuses` (429, 503, no answer) pauses the scope
  for `pause_s`: until then requests aren't sent (`Paused`; matching counts that as "not asked").
  The watcher, the CLI and every variant share it: arXiv's 1 request / 3 s is per machine;
* **the record**: every attempt, with its timing, status, headers and body (secrets redacted:
  apilog.py's rules), the run and the subject (`with subject("work:1048"):`) it was for.

Services are recognised by host (SERVICES; anything else is `web:<host>`). A service's identity
is {name, base_url, api_version}: who answers. The credential (a key's fingerprint, or public) and
the policy (interval, retries, timeout, pause, cache TTL) are recorded per call, not part of it, so
the history of a service survives a new key or a new policy. The policies are config: shipped in
src/papyrus/data/default-config.toml (with the reasons for each), changed per library in
config.toml's [services] and [services.<name>] tables (config.services_layers, `policy_for`).

Ids and hashes: the first 16 hex digits (64 bits) of a sha256 of canonical JSON (inference's rule).
Times in `calls` are UTC ISO strings; `state` keeps unix seconds.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import socket
import sqlite3
import statistics
import threading
import time
import uuid
from compression import zstd
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import apilog, db

SCHEMA_VERSION = "0.1"
HASH_CHARS = 16
DAY = 86400.0

AUTO = sentinel("AUTO")  # cache_ttl: the service policy's (config). Truthy: test it with `is AUTO`

_subject: contextvars.ContextVar[str | None] = contextvars.ContextVar("papyrus_services_subject", default=None)
_run: dict[str, Any] = {"run_id": None, "library": None}  # process-wide, so download threads inherit it


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode()


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS]


def now_iso(ts: float | None = None) -> str:
    return apilog.now_iso(ts)


@contextlib.contextmanager
def subject(what: str | None) -> Iterator[None]:
    """Requests made inside are about this (`work:1048`, a file's sha256): `calls.subject`."""
    token = _subject.set(what)
    try:
        yield
    finally:
        _subject.reset(token)


def set_run(run_id: int | None, library: str | None = None) -> tuple[int | None, str | None]:
    """Tag later calls with the library's run in progress (db.recorded_run). Returns the previous
    (run_id, library), for restoring."""
    prev = (_run["run_id"], _run["library"])
    _run["run_id"], _run["library"] = run_id, library
    return prev


# --- services and their policies ----------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """How a service is asked. The values in effect come from config (default-config.toml's
    [services] and [services.<name>], then the library's config.toml); these are only the fallback
    should a key be missing from both."""

    interval_s: float = 0.0       # at least this long between requests (per scope)
    timeout_s: float = 30.0       # per attempt
    max_retries: int = 5          # on 429, 5xx and no answer
    backoff_base: float = 2.0     # retry waits: base * 2^attempt (jittered), or Retry-After
    backoff_cap: float = 60.0
    pause_s: float = 0.0          # after a request fails for good with a pause status: ask nothing for this long
    pause_statuses: tuple[int, ...] = (0, 429, 503)  # 0: no answer (timeout, connection refused)
    cache_ttl: float = 30 * DAY   # reuse an answer this young (0: never; inf: forever)
    scope: str = "credential"     # what the rate limit and pause count: "credential" (per key) | "service"

    def settings(self) -> dict[str, Any]:
        d = asdict(self)
        d["pause_statuses"] = list(self.pause_statuses)
        if d["cache_ttl"] == math.inf:
            d["cache_ttl"] = "inf"  # (JSON has no infinity)
        return d


@dataclass(frozen=True)
class Service:
    """Who answers. Its policy is config: [services.<name>] (`policy_for`)."""

    name: str
    hosts: tuple[str, ...]
    base_url: str
    api_version: str = ""
    ignore_params: frozenset[str] = frozenset({"mailto"})  # not part of a request's identity


SERVICES: tuple[Service, ...] = (
    Service("arxiv", ("export.arxiv.org",), "https://export.arxiv.org/api"),
    Service("arxiv-web", ("arxiv.org", "www.arxiv.org"), "https://arxiv.org"),
    Service("openalex", ("api.openalex.org",), "https://api.openalex.org"),
    Service("crossref", ("api.crossref.org",), "https://api.crossref.org"),
    Service("s2", ("api.semanticscholar.org",), "https://api.semanticscholar.org/graph", "v1"),
    Service("slack", ("slack.com",), "https://slack.com/api"),
)
_BY_HOST = {h: s for s in SERVICES for h in s.hosts}


def service_for(url: str) -> Service:
    host = (urlsplit(url).hostname or "").lower()
    if host in _BY_HOST:
        return _BY_HOST[host]
    return Service(f"web:{host}", (host,), f"{urlsplit(url).scheme or 'https'}://{host}")


def by_name(name: str) -> Service | None:
    return next((s for s in SERVICES if s.name == name), None)


Layers = list[tuple[dict[str, Any], dict[str, dict[str, Any]]]]  # ([services] keys, {name: [services.<name>] keys})
_layers_cache: dict[str, Any] = {"key": None, "value": None}


def layers() -> Layers:
    """The policy settings in force (config.services_layers: the shipped defaults, then this
    library's config.toml), re-read when the config file changes. A bad table is reported and the
    shipped defaults used alone: HTTP must not stop over it."""
    from . import config

    p = config.path()
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        key = (str(p), None)
    if _layers_cache["key"] != key:
        try:
            value = config.services_layers()
        except config.ConfigError as e:
            import logging

            logging.getLogger(__name__).warning("%s (using papyrus's default service policies)", e)
            value = [config.default_services_layer()]
        _layers_cache.update(key=key, value=value)
    return _layers_cache["value"]


def effective_policy(name: str) -> Policy | None:
    """A known service's policy as this library has it (the defaults, then config.toml)."""
    svc = by_name(name)
    return policy_for(svc, layers()) if svc else None


def policy_for(svc: Service, layers: Layers | None = None, **kw: Any) -> Policy:
    """The service's policy: Policy()'s fallbacks < every layer's [services] keys < every layer's
    [services.<name>] keys (for any other host: [services.web], then [services."web:<host>"]) <
    `kw` (a caller's max_retries, timeout_s, …; None values ignored)."""
    names = ["web", svc.name] if svc.name.startswith("web:") else [svc.name]
    o: dict[str, Any] = {}
    for base, _ in layers or ():
        o.update(base)
    for n in names:
        for _, per in layers or ():
            o.update(per.get(n) or {})
    o.update({k: v for k, v in kw.items() if v is not None})
    if "pause_statuses" in o:
        o["pause_statuses"] = tuple(o["pause_statuses"])
    return replace(Policy(), **o)


def resolve_ttl(*choices: Any, policy: Policy) -> float:
    """The cache TTL in seconds (inf: forever) for a request: the first of `choices` (the call's
    `cache_ttl`, then the client's) that isn't AUTO, None meaning forever; else 0 if
    PAPYRUS_HTTP_CACHE=0; else the policy's."""
    for c in choices:
        if c is AUTO:
            continue
        if c is None:
            return math.inf
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not c >= 0:
            raise ValueError(f"cache_ttl must be seconds >= 0, None (forever) or AUTO, got {c!r}")
        return float(c)
    if os.environ.get("PAPYRUS_HTTP_CACHE", "").strip().lower() in {"0", "false", "no", "off"}:
        return 0.0
    return policy.cache_ttl


# --- identities ----------------------------------------------------------------------------------


def identity_url(url: str, svc: Service) -> str:
    """The URL as a request's identity: query sorted, secrets and the service's ignore_params dropped."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    drop = apilog.SECRET_PARAMS | svc.ignore_params
    q = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in drop)
    return urlunsplit(parts._replace(query=urlencode(q)))


CREDENTIAL_HEADERS = ("authorization", "x-api-key", "api-key", "crossref-plus-api-token", "x-auth-token")


def credential_of(url: str, headers: Mapping[str, str] | None) -> tuple[str, str | None, str | None]:
    """(kind, location, fingerprint) of the secret a request carries, or ('public', None, None).
    A value already redacted (an imported log line) gives kind 'unknown'."""
    for k, v in (headers or {}).items():
        if k.lower() in CREDENTIAL_HEADERS and v:
            loc = f"header:{k.lower()}"
            return (("unknown", loc, None) if v == apilog.REDACTED
                    else ("key", loc, short_hash(f"{loc}:{v}".encode())))
    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if k.lower() in apilog.SECRET_PARAMS and v:
            loc = f"param:{k.lower()}"
            return (("unknown", loc, None) if v == apilog.REDACTED
                    else ("key", loc, short_hash(f"{loc}:{v}".encode())))
    return "public", None, None


@dataclass
class Ids:
    service: Service
    policy: Policy
    service_id: str
    credential_id: str
    policy_id: str | None
    request_id: str
    scope: str
    url: str           # as sent, secrets redacted
    body_hash: str | None


@dataclass
class Hit:
    call_id: str
    status: int
    text: str
    url: str
    headers: dict[str, str]


class Paused(Exception):
    """The scope is paused: the request was not sent."""

    def __init__(self, service: str, until: float, reason: str | None):
        super().__init__(f"{service} paused until {now_iso(until)} ({reason})")
        self.service, self.until, self.reason = service, until, reason


# --- the store -----------------------------------------------------------------------------------


def root() -> Path:
    """Where the store lives: PAPYRUS_SERVICES, default PAPYRUS_ROOT (~/.papyrus)."""
    env = os.environ.get("PAPYRUS_SERVICES", "").strip()
    return Path(env).expanduser() if env else db.papyrus_root()


class Store:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "services.sqlite"
        self.bodies = self.dir / "services.bodies"  # a dot: not a name a variant (~/.papyrus/<v>) can have
        old = self.dir / "services" / "bodies"  # where they were for an hour on 2026-11-02 (remove in 2026-12)
        if old.is_dir() and not self.bodies.exists():
            try:
                os.rename(old, self.bodies)
                old.parent.rmdir()
            except OSError:
                pass
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()  # one connection, shared by download's threads
        self._known: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.dir.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists()
                c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
                if new:
                    os.chmod(self.path, 0o600)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA synchronous = NORMAL")  # WAL: no corruption; a power cut loses the last calls
                c.execute("PRAGMA busy_timeout = 10000")
                row = None
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                    row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is not None and db.version(row[0]) != db.version(SCHEMA_VERSION):
                    raise RuntimeError(f"{self.path}: services schema v{row[0]}, this papyrus has v{SCHEMA_VERSION}")
                c.executescript(resources.files("papyrus").joinpath("services.sql").read_text())
                if row is None:
                    c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
                self._conn = c
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(args))

    @contextlib.contextmanager
    def _batch(self) -> Iterator[None]:
        """One transaction for many writes (the imports), holding the lock."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # bodies

    def body_path(self, h: str) -> Path:
        return self.bodies / f"{h}.zst"

    def write_body(self, data: bytes) -> str:
        """Store bytes under their hash (zstd); returns the hash (the full sha256 if the short one
        is another body's)."""
        h = short_hash(data)
        p = self.body_path(h)
        if p.exists():
            try:
                same = zstd.decompress(p.read_bytes()) == data
            except (OSError, zstd.ZstdError):
                same = False
            if not same:
                h = hashlib.sha256(data).hexdigest()
                p = self.body_path(h)
        if not p.exists():
            if not self.bodies.exists():
                self.bodies.mkdir(parents=True, exist_ok=True)
                os.chmod(self.bodies, 0o700)
            tmp = p.with_name(f".{uuid.uuid4().hex}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(zstd.compress(data, level=3))
            os.replace(tmp, p)
        return h

    def read_body(self, h: str | None) -> bytes | None:
        if not h:
            return None
        try:
            return zstd.decompress(self.body_path(h).read_bytes())
        except (OSError, zstd.ZstdError):
            return None

    # identities

    def _insert_once(self, key: str, sql: str, args: tuple) -> None:
        if key not in self._known:
            self._x(sql, args)
            self._known.add(key)

    def service_id(self, svc: Service) -> str:
        sid = short_hash(canonical({"name": svc.name, "base_url": svc.base_url, "api_version": svc.api_version}))
        self._insert_once(sid, "INSERT OR IGNORE INTO services(id, name, base_url, api_version, first_seen_at)"
                               " VALUES (?,?,?,?,?)", (sid, svc.name, svc.base_url, svc.api_version, now_iso()))
        return sid

    def credential_id(self, service_id: str, kind: str, location: str | None, fingerprint: str | None) -> str:
        cid = short_hash(canonical({"service": service_id, "kind": kind, "location": location,
                                    "fingerprint": fingerprint}))
        self._insert_once(cid, "INSERT OR IGNORE INTO credentials(id, service_id, kind, location, fingerprint,"
                               " first_seen_at) VALUES (?,?,?,?,?,?)",
                          (cid, service_id, kind, location, fingerprint, now_iso()))
        return cid

    def policy_id(self, service_id: str, policy: Policy) -> str:
        settings = policy.settings()
        pid = short_hash(canonical({"service": service_id, "settings": settings}))
        self._insert_once(pid, "INSERT OR IGNORE INTO policies(id, service_id, settings, first_seen_at)"
                               " VALUES (?,?,?,?)", (pid, service_id, canonical(settings).decode(), now_iso()))
        return pid

    def request_id(self, service_id: str, method: str, ident_url: str, body_hash: str | None) -> str:
        rid = short_hash(canonical({"service": service_id, "method": method, "url": ident_url, "body": body_hash}))
        self._insert_once(rid, "INSERT OR IGNORE INTO requests(id, service_id, method, url, body_hash, first_seen_at)"
                               " VALUES (?,?,?,?,?,?)", (rid, service_id, method, ident_url, body_hash, now_iso()))
        return rid

    def ids(self, method: str, url: str, headers: Mapping[str, str] | None, body: str | None,
            policy: Policy | None = None, layers: Layers | None = None,
            record_policy: bool = True, **policy_kw: Any) -> Ids:
        """Every id a request needs (registering them), and its effective policy (`record_policy`
        False: no policy row, for imported calls)."""
        method = method.upper()
        svc = service_for(url)
        pol = policy or policy_for(svc, layers, **policy_kw)
        sid = self.service_id(svc)
        kind, loc, fp = credential_of(url, headers)
        cid = self.credential_id(sid, kind, loc, fp)
        bh = self.write_body(body.encode()) if body else None
        rid = self.request_id(sid, method, identity_url(url, svc), bh)
        scope = sid if pol.scope == "service" else f"{sid}/{cid}"
        return Ids(svc, pol, sid, cid, self.policy_id(sid, pol) if record_policy else None, rid, scope,
                   apilog.redact_url(url), bh)

    # reuse

    def lookup(self, request_id: str, statuses: Iterable[int], ttl: float,
               now: float | None = None) -> Hit | None:
        """The newest successful answer to this request, no older than `ttl` seconds (inf: any
        age; 0: none), with one of `statuses`, whose body is still there; counts the hit."""
        if not ttl > 0:
            return None
        statuses = sorted(set(statuses))
        if not statuses:
            return None
        cutoff = "" if ttl == math.inf else now_iso(max(0.0, (time.time() if now is None else now) - ttl))
        rows = self._x(f"SELECT id, http_status, resp_hash, resp_headers, url, final_url FROM calls WHERE request_id=?"
                       f" AND status='ok' AND http_status IN ({','.join('?' * len(statuses))}) AND started_at >= ?"
                       " ORDER BY started_at DESC LIMIT 5", (request_id, *statuses, cutoff)).fetchall()
        for r in rows:
            data = self.read_body(r["resp_hash"])
            if data is None and r["resp_hash"]:
                continue
            self._x("UPDATE calls SET hits=hits+1, last_hit_at=? WHERE id=?", (now_iso(), r["id"]))
            return Hit(r["id"], r["http_status"], (data or b"").decode("utf-8", "replace"), r["final_url"] or r["url"],
                       json.loads(r["resp_headers"] or "{}"))
        return None

    # rate limits and pauses

    def claim(self, ids: Ids, now: float) -> float:
        """Reserve this scope's next slot: the seconds to wait for it. Raises Paused (and records a
        'paused' call) while the scope is paused."""
        with self._lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT next_ok_at, paused_until, pause_reason FROM state WHERE scope=?",
                              (ids.scope,)).fetchone()
                if r is not None and r["paused_until"] and r["paused_until"] > now:
                    c.execute("COMMIT")
                    self.record(ids, attempt=0, status="paused", started=now, finished=now)
                    raise Paused(ids.service.name, r["paused_until"], r["pause_reason"])
                start = max(now, r["next_ok_at"] if r is not None else 0.0)
                if r is None or ids.policy.interval_s > 0:
                    c.execute("INSERT INTO state(scope, service_id, credential_id, next_ok_at, updated_at)"
                              " VALUES (?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET next_ok_at=excluded.next_ok_at,"
                              " updated_at=excluded.updated_at",
                              (ids.scope, ids.service_id, None if ids.policy.scope == "service" else ids.credential_id,
                               start + ids.policy.interval_s, now_iso()))
                c.execute("COMMIT")
            except Paused:
                raise
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        return start - now

    def ensure_scope(self, ids: Ids) -> None:
        self._x("INSERT OR IGNORE INTO state(scope, service_id, credential_id, updated_at) VALUES (?,?,?,?)",
                (ids.scope, ids.service_id, None if ids.policy.scope == "service" else ids.credential_id, now_iso()))

    def penalise(self, ids: Ids, until: float) -> None:
        """No request in this scope before `until` (a retry's wait)."""
        self._x("UPDATE state SET next_ok_at=MAX(next_ok_at, ?), updated_at=? WHERE scope=?",
                (until, now_iso(), ids.scope))

    def succeeded(self, ids: Ids) -> None:
        self._x("UPDATE state SET failures=0, last_ok_at=?, updated_at=? WHERE scope=?",
                (now_iso(), now_iso(), ids.scope))

    def failed(self, ids: Ids, http_status: int | None, error: str | None, call_id: str, now: float) -> float | None:
        """A request failed for good: count it, and pause the scope if the policy says so. Returns
        the pause's end (unix seconds) or None."""
        p = ids.policy
        pause = p.pause_s > 0 and (http_status or 0) in p.pause_statuses
        reason = f"HTTP {http_status}" if http_status else f"no answer ({error})" if error else None
        until = now + p.pause_s if pause else None
        self._x("UPDATE state SET failures=failures+1, last_failure_at=?, updated_at=?,"
                " paused_until=COALESCE(?, paused_until), pause_reason=CASE WHEN ? IS NULL THEN pause_reason ELSE ? END,"
                " paused_by=CASE WHEN ? IS NULL THEN paused_by ELSE ? END WHERE scope=?",
                (now_iso(), now_iso(), until, until, reason, until, call_id, ids.scope))
        return until

    def resume(self, name: str | None = None) -> int:
        """End the pauses of a service (by name), or of every service. Returns how many."""
        if name is None:
            return self._x("UPDATE state SET paused_until=NULL, updated_at=? WHERE paused_until IS NOT NULL",
                           (now_iso(),)).rowcount
        return self._x("UPDATE state SET paused_until=NULL, updated_at=? WHERE paused_until IS NOT NULL AND service_id"
                       " IN (SELECT id FROM services WHERE name=?)", (now_iso(), name)).rowcount

    def pause(self, name: str, seconds: float, reason: str = "by hand") -> int:
        """Pause a service by hand (`papyrus services pause`)."""
        until = time.time() + seconds
        return self._x("UPDATE state SET paused_until=?, pause_reason=?, paused_by=NULL, updated_at=? WHERE service_id"
                       " IN (SELECT id FROM services WHERE name=?)", (until, reason, now_iso(), name)).rowcount

    # the record

    def record(self, ids: Ids, *, attempt: int, status: str, started: float, finished: float,
               http_status: int | None = None, error: str | None = None, req_headers: Mapping[str, str] | None = None,
               resp_headers: Mapping[str, str] | None = None, body: str | bytes | None = None,
               resp_size: int | None = None, resp_sha256: str | None = None, final_url: str | None = None,
               redirects: list | None = None, retry_after: float | None = None) -> str:
        """One call; returns its id. `body` (the response) is redacted and stored."""
        call_id = uuid.uuid4().hex[:HASH_CHARS]
        rh = None
        if body is not None:
            text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
            ctype = (resp_headers or {}).get("content-type")
            rh = self.write_body((apilog.redact_body(text, ctype) or "").encode())
        self._x(
            "INSERT INTO calls(id, request_id, service_id, credential_id, policy_id, attempt, status, http_status, error,"
            " started_at, finished_at, duration_ms, url, final_url, redirects, req_headers, resp_headers, resp_type,"
            " resp_hash, resp_size, resp_sha256, retry_after_s, subject, run_id, library, pid, host)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, ids.request_id, ids.service_id, ids.credential_id, ids.policy_id, attempt, status, http_status,
             error, now_iso(started), now_iso(finished), round((finished - started) * 1000), ids.url,
             apilog.redact_url(final_url) if final_url and final_url != ids.url else None,
             json.dumps(redirects) if redirects else None,
             json.dumps(apilog.request_headers(req_headers)) if req_headers else None,
             json.dumps(apilog.redact_headers(resp_headers)) if resp_headers else None,
             (resp_headers or {}).get("content-type"), rh, resp_size, resp_sha256, retry_after, _subject.get(),
             _run["run_id"], _run["library"], os.getpid(), socket.gethostname()))
        return call_id

    # reading

    def states(self) -> list[dict[str, Any]]:
        rows = self._x("SELECT st.*, s.name, c.kind AS credential FROM state st JOIN services s ON s.id=st.service_id"
                       " LEFT JOIN credentials c ON c.id=st.credential_id ORDER BY s.name").fetchall()
        return [dict(r) for r in rows]

    def stats(self, since: str | None = None) -> list[dict[str, Any]]:
        """Per service and credential kind: calls by status, error statuses, hits, timings."""
        groups: dict[tuple, dict] = {}
        for r in self._x("SELECT s.name, cr.kind, c.status, c.http_status, c.error, c.duration_ms, c.hits, c.started_at"
                         " FROM calls c JOIN services s ON s.id=c.service_id JOIN credentials cr ON cr.id=c.credential_id"
                         " WHERE (? IS NULL OR c.started_at >= ?) ORDER BY c.started_at", (since, since)):
            g = groups.setdefault((r["name"], r["kind"]), {
                "service": r["name"], "credential": r["kind"], "calls": 0, "ok": 0, "failed": 0, "paused": 0,
                "hits": 0, "errors": {}, "ms": [], "first": r["started_at"], "last": r["started_at"]})
            g["last"] = r["started_at"]
            g["hits"] += r["hits"]
            if r["status"] == "paused":
                g["paused"] += 1
                continue
            g["calls"] += 1
            if r["status"] == "ok":
                g["ok"] += 1
                if r["duration_ms"] is not None:
                    g["ms"].append(r["duration_ms"])
            else:
                g["failed"] += 1
                what = f"HTTP {r['http_status']}" if r["http_status"] else (r["error"] or r["status"])
                g["errors"][what] = g["errors"].get(what, 0) + 1
        out = []
        for g in groups.values():
            ms = sorted(g.pop("ms"))
            g["median_ms"] = round(statistics.median(ms)) if ms else None
            g["p90_ms"] = ms[int(0.9 * (len(ms) - 1))] if ms else None
            out.append(g)
        return sorted(out, key=lambda g: (g["service"], g["credential"]))

    def show(self, call_id: str) -> dict[str, Any] | None:
        r = self._x("SELECT c.*, s.name AS service, r.method, r.url AS request_url, r.body_hash, cr.kind AS credential,"
                    " p.settings AS policy FROM calls c JOIN services s ON s.id=c.service_id JOIN requests r"
                    " ON r.id=c.request_id JOIN credentials cr ON cr.id=c.credential_id LEFT JOIN policies p"
                    " ON p.id=c.policy_id WHERE c.id LIKE ? ORDER BY c.started_at DESC LIMIT 1",
                    (call_id + "%",)).fetchone()
        if r is None:
            return None
        out = dict(r)
        body = self.read_body(r["resp_hash"])
        out["body"] = None if body is None else body.decode("utf-8", "replace")
        req = self.read_body(r["body_hash"])
        out["request_body"] = None if req is None else req.decode("utf-8", "replace")
        return out

    # bringing in the old stores

    def import_api_log(self, directory: Path, library: str | None = None) -> dict[str, int]:
        """Calls from the JSONL API log (apilog.py) of one library. Each file is read on from where
        the last import stopped (meta `import:<path>` = lines read: the files are append-only), so
        importing again takes only what was added. Cache-hit events add to the newest earlier
        call's hits."""
        n = {"calls": 0, "hits": 0, "skipped": 0}
        library = library or str(Path(directory).parent)
        for p in sorted(Path(directory).glob("*.jsonl")):
            key = f"import:{p.resolve()}"
            r = self._x("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            done = int(r[0]) if r else 0
            with open(p, encoding="utf-8") as fh:
                lines = fh.readlines()
            with self._batch():
                for line in lines[done:]:
                    if not line.endswith("\n"):  # being written: next time
                        break
                    done += 1
                    if line.strip():
                        try:
                            self._import_event(json.loads(line), library, n)
                        except ValueError:
                            n["skipped"] += 1
                self._x("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(done)))
        return n

    def _import_event(self, ev: dict[str, Any], library: str, n: dict[str, int]) -> None:
        url = ev.get("requested_url") or ev.get("url") or ""
        if not url:
            n["skipped"] += 1
            return
        body = ev.get("req_body")
        ids = self.ids(ev.get("method") or "GET", url, ev.get("req_headers"), body, policy=Policy(),
                       record_policy=False)
        if ev.get("from_cache"):
            r = self._x("SELECT id FROM calls WHERE request_id=? AND status='ok' AND started_at <= ?"
                        " ORDER BY started_at DESC LIMIT 1", (ids.request_id, ev.get("t"))).fetchone()
            if r:
                self._x("UPDATE calls SET hits=hits+1, last_hit_at=MAX(COALESCE(last_hit_at, ''), ?) WHERE id=?",
                        (ev.get("t"), r["id"]))
                n["hits"] += 1
            return
        cid = str(ev.get("id") or uuid.uuid4().hex)[:HASH_CHARS]
        if self._x("SELECT 1 FROM calls WHERE id=?", (cid,)).fetchone():
            return
        status = ev.get("status")
        err = ev.get("error")
        # what HttpClient accepted then: < 400, and an API's 404 ("no such record"), not a download's
        api = ids.service.name in {s.name for s in SERVICES} and ids.service.name != "arxiv-web"
        state = ("too_large" if err and "larger than" in err else "no_answer" if status is None
                 else "ok" if status < 400 or (status == 404 and api and not ev.get("resp_sha256"))
                 else "http_error")
        rh = self.write_body(ev["resp_body"].encode()) if ev.get("resp_body") is not None else None
        started = _parse_iso(ev.get("t"))
        hdrs = ev.get("resp_headers") or {}
        final = ev.get("url") if ev.get("requested_url") else None
        self._x(
            "INSERT OR IGNORE INTO calls(id, request_id, service_id, credential_id, policy_id, attempt, status,"
            " http_status, error, started_at, finished_at, duration_ms, url, final_url, redirects, req_headers,"
            " resp_headers, resp_type, resp_hash, resp_size, resp_sha256, run_id, library, pid, imported)"
            " VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'api-log')",
            (cid, ids.request_id, ids.service_id, ids.credential_id, ev.get("attempt") or 0, state, status,
             (err or "").split(":", 1)[0] or None, ev.get("t"),
             now_iso(started + (ev.get("ms") or 0) / 1000) if started else None, ev.get("ms"), apilog.redact_url(url),
             final, json.dumps(ev["redirects"]) if ev.get("redirects") else None,
             json.dumps(ev["req_headers"]) if ev.get("req_headers") else None, json.dumps(hdrs) if hdrs else None,
             hdrs.get("content-type"), rh, ev.get("resp_size"), ev.get("resp_sha256"), ev.get("run_id"), library,
             ev.get("pid")))
        n["calls"] += 1

    def import_http_cache(self, directory: Path) -> dict[str, int]:
        """Answers from the old response cache (`~/.cache/papyrus/http`), as calls of unknown
        credential, unless the API log already brought in the same answer."""
        n = {"calls": 0, "known": 0, "skipped": 0}
        with self._batch():
            self._import_cache_files(sorted(Path(directory).glob("*.json")), n)
        return n

    def _import_cache_files(self, paths: list[Path], n: dict[str, int]) -> None:
        for p in paths:
            try:
                e = json.loads(p.read_text())
            except (OSError, ValueError):
                n["skipped"] += 1
                continue
            key = e.get("key") or ""
            method, _, rest = key.partition(" ")
            url, _, body = rest.partition("\n")
            if not url or "status" not in e:
                n["skipped"] += 1
                continue
            ids = self.ids(method or "GET", url, None, body or None, policy=Policy(), record_policy=False)
            cred = self.credential_id(ids.service_id, "unknown", None, None)
            text = e.get("body") or ""
            rh = self.write_body((apilog.redact_body(text, (e.get("headers") or {}).get("content-type")) or "").encode())
            if self._x("SELECT 1 FROM calls WHERE request_id=? AND resp_hash=?", (ids.request_id, rh)).fetchone():
                n["known"] += 1
                continue
            started = now_iso(float(e.get("fetched_at") or p.stat().st_mtime))
            cid = short_hash(canonical({"key": key, "fetched_at": e.get("fetched_at")}))
            hdrs = {k.lower(): v for k, v in (e.get("headers") or {}).items()}
            self._x("INSERT OR IGNORE INTO calls(id, request_id, service_id, credential_id, policy_id, attempt, status,"
                    " http_status, started_at, finished_at, url, resp_headers, resp_type, resp_hash, resp_size, imported)"
                    " VALUES (?,?,?,?,NULL,0,'ok',?,?,?,?,?,?,?,?,'http-cache')",
                    (cid, ids.request_id, ids.service_id, cred, e["status"], started, started, ids.url,
                     json.dumps(hdrs) if hdrs else None, hdrs.get("content-type"), rh, len(text.encode())))
            n["calls"] += 1


def _parse_iso(t: str | None) -> float | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


_stores: dict[Path, Store] = {}
_stores_lock = threading.Lock()


def store() -> Store:
    """The machine's services store (PAPYRUS_SERVICES, else PAPYRUS_ROOT)."""
    d = root()
    with _stores_lock:
        if d not in _stores:
            _stores[d] = Store(d)
        return _stores[d]
