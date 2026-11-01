-- papyrus inference cache: <library home>/inference.sqlite (inference.py).
--
-- Every question asked of a model: which model, which question, a hash of the data it was
-- asked about, when, how long it took, and a hash of the answer. The texts themselves (the
-- data, the answer) are files beside it, named by their hash:
--
--   <home>/inference/inputs/<hash>.txt     the data (a document's first pages, …)
--   <home>/inference/outputs/<hash>.json   the answer
--
-- The database holds no document text, so it can be kept (for timing statistics, and to see
-- every inference in one place) while the files are deleted: `papyrus inference prune`, and
-- `purge` for a document's calls. Separate from papyrus.sqlite, and not reachable through
-- MCP's `sql` tool. Ids and hashes are the first 16 hex digits (64 bits) of a sha256, of canonical
-- JSON for models and queries (inference.canonical); a text whose 16 digits are already another
-- text's is named by its full sha256. Schema version 2 (meta.schema_version).

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A model, as far as its answers go: id = hash of {backend, model, build}. The endpoint is
-- not part of it (the same weights answer the same on localhost:1234 or another port); it is
-- recorded per call.
CREATE TABLE IF NOT EXISTS models (
    id            TEXT PRIMARY KEY,
    backend       TEXT NOT NULL,        -- lmstudio | apple | jev | openai | …
    model         TEXT NOT NULL,        -- 'qwen3.5-4b-mlx'
    build         TEXT NOT NULL,        -- JSON: what pins the weights (LM Studio quantization, arch,
                                        -- format; Apple: the OS release)
    seed_honoured INTEGER,              -- 1 / 0 / NULL unknown: does the same seed give the same answer?
    first_seen_at TEXT NOT NULL
);

-- A question, without the data it is asked about: id = hash of {kind, instructions,
-- output_schema, examples, params}. params hold everything else that shapes the answer:
-- temperature, max_tokens, seed, and the backend's own settings (cache_params()).
CREATE TABLE IF NOT EXISTS queries (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,        -- extract | decide
    instructions  TEXT NOT NULL,        -- extract: the system instructions; decide: the question
    output_schema TEXT NOT NULL,        -- JSON: extract: the JSON Schema; decide: {key, type, criteria}
    examples      TEXT,                 -- JSON, when the question carries examples
    params        TEXT NOT NULL,        -- JSON
    first_seen_at TEXT NOT NULL
);

-- One model call. The cache answers (model_id, query_id, input_hash) with the newest 'ok'
-- call whose output file still exists; `hits` counts the times it did.
CREATE TABLE IF NOT EXISTS calls (
    id              TEXT PRIMARY KEY,   -- 16 random hex digits, referenced by papyrus.sqlite assertions.call_id
    model_id        TEXT NOT NULL REFERENCES models(id),
    query_id        TEXT NOT NULL REFERENCES queries(id),
    input_hash      TEXT NOT NULL,
    input_chars     INTEGER NOT NULL,
    subject         TEXT,               -- sha256 of the document asked about (files.sha256), so
                                        -- purge finds every call about it; NULL if none
    status          TEXT NOT NULL,      -- running | ok | error | busy | unavailable | interrupted
    error           TEXT,               -- the exception's type only (messages can hold document text)
    output_hash     TEXT,
    output_chars    INTEGER,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    input_tokens    INTEGER,            -- as the backend reports them
    output_tokens   INTEGER,
    model_loaded    INTEGER,            -- LM Studio: was the model loaded before the call (else the
                                        -- duration includes loading it)
    memory_pressure TEXT,               -- normal | warn | critical, when known
    endpoint        TEXT,
    host            TEXT,
    extra           TEXT,               -- JSON: what the backend actually did (a retry's temperature, …)
    hits            INTEGER NOT NULL DEFAULT 0,
    last_hit_at     TEXT
);
CREATE INDEX IF NOT EXISTS calls_key ON calls(model_id, query_id, input_hash);
CREATE INDEX IF NOT EXISTS calls_subject ON calls(subject);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
