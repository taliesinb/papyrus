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
                               │       filename_hint assertion
                               ▼
                           triage ─▶ assertions on files: kind(+probs), is_library_material(+probs),
                               │       title, authors, year, venue, abstract, summary, topics,
                               │       language, ids
                               ▼
                           promote ─▶ works, work_ids, work_authors(raw), topics(llm), file_works,
                               │        works_fts; rejects works whose files are personal
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

| stage | module | pending means | notes |
|---|---|---|---|
| scan | `scan.py`, `pdf.py`, `macos.py`, `filenames.py` | the path is new, or its (size, mtime) changed | Files are deduplicated by sha256. Text is extracted from the first 2 pages only. `kMDItemWhereFroms` gives the download URL and referrer. Contexts whose path has disappeared get `missing_at`. |
| triage | `triage.py`, `llm/*`, `textspan.py` | `files.triage_status <> 'done'` | One extraction call per file, plus one decider call per question. The state sent to the model holds the filename(s), path, download URL, PDF metadata, filename hint and ≤4000 characters of page text. |
| promote | `works.py` | the file has no `file_works` row | Deduplicates by id first, then by normalised title ± 1 year. Library-ness comes from `works.is_library`. |
| enrich | `enrich/*` | the work has no `api:*` claims and no `enrich_candidates` | `match.resolve` (ids first, then title search) → exclude conflicting candidates → `cluster` → `merge` → write. The score threshold is 0.85. |

## Module map

| module | role |
|---|---|
| `schema.sql`, `db.py` | DDL; connect/init; `tx`, `assert_`, `current_assertions`, `recorded_run` |
| `scan.py`, `pdf.py`, `macos.py`, `filenames.py` | file discovery, hashing, PDF info and text, macOS provenance (xattrs via ctypes), hints from hand-made filenames |
| `triage.py` | prompts (`INSTRUCTIONS`, `EXTRACT_SCHEMA`, `KINDS`, `DECISIONS`), `build_state`, `detect_ids`, `triage_file` |
| `textspan.py` | find spans from anchors (`locate`), the `heuristic_abstract` regex, and `best_abstract`, which reconciles the two |
| `works.py` | `SOURCE_RANK`/`best` resolution, `is_library`, `promote_file`, `reject_work`, `reindex_work`, `work_json` |
| `llm/base.py` | contracts: `Backend.extract` (JSON-schema output) and `Decider.decide` (Jev-style typed questions → probabilities) |
| `llm/lmstudio.py` | extraction through any OpenAI-compatible server (the LM Studio quirks are handled here) |
| `llm/apple.py` + `tools/afm-bridge` | extraction through Apple Foundation Models (a Swift CLI speaking NDJSON; guided generation via `DynamicGenerationSchema`) |
| `llm/decide.py` | `JevHTTPDecider` (`/v1/systemone`: TypeSafe, Ollama `tev1`, …) and `OpenAICompatDecider` (logprobs over lettered options) |
| `llm/schema_lite.py` | JSON extraction and coercion for a JSON Schema subset; all model output passes through it |
| `enrich/http.py` | polite shared HTTP client: per-host rate limits, retries, disk cache |
| `enrich/ids.py` | arXiv / DOI / ISBN detection and normalisation (with checks against false positives) |
| `enrich/{arxiv,openalex,semanticscholar,crossref}.py` | source clients → `models.WorkRecord` |
| `enrich/match.py` | `resolve`, `score`, `cluster`, `merge` |
| `enrich/pipeline.py` | `enrich_work` writes to the database (`_conflict`, authors → people, topics, ids); `reset_enrichment` |
| `mcp_server.py` | MCP tools: `search_library`, `fulltext_search`, `get_work`, `get_file`, `recent_discoveries`, `sql` (read-only connection), `library_stats`; resource `papyrus://schema` |
| `cli.py` | click commands; SIGTERM becomes KeyboardInterrupt so runs close as `interrupted` |

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
`--decider auto|none|lmstudio[:model]|jev:<url>`.

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

## Adding a context source (Slack, Safari Reading List, Twitter…)

The recipe for an importer, e.g. a new `contexts/slack.py` (module and CLI
command `papyrus import-slack`):

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
3. **A resolver** (shared, to be written in `resolve.py`) turns unresolved
   refs into works:
   * arXiv/DOI links → `enrich.ids` → `find_work`, or create a candidate
     work from `match.resolve_best`;
   * PDF links can be downloaded, then go through scan → triage;
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
* **Full-text extraction of all pages** (today only pages 1–2), and OCR for
  the `no_text` files (Apple Vision, or a VLM).
* **Context importers:** Slack, Safari Reading List, Twitter (see the recipe
  above), and the shared resolver.
* **Relations between works:** thesis ↔ paper, slides_for, versions. There
  is no table yet. Today work 12 (paper) and work 22 (Pang's thesis) are
  correctly separate but unlinked.
* **Review workflow:** confirm, merge or reject works from the CLI/MCP; MCP
  write tools (tags, notes, status).
* **A managed file store** (`files.stored_path`): copy PDFs into the
  library, keyed by hash.
* **Decider calibration:** fit a temperature on labelled triage data. 64
  labelled files exist now.
* Thesis venue: S2 gives "arXiv: Combinatorics" where an institution is
  better.
