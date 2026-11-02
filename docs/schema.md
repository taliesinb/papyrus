# papyrus database schema

One SQLite file per library variant (default
`~/.papyrus/release/papyrus.sqlite`, `~/.papyrus/<variant>/papyrus.sqlite`
for others; override with `PAPYRUS_DB`). The authoritative DDL is
[`src/papyrus/schema.sql`](../src/papyrus/schema.sql); this document explains
the model. Everything is plain SQLite (FTS5 for text search); no extensions
are required to read it.

The schema version is `meta.schema_version` (now `0.5`; versions stay 0.x
until the schema is stable, then 1.0; an old bare `4` reads as 0.4). papyrus
refuses to open a database of another version, and there are no automatic
migrations: a version change is migrated once, by hand.

The pipeline's stages, each with its state on `files` where it has one:
**scan** (adding files) → **read** (the text of the first pages, with OCR for
pages without a text layer; `read_status`) → **classify** (`classify_status`)
→ **extract** (the extraction model's metadata; `extract_status`) → **promote**
(works) → **index** (every page, for full-text search; `index_status`) →
**match** (external APIs). All of them together is **ingest** (`papyrus
ingest`, `papyrus watch`).

## The four ideas

```
            contexts ──< context_refs >── files ──< file_works >── works ──< work_authors >── people
 (where/how it was found)    (links)    (bytes)                (the thing)            (who wrote it)
                                          │                      │
                                file_pages, file_outline      work_ids, work_topics
                                       (+ FTS5)               (+ works_fts)

            assertions: provenance-tracked claims about any file / work / person / context
```

1. **files** are byte-identical PDFs, keyed by `sha256`. Copies of the same
   file in two folders are one `files` row with two contexts.
2. **works** are the intellectual items: a paper, a book, a chapter, a slide
   deck. A work can have several files (arXiv v1 and v3, the published
   version, an excerpt), linked by `file_works` with a `relation`.
3. **contexts** record *where and how* something was encountered. There can
   be any number of them per document, and they are kept even if the file
   moves or disappears (`missing_at`).
4. **assertions** hold every claim from every source (LLM, external API,
   filename heuristic, the user) with its provenance and confidence. The
   columns on `works` and `people` are the *resolved* view; assertions are
   the evidence. Re-running a model supersedes that source's earlier claims
   (`superseded_at`) rather than deleting them.

## Conventions

* Timestamps: ISO-8601 UTC strings, `2026-10-26T14:37:08.123Z`.
* JSON: stored as TEXT (`extra`, `assertions.value`, `runs.args`).
* Identifiers are normalised:
  * arXiv ids drop the version (`2412.06464`; the version goes in `file_works.version`).
  * DOIs are lowercased.
  * ORCIDs are bare (`0000-0002-1825-0097`).
* Provenance tags (`source` columns):
  * `heuristic:filename`, `heuristic:regex` (identifiers found in text or filenames)
  * `heuristic:abstract` (abstract found by the "Abstract … Introduction" regex)
  * `pdf:info`
  * `llm:<backend>:<model>`, e.g. `llm:lmstudio:qwen3.5-4b-mlx`, `llm:apple:system`.
    For abstracts a suffix says how the span was cut: `#anchors` or `#anchors+heuristic`.
  * `classify:<backend>:<model>` (Jev-style decision probabilities)
  * `screen:<backend>:<model>`, e.g. `screen:lmstudio:text-embedding-nomic-embed-text-v1.5`:
    the classify stage's fast first step (`screen.py`)
  * `api:arxiv`, `api:openalex`, `api:s2`, `api:crossref`: claims from one source
  * `api:match`: the match stage's own records (candidates, exclusions, duplicates)
  * `papyrus:reject`: written when a work is rejected
  * `papyrus:purge`: written when a file is purged as personal paperwork
  * `user`
  * Link tables also use plain stage names: `scan`, `download`, `extract`, `promote`.
* Precedence when resolving claims (`works.SOURCE_RANK`):
  `user > api:* > classify:* > llm:* > heuristic:* > pdf:*`.

## Tables

### files

| column | meaning |
|---|---|
| `sha256`, `size`, `media_type` | identity of the bytes |
| `page_count`, `pdf_*` | the PDF's own document-info dictionary, verbatim |
| `read_status`, `read_error` | reading the first pages' text: `pending`, `ok`, `no_text` (scanned/image-only: OCR reads them) or `error`; `read_error` says why reading (or indexing) failed |
| `classify_status` | `pending`, `done` or `error`: the screen and the classifier (`papyrus classify`) |
| `extract_status` | `pending`, `done`, or `skipped` (not library material: personal paperwork gets no metadata from a model): the extraction model (`papyrus extract`). A file is through the model stages when `classify_status = 'done' AND extract_status IN ('done', 'skipped')` (`db.MODELS_DONE`) |
| `index_status` | `pending` (only the first pages are in `file_pages`), `done` (every page, searchable) or `error` (`read_error` says why); see `papyrus index`. Only files of live works are indexed |
| `stored_path` | papyrus's own copy, relative to the library root (`PAPYRUS_HOME`, default the variant's home `~/.papyrus/<variant>`): `store/<first 16 hex digits of sha256>.pdf`, one flat directory (a different file whose 16 digits are already taken would be stored under its full sha256). Read-only and never rewritten. NULL until stored (`papyrus store` backfills files scanned before the store existed), and NULL again once purged. |
| `purged_at` | set when the file was purged as personal paperwork; the row is then a tombstone (see below) |

**Personal paperwork: held or purged** (`purge.py`). A classified file that is
not library material never becomes a work. What happens to it next depends on
the classifier's P(kind=personal):

* **Purged** at ≥ 0.9 (by `promote`, `papyrus purge`), or when the user says
  so (`papyrus purge ID`). The `files` row keeps `sha256`, `size`,
  `first_seen_at` and `purged_at`, plus one `purged` assertion. Download
  contexts keep their URL (`locator`, `url`, `where_froms`; `extra.purged`).
  Everything else is erased:
  * page text and its full-text index entries, and the outline;
  * `page_count` and the `pdf_*` columns;
  * every other assertion about the file;
  * filesystem contexts (path, filename, dates), and download contexts'
    title and filename;
  * the stored copy, and the web app's cached thumbnails.

  A work whose files were all purged is rejected and stripped: no title,
  abstract, authors or topics, and only its `released_ids` assertion is kept.
  Scan and download skip purged bytes without recording anything.
  `papyrus keep PATH` brings a purged file back from its original.
* **Held** between 0.5 and 0.9, or without a classifier, or when the screen set
  it aside before any language model was asked (a `screen` claim with
  `rejected: true`). No work is made and the first pages are kept, for
  `papyrus purge --dry-run` (which lists held files) and a decision:
  `papyrus purge ID` or `papyrus keep ID` (a screened file then goes through
  the classifier and extraction; one the classifier held, through
  extraction).

Neither kind is ever served: the web app and MCP show only files of live
works.

`file_pages(file_id, page, text, source, kind)` holds the text of each page.

* **Which pages:** scanning reads the first 2 pages of every file
  (`pdf.FIRST_PAGES`; the document description the models read takes more from
  the stored copy without storing it). `papyrus index` adds the rest for files of live works
  (`candidate` or `confirmed`). Held personal paperwork stays at 2 pages;
  purged files have none.
* **`source`:** `pdfium` (the PDF's text layer) or `ocr:vision` (Apple
  Vision, for pages without one). OCR'd pages are never overwritten by the
  text layer.
* **`kind`:** `body`, `references`, `index` or `contents`, from the page's
  text (`index.page_kind`). Search ranks non-body pages lower.
* **Cleaning:** PDFium's soft-hyphen marker `\x02` and other control and
  zero-width characters are stripped, and end-of-line hyphenation is joined
  back together.

`file_pages_fts` is an FTS5 external-content index over the text, kept in sync
by triggers. Example query:

```sql
SELECT file_id, page, kind, snippet(file_pages_fts, 0, '[', ']', '…', 12)
FROM file_pages_fts JOIN file_pages p ON p.id = file_pages_fts.rowid
WHERE file_pages_fts MATCH 'equality NEAR saturation';
```

Two FTS5 gotchas:

* `bm25()` and `snippet()` can't be used in an aggregate query (`GROUP BY
  file_id`), not even inside a subquery, which SQLite flattens. Compute them
  in a `WITH h AS MATERIALIZED (…)` CTE first (see `search.page_sql`).
* `MATCH ? AND rowid IN (…)` reruns the whole MATCH once per rowid. Write
  `+rowid IN (…)`.

`file_outline(file_id, position, level, title, page)` is the PDF's outline
(bookmarks) in document order. A page belongs to the last entry that starts
on or before it, which is how search labels hits with their section:

```sql
SELECT title FROM file_outline WHERE file_id = ? AND page <= ?
ORDER BY page DESC, position DESC LIMIT 1;
```

### works, work_ids, file_works

* `works.kind` is one of `paper`, `book`, `chapter`, `thesis`, `slides`,
  `tutorial`, `lecture_notes`, `article`, `report`, `manual`, `personal` or
  `other` (`classify.KINDS`).
* `works.status` is one of:
  * `candidate`: created automatically, not reviewed
  * `confirmed`
  * `rejected`: not library material. Rows are never deleted. Rejecting a
    work releases its `work_ids` (recorded as a `released_ids` assertion) so
    that the real document can claim them. Hidden from search and MCP; the
    web app lists them only on request, without their files. A rejected work
    whose files were purged has its title and everything else erased.
  * `merged`: see `merged_into`
* `work_ids(scheme, value)` is globally unique, so an arXiv id or DOI maps to
  exactly one work.
  * Schemes: `arxiv`, `doi`, `isbn`, `openalex`, `s2`, `s2_corpus`, `mag`,
    `dblp`, `pmid`, `url`.
  * ISSNs are never stored here, since they identify journals, not works.
  * Lookups ignore rejected and merged works.
* `file_works.relation` is one of `full`, `preprint`, `published`, `excerpt`,
  `supplement` or `slides_for`.

### people, work_authors

* `work_authors` keeps the printed `raw_name` and its position. `person_id`
  stays NULL until the name is disambiguated.
* `people` carries external author ids (`orcid`, `openalex_id`, `s2_id`,
  `dblp_pid`), each unique when set. Two works by the same person resolve to
  the same `people` row through these ids, not through string matching.

### topics, work_topics

* `topics` is a hierarchical vocabulary (`level`: domain, field, subfield or
  topic; `parent_id`), namespaced by `scheme`:
  * `llm`: free topics from the extraction model
  * `arxiv`: categories such as `cs.LG`
  * `openalex`: the OpenAlex topic tree
  * `user`
* `work_topics` links works to topics, with a score and its source.

### contexts, context_refs

`(kind, locator)` is unique, so re-importing the same source is idempotent.

| kind | locator | occurred_at | actor | text | extra (JSON) |
|---|---|---|---|---|---|
| `filesystem` | absolute path | file birth time (≈ download time) | – | – | `filename`, `dir`, `size`, `mtime`, `where_froms`, `download_agent` |
| `download` | the URL as given to `papyrus download` | time of the first successful download (NULL until then) | – | – | `filename` (Content-Disposition or URL), `where_froms` `[PDF URL after redirects, landing page]`, `content_type`, `size`; on failure `error`, `status` (or `last_error`, `last_status` after an earlier success) |
| `slack` | message permalink (`?thread_ts=` for replies) | message ts | poster's display name (`actor_id` = Slack user id); `title` `#channel`, `source` `<workspace>/#channel` | the message as Markdown | `team_id`, `channel_id`, `channel_name`, `channel_type` (public/private/im/mpim), `ts`, `thread_ts`, `author_handle`, `links` (`[{url, source, kind, id, label, title, site, file_id…}]`, see docs/slack.md) |
| `safari_rl` *(planned)* | bookmark UUID | date added to Reading List | – | preview text | `date_last_viewed`, `site` |
| `twitter` *(planned)* | tweet URL | tweet time | author handle | tweet text | `thread_root`, `quoted`, `likes` |
| `web` *(planned)* | page URL | visit time | – | excerpt | – |

* `source` holds the host, workspace/#channel or account.
* `parent_id` links nested contexts, e.g. a message to its thread.
* For files, `where_froms` comes from the macOS `kMDItemWhereFroms` extended
  attribute written by the browser: `[download URL, referring page]`.
  `contexts.url` holds the download URL.
* For downloads, `title` is the link's title where known (e.g. the Safari
  tab's title, `[2312.00429] Polygraphs: …`), `source` the host, and
  `contexts.url` the PDF's URL. `where_froms` has the same shape as for files.

`context_refs` connects a context to the documents it mentions. A Slack
message with three arXiv links has three refs. A ref starts `unresolved`
(only `url` and/or `mention` are set) and becomes `resolved` once it points to
a `file_id` and/or `work_id`. A filesystem context has exactly one resolved
ref, to the file at that path. A download context has one ref: resolved to
the downloaded file, or `unresolved` (with the URL) while fetching fails;
the next `papyrus download` of that URL retries it. A Slack context has one
ref per paper link; `resolve.py` resolves those naming an arXiv id or DOI to
a work (`source` `resolve:arxiv` / `resolve:doi`, confidence 1.0), creating
a `candidate` work (ids from `heuristic:link`, the rest from matching)
when the library has none. Works created this way have no files.

### assertions

`(subject_type, subject_id, field, value JSON, confidence, source, run_id,
created_at, superseded_at, call_id)`.

`call_id` (since schema 0.4) names the model call a claim came from: a row of
`calls` in the separate inference cache, `<home>/inference.sqlite` (see
below). Claims from the extraction model point at its extraction call (the
`abstract` too, since it is cut out using the model's anchors), classifier claims
at the call for their question. When a question is answered from the cache, the
new claims point at the original call. NULL for claims that no model made.

The fields classify and extract write for a file:

* `filename_hint`: `{title, surnames[], year}`
* `screen` (source `screen:<backend>:<model>`, confidence = p): `{p_personal,
  threshold, rejected}` from the screen, the embedding classifier that runs
  first (`screen.py`). With `rejected: true` classifying stopped there, so the
  file has no classifier or extraction claims; `works.is_library` counts it as personal unless
  the user or a classifier says otherwise.
* `kind`, plus `kind_probs` (`{option: p}`) when a classifier is used
* `is_library_material` (from `user`, or from the extraction model when there is no classifier);
  `classify:` claims and `is_library_material_probs` only from prompts before 0.6 (`triage-6`)

  With a classifier, `kind` comes from it alone (`classify:`), and library-ness is its
  P(kind=personal) (`works.is_library`); the extraction model is asked for kind and
  `is_library_material` only without one. A file the classifier doesn't keep as library material
  gets no extraction claims at all: promote purges or holds it.
* `title`, `authors` (list of names), `year`, `venue`
* `abstract`: an exact span of the page text, located from start/end
  anchors that the model gives (see the source suffix), or found by
  regex. Absent when there is none, and never stored for non-library
  documents.
* `summary`: generated by the model
* `topics`: `[{name, field}]`
* `language`
* `ids`: `{arxiv, doi, isbn}` found by regex
* `purged` (source `papyrus:purge`): `{by, p_personal, prompt_version}`,
  where `by` is the classifier's tag or `user`. After a purge it is the file's
  only assertion; `papyrus keep PATH` supersedes it.
* `is_library_material` from `user` (`papyrus keep`): outranks the classifier,
  both for promotion and for purging.

The fields written for a work:

* **by matching, one claim per contributing source** (`api:<source>`):
  `title`, `abstract`, `authors`, `author_ids` (`[{name, orcid, openalex_id,
  s2_id, affiliations}]`), `year`, `venue`, `ids`, `topics`,
  `citation_count`. Confidence is the match score.
* **by the match stage** (`api:match`):
  * `match_candidates`: written when nothing reached the 0.85 threshold
  * `excluded_candidate`: `{source, title, ids, reason}`, a candidate that
    is provably a different work
  * `possible_duplicate`: `{other_work_id, scheme, value}`
* **by promote:** `summary` (source `extract`).
* **on rejection:** `released_ids` (source `papyrus:reject`).

Live claims are the rows where `superseded_at IS NULL`. Claims are never
deleted; a re-run supersedes the same source's earlier claim.

### runs

`runs` has one row per pipeline invocation (`scan`, `classify`, `extract`,
`promote`, `index`, `match`…; runs from before schema 0.5 say `triage`,
`enrich`, and `extract` for full-text indexing),
with its arguments, the backend/model, the prompt version and counters, so
any assertion can be traced back to the exact model and prompt that produced
it. `stats.status` says how the run ended: `completed`, `interrupted` or
`crashed: <error>`. Runs are opened and closed by `db.recorded_run`, so
every run gets a `finished_at`.

`papyrus watch` records one `watch` run per batch of work. Its `args` hold
`trigger` (`startup` for the catch-up scan, `events` for new files, `pending`
for work left over, `once`) and counts; its `stats` combine `scan`'s counters
(`new_files`, `known_files`, `purged_skipped`, `went_missing`…) with the
steps it took (`classified`, `screened`, `extracted`, `promote_<action>`,
`indexed`, `match_<status>`, `<stage>_error`) and its back-offs
(`backoff_waiting-memory`, `backoff_lmstudio-unavailable`,
`backoff_rate-limited`). `backend` is filled in once the batch has asked a
model.

## The inference cache: `<home>/inference.sqlite`

A second database, beside the library and independent of it (its own DDL,
[`src/papyrus/inference.sql`](../src/papyrus/inference.sql), and its own
`meta.schema_version`, now `0.2`; an old `2` reads as the same). It records every question asked of a model,
and answers repeated ones (`src/papyrus/inference.py`). It is not reachable
through MCP's `sql` tool.

| table | key | what |
|---|---|---|
| `models` | hash of `{backend, model, build}` | a model as far as its answers go: `build` pins the weights (LM Studio: quantization, arch, format; Apple: the OS release). The endpoint is not in the key. `seed_honoured`: whether the same seed gives the same answer (Apple: yes; LM Studio MLX: no) |
| `queries` | hash of `{kind, instructions, output_schema, examples, params}` | a question without its data: `extract` (instructions + JSON Schema) or `classify` (one question: instructions + `{key, type, criteria}`). `params`: temperature, max_tokens, seed and the backend's own settings |
| `calls` | 16 random hex digits (`assertions.call_id`) | one call: `model_id`, `query_id`, `input_hash`, `input_chars`, `subject` (the document's `files.sha256`), `status` (`running`, `ok`, `error`, `busy`, `unavailable`, `interrupted`), `error` (the exception type only), `output_hash`, timings (`started_at`, `finished_at`, `duration_ms`), tokens, `model_loaded`, `memory_pressure`, `endpoint`, `host`, `extra`, and `hits`/`last_hit_at` for the times the cache answered with it |

Every hash and id here is 16 hex digits (64 bits): a hash is the first 16
digits of a sha256 (of canonical JSON for models and queries), and only
`subject` holds a full one, the document's `files.sha256`.

The texts are not in the database. They are files named by their hash, in
one flat directory each: `<home>/inference/inputs/<hash>.txt` (the data
asked about, e.g. a document's first pages) and
`<home>/inference/outputs/<hash>.json` (the answer). A text whose 16 digits
are already another's would be named by its full sha256. The cache answers `(model, query, input)` with the newest `ok` call
whose output file exists, so deleting files never breaks anything; it only
means asking again. `papyrus inference prune` deletes old texts, and purging a
document deletes the texts of every call whose `subject` is its sha256.

```sql
-- how long extraction takes per model, cache hits aside
SELECT m.model, count(*), avg(c.duration_ms) / 1000.0 AS seconds
FROM calls c JOIN models m ON m.id = c.model_id JOIN queries q ON q.id = c.query_id
WHERE q.kind = 'extract' AND c.status = 'ok' AND c.model_loaded IS NOT 0
GROUP BY m.id;
```

## The web services store: `<PAPYRUS_ROOT>/services.sqlite`

A third database, one per machine (`~/.papyrus/services.sqlite`, shared by
every variant), with its own DDL,
[`src/papyrus/services.sql`](../src/papyrus/services.sql), and its own
`meta.schema_version`, now `0.1`. It records every HTTP exchange papyrus
makes (matching APIs, `papyrus download`, Slack, tools), reuses answers, and
holds each service's rate limit and pause for every process
(`src/papyrus/services.py`, used by `http.HttpClient`). It is not reachable
through MCP's `sql` tool: it holds Slack messages. `papyrus services
stats | state | show | pause | resume | import`.

| table | key | what |
|---|---|---|
| `services` | hash of `{name, base_url, api_version}` | who answers: `arxiv`, `openalex`, `crossref`, `s2`, `slack`, `arxiv-web` (PDFs), or `web:<host>` for any other host |
| `credentials` | hash of `{service, kind, location, fingerprint}` | what it was asked with: `public`, a `key` (where it was sent, e.g. `header:x-api-key`, and the first 16 hex digits of the sha256 of `<location>:<secret>`: never the secret), or `unknown` (imported, redacted before we saw it) |
| `policies` | hash of `{service, settings}` | how it was asked: `interval_s`, `timeout_s`, `max_retries`, `backoff_base`, `backoff_cap`, `pause_s`, `pause_statuses`, `reuse_s`, `scope` (`services.Policy`; defaults in `services.SERVICES`, overrides in config.toml's `[services.<name>]`) |
| `requests` | hash of `{service, method, url, body_hash}` | what was asked: the URL with its query sorted, secrets and the service's `ignore_params` (`mailto`) dropped. The same request with another key is the same request |
| `calls` | 16 random hex digits | one attempt (retries are calls of their own, `attempt` 0, 1, …) or a request not sent because its service was paused: `status` (`ok`, `http_error`, `no_answer`, `paused`, `interrupted`, `too_large`), `http_status`, `error` (the exception type only), `started_at`, `finished_at`, `duration_ms`, `url` (as sent, secrets redacted), `final_url`, `redirects`, `req_headers` / `resp_headers` (redacted), `resp_type`, `resp_hash` (the body, in `services/bodies/`), `resp_size`, `resp_sha256` (downloads: `files.sha256`), `retry_after_s`, `subject` (`work:<id>` for matching), `run_id` + `library` (the library's run), `pid`, `host`, `hits`/`last_hit_at` (times the answer was reused), `imported` (`api-log`, `http-cache`) |
| `state` | `<service id>` or `<service id>/<credential id>` | one per rate-limit scope: `next_ok_at` (unix seconds: no request before), `paused_until`, `pause_reason` (`HTTP 429`, `no answer (ReadTimeout)`, `by hand`), `paused_by` (the call), `last_ok_at`, `last_failure_at`, `failures` (in a row) |

Response bodies (and POST bodies) are not in the database: they are zstd
files named by the hash of the redacted text,
`~/.papyrus/services/bodies/<hash>.zst`. Downloaded PDFs are not kept here
(they go to the library's store). Nothing is deleted: an answer older than
the policy's `reuse_s` (30 days; never for Slack and PDFs) is just not
reused. A request that fails for good with one of `pause_statuses` (429,
503, no answer) pauses its scope for `pause_s` (arXiv 5 min, Semantic
Scholar 2 min); matching counts a paused source as "not asked".

```sql
-- arXiv, hour by hour: answered, refused, unanswered, not sent
SELECT substr(c.started_at, 1, 13) AS hour,
       sum(c.status = 'ok') AS ok, sum(c.http_status = 429) AS refused,
       sum(c.status = 'no_answer') AS no_answer, sum(c.status = 'paused') AS not_sent
FROM calls c JOIN services s ON s.id = c.service_id
WHERE s.name = 'arxiv' GROUP BY hour ORDER BY hour;

-- works whose matching met trouble, to match again
SELECT DISTINCT c.subject FROM calls c
WHERE c.subject LIKE 'work:%' AND c.status IN ('no_answer', 'paused', 'http_error');
```
