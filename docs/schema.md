# papyrus database schema

One SQLite file (default `~/.papyrus/papyrus.sqlite`, override with
`PAPYRUS_DB`). The authoritative DDL is
[`src/papyrus/schema.sql`](../src/papyrus/schema.sql); this document explains
the model. Everything is plain SQLite (FTS5 for text search); no extensions
are required to read it.

## The four ideas

```
            contexts ──< context_refs >── files ──< file_works >── works ──< work_authors >── people
 (where/how it was found)    (links)    (bytes)                (the thing)            (who wrote it)
                                          │                      │
                                       file_pages             work_ids, work_topics
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
  * `decide:<backend>:<model>` (Jev-style decision probabilities)
  * `api:arxiv`, `api:openalex`, `api:s2`, `api:crossref`: claims from one source
  * `api:enrich`: the enrichment pipeline's own records (candidates, exclusions, duplicates)
  * `papyrus:reject`: written when a work is rejected
  * `user`
  * Link tables also use plain stage names: `scan`, `download`, `triage`, `promote`.
* Precedence when resolving claims (`works.SOURCE_RANK`):
  `user > api:* > decide:* > llm:* > heuristic:* > pdf:*`.

## Tables

### files

| column | meaning |
|---|---|
| `sha256`, `size`, `media_type` | identity of the bytes |
| `page_count`, `pdf_*` | the PDF's own document-info dictionary, verbatim |
| `text_status` | `pending`, `ok`, `no_text` (scanned/image-only) or `error` |
| `triage_status` | `pending`, `done` or `error` (LLM triage) |
| `stored_path` | papyrus's own copy, relative to the library root (`PAPYRUS_HOME`, default `~/.papyrus`): `store/<first 2 hex digits>/<sha256>.pdf`. Read-only and never rewritten. NULL until stored (`papyrus store` backfills files scanned before the store existed). |

`file_pages(file_id, page, text)` holds extracted text per page. Scanning
extracts the first 2 pages; full extraction will fill in the rest. The text is
cleaned:

* PDFium's soft-hyphen marker `\x02` and other control and zero-width
  characters are stripped;
* end-of-line hyphenation is joined back together.
`file_pages_fts` is an FTS5 external-content index over it, kept in sync by
triggers. Example query:

```sql
SELECT file_id, page, snippet(file_pages_fts, 0, '[', ']', '…', 12)
FROM file_pages_fts JOIN file_pages p ON p.id = file_pages_fts.rowid
WHERE file_pages_fts MATCH 'equality NEAR saturation';
```

### works, work_ids, file_works

* `works.kind` is one of `paper`, `book`, `chapter`, `thesis`, `slides`,
  `tutorial`, `lecture_notes`, `article`, `report`, `manual`, `personal` or
  `other` (`triage.KINDS`).
* `works.status` is one of:
  * `candidate`: created automatically, not reviewed
  * `confirmed`
  * `rejected`: not library material. Rows are never deleted. Rejecting a
    work releases its `work_ids` (recorded as a `released_ids` assertion) so
    that the real document can claim them. Hidden from search and MCP.
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
  * `llm`: free topics from triage
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
| `slack` *(planned)* | message permalink | message ts | poster's display name (`actor_id` = Slack user id) | the message paragraph | `channel_id`, `thread_ts`, `reactions`, `files` |
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
the next `papyrus download` of that URL retries it.

### assertions

`(subject_type, subject_id, field, value JSON, confidence, source, run_id,
created_at, superseded_at)`.

The fields triage writes for a file:

* `filename_hint`: `{title, surnames[], year}`
* `kind`, plus `kind_probs` (`{option: p}`) when a decider is used
* `is_library_material`, plus `is_library_material_probs`
* `title`, `authors` (list of names), `year`, `venue`
* `abstract`: an exact span of the page text, located from start/end
  anchors that the model gives (see the source suffix), or found by
  regex. Absent when there is none, and never stored for non-library
  documents.
* `summary`: generated by the model
* `topics`: `[{name, field}]`
* `language`
* `ids`: `{arxiv, doi, isbn}` found by regex

The fields written for a work:

* **by enrichment, one claim per contributing source** (`api:<source>`):
  `title`, `abstract`, `authors`, `author_ids` (`[{name, orcid, openalex_id,
  s2_id, affiliations}]`), `year`, `venue`, `ids`, `topics`,
  `citation_count`. Confidence is the match score.
* **by the pipeline** (`api:enrich`):
  * `enrich_candidates`: written when nothing reached the 0.85 threshold
  * `excluded_candidate`: `{source, title, ids, reason}`, a candidate that
    is provably a different work
  * `possible_duplicate`: `{other_work_id, scheme, value}`
* **by promote:** `summary` (source `triage`).
* **on rejection:** `released_ids` (source `papyrus:reject`).

Live claims are the rows where `superseded_at IS NULL`. Claims are never
deleted; a re-run supersedes the same source's earlier claim.

### runs

`runs` has one row per pipeline invocation (`scan`, `triage`, `enrich`…),
with its arguments, the backend/model, the prompt version and counters, so
any assertion can be traced back to the exact model and prompt that produced
it. `stats.status` says how the run ended: `completed`, `interrupted` or
`crashed: <error>`. Runs are opened and closed by `db.recorded_run`, so
every run gets a `finished_at`.
