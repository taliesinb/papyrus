# papyrus architecture

## Goals

* A personal library of papers, books, slides, notes and the like:
  * hundreds to ~50k documents;
  * one user, one machine, local-first;
  * models run locally where possible.
* An **open, documented database** that any tool or agent can query with
  SQL: SQLite, FTS5, no required extensions.
* **Provenance:** where each document was encountered (paths, download URLs,
  and later Slack messages, Safari Reading List, tweets), and which model or
  API asserted which fact.
* A CLI and an MCP server over the same library.

Non-goals for now: a GUI, sync between machines, citation-style
formatting.

## Pipeline

```
 sources                 papyrus stages                                 outputs
 ───────                 ──────────────                                 ───────
 ~/Desktop, ~/Downloads ─▶ scan ──▶ files, file_pages(+FTS), contexts(filesystem), context_refs
   (or, as files appear: watch ─▶ scan.ingest_path, then the stages below, one file at a time)
 URLs, copied tabs ─────▶ download ─▶ … the same, with contexts(download); filename_hint assertion
                               │       both keep a copy in <home>/store (files.stored_path)
                               ▼
                           triage ─▶ assertions on files: kind(+probs), is_library_material(+probs),
                               │       title, authors, year, venue, abstract, summary, topics,
                               │       language, ids
                               ▼
                           promote ─▶ works, work_ids, work_authors(raw), topics(llm), file_works,
                               │        works_fts; rejects works whose files are personal;
                               │        purges files that are surely personal down to a tombstone
                               ▼
                           extract ─▶ every page of live works' files (+FTS), page kinds, file_outline;
                               │        OCR (Apple Vision) of pages without a text layer
                               ▼
                           enrich ──▶ assertions api:*, people (ORCID/OpenAlex/S2 ids),
                                        work_ids, venue/date/abstract, topics(openalex, arxiv)
 (planned) Slack, Safari RL, Twitter ─▶ contexts + unresolved context_refs ─▶ resolver ─▶ works
```

Each stage:

* reads only what it needs from the previous stage;
* writes inside one short transaction per item;
* is idempotent;
* is recorded in `runs` through `db.recorded_run`.

Re-running a stage processes only pending items, unless you pass `--all` or
`--reset`.

**`papyrus watch`** (`watch/`) runs the same stages continuously, in the
background:

1. **Hearing about files:** FSEvents on macOS, inotify on Linux (both through
   ctypes), polling as a fallback, on the `[watch] dirs` of the config (default
   `~/Downloads`, `~/Desktop`; not their subdirectories).
2. **Waiting until they are finished** (`debounce.py`): size, mtime and ctime
   unchanged for `settle` seconds (3). Browsers' partial files (`.download`,
   `.crdownload`, `.part`) and hidden files are ignored; the rename to the
   final name is an event of its own. An empty `x.pdf` next to `x.pdf.part`
   (Firefox) waits for the `.part` to go.
3. **Ingesting** through `scan.ingest_path`, the per-file body of `scan`: the
   same snapshot, sha256 dedupe, purged tombstones and provenance. At start, a
   catch-up scan of the directories covers what happened while it was down.
4. **Processing**, one item per step, new files first: extract (a live work's
   text layer), OCR, enrich, then triage + promote. Each batch of steps is one
   recorded run (`command='watch'`), at most 25 steps long.
5. **Staying out of the way:** background priority (`PRIO_DARWIN_BG`; Linux
   nice 19, `SCHED_IDLE`, idle I/O). Before every model call it reads the
   memory pressure and, for LM Studio, what is loaded (`models.ModelLedger`).
   Model work then waits (`Backoff`) under critical pressure, when a model
   would have to be loaded under any pressure, while LM Studio is off or has
   another model loaded, when Apple's model refuses (error 1013, `ModelBusy`),
   or without a decider. Extract and enrich go on meanwhile. It unloads only
   the models it loaded, after `unload_after` seconds idle (600) and at exit.
6. **The work queue** (`watch/queue.py`, `<home>/queue.sqlite`): what is
   pending is still the library's (files not triaged, live works' files not
   extracted, works without API data). Before every step the queue mirrors it,
   one row per (stage, id), and adds the watcher's own working state:
   * **retries:** a step that fails, or finishes without changing anything
     (`no_progress`), is tried again after 1 h, 4 h and 16 h, then marked
     `failed` (`papyrus watch --retry-failed`). They survive restarts;
   * **history:** each finished step's duration.
7. **Status:** `<home>/watch.json` (idle | ingesting | waiting-memory |
   lmstudio-unavailable, the queue's counts, `eta_s`), served in
   `/api/version` for the menu bar; one watcher per library
   (`<home>/watch.lock`, an flock). **The estimate of time left:** triage per
   document from the inference cache (`Store.document_estimate_s`: the
   questions asked about the latest document, each estimated from its own
   history), the other stages from the queue's median step durations. The
   menu-bar scroll's paper is solid while the queue has items.

Papyrus Server.app runs it on macOS; a systemd user unit on Linux.

| stage | module | pending means | notes |
|---|---|---|---|
| scan | `scan.py`, `pdf.py`, `macos.py`, `linux.py`, `filenames.py` | the path is new, or its (size, mtime) changed | Files are deduplicated by sha256. Text is extracted from the first 2 pages only. `kMDItemWhereFroms` (Linux: `user.xdg.origin.url`, `user.xdg.referrer.url`) gives the download URL and referrer. Contexts whose path has disappeared get `missing_at`. |
| download | `download.py`, `store.py`, `enrich/http.py` | the URL has no download context resolved to a readable file (and no filesystem context with that download URL) | Input: arguments, files, stdin, or the clipboard (RTF links with titles, else HTML, else plain text). arXiv/OpenReview pages map to their PDF; HTML landing pages are followed once via `citation_pdf_url`; a PDF is recognised by its first bytes. Hosts in parallel, one URL at a time per host; the database is written from the main thread only. Failures stay as unresolved refs and are retried next time. |
| triage | `triage.py`, `llm/*`, `textspan.py` | `files.triage_status <> 'done'` | One extraction call per file, plus one decider call per question. The state sent to the model holds the filename(s), path, download URL, PDF metadata, filename hint and ≤4000 characters of page text. |
| promote | `works.py`, `purge.py` | the file has no `file_works` row (and isn't purged) | Deduplicates by id first, then by normalised title ± 1 year. Library-ness comes from `works.is_library`. Files with P(kind=personal) ≥ 0.9 are purged (`purge.verdict`, `purge_file`); between 0.5 and 0.9 they are held for review. |
| extract | `fulltext.py`, `ocr.py`, `tools/afm-bridge` | text layer: a file of a live work with `fulltext_status='pending'`; OCR: a page with < 40 characters and `source='pdfium'` (all such pages of extracted files, the first 2 of image-only files) | Reads every page (pypdfium2), labels its kind, stores the outline. OCR is written every 32 pages, so an interrupted run keeps its work; an image-only file that is no work yet goes back to triage once OCR finds text. Whole library (81 PDFs, 6,500 pages): 26 s, most of it Vision's warm-up. |
| enrich | `enrich/*` | the work has no `api:*` claims and no `enrich_candidates` | `match.resolve` (ids first, then title search) → exclude conflicting candidates → `cluster` → `merge` → write. The score threshold is 0.85. |

## Module map

| module | role |
|---|---|
| `schema.sql`, `db.py` | DDL; connect/init; `tx`, `assert_`, `current_assertions`, `recorded_run` |
| `scan.py`, `pdf.py`, `macos.py`, `linux.py`, `filenames.py` | file discovery, hashing, PDF info and text, provenance (macOS: xattrs and `clonefile` via ctypes; Linux: XDG xattrs and `FICLONE` reflinks), hints from hand-made filenames; `ingest_path` (one path, shared with `watch`), `scan_roots`, `mark_missing` |
| `watch/` | `papyrus watch`: `sources.py` (FSEvents / inotify / polling), `debounce.py` (finished files, partial files), `system.py` (background priority, memory pressure, the instance lock), `models.py` (`ModelLedger`: never a second LM Studio model, unload only our own), `queue.py` (`Queue`: `<home>/queue.sqlite`, synced from the library; retries, step durations, `eta_s`), `daemon.py` (`Watch`: catch-up, batches, steps, back-off, `watch.json`) |
| `store.py` | the managed store: `snapshot` (clone or copy into `store/tmp`), `commit` (rename to `store/ab/<sha256>.pdf`, read-only), `backfill`, `check` |
| `download.py` | link parsing (RTF / HTML / text, Safari's copied tabs), URL → PDF resolution, `fetch`, recording `download` contexts |
| `triage.py` | prompts (`INSTRUCTIONS`, `EXTRACT_SCHEMA`, `KINDS`, `DECISIONS`), `build_state`, `detect_ids`, `triage_file` |
| `textspan.py` | find spans from anchors (`locate`), the `heuristic_abstract` regex, and `best_abstract`, which reconciles the two |
| `works.py` | `SOURCE_RANK`/`best` resolution, `is_library`, `promote_file`, `reject_work`, `reindex_work`, `work_json` |
| `purge.py` | personal paperwork: `verdict` (purge, hold or keep), `purge_file` (tombstone, secure delete, store copy and thumbnails), `keep`/`restore` (the user's override), `is_purged` (used by scan and download) |
| `fulltext.py` | the `extract` stage: `page_kind`, `extract_file` (text layer, outline), `ocr_file`, `run` (a recorded run around `process`; `process(live_only=True)` never reads a non-work file past its first pages) |
| `ocr.py` | `VisionOCR`: page OCR through the bridge's `ocr` op (Vision `RecognizeDocumentsRequest`) |
| `search.py` | ranking shared by the CLI and MCP: `rank_files` (pages → files), `search_works` (RRF with metadata), `fill_snippets`; mirrored in `web/src/lib/server/query.ts` |
| `llm/base.py` | contracts: `Backend.extract` (JSON-schema output) and `Decider.decide` (Jev-style typed questions → probabilities) |
| `llm/lmstudio.py` | extraction through any OpenAI-compatible server (the LM Studio quirks are handled here); `ensure_server`, `loaded_models`, `unload_model` |
| `llm/apple.py` + `tools/afm-bridge` | extraction through Apple Foundation Models (a Swift CLI speaking NDJSON; guided generation via `DynamicGenerationSchema`); the same bridge does OCR (`OCR.swift`) |
| `llm/decide.py` | `JevHTTPDecider` (`/v1/systemone`: TypeSafe, Ollama `tev1`, …) and `OpenAICompatDecider` (logprobs over lettered options) |
| `llm/schema_lite.py` | JSON extraction and coercion for a JSON Schema subset; all model output passes through it |
| `inference.py`, `inference.sql` | the inference cache: `Store` (`<home>/inference.sqlite` + text files by sha256; `lookup`, `start`/`finish`/`fail`, `forget_subject`, `prune`, `stats`, `estimate_s`), `cached_backend` / `cached_decider`, `subject()` |
| `enrich/http.py` | polite shared HTTP client: per-host rate limits, retries, disk cache |
| `enrich/ids.py` | arXiv / DOI / ISBN detection and normalisation (with checks against false positives) |
| `enrich/{arxiv,openalex,semanticscholar,crossref}.py` | source clients → `models.WorkRecord` |
| `enrich/match.py` | `resolve`, `score`, `cluster`, `merge` |
| `enrich/pipeline.py` | `enrich_work` writes to the database (`_conflict`, authors → people, topics, ids); `reset_enrichment` |
| `mcp_server.py` | MCP tools: `search_library`, `fulltext_search`, `get_work`, `get_file`, `recent_discoveries`, `sql` (read-only connection), `library_stats`; resource `papyrus://schema` |
| `config.py` | `<home>/config.toml` (home: `~/.papyrus/<variant>`, `db.library_home`): default backend, decider, LM Studio autostart (CLI > env > file > `auto`), `[watch]` |
| `cli.py` | click commands; SIGTERM becomes KeyboardInterrupt so runs close as `interrupted` |
| `web/` | read-only SvelteKit browser (Node, `node:sqlite` read-only), served under `/papyrus` (`/papyrus-<variant>` for other variants) |
| `apps/macos/` | `Papyrus.app` (WKWebView window) and `Papyrus Server.app` (menu bar: runs `web/`, `tailscale serve --set-path=/papyrus`, and `papyrus watch`; carries `papyrus-thumb`, the CoreGraphics page renderer), one pair per variant; `build.py` |
| `apps/linux/` | `install.py`: the web server as a systemd user service per variant (`io.github.taliesinb.papyrus.<v>.server.service`), the watcher as another (`….watch.service`), XDG data and state directories, `tailscale serve`; thumbnails by poppler's `pdftoppm` |
| `apps/variants.py` | variant rules shared by both installers: `variant.yml`, ports, colours, the web build, a new variant's empty library |

## Model layer

Two question types, kept deliberately separate:

* **Extraction** (`Backend.extract`): generative, filling a small JSON
  schema. Every result goes through `schema_lite.coerce`, because local
  servers don't reliably enforce `response_format`.
* **Decision** (`Decider.decide`): Jev / TypeSafe "System One" style. A
  `state` plus typed questions (`choice`, `yesno`, `score`) come back as
  probabilities per option.
  * The wire format: `docs.typesafe.ai/api/system-one`.
  * The local emulation reads the first-token logprobs of option labels
    (A/B/C…, Yes/No). Labels outside the top 10 are capped at the 10th
    value, then the distribution is renormalised.
  * Any `/v1/systemone` server can be plugged in with
    `--decider jev:http://…`.

Backend specs: `--backend auto|lmstudio[:model]|apple` and
`--decider auto|none|lmstudio[:model]|jev:<url>`. Defaults come from
`config.py` (`~/.papyrus/<variant>/config.toml`, then `auto`). Selecting LM Studio
starts its server if it is off (`lmstudio.ensure_server`, local URLs only,
once per process). Triage refuses to run without a decider unless it is
explicitly `none`: when LM Studio was off, `auto` used to fall back silently
to Apple FM with no decider, the combination that once promoted exam papers
and a CV to works.

**The inference cache** (`inference.py`, `<home>/inference.sqlite`; tables in
[schema.md](schema.md#the-inference-cache-homeinferencesqlite)). Triage
(CLI and `watch`) wraps its backend and decider in `cached_backend` /
`cached_decider`, which implement the same contracts:

* **Key:** model (backend, model, build: not the endpoint) × query
  (instructions, output schema, examples, params including the **seed**,
  default 0, and the backend's `cache_params()`) × sha256 of the input. A
  decider is cached per question.
* **Hit:** the newest `ok` call whose output file still exists answers. The
  claims written point at that call (`assertions.call_id`). `--fresh` asks
  anyway.
* **Miss:** `before_call` runs first (`watch`'s memory and LM Studio gate),
  then the call is timed and recorded, failures included (type only).
* **Texts** live in files beside the database (`inference/inputs`,
  `inference/outputs`, by sha256); the database holds hashes. Each call
  carries its document's sha256 (`subject`), which `purge` follows to delete
  the texts.
* **Seeds:** passed where the backend takes one. Apple FM honours it (the
  bridge samples with `SamplingMode.random(probabilityThreshold: 1, seed:)`);
  LM Studio's MLX engine accepts and ignores it. `models.seed_honoured` says
  which, so a cached seed-7 answer isn't mistaken for a reproducible one.
* **Timings** (`duration_ms`, tokens, whether the model was loaded, memory
  pressure) feed `papyrus inference stats` and `Store.estimate_s`.

## Resolving conflicting claims

* **File → work:** `works.best()` ranks claims by
  `user > api:* > decide:* > llm:* > heuristic:* > pdf:*`, then by
  confidence.
* **Enrichment:**
  1. Candidates come from the id route (score ≈ 1 unless the titles
     disagree) and the title route. The title score is 0.6 × title + 0.25 ×
     authors + 0.15 × year.
  2. Candidates whose ids belong to another live work, or whose arXiv id
     differs from the work's own, are **excluded and recorded**
     (`excluded_candidate`).
  3. The rest are clustered. The best cluster is merged:
     * the arXiv abstract is preferred;
     * author ids come from OpenAlex / S2;
     * the published-version DOI is kept next to the arXiv id.
* `works` columns are filled conservatively: title and abstract only when
  empty; venue, date and year with `COALESCE`.

## Design decisions (with the evidence behind them)

| decision | why |
|---|---|
| Model calls in an inference cache of their own (`inference.sqlite`), texts as files named by hash | The API log leaves model calls out because their prompts hold personal documents' first pages. Keeping hashes in the database and texts in files means the record (what was asked, how long it took) can outlive the texts, and a purge or `prune` deletes text without touching the statistics. A separate database: different lifecycle (deletable wholesale), and out of MCP's `sql` reach. The endpoint is not part of the model key (the same weights answer the same on another port); the seed is part of the query (another seed, another answer). |
| Every API exchange appended to `api-log/<date>.jsonl`, separately from the 30-day cache | Responses hold more than we extract at the time. Re-reading the log costs nothing; re-calling an API costs rate limit (a 180-day Slack scan is ~1,000 calls and 15 min) and may return something different. JSONL files, not a table: append-only, written by several processes, and kept out of the documented schema. |
| SQLite + FTS5 as the source of truth, with a JSON-in-TEXT `extra` | Single user, one file, zero ops, readable by any tool. Vectors will go in a sidecar table (`sqlite-vec`) or file, so the core stays extension-free. |
| `assertions` table instead of overwriting fields | Several noisy sources (LLMs, APIs, heuristics) disagree. Keeping every claim lets us re-resolve, audit and undo (`reset_enrichment`). |
| contexts as their own entity, many per document, `(kind, locator)` unique | Discovery history (who shared it where, when it was downloaded, from which page) is a first-class feature, and Slack/Safari/Twitter importers slot in without schema changes. |
| pypdfium2, not PyMuPDF | Apache/BSD licence (PyMuPDF is AGPL). Fast enough: 66 PDFs scanned in about 1 s. |
| 4B model by default, 4000-character state | Base M4, 24 GB: the 9B ran at about 11 tok/s, and loading two models swapped. The 4B matched the 9B on kind, title, authors and year for 10/10 papers at about 4× the speed. |
| abstract = model anchors + span cut from the page | Copying the abstract verbatim cost hundreds of output tokens per file. Anchors reproduced 23/23 reference abstracts exactly; reconciling with the regex fixes clipped or over-running anchors. |
| library-ness from P(kind=personal) | The decider's yes/no answer is biased towards yes; its kind distribution isn't. |
| page-1 arXiv ids only from the margin stamp | A CV listing its author's paper as an arXiv URL got that paper's id. The stamp rule finds 21/21 real arXiv PDFs here and 0 false positives. |
| reject, never delete; rejected works release their ids | Ids are globally unique: a rejected CV holding a paper's arXiv id would capture the real paper later. |
| exclude candidates that are provably other works | A thesis and the journal paper sharing its title prefix were merged (score 0.93), which gave the thesis the paper's co-authors and venue. |
| decider/model calls outside write transactions | A decider call inside `tx()` held the write lock for seconds and blocked other writers. |
| a content-addressed store, filled by APFS clones | Files in `~/Downloads` get moved or deleted; the library should outlive them. Naming by sha256 needs no naming scheme and makes writes idempotent. Measured on this machine: `cp` and `shutil.copy*` duplicate a 2 GB file, `clonefile(2)` (`cp -c`) costs 0 MB; backfilling 64 files cost 0 MB. Scan hashes the clone, so the stored bytes are exactly the hashed bytes. |
| downloads: sniff bytes, not Content-Type | A journal API serves its PDFs as `application/json`. |
| full text in FTS5, page rows in the main database | Every page of 81 PDFs (13M characters) adds 25 MB and indexes in 0.2 s; queries take 1–15 ms. Pages keep hits citable by page number. A separate index (Tantivy, Meilisearch) would be invisible to SQL and MCP. At ~50k documents the text may reach a few GB: then consider moving `file_pages` to an `ATTACH`ed file. |
| extract only live works' files | Personal paperwork sits next to papers in `~/Downloads`; its first 2 pages are needed for triage, the rest aren't needed at all. Search also filters to live works, since those 2 pages are indexed. |
| purge sure personal paperwork to a tombstone; hold unsure cases; serve only live works' files | Before this, scanning and triage kept the first pages, paths, URLs, PDF metadata, a stored copy and model claims of every payslip and CV. The web app served any file id to the tailnet, and MCP's `get_file` and `sql` read them. Containment can't cover `sql`, so the data goes. The tombstone (sha256, size, URL) keeps "never triage these bytes again" without the content. 0.9 rather than 0.5 because a purge can't be undone from the library: five synthetic personal documents scored P(personal) 0.998–1.0 with the 4B model, and `papyrus keep PATH` restores from the original. A plain `DELETE` left 8 of 24 text fragments in the file; `secure_delete` plus FTS5 `secure-delete` left none. |
| rank works by Reciprocal Rank Fusion of metadata and pages | bm25 from `works_fts` and `file_pages_fts` aren't on one scale (adding them, as before, let either swamp the other). RRF needs only ranks, and an embedding ranking can join later. |
| page kinds from the text, not the outline | Outlines were wrong as often as right (a book's "Index" entry spanning its references, "2.1.2 Bibliography" a manual's section about bibliographies). Bibliography-entry density ≥ 0.5 marks 86% of reference pages and 1% of body pages; the body pages were mostly unlabelled reference pages. |
| OCR with Vision `RecognizeDocumentsRequest` in the existing bridge | On-device, no new dependency, reading order across columns; ~57 ms a page warm, ~25 s warm-up per process. |
| `watch` reuses `scan.ingest_path` and the stages' own functions, in recorded runs of its own | One ingestion path means dedupe, purged tombstones and provenance can't drift between `scan` and the watcher. Each batch is one `watch` run (at most 25 steps), so `runs` shows what happened, and the web's "running" indicator is on only while it works. |
| `watch`: settle on size + mtime + ctime, not on close events | FSEvents has no close event and inotify's `CLOSE_WRITE` fires for every chunk some tools write. Stability over a few seconds works the same everywhere; ctime catches a browser writing the download URL xattr after the bytes. |
| `watch`: FSEvents and inotify through ctypes, not `watchdog` | No compiled dependency (wheels for Python 3.14 lag); about 100 lines each. Polling covers everything else. |
| `watch`: memory pressure gates *loading*, not using | On this 24 GB M4, `kern.memorystatus_vm_pressure_level` reads 2 (warn) while LM Studio holds the 4B model. Refusing at warn would mean never triaging with the model already loaded; loading a model at warn is what swaps. So: critical stops every model call, warn stops only calls that may load something (an LM Studio model that isn't loaded, Apple FM, Vision OCR). |
| `watch`: unload only models it loaded itself, idle for 10 min | LM Studio is shared with the user and other agents. A model loaded before the watcher asked, or reloaded by someone after it went away, is not the watcher's (`ModelLedger.refresh`). |
| `watch` runs inside Papyrus Server.app on macOS, not as a launchd agent | macOS asks for access to `~/Downloads` and `~/Desktop` on behalf of the responsible app. As a child of Papyrus Server.app, the prompt names that app, which already carries the usage strings; a launchd agent running `python3` would get a prompt naming Python, or nothing. The app also gives the menu-bar status and an on/off item for free. |
| `watch`'s queue mirrors the library rather than replacing it | `papyrus triage`, `process`, `keep` and other agents change what is pending behind the watcher's back. A queue that was the source of truth would go stale; one synced from the library before every step can't, and it keeps only what the library has no place for: retries, failures, step durations. Its own database, since that is working state, not knowledge, and losing it only forgets retries. |
| `watch` defaults to the `release` variant only | Every variant's watcher would add the same `~/Downloads` to its own library. Other variants opt in (`--watch`, recorded in `variant.yml`). |

## Adding a context source (Slack, Safari Reading List, Twitter…)

The recipe for an importer, e.g. a new `contexts/slack.py` (module and CLI
command `papyrus import-slack`; Slack's API, auth, filters and link
recognition are in [slack.md](slack.md)):

1. **One context per message or entry.**
   * Columns: `kind='slack'`, `locator=<permalink>` (unique per kind),
     `occurred_at` = message time, `actor`/`actor_id` = poster,
     `source='workspace/#channel'`, `text` = the paragraph, `url` =
     permalink.
   * `parent_id` points to the thread context.
   * Platform specifics go in `extra` (channel id, thread_ts, reactions).
   * Upsert on `(kind, locator)` so re-imports are idempotent.
2. **One `context_ref` per link or mention** in the text: `url=<link>` or
   `mention=<free text>`, `status='unresolved'`.
3. **The resolver** (`resolve.py`, `papyrus resolve`) turns unresolved
   refs into works:
   * arXiv/DOI links: `find_work` by the id; else `match.resolve`, then
     `find_work` by the record's other ids, title and year; else a new
     candidate work holding the id, enriched by `enrich_work` (done);
   * PDF links (to do) can be fetched with `download.fetch`/`download.record`,
     then go through triage; Slack uploads need `files:read` and the token;
   * on success, set `status='resolved'` and `work_id`/`file_id`, with
     `confidence` and `source`.
4. **Record a run** (`db.recorded_run(conn, "import-slack", …)`), and add an
   MCP tool or extend `recent_discoveries` if useful.
5. **Document the kind** in the contexts table of `docs/schema.md`.

Safari Reading List lives in `~/Library/Safari/Bookmarks.plist`
(`com.apple.ReadingList` children), which may need Full Disk Access.

## Open issues and roadmap

Roughly by value:

* **Books:** add an Open Library client (`enrich/openlibrary.py`).
  * AIMA 4th edition was matched to the 2010 3rd edition.
  * The Sutton & Barto PDF came out titled by its series name ("Adaptive
    Computation and Machine Learning").
* **Keys:** `S2_API_KEY` (venues, SPECTER2 embeddings, papers only S2 has,
  e.g. Bellard's) and `OPENALEX_API_KEY`. Ask the user.
* **Embeddings and semantic search:**
  * paper level: SPECTER2 from S2, or `text-embedding-nomic-embed-text-v1.5`
    in LM Studio;
  * chunk level: full text;
  * storage: `sqlite-vec`;
  * an MCP `semantic_search` tool.
* **Full text, next steps:** a PDF.js viewer that opens a hit at
  `#page=N&search=…` with the words highlighted; chunk embeddings for hybrid
  search (above), fused into the same RRF. OCR quality on maths is untested;
  Vision reads formulas as text.
* **Context importers:** Slack is done for links with an arXiv id or DOI
  (`papyrus slack import`, docs/slack.md); PDF links and uploads are left
  unresolved. Safari Reading List and Twitter still to do (see the recipe
  above).
* **Relations between works:** thesis ↔ paper, slides_for, versions. There
  is no table yet. Today work 12 (paper) and work 22 (Pang's thesis) are
  correctly separate but unlinked.
* **Review workflow:** confirm, merge or reject works from the CLI/MCP; MCP
  write tools (tags, notes, status).
* **Downloads:** publisher pages that need cookies or JavaScript (no
  `citation_pdf_url` in the HTML) fail and stay unresolved; DOIs given
  directly could go through Unpaywall/OpenAlex `best_oa_location`.
* **Decider calibration:** fit a temperature on labelled triage data. 64
  labelled files exist now.
* Thesis venue: S2 gives "arXiv: Combinatorics" where an institution is
  better.
* **The apps write only through the watcher.** Papyrus Server.app runs
  `papyrus watch`, which adds whatever lands in `~/Downloads` and
  `~/Desktop`. Dropping PDFs on the window or the menu-bar icon, pasting
  links and "download these tabs" still need a path; the likely shape is the
  same: the server runs `papyrus download`/`scan` as recorded runs, rather
  than the web server opening the database for writing.
* **The watcher:** subdirectories of watched directories are not watched
  (FSEvents would allow it; inotify needs a watch per directory). A model
  the watcher loaded and the user then started using is unloaded after 10
  idle minutes of the watcher, since LM Studio can't say who uses a model.
