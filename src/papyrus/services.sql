-- papyrus web services store: <PAPYRUS_ROOT>/services.sqlite (services.py), one per machine.
--
-- Every HTTP exchange papyrus makes (matching APIs, `papyrus download`, Slack, tools): which
-- service, with which credential, under which policy, what was asked, when, how long it took,
-- what came back. Response bodies are files beside it, zstd-compressed and named by their hash:
--
--   <PAPYRUS_ROOT>/services/bodies/<hash>.zst     a response (or POST request) body, redacted
--
-- Downloaded files (PDFs) are not kept here: they go to the library's store, and the call
-- records their full sha256 (= files.sha256). Nothing is garbage-collected: answers older than
-- the policy's `reuse_s` are simply not reused.
--
-- One per machine, not per library: arXiv counts every request from this machine together, so
-- the rate limits and pauses (`state`) are shared by every variant and process. Holds private data
-- (Slack messages): not reachable through MCP's `sql` tool; the directory is 0700, files 0600.
--
-- Ids and hashes are the first 16 hex digits (64 bits) of a sha256 of canonical JSON
-- (services.canonical); a body whose 16 digits are already another body's is named by its full
-- sha256. Times are UTC ISO strings, except `state`'s, which are unix seconds (they are compared).
-- Schema version 0.1 (meta.schema_version).

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A web service: who answers. id = hash of {name, base_url, api_version}.
CREATE TABLE IF NOT EXISTS services (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,        -- arxiv | openalex | crossref | s2 | slack | arxiv-web | web:<host>
    base_url      TEXT NOT NULL,        -- 'https://export.arxiv.org/api'
    api_version   TEXT NOT NULL,        -- '' when the API has none
    first_seen_at TEXT NOT NULL
);

-- What a service was asked with: id = hash of {service_id, kind, fingerprint}. Never the secret
-- itself: `fingerprint` is the first 16 hex digits of the sha256 of "<where>:<secret>".
CREATE TABLE IF NOT EXISTS credentials (
    id            TEXT PRIMARY KEY,
    service_id    TEXT NOT NULL REFERENCES services(id),
    kind          TEXT NOT NULL,        -- public | key | unknown (imported: redacted before we saw it)
    location      TEXT,                 -- where it was sent: 'header:x-api-key', 'header:authorization', 'param:token'
    fingerprint   TEXT,                 -- NULL for public
    first_seen_at TEXT NOT NULL
);

-- How a service was asked: id = hash of {service_id, settings}. settings (JSON): interval_s,
-- timeout_s, max_retries, backoff_base, backoff_cap, pause_s, pause_statuses, reuse_s, scope
-- (services.Policy). Recorded per call, so a change of policy shows in the history.
CREATE TABLE IF NOT EXISTS policies (
    id            TEXT PRIMARY KEY,
    service_id    TEXT NOT NULL REFERENCES services(id),
    settings      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

-- What was asked, without credentials: id = hash of {service_id, method, url, body_hash}, the
-- URL with its query sorted and without secrets or the service's `ignore_params` (`mailto`).
-- The same request with another credential is the same request (and may reuse the answer).
CREATE TABLE IF NOT EXISTS requests (
    id            TEXT PRIMARY KEY,
    service_id    TEXT NOT NULL REFERENCES services(id),
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,        -- as identified (sorted query, secrets and ignore_params dropped)
    body_hash     TEXT,                 -- a POST body (redacted), in bodies/
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_service ON requests(service_id);

-- One exchange: an attempt (retries are calls of their own, attempt = 1, 2, …), or a request
-- not sent because its scope was paused (status 'paused'). A reused answer is not a call: it
-- counts in the original's `hits`.
CREATE TABLE IF NOT EXISTS calls (
    id              TEXT PRIMARY KEY,   -- 16 random hex digits
    request_id      TEXT NOT NULL REFERENCES requests(id),
    service_id      TEXT NOT NULL REFERENCES services(id),
    credential_id   TEXT NOT NULL REFERENCES credentials(id),
    policy_id       TEXT REFERENCES policies(id),   -- NULL for imported calls
    attempt         INTEGER NOT NULL,   -- 0 for the first try
    -- ok (a status the caller accepts) | http_error (any other status) | no_answer (timeout,
    -- connection refused or dropped) | paused (not sent) | interrupted | too_large (download stopped)
    status          TEXT NOT NULL,
    http_status     INTEGER,
    error           TEXT,               -- the exception's type ('ReadTimeout'); never its message
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    url             TEXT NOT NULL,      -- as sent (secrets redacted)
    final_url       TEXT,               -- after redirects, when different
    redirects       TEXT,               -- JSON [[status, url], …]
    req_headers     TEXT,               -- JSON, as sent minus httpx's defaults, secrets redacted
    resp_headers    TEXT,               -- JSON, secrets redacted
    resp_type       TEXT,               -- Content-Type
    resp_hash       TEXT,               -- the body (redacted), in bodies/
    resp_size       INTEGER,            -- bytes of the body as received
    resp_sha256     TEXT,               -- downloads: the full sha256 of the bytes (= files.sha256)
    retry_after_s   REAL,               -- the server's Retry-After, when it gave one
    subject         TEXT,               -- what it was about ('work:1048'), when the caller said
    run_id          INTEGER,            -- the library's `runs` row in progress
    library         TEXT,               -- that library's home (runs are per library)
    pid             INTEGER,
    host            TEXT,
    hits            INTEGER NOT NULL DEFAULT 0,  -- times the answer was reused
    last_hit_at     TEXT,
    imported        TEXT                -- 'api-log' | 'http-cache' for calls brought in from the old stores
);
CREATE INDEX IF NOT EXISTS calls_request ON calls(request_id, status, started_at);
CREATE INDEX IF NOT EXISTS calls_service ON calls(service_id, started_at);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
CREATE INDEX IF NOT EXISTS calls_subject ON calls(subject);
CREATE INDEX IF NOT EXISTS calls_run ON calls(library, run_id);

-- Where each rate limit and pause stands: one row per scope, `<service id>` (scope 'service': the
-- limit is counted per machine, as arXiv's) or `<service id>/<credential id>` (scope 'credential':
-- per key). Shared by every process: a request first reserves the next slot here (a short
-- transaction), then sleeps until it, outside any transaction.
CREATE TABLE IF NOT EXISTS state (
    scope            TEXT PRIMARY KEY,
    service_id       TEXT NOT NULL REFERENCES services(id),
    credential_id    TEXT REFERENCES credentials(id),
    next_ok_at       REAL NOT NULL DEFAULT 0,   -- unix seconds: no request before this
    paused_until     REAL,                       -- unix seconds: none at all before this
    pause_reason     TEXT,                       -- 'HTTP 429' | 'no answer (ReadTimeout)' | 'by hand'
    paused_by        TEXT,                       -- the call that started the pause
    last_ok_at       TEXT,
    last_failure_at  TEXT,
    failures         INTEGER NOT NULL DEFAULT 0, -- requests failed for good since the last success
    updated_at       TEXT NOT NULL
);
