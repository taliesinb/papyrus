# papyrus

A local-first library for papers, books, slides and notes. It has:

* an open, documented SQLite database
* provenance for everything: where and how each document was found, and
  which model or API said what about it
* local-model classification and metadata extraction
* full-text search
* an MCP server

## Status

Early. What works today:

| stage | command | what it does |
|---|---|---|
| scan | `papyrus scan [-r] DIR...` | Finds PDFs, hashes and dedupes them, and reads the PDF info and the text of the first 2 pages into FTS5. It records a `filesystem` context for each path, including the download URL and referrer from macOS `kMDItemWhereFroms` (on Linux, the `user.xdg.origin.url` attributes). New files are copied into the managed store (an APFS clone or a Linux reflink where the filesystem has them, so free). Re-scans are idempotent, and paths that have gone are marked missing. |
| download | `papyrus download URL...`, `-c`/`--clipboard`, `-`/`--stdin`, `-f`/`--file FILE` | Fetches PDFs into the store and ingests them like `scan`. Takes tabs copied from Safari (titles included), or URLs separated by newlines, commas or spaces, markdown links and arXiv ids. arXiv/OpenReview pages map to their PDF; other landing pages are followed via `citation_pdf_url`. Each URL is a `download` context; URLs already in the library are skipped. `--dry-run` shows what would happen; `--ingest` then ingests the new files (classify, extract, promote, index, match). |
| store | `papyrus store [--check]` | Copies files scanned before the store existed into it; `--check` re-hashes the stored copies. |
| classify | `papyrus classify [FILE_IDS] [--classifier C] [--no-screen] [--all] [--fresh]` | First a **screen**, a small embedding model (~30 ms a file), sets aside files that are surely personal paperwork; they are held for review without asking the language models (`[screen]` in the config). Then a **classifier** (Jev/System-One style typed questions answered with probabilities) settles each file's `kind`, so whether it is library material. Identifiers (arXiv/DOI/ISBN) are found by regex in the filename, download URL, the PDF's own subject and keywords, and page 1. |
| extract | `papyrus extract [FILE_IDS] [--backend B] [--all] [--fresh]` | For files classified as library material: an **extraction** model returns title, authors, year, venue, the abstract's span (cut from the page text), summary, topics with fields, and language. Personal paperwork gets no metadata from a model. |
| promote | `papyrus promote` | Creates or links `works` from classified and extracted files, deduplicating by identifier, then by normalised title. Personal paperwork the classifier is sure of (P(kind=personal) ≥ 0.9) is purged: its text, paths, filenames, PDF metadata, model claims and stored copy are erased. Only the sha256, size and download URL remain, so the same bytes are skipped from then on. Less certain cases are held: no work, never served. |
| purge, keep | `papyrus purge [FILE_IDS] [--dry-run]`, `papyrus keep ID\|PATH...` | `purge` erases what promote would, lists held files, or purges the given files on your say-so. `keep` marks files as library material whatever the classifier says; given the path of the original, it also brings back a purged file. |
| index | `papyrus index [FILE_IDS] [--all] [--no-ocr]` | Reads every page of the files of live works into the full-text index (personal paperwork stays at the 2 pages scanning read). Labels references, index and contents pages, which rank lower; keeps the PDF outline, so hits show their section; OCRs pages without a text layer with Apple Vision (through `tools/afm-bridge`). |
| match | `papyrus match [WORK_IDS] [--all] [--reset]` | Matches works against arXiv, OpenAlex, Semantic Scholar and Crossref. Adds the abstract, venue, the published DOI next to the arXiv id, authors with ORCID/OpenAlex/S2 ids, and topics. Candidates that are provably other works are excluded. A lookup a source refused (rate limits) records nothing and is tried again later. |
| ingest | `papyrus ingest [FILE_IDS] [--backend B] [--classifier C]` | Everything after `scan` over whatever is pending: classify, extract, promote, index, match (with FILE_IDS: those files and their works). Stops before promoting if classifying can't run. |
| watch | `papyrus watch [--once] [--dir DIR] [--status] [--perf]` | A background daemon that ingests new PDFs from `~/Downloads` and `~/Desktop` (config `[watch] dirs`) once they have finished downloading: it scans them through the same path as `scan`, then reads (OCR), classifies, extracts, promotes, indexes and matches them, one step at a time at background priority. Classifying and extracting run in phases (all pending files classified, then all extracted), so the two may be different models. Model work waits under memory pressure, while LM Studio is off or has another model loaded, and without a classifier; it loads a second model only when it fits in memory and unloads only what it loaded. Logs carry file ids and counts only; `--perf` summarises its performance log. Its queue (`queue.sqlite`) retries failed steps after 1 h, 4 h and 16 h, then gives up (`--retry-failed`). `--pause` / `--resume` (Pause Model Work in the menu) hold reading, classifying and extracting, e.g. while you experiment with LM Studio; new files are still added. Papyrus Server.app runs it on macOS (Watch for New PDFs in its menu: "Ingesting N/M files" and a line per stage with its pace and time left; the menu-bar scroll's paper is solid while there is work), a systemd user unit on Linux; `--once` catches up and exits. |
| search | `papyrus search QUERY [--kind K] [--files] [--json]` | Works matching in their metadata or anywhere in their pages, ranked by Reciprocal Rank Fusion of the two, each with its best pages (page, section, snippet). Free text or FTS5 syntax. `--files` ranks files instead, personal ones included. |
| browse | `papyrus works [QUERY]`, `papyrus work ID`, `papyrus files`, `papyrus show ID`, `papyrus stats` | |
| inference | `papyrus inference stats`, `show CALL_ID`, `prune [--older-than DAYS] [--outputs]` | Every model call is recorded in `~/.papyrus/<variant>/inference.sqlite`: which model, which question, a hash of the data, how long it took, a hash of the answer. The texts are files beside it, which `prune` deletes and purging a document removes. A question already asked about the same text (same model, prompt, parameters and seed) is answered from it; `--fresh` (`papyrus classify`, `extract`) asks anyway. Each claim a model writes points at the call it came from (`assertions.call_id`). |
| web | `cd web && pnpm dev` | Read-only SvelteKit browser at `/papyrus/`: facets, full-text search, TF‑IDF "similar", inline PDF viewer, provenance. See [`web/README.md`](web/README.md). |
| apps | `python3 apps/macos/build.py --install` | `Papyrus.app`, a window on the web browser, and `Papyrus Server.app`, a menu-bar app that runs the server, publishes it with `tailscale serve` at `https://<mac>.<tailnet>.ts.net/papyrus/`, and runs `papyrus watch`. See [`apps/macos/README.md`](apps/macos/README.md). |
| Linux | `python3 apps/linux/install.py` | The web server as a systemd user service, published the same way, and `papyrus watch` as another. Page thumbnails need poppler's `pdftoppm` there. See [`apps/linux/README.md`](apps/linux/README.md). |
| MCP | `papyrus mcp` | stdio MCP server with these tools: `search_library`, `fulltext_search`, `get_work`, `get_file`, `recent_discoveries`, `sql` (read-only), `library_stats`. Resource: `papyrus://schema`. |

Next on the list:

* Open Library for books
* Slack: resolve PDF links and uploads (arXiv/DOI links are done:
  `papyrus slack import`); Safari Reading List and Twitter contexts
* embeddings with `sqlite-vec`
* a managed file store

The full list is under "Open issues" in
[`docs/architecture.md`](docs/architecture.md).

**Contributing (humans or agents):** start with [`AGENTS.md`](AGENTS.md),
which covers data-safety rules, invariants and the hard-won gotchas.

## Models

All models run locally.

| role | backend | spec |
|---|---|---|
| extraction | LM Studio (OpenAI-compatible, `localhost:1234`); the default is the 4B model | `--backend lmstudio:qwen3.5-4b-mlx` |
| extraction | Apple Foundation Models (on-device; Swift bridge in `tools/afm-bridge`) | `--backend apple` |
| classifier | LM Studio emulation of the Jev `/v1/systemone` API, using logprobs over option letters | `--classifier lmstudio:qwen3.5-4b-mlx` |
| classifier | any Jev-compatible server (TypeSafe, Ollama `tev1`, classifier, Lichen…) | `--classifier jev:http://host:port` |

`--backend auto` picks LM Studio, starting its server (`lms server start`) if it
is off, otherwise Apple FM. `papyrus classify` stops if no classifier is available rather than
relying on the extraction model's own kind and "library material" answers;
pass `--classifier none` to go on anyway. Build the Apple bridge once with:

```sh
cd tools/afm-bridge && swift build -c release
```

Two LM Studio notes:

* With Qwen3.5 MLX models in LM Studio 0.4.x, thinking can only be switched
  off by prefilling an empty `<think></think>` assistant turn. Both backends
  do this.
* The JSON answer may also arrive in `reasoning_content` and is not
  schema-enforced, so every result is parsed and validated (`llm/schema_lite.py`).

## Configuration

`~/.papyrus/release/config.toml` (in the library's home; `PAPYRUS_CONFIG` points
elsewhere). Every key is optional. `papyrus config` shows the file's path and
the settings in effect with where each comes from; `papyrus config --init`
writes a commented template.

```toml
backend = "auto"      # auto | lmstudio[:model] | apple
classifier = "auto"   # auto | none | lmstudio[:model] | jev[:url] | openai:model@url

[lmstudio]
autostart = true      # start LM Studio's server when it is off

[watch]               # papyrus watch
dirs = ["~/Downloads", "~/Desktop"]
settle = 3            # seconds a new file must stay unchanged
unload_after = 600    # seconds idle before unloading a model the watcher loaded
background = true     # background priority (false: normal, e.g. to benchmark)

[describe]            # what the models read of a document
state_tokens = 1000   # about this many tokens: names, metadata, front and back pages
```

Precedence: command-line option > environment variable (`PAPYRUS_BACKEND`,
`PAPYRUS_CLASSIFIER`, `PAPYRUS_LMSTUDIO_AUTOSTART`) > config file > `auto`.

## Data

* Variants: every library is a **variant** with a home of its own,
  `~/.papyrus/<variant>/`. The everyday one is `release`
  (`~/.papyrus/release/`); `papyrus --variant personal …` (or
  `PAPYRUS_VARIANT=personal`) works on `~/.papyrus/personal/`. Each variant
  has its own apps, port and tailnet path (`apps/macos/build.py --variant
  personal`, or `apps/linux/install.py --variant personal`; see
  [`apps/macos/README.md`](apps/macos/README.md)).
* Database: `~/.papyrus/<variant>/papyrus.sqlite`; override with `PAPYRUS_DB` or `--db`.
* Managed store: `~/.papyrus/<variant>/store/<16 hex digits of the sha256>.pdf` (under the home, one flat directory),
  read-only copies named by content hash; `files.stored_path` points there.
  Copies of local files are APFS clones on macOS and reflinks on Linux
  (btrfs, XFS): no extra space while the original exists. Plain `cp` would
  not clone. Other filesystems get a copy.
* Schema: [`src/papyrus/schema.sql`](src/papyrus/schema.sql), explained in
  [`docs/schema.md`](docs/schema.md).
* Read the databases with `sqlite3`, datasette or the MCP `sql` tool. Don't
  open them in a GUI editor that saves (SQLPro for SQLite, DB Browser): a
  sandboxed app's safe-save swaps a copy in under the running watcher. Give
  such an editor a `.backup` copy instead.
* Inference cache: `~/.papyrus/<variant>/inference.sqlite` (hashes and
  timings of every model call) and `~/.papyrus/<variant>/inference/` (the
  texts asked about and answered, by hash; private, like the store).

## Environment

| variable | purpose |
|---|---|
| `PAPYRUS_VARIANT`, `PAPYRUS_ROOT` | the variant (default `release`; also `--variant`) and where homes live (default `~/.papyrus`): the home is `$PAPYRUS_ROOT/$PAPYRUS_VARIANT` |
| `PAPYRUS_DB`, `PAPYRUS_HOME` | database location; `PAPYRUS_HOME` (default: the variant's home) also holds the store and config, and overrides the variant |
| `PAPYRUS_LMSTUDIO_URL`, `PAPYRUS_LMSTUDIO_MODEL` | extraction via LM Studio |
| `PAPYRUS_BACKEND`, `PAPYRUS_CLASSIFIER` | override `backend` / `classifier` from the config |
| `PAPYRUS_CLASSIFY_MODEL` | classifier model via LM Studio |
| `PAPYRUS_LMSTUDIO_AUTOSTART` | `0` stops papyrus starting LM Studio's server |
| `PAPYRUS_CONFIG` | config file (default `$PAPYRUS_HOME/config.toml`) |
| `PAPYRUS_JEV_URL`, `PAPYRUS_JEV_API_KEY` | Jev-compatible classifier server |
| `PAPYRUS_AFM_BIN` | Apple bridge binary |
| `OPENALEX_API_KEY`, `S2_API_KEY`, `PAPYRUS_MAILTO` | matching APIs |

## Development

```sh
uv run pytest            # live LM Studio / Apple tests skip themselves when unavailable
```
