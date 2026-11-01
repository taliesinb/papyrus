"""Append-only log of every HTTP exchange papyrus makes through `http.HttpClient`.

API responses carry more than we extract at the time (fields we ignore today, rate-limit
headers, records we only skim). They are never thrown away: each attempt, retries and
errors included, becomes one JSON line in ``<library home>/api-log/<UTC date>.jsonl``, so
later code can trawl old responses instead of calling the API again.

One event (``"v": 1``):

* ``id`` (uuid hex), ``t`` (start, UTC ISO), ``ms`` (duration), ``pid``, ``run_id``
  (the `runs` row in progress, if any), ``attempt`` (0 for the first try)
* ``method``, ``url`` (final, secrets redacted), ``req_headers`` (as sent, minus
  httpx defaults, secrets redacted), ``req_body`` (text, secrets redacted)
* ``status``, ``resp_headers``, ``redirects`` (``[[status, url], ...]`` before the final
  response), and either ``resp_body`` (text) or, for `download`, ``resp_size`` and
  ``resp_sha256`` (the bytes themselves go to the managed store)
* ``error`` for transport errors (then there is no status)
* ``from_cache: true`` events record that a cached response was used (no body: it was
  logged when it was fetched)

Secrets: credentials in headers (``Authorization``, cookies, API-key headers), query and
form params in `SECRET_PARAMS` and JSON fields in `SECRET_FIELDS` are replaced by
``"<redacted>"`` before anything is written. Bodies are otherwise stored verbatim, and
can hold private data (Slack messages): the directory is created 0700, files 0600.

Environment: ``PAPYRUS_API_LOG`` = ``0`` turns logging off, or a directory to use instead.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "<redacted>"
SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-auth-token",
})
# Query and form parameters that carry credentials (Slack accepts `token=`).
SECRET_PARAMS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "refresh_token", "id_token", "client_secret",
    "password", "code_verifier",
})
# JSON fields that carry credentials. Narrower than SECRET_PARAMS: a `key` or `token` field
# in an API record is usually data (Crossref reference keys), and data is never dropped.
SECRET_FIELDS = frozenset({
    "api_key", "apikey", "access_token", "refresh_token", "id_token", "client_secret", "password",
    "code_verifier",
})
# Headers httpx adds to every request; logging them on every line is noise.
_DEFAULT_REQ_HEADERS = frozenset({"host", "accept", "accept-encoding", "connection", "user-agent", "content-length"})

_lock = threading.Lock()
_run_id: int | None = None


def set_run(run_id: int | None) -> int | None:
    """Tag later events with `run_id` (process-wide, so download threads inherit it). Returns
    the previous value, for restoring."""
    global _run_id
    prev, _run_id = _run_id, run_id
    return prev


def log_dir() -> Path | None:
    env = os.environ.get("PAPYRUS_API_LOG", "").strip()
    if env.lower() in {"0", "false", "no", "off"}:
        return None
    if env:
        return Path(env).expanduser()
    from . import db  # late import: db imports this module

    return db.library_home() / "api-log"


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, REDACTED if k.lower() in SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(q, safe="<>")))


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if isinstance(k, str) and k.lower() in SECRET_FIELDS and v else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def redact_body(body: str | None, content_type: str | None = None) -> str | None:
    """Replace secret fields in a JSON or form-encoded body; other text is returned as is."""
    if not body:
        return body
    ct = (content_type or "").lower()
    s = body.lstrip()
    if s[:1] in "{[":
        try:
            data = json.loads(body)
        except ValueError:
            return body
        clean = _scrub(data)
        return body if clean == data else json.dumps(clean, ensure_ascii=False)
    if "x-www-form-urlencoded" in ct:
        pairs = parse_qsl(body, keep_blank_values=True)
        if any(k.lower() in SECRET_PARAMS for k, _ in pairs):
            return urlencode([(k, REDACTED if k.lower() in SECRET_PARAMS else v) for k, v in pairs], safe="<>")
    return body


def request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in redact_headers(headers).items() if k not in _DEFAULT_REQ_HEADERS}


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record(event: dict[str, Any]) -> None:
    """Append one event. Never raises: losing a log line must not break the caller, but it
    is reported on stderr."""
    d = log_dir()
    if d is None:
        return
    ev = {"v": 1, "id": uuid.uuid4().hex, "pid": os.getpid(), "run_id": _run_id, **event}
    line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = d / f"{ev.get('t', now_iso())[:10]}.jsonl"
    try:
        with _lock:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)  # other papyrus processes share the file
                os.write(fd, line.encode())
            finally:
                os.close(fd)
    except OSError as e:
        import sys

        print(f"papyrus: could not write the API log {path}: {e}", file=sys.stderr)


def events(directory: Path | None = None, *, since: str | None = None) -> Iterator[dict[str, Any]]:
    """Every logged event, oldest file first; `since` is a UTC date (``YYYY-MM-DD``)."""
    d = directory or log_dir()
    if d is None or not d.exists():
        return
    for p in sorted(d.glob("*.jsonl")):
        if since and p.stem < since:
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
