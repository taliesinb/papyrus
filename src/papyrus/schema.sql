-- papyrus schema, version 3.
--
-- Design notes (see docs/schema.md for the long form):
--
--   files      physical byte-identical documents, keyed by sha256.
--   works      the intellectual item (a paper, book, chapter, slide deck...).
--              One work can have many files (arXiv v1/v3, published version,
--              a chapter excerpt) and, rarely, one file can bundle many works.
--   contexts   *where and how a document was encountered*: a path on disk, a
--              Slack message, a Safari Reading List entry, a tweet. Many per
--              file/work. A context refers to documents via context_refs,
--              because one Slack message can link to several papers, and a
--              link may be unresolved for a while.
--   assertions every claim about a file/work/person with its provenance
--              (LLM backend+model, external API, filename heuristic, user).
--              Columns on works/people are the *resolved* view of these.
--
-- Conventions: timestamps are ISO-8601 UTC text ('2026-10-26T14:37:08.123Z');
-- JSON is stored as TEXT; identifiers are normalised (arXiv without version,
-- DOI lowercased, ORCID bare digits).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per pipeline invocation, so assertions can be traced and re-run.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,          -- e.g. 'scan', 'triage', 'enrich'
    args        TEXT,                   -- JSON
    backend     TEXT,                   -- e.g. 'lmstudio:qwen3.5-9b-mlx'
    prompt_version TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT,
    stats       TEXT                    -- JSON counters
);

------------------------------------------------------------------------------
-- Files
------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    size          INTEGER NOT NULL,
    media_type    TEXT NOT NULL DEFAULT 'application/pdf',
    page_count    INTEGER,
    -- PDF document-info dictionary, verbatim (often junk, sometimes gold)
    pdf_title     TEXT,
    pdf_author    TEXT,
    pdf_subject   TEXT,
    pdf_keywords  TEXT,
    pdf_creator   TEXT,
    pdf_producer  TEXT,
    pdf_created   TEXT,
    pdf_modified  TEXT,
    -- the pipeline, one state per stage (scan -> read -> classify -> extract -> promote -> index -> match):
    -- read: the first pages' text: pending | ok | no_text (scanned/image-only: OCR reads them) | error
    read_status   TEXT NOT NULL DEFAULT 'pending',
    read_error    TEXT,  -- why reading (or indexing) failed
    -- classify: the screen and the classifier: pending | done | error
    classify_status TEXT NOT NULL DEFAULT 'pending',
    -- extract: the extraction model's metadata: pending | done | skipped (not library material:
    -- personal paperwork gets no metadata)
    extract_status TEXT NOT NULL DEFAULT 'pending',
    -- index: pending (only the first pages are in file_pages) | done (every page, searchable) |
    -- error (read_error says why). Only files of live works are indexed.
    index_status  TEXT NOT NULL DEFAULT 'pending',
    -- where papyrus keeps its own copy, relative to the library root (NULL = not copied)
    stored_path   TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- set when the file was purged as personal paperwork (purge.py): the row is then a tombstone
    -- (sha256, size and a 'purged' assertion), so the same bytes are skipped wherever they turn up
    purged_at     TEXT
);

-- Extracted text, one row per page (1-based). Scanning extracts the first
-- pages; `papyrus extract` fills in the rest for files of live works.
CREATE TABLE IF NOT EXISTS file_pages (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    page    INTEGER NOT NULL,
    text    TEXT NOT NULL,
    -- how the text was obtained: pdfium (the PDF's text layer) | ocr:vision (Apple Vision)
    source  TEXT NOT NULL DEFAULT 'pdfium',
    -- body | references | index | contents; non-body pages rank lower in search
    kind    TEXT NOT NULL DEFAULT 'body',
    UNIQUE (file_id, page)
);

CREATE VIRTUAL TABLE IF NOT EXISTS file_pages_fts USING fts5(
    text,
    content = 'file_pages',
    content_rowid = 'id',
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS file_pages_ai AFTER INSERT ON file_pages BEGIN
    INSERT INTO file_pages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS file_pages_ad AFTER DELETE ON file_pages BEGIN
    INSERT INTO file_pages_fts(file_pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS file_pages_au AFTER UPDATE OF text ON file_pages BEGIN
    INSERT INTO file_pages_fts(file_pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO file_pages_fts(rowid, text) VALUES (new.id, new.text);
END;

-- The PDF's outline (bookmarks), in document order: labels search hits with
-- their section. A page belongs to the last entry that starts on or before it.
CREATE TABLE IF NOT EXISTS file_outline (
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,     -- 0-based, document order
    level    INTEGER NOT NULL,     -- 0 = top level
    title    TEXT NOT NULL,
    page     INTEGER NOT NULL,     -- 1-based
    PRIMARY KEY (file_id, position)
);

------------------------------------------------------------------------------
-- Works, identifiers, people
------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS works (
    id          INTEGER PRIMARY KEY,
    -- paper | book | chapter | thesis | slides | tutorial | lecture_notes |
    -- article | report | manual | other
    kind        TEXT,
    title       TEXT,
    subtitle    TEXT,
    abstract    TEXT,
    year        INTEGER,
    date        TEXT,              -- ISO date of (first) publication
    venue       TEXT,
    language    TEXT,
    -- candidate (auto-created, unreviewed) | confirmed | rejected | merged
    status      TEXT NOT NULL DEFAULT 'candidate',
    merged_into INTEGER REFERENCES works(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- External identifiers. (scheme, value) is globally unique: an arXiv id or
-- DOI belongs to exactly one work.
--   scheme: arxiv | doi | isbn | openalex | s2 | dblp | pmid | url
CREATE TABLE IF NOT EXISTS work_ids (
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    scheme  TEXT NOT NULL,
    value   TEXT NOT NULL,
    source  TEXT,                  -- provenance tag
    PRIMARY KEY (scheme, value)
);
CREATE INDEX IF NOT EXISTS work_ids_work ON work_ids(work_id);

-- Which files embody which works.
--   relation: full | preprint | published | excerpt | supplement | slides_for
CREATE TABLE IF NOT EXISTS file_works (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    work_id    INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    relation   TEXT NOT NULL DEFAULT 'full',
    version    TEXT,               -- e.g. 'v3' for arXiv
    confidence REAL,
    source     TEXT,
    PRIMARY KEY (file_id, work_id)
);
CREATE INDEX IF NOT EXISTS file_works_work ON file_works(work_id);

-- Disambiguated people (authors, editors). External ids are unique when set.
CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    given_name   TEXT,
    family_name  TEXT,
    orcid        TEXT UNIQUE,
    openalex_id  TEXT UNIQUE,
    s2_id        TEXT UNIQUE,
    dblp_pid     TEXT UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Ordered author list. person_id is NULL until the name is disambiguated.
CREATE TABLE IF NOT EXISTS work_authors (
    work_id     INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,  -- 0-based
    raw_name    TEXT NOT NULL,     -- as printed / as given by the source
    person_id   INTEGER REFERENCES people(id),
    role        TEXT NOT NULL DEFAULT 'author',   -- author | editor | translator
    affiliation TEXT,
    source      TEXT,
    PRIMARY KEY (work_id, role, position)
);
CREATE INDEX IF NOT EXISTS work_authors_person ON work_authors(person_id);

-- Topic vocabulary, hierarchical: domain > field > subfield > topic.
--   scheme: llm | arxiv | openalex | user
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    scheme      TEXT NOT NULL,
    name        TEXT NOT NULL,
    level       TEXT,              -- domain | field | subfield | topic
    parent_id   INTEGER REFERENCES topics(id),
    external_id TEXT,              -- e.g. 'cs.LG', OpenAlex 'T10320'
    UNIQUE (scheme, name, parent_id)
);

CREATE TABLE IF NOT EXISTS work_topics (
    work_id  INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    score    REAL,
    source   TEXT,
    PRIMARY KEY (work_id, topic_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
    title, authors, abstract, topics,
    tokenize = 'porter unicode61 remove_diacritics 2'
);  -- rowid = works.id; maintained by papyrus.db.reindex_work()

------------------------------------------------------------------------------
-- Discovery contexts
------------------------------------------------------------------------------

-- Where/how a document was encountered. (kind, locator) is unique so that
-- re-scanning is idempotent.
--
--   kind        locator              occurred_at          actor        text
--   ----------- -------------------- -------------------- ------------ -----------------
--   filesystem  absolute path        file birth time      -            -
--   download    URL as given         first download time  -            -   (title = link/tab title)
--   slack       message permalink    message ts           poster       message paragraph
--   safari_rl   bookmark UUID / URL  date added           -            preview text
--   twitter     tweet URL            tweet time           tweet author tweet text
--   web         page URL             visit time           -            page excerpt
CREATE TABLE IF NOT EXISTS contexts (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    locator       TEXT NOT NULL,
    source        TEXT,            -- host name, Slack workspace/#channel, @account...
    occurred_at   TEXT,
    actor         TEXT,            -- display name of who shared/posted it
    actor_id      TEXT,            -- platform id of the actor
    title         TEXT,            -- channel / thread / page title
    text          TEXT,            -- the surrounding paragraph / message
    url           TEXT,            -- canonical URL of the context itself (permalink)
    parent_id     INTEGER REFERENCES contexts(id),  -- e.g. message -> thread
    extra         TEXT,            -- JSON, kind-specific (see docs/schema.md)
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    missing_at    TEXT,            -- set when the context disappears (file moved/deleted)
    UNIQUE (kind, locator)
);
CREATE INDEX IF NOT EXISTS contexts_occurred ON contexts(occurred_at);

-- A reference from a context to a document. For a file on disk this is a
-- single resolved ref; for a Slack message there is one per link/mention.
--   status: unresolved | resolved | ignored
CREATE TABLE IF NOT EXISTS context_refs (
    id         INTEGER PRIMARY KEY,
    context_id INTEGER NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    url        TEXT,               -- link as it appeared (download URL for files)
    mention    TEXT,               -- free-text mention, e.g. 'the Mamba-2 paper'
    file_id    INTEGER REFERENCES files(id) ON DELETE SET NULL,
    work_id    INTEGER REFERENCES works(id) ON DELETE SET NULL,
    status     TEXT NOT NULL DEFAULT 'unresolved',
    confidence REAL,
    source     TEXT,
    UNIQUE (context_id, url, mention)
);
CREATE INDEX IF NOT EXISTS context_refs_file ON context_refs(file_id);
CREATE INDEX IF NOT EXISTS context_refs_work ON context_refs(work_id);

------------------------------------------------------------------------------
-- Assertions (provenance-tracked claims)
------------------------------------------------------------------------------

--   subject_type: file | work | person | context
--   field: kind | is_library_material | title | authors | abstract | year |
--          topics | venue | ids | ...   (value is JSON)
--   source: 'llm:lmstudio:qwen3.5-9b-mlx', 'classify:lmstudio:...', 'api:arxiv',
--           'heuristic:filename', 'pdf:info', 'user'
CREATE TABLE IF NOT EXISTS assertions (
    id           INTEGER PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id   INTEGER NOT NULL,
    field        TEXT NOT NULL,
    value        TEXT NOT NULL,
    confidence   REAL,
    source       TEXT NOT NULL,
    run_id       INTEGER REFERENCES runs(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    superseded_at TEXT,
    call_id      TEXT     -- the model call this claim came from: calls.id in inference.sqlite
                          -- (inference.py); on a cache hit, the original call's. NULL otherwise.
);
CREATE INDEX IF NOT EXISTS assertions_subject ON assertions(subject_type, subject_id, field);
