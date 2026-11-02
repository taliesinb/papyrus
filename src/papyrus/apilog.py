"""Secrets out of HTTP records, and the old JSONL API log.

The redaction rules every recorded exchange goes through (services.py, the web services store):
credentials in headers (``Authorization``, cookies, API-key headers), query and form params in
`SECRET_PARAMS` and JSON fields in `SECRET_FIELDS` are replaced by ``"<redacted>"`` before
anything is written. Bodies are otherwise kept verbatim, and can hold private data (Slack messages).

Until 2026-11-02 every exchange was also appended to ``<library home>/api-log/<UTC date>.jsonl``
(one JSON object per attempt: ``id``, ``t``, ``ms``, ``pid``, ``run_id``, ``attempt``, ``method``,
``url``, ``req_headers``, ``req_body``, ``status``, ``resp_headers``, ``redirects``,
``requested_url``, ``resp_body`` or ``resp_size`` + ``resp_sha256``, ``error``, ``from_cache``).
The web services store replaced it; `events` reads those files for `papyrus services import`.
"""

from __future__ import annotations

import json
import time
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


def events(directory: Path, *, since: str | None = None) -> Iterator[dict[str, Any]]:
    """Every event of an old API log directory, oldest file first; `since` is a UTC date (``YYYY-MM-DD``)."""
    d = Path(directory)
    if not d.exists():
        return
    for p in sorted(d.glob("*.jsonl")):
        if since and p.stem < since:
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
