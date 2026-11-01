# AGENTS.md: working on papyrus

papyrus is a local-first library of papers, books and other documents. It
pairs one documented SQLite database with a CLI and an MCP server.

Read these first:

* **This file**: rules, invariants and gotchas.
* [`docs/architecture.md`](docs/architecture.md): how the pieces fit, why
  they are built this way, and the open issues.
* [`docs/schema.md`](docs/schema.md) with
  [`src/papyrus/schema.sql`](src/papyrus/schema.sql): the data model. The DDL
  is authoritative.

## Commands

```sh
uv run pytest -q                       # ~10 s; live LM Studio / Apple FM / network tests skip themselves
PAPYRUS_LIVE=1 uv run pytest -q tests/test_enrich_live.py   # hits arXiv/OpenAlex/S2/Crossref
uv run papyrus --help                  # scan | download | store | triage | promote | purge | keep | extract | enrich | process | watch | inference | works | work | files | show | search | stats | config | mcp | slack | resolve
uv run papyrus config                  # config file path and the backend/decider in effect
uv run papyrus inference stats         # every model call so far: hits, failures, timings (also: show CALL_ID, prune)
PAPYRUS_HOME=/tmp/ph uv run papyrus watch --dir /tmp/dl   # the ingestion daemon in the foreground (--once: catch up and exit; --status)
cd tools/afm-bridge && swift build -c release                # Apple Foundation Models bridge (CLT is enough)
cd web && pnpm check                   # svelte-check; the app is served under /papyrus (links go through href())
python3 apps/macos/build.py [--install]   # Papyrus.app + Papyrus Server.app (menu bar, tailscale serve /papyrus)
python3 apps/macos/build.py --variant personal [--install]   # Papyrus Personal(.app, Server.app); --all rebuilds every variant
python3 apps/linux/install.py [--variant personal | --all] [--uninstall]   # Linux: the web server (and watcher) as systemd user services
# both installers: --watch / --no-watch (recorded in variant.yml; default: release watches, other variants don't)
```

**Platforms:** macOS and Linux. The installers share `apps/variants.py`.
Each OS-specific piece of the CLI has its counterpart:

* `macos.py` ↔ `linux.py`: clones/reflinks and download-URL xattrs;
* `download._clipboard_*`: `osascript`/`pbpaste` ↔ `wl-paste`/`xclip`;
* page thumbnails: `papyrus-thumb` (CoreGraphics, in Papyrus Server.app) ↔
  poppler's `pdftoppm` (`web/src/lib/server/files.ts`).

Keep both sides working. The Apple bridge (Foundation Models, Vision OCR)
has no Linux counterpart. Tests that need one OS skip themselves on the
other.

**Variants:** each library is a variant with its own home `~/.papyrus/<variant>/`
(`release` is the default; `--variant` / `PAPYRUS_VARIANT` pick another,
`PAPYRUS_HOME` overrides both). The naming rules (bundle ids, `~/Library`
directories, ports, tailnet paths) are in `apps/macos/README.md`, "Variants".

The usual pipeline is `scan` (or `download`) `→ triage → promote → extract → enrich`;
`papyrus process` runs the last four. Every stage is idempotent and processes
only what is pending (see `--all` / `--reset`). `papyrus watch` (`watch/`)
runs the same stages in the background as PDFs land in `~/Downloads` and
`~/Desktop`, one file at a time; Papyrus Server.app (macOS) or a systemd user
unit (Linux) keeps it running.

## Data safety (read before running anything)

* **`~/.papyrus/release/papyrus.sqlite` is the user's real library** (and so
  is every other variant's, `~/.papyrus/<variant>/papyrus.sqlite`).
  * Experiment against a scratch copy:
    `mkdir /tmp/ph && sqlite3 ~/.papyrus/release/papyrus.sqlite ".backup /tmp/ph/papyrus.sqlite"`,
    then set `PAPYRUS_HOME=/tmp/ph` (this moves the managed store too; with
    only `PAPYRUS_DB` set, `scan`/`download` would still write into
    `~/.papyrus/release/store`). Tests do this automatically (`tests/conftest.py`).
  * Before any bulk change to the real database, take a dated `.backup` next
    to it (`~/.papyrus/release/papyrus.backup-YYYY-MM-DD-<why>.sqlite`).
* **Never modify, move or delete the user's files on disk.** papyrus only
  reads them. Report odd files instead. For example, a PDF whose filename
  suggests a textbook may turn out to be an unrelated document.
* **The managed store** (`~/.papyrus/<variant>/store/<16 hex digits>.pdf`, flat) is papyrus's
  own: content-addressed, read-only, written only through `store.commit`.
  Never rewrite or delete a stored file; `files.stored_path` points there.
  The one exception is `purge.purge_file`, which erases personal paperwork.
  Copy local files in with `store.snapshot` (an APFS clone or Linux reflink, falling back to a
  copy), never `shutil.copy`/`cp`, which duplicate the bytes.
* **Never delete rows that carry knowledge:**
  * works are rejected (`status='rejected'`, ids released by
    `works.reject_work`) or merged, never deleted;
  * assertions are superseded (`superseded_at`), never deleted;
  * undo an enrichment with `pipeline.reset_enrichment`, which keeps the
    history.

  The exception is a file purged as personal paperwork (`purge.py`): what
  papyrus knew about it is erased on purpose.
* **Downloaded personal documents live alongside papers in `~/Downloads`:**
  payslips, tax certificates, CVs, exam papers. Don't paste their text into
  commits, tests, fixtures or logs. Triage deliberately stores no abstract or
  topics for non-library documents.
* **The inference cache holds that text too** (`<home>/inference/inputs/`:
  every model prompt, i.e. documents' first pages and paths;
  `inference/outputs/`: the answers). `inference.sqlite` itself holds only
  hashes and timings. Treat the files like the API log: private, never in
  commits or fixtures. Model calls about a document must run inside
  `with inference.subject(files.sha256):`, or purge can't find their texts.
* **Personal paperwork is purged, or held out of sight** (`purge.py`):
  * when the decider gives P(kind=personal) ≥ 0.9, promote purges the file down
    to a tombstone: sha256, size, `purged_at`, the download URL, and one
    `purged` assertion. Scan and download then skip those bytes without
    recording anything;
  * between 0.5 and 0.9 (or without a decider) the file is *held*: no work,
    first pages kept for review (`papyrus purge --dry-run`, `purge ID`,
    `keep ID`);
  * so is a file the screen (`screen.py`, triage's embedding-model first
    stage) scores P(personal) ≥ 0.9: triage stops before any language model.
    The screen alone never purges;
  * nothing about a file outside a live work may reach the browser or MCP.
    The web's `LIVE_FILE` (`web/src/lib/server/files.ts`) and Python's
    `search.LIVE_FILE` gate file bytes, thumbnails, paths, URLs and claims.
    New routes and tools must use them too.

## Invariants

1. **Provenance on every claim.** Model and API output goes into `assertions`
   with a `source` tag:
   * `llm:<backend>:<model>`, `decide:<backend>:<model>`, `screen:<backend>:<model>`
   * `api:<service>`, `api:enrich`
   * `heuristic:<name>`, `papyrus:<action>`
   * `user`

   Columns on `works` and `people` are the *resolved* view. Never overwrite
   a claim in place; `db.assert_` supersedes the same source's earlier one.
   The precedence used when resolving is in `works.SOURCE_RANK`.
2. **Every pipeline invocation is a recorded run.** Wrap it in
   `with db.recorded_run(conn, "<command>", args, backend=..., prompt_version=...) as (run_id, stats):`,
   which always closes the run as `completed`, `interrupted` or
   `crashed: …`. Pass `run_id` to `assert_`.
3. **No model or network calls inside `db.tx()`.** The database is shared
   (WAL) with other processes, including the MCP server and other agents.
   Compute first, then write in one short transaction.
4. **Identifiers are globally unique** (`work_ids` primary key
   `(scheme, value)`) and normalised:
   * arXiv ids without the version (the version goes in `file_works.version`);
   * DOIs lowercased;
   * ORCIDs as bare digits.

   Enrichment never merges a candidate whose ids belong to another live work
   (`pipeline._conflict`).
5. **This document's ids, not cited ones.** `triage.detect_ids` trusts:
   * the filename and the download URLs (`kMDItemWhereFroms`, or the
     `where_froms` of a `download` context) and the title of the link it was
     downloaded from;
   * on page 1 only, arXiv's margin stamp
     (`arXiv:2412.06464v3 [cs.CL] 6 Mar 2025`).

   arXiv URLs elsewhere on page 1 are citations.
6. **Schema changes:**
   * edit `schema.sql`;
   * bump `db.SCHEMA_VERSION` (`db.init` then refuses older databases);
   * migrate the user's database once, by hand, after a dated `.backup`
     (one user, so no migration code is kept);
   * update `docs/schema.md` in the same commit.

   The schema is a public contract: users and agents query it with SQL over
   MCP.
7. **Changing a prompt** in `triage.py` (instructions, schema, `KINDS`,
   `DECISIONS`) means bumping `PROMPT_VERSION` and noting what changed in the
   comment above it. It is recorded on every run.
8. **The `Backend` / `Decider` / `Embedder` contracts** in `llm/base.py` and the
   `WorkRecord` / `AuthorRecord` types in `models.py` are shared by several
   modules. Change them deliberately, and update every implementation.
   The optional hooks the inference cache uses (`seed`, `cache_params`,
   `model_build`, `seed_honoured`, `model_loaded`, `last_usage`) are listed
   there too. **Changing a backend's prompt layout** (message order, the
   prefill, how the schema is sent) means bumping its `cache_params()["impl"]`,
   or old cached answers are served for the new layout.
9. **Never throw away an API response.** Every request through
   `enrich/http.HttpClient` (enrichment, `papyrus download`, Slack) is
   appended, with its raw response, to `<library home>/api-log/<UTC date>.jsonl`
   (`apilog.py`; format in its docstring). Later code can trawl old responses
   for fields nobody extracted at the time. So:
   * new API clients go through `HttpClient`, never a bare `httpx`/`urllib`
     call, and one-off experiments too (`uv run python …` with
     `http.default_client()`);
   * credentials are redacted before writing (`apilog.SECRET_*`); pass keys as
     headers;
   * the log is private data (Slack messages): never copy it into commits,
     tests or fixtures;
   * LM Studio and decider calls are *not* logged (`llm/*` uses its own httpx
     clients): their prompts hold the first pages of personal documents.
     They are recorded by the inference cache instead (next).
10. **Model calls go through the inference cache** (`inference.cached_backend`
    / `cached_decider` / `cached_embedder`) so they are recorded, timed and reusable, and claims
    carry `call_id`. New callers wrap their models the same way.

## Gotchas (all learned the hard way)

**LM Studio** (0.4.x, `localhost:1234`, shared with the user and other agents):

* **The server is separate from the app** and is often off.
  `lmstudio.ensure_server` starts it with `lms server start` (not on PATH:
  `~/.cache/lm-studio/bin/lms` since LM Studio 0.4, `~/.lmstudio/bin/lms`
  before; `lmstudio.lms_path` tries both) when the backend or decider selects LM Studio; turn that off
  with `lmstudio.autostart = false` or `PAPYRUS_LMSTUDIO_AUTOSTART=0`. Tests
  set the latter (`tests/conftest.py`).
* **Live tests load models:** once the server is up, `test_live_lmstudio_decides`
  runs and loads the 4B model (1 h idle timeout). Unload it afterwards
  (`lms unload qwen3.5-4b-mlx`) unless the user is using it.
* **No silent downgrade:** `triage` exits if no decider is available unless
  the decider is explicitly `none` (option, `PAPYRUS_DECIDER` or the
  config). Without one, library-ness rests on the extraction model's biased
  yes/no.

* **Thinking:** Qwen3.5 MLX models ignore `enable_thinking`,
  `reasoning_effort` and `/no_think`. The only way to switch thinking off is
  to end the messages with an assistant turn `"<think>\n\n</think>\n\n"`.
* **Output:** answers can arrive in `reasoning_content` instead of
  `content`, and `response_format` is not enforced. Always parse and
  validate with `llm/schema_lite.py`.
* **Logprobs:** returned only when thinking is off, with at most
  `top_logprobs=10` (more gives HTTP 400). Values are coarsely quantised. Use
  `max_tokens >= 2` with the prefill.
* **Model loading:** models load just-in-time and unload after an idle
  timeout. Two models at once on this 24 GB **base M4** caused 8 GB of swap
  and a 5× slowdown. Don't load models you don't need, and only unload what
  you loaded (`~/.cache/lm-studio/bin/lms ps / unload`).
  The exception is the screen's embedding model
  (`text-embedding-nomic-embed-text-v1.5`, 84 MB, ~30 ms a file, ~6 s to load
  just in time). It sits beside the 4B without pressure (measured 2026-11-01),
  `lmstudio.loaded_models` doesn't list it, and the watcher lets it load even
  under "warn". LM Studio's own idle timeout (1 h) unloads it.
* **Speed on this machine:**

  | model | prompt processing | generation | per file |
  |---|---|---|---|
  | qwen3.5-9b | ~60–100 tok/s | ~11 tok/s | 60–100 s |
  | qwen3.5-4b | — | — | 12–30 s, including the decider |

  The 4B is the default for both extraction and decisions. Prompt caching is
  automatic (prefix-based), with no markers to set, but for Qwen3.5 (hybrid
  layers) the MLX engine restores a prefix only at 256-token checkpoints where
  a prefill call ended. Questions that share a prefix but diverge after it get
  no reuse unless something primes it: the decider sends one short prime per
  state (`OpenAICompatDecider._prime`). Restore counts are in
  `~/.cache/lm-studio/server-logs` ("Prompt cache restore: cached_tokens=").

**Seeds:** LM Studio accepts `seed` but its MLX engine ignores it (same seed,
different answers at temperature 1). The Apple bridge honours it (same seed,
same answer). Rebuild the bridge after pulling, or an old binary ignores it.
The seed is part of the inference cache's key either way.

**Apple Foundation Models** (`tools/afm-bridge`, `llm/apple.py`):

* 8192-token context, about 40 output tokens/s, no logprobs (so no decider).
* Requests from command-line processes are refused under memory pressure
  (`CriticalMemoryPressure`, error 1013), which is likely while LM Studio
  models are loaded.
* It misclassifies document kinds (it called a CV an "article"). Use it for
  extraction only, as a fallback.

**The decider** (`llm/decide.py`, a Jev / "System One" style API):

* Its `kind` distribution is good. Its yes/no "is this library material?"
  answer is biased towards yes (0.73 for an exam paper it labels `personal`
  with p=1.0).
* So library-ness comes from `P(kind=personal)` (`works.is_library`).
  Probabilities are uncalibrated.

**The screen** (`screen.py`, `data/screen.bin`, `tools/screen/`):

* `data/screen.bin` is the production classifier: a logistic regression on
  nomic-embed-text embeddings of the name, folder, size, download host and
  first 128 words. Retrain it with `tools/screen/export.py` (each library, any
  machine) and `tools/screen/train.py` (its docstring has the commands), then
  commit the new file. The weights hold no text (standardisation folded in).
* The exports and the training run see the first pages of personal
  documents. Keep them out of git (`/training/` is ignored), tests and logs.
  `train.py` embeds without the inference cache on purpose: the cache would
  write purged documents' text back to disk.
* Its labels come from earlier triage, which made mistakes: the first
  training run found two personal documents that triage had promoted to
  works. Where the screen disagrees with a label, look at the file.
* It is skipped for files without a text layer: from the name alone it
  misjudged library files.

**PDF text** (pypdfium2):

* PDFium marks soft hyphens with `\x02`, which splits words in full-text
  search. `pdf.clean_text` strips control characters and zero-width
  characters.
* About 5 of 81 PDFs have no text layer (`text_status='no_text'`, judged on
  the first 2 pages). `papyrus extract` OCRs pages without one through the
  bridge's `ocr` op (Apple Vision). Rebuild the bridge after pulling
  (`swift build -c release`); an old binary answers "unknown op" and OCR is
  skipped with a message.
* `text_status='no_text'` can be wrong both ways: AIMA's first pages are
  scanned images but the other 2,100 have a text layer.

**Full text and search** (`fulltext.py`, `search.py`, `web/src/lib/server/query.ts`):

* **Privacy:** only files of live works are extracted past page 2, and every
  search path filters to live works. Keep it that way in new search tools:
  the first pages of *held* personal files (see Data safety) are in
  `file_pages`. Purged files have none.
* **Erasing in SQLite:** a plain `DELETE` leaves the text in free pages (8
  of 24 fragments of a test payslip survived), and FTS5 masks deleted entries
  rather than removing them. `purge_file` therefore sets `PRAGMA
  secure_delete` and the index's `secure-delete` option in the same
  connection, then checkpoints the WAL. A test checks the bytes on disk.
* **The ranking exists twice**, in `search.py` and `query.ts`. Change both.
* **FTS5:** `bm25()`/`snippet()` can't go in an aggregate query, even in a
  subquery (SQLite flattens it): use a `MATERIALIZED` CTE. `MATCH ? AND
  rowid IN (…)` reruns the MATCH per rowid (1.7 s for 180 snippets of `t*`);
  write `+rowid IN (…)` (24 ms).
* **Triage reads pages ≤ `TRIAGE_PAGES` only**, whatever `file_pages` holds.

**Abstracts:**

* Don't ask a small model to copy text verbatim; it's slow and it pads.
  Triage asks for start/end anchors and `textspan.best_abstract` cuts the
  span out of the page. A regex fallback finds "Abstract … Introduction".
* Small models fill optional fields with junk (a whole payslip as the
  "abstract"). Make fields required, give them explicit empty-value rules,
  and gate them (`has_abstract`).

**Enrichment APIs:**

* **Semantic Scholar** returns 429 almost always without `S2_API_KEY`; after
  repeated 429s it is skipped for 120 s.
* **OpenAlex** works keyless on a small shared budget (`OPENALEX_API_KEY`
  for $1/day). Its records are sometimes wrong (another paper's title under
  an arXiv id, wrong affiliations).
* **Books:** Crossref, OpenAlex and S2 are poor for books. Open Library is
  the planned fix.
* Set `PAPYRUS_MAILTO`. Responses are cached in `~/.cache/papyrus/http` for
  30 days (`PAPYRUS_HTTP_CACHE=0` turns that off).

**Downloads and the clipboard** (`download.py`):

* Safari's copied tabs are on the clipboard as RTF (`HYPERLINK` fields, with
  titles) and as plain text (`title\nURL\n\n`; a PDF tab's title is its URL,
  hence the doubled lines). `pbpaste -Prefer rtf` falls back to the plain text
  whenever there is any, so the RTF is read with
  `osascript -e 'the clipboard as «class RTF »'` (hex, ~0.1 s).
* Decide "is it a PDF" by the first bytes: some servers send PDFs as
  `Content-Type: application/json`.
* arXiv's PDF responses carry `filename="<id>v<N>.pdf"`, which is where the
  version of a downloaded preprint comes from.

**`papyrus watch`** (`watch/`, docs/architecture.md "Pipeline"):

* **It writes to the real library** as soon as Papyrus Server.app (or the
  Linux unit) runs it. Try changes on a scratch library and directory:
  `PAPYRUS_HOME=/tmp/ph uv run papyrus watch --dir /tmp/dl`. Without `--dir` it
  watches the config's `[watch] dirs`, by default the real `~/Downloads`.
* **One watcher per library** (`<home>/watch.lock`). A second one, or `--once`
  while one runs, exits with status 75, which the app and the unit don't
  treat as a crash.
* **Its log names ids and counts only.** No titles, paths, page text, or
  exception messages (an `OSError`'s message is a path). Keep it that way in
  new log lines.
* **Memory pressure reads "warn" on this machine whenever LM Studio holds a
  model.** So warn blocks only calls that may *load* a model; critical
  blocks all of them (`Watch._gate`).
* **Never a second LM Studio model, unload only its own**
  (`watch/models.ModelLedger`). If the user's model is loaded and differs
  from the configured one, triage waits (state `lmstudio-unavailable`, "another
  model is loaded").
* **`uv run` forwards SIGTERM** to the watcher and passes its exit status
  through, so Papyrus Server.app can supervise `uv run … papyrus watch`
  directly. SIGTERM closes the open run as `interrupted`.
* **Its queue (`<home>/queue.sqlite`) mirrors the library**, synced before
  every step: to queue work, make it pending in the library (e.g.
  `triage_status='pending'`), not in the queue. A step that leaves its item
  pending counts as `no_progress` and is retried later (1 h, 4 h, 16 h, then
  `failed`), which is what stops an enrichment that finds nothing from
  looping.
* **Never open the live databases in a GUI SQLite editor that saves**
  (SQLPro for SQLite, DB Browser, …). On 2026-11-01 SQLPro, a sandboxed app,
  safe-saved copies over `papyrus.sqlite` and `inference.sqlite-wal` (leaving
  `*.sb-<hex>-<random>` names) while the watcher held them. Its writes then
  went to the nameless old files, and every new reader paired the copy with
  the live WAL ("file is not a database"). Read the live files with `sqlite3`,
  datasette or MCP's `sql`, or give the editor a `.backup` copy.
  * The watcher now notices a replaced file (`Watch._guard`, inode checks
    before every step). It writes what its connections see to
    `<home>/rescued-<time>/` with SQLite's backup API, then exits (status 70)
    *without closing*, since closing would checkpoint into the replaced
    files. It refuses to start (status 78) while a `rescued-*` folder exists.
  * **To restore:** quit Papyrus Server.app and any watcher; check
    `lsof +D ~/.papyrus/<variant>` is empty; move `papyrus.sqlite*`,
    `inference.sqlite*` and `queue.sqlite*` aside; copy the rescued files in;
    run `PRAGMA integrity_check`; delete the set-aside copies (they can hold
    text that purge erased) and the rescued folder.
  * A process without the guard can still be rescued: `lldb -p PID` and
    `expr (int)fcopyfile(FD, open("/tmp/x", 0x601, 420), 0, 8)` for each
    database fd that `lsof -p PID` lists, then `kill -9` it (not SIGTERM).
* **Pause it for LM Studio experiments:** `papyrus watch --pause` (or the
  menu bar's Pause Model Work) creates `<home>/watch.pause`. While it exists,
  the watcher, whoever runs it, asks no model (triage and OCR wait; state
  `paused`) but still adds, extracts and enriches files. The file in progress
  finishes first (up to ~20 s). It lasts across restarts until `--resume`.
* **Its runs are `command='watch'`**, one per batch. `scan`'s own runs are
  not involved; `scan.ingest_path` is the shared per-file body.
* **Background priority throttles I/O too** (`PRIO_DARWIN_BG`): hashing and
  enrichment are slower in the watcher than on the command line. Don't call
  `system.lower_priority()` in tests: it would slow the rest of the suite.

**uv:** `~/pyproject.toml` defines a uv workspace. This project declares its
own `[tool.uv.workspace]` so that uv never adds it there. Don't remove that
table, and never edit `~/pyproject.toml`.

**MCP SDK:** this is v2; `FastMCP` is now `mcp.server.mcpserver.MCPServer`,
with the same decorators.

## Testing patterns

* **No model needed:** `tests/test_triage_promote.py` has `FakeBackend`,
  `FakeDecider` and `_lib()`, which builds a temporary library from
  generated PDFs (`test_core._make_pdf`).
* **No network:** record API responses as fixtures under `tests/fixtures/`,
  and monkeypatch `match.resolve` for pipeline tests.
* **Downloads:** `tests/test_download.py::_web` serves PDFs, redirects and
  landing pages through `httpx.MockTransport`; pass the client to
  `download.download` (or `http.set_default_client` for CLI tests).
* **The inference cache:** `tests/test_inference.py`. Wrap `FakeBackend` /
  `FakeDecider` with `inference.cached_backend` / `cached_decider`; the store
  follows `PAPYRUS_HOME`, so each test has its own.
* **The watcher:** `tests/test_watch.py` drives `Watch` with a fake watcher
  (`inject` paths), a fake clock, a fake memory probe, a fake LM Studio
  (`LMStudio`: what is loaded, what was unloaded) and `FakeBackend` /
  `FakeDecider`. Call `pump()` once after injecting (the settle timer starts
  there), advance the clock, then `pump()` again. Use `_pdf(..., old=True)`
  for files the catch-up scan should take at once. The watcher enriches what
  it promotes: a test module that runs it (even through `test_watch`'s
  helpers) must use conftest's `no_enrich_network` fixture, or it calls the
  real APIs (it did, twice: 12 s and 2 min test runs).
* **Live tests** must skip themselves when the service isn't reachable
  (LM Studio, the AFM bridge, `PAPYRUS_LIVE` for the network).
* **Regressions found by reviewing real data** go into
  `tests/test_review_fixes.py`, using synthetic inputs and no personal text.

## Working in parallel

* Pick an area from "Open issues" in `docs/architecture.md`. Areas with
  little overlap between files:
  * `enrich/<new source>.py`
  * `contexts/<importer>.py`
  * `llm/*`
  * MCP tools
  * embeddings
* `cli.py`, `schema.sql` and `triage.py` are the shared hot spots. Keep
  edits to them small, and say so in your commit message.
* Commit small, self-describing commits, and run `uv run pytest -q` before
  each one. Push to `origin` (GitHub) only when asked.
