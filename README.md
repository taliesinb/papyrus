# papyrus

A local-first library for papers, books, slides and notes. It has:

* an open, documented SQLite database
* provenance for everything: where and how each document was found, and
  which model or API said what about it
* local-model triage
* full-text search
* an MCP server

## Status

Early. What works today:

| stage | command | what it does |
|---|---|---|
| scan | `papyrus scan [-r] DIR...` | Finds PDFs, hashes and dedupes them, and reads the PDF info and the text of the first 2 pages into FTS5. It records a `filesystem` context for each path, including the download URL and referrer from macOS `kMDItemWhereFroms`. New files are copied into the managed store (an APFS clone, so free). Re-scans are idempotent, and paths that have gone are marked missing. |
| download | `papyrus download URL...`, `-c`/`--clipboard`, `-`/`--stdin`, `-f`/`--file FILE` | Fetches PDFs into the store and ingests them like `scan`. Takes tabs copied from Safari (titles included), or URLs separated by newlines, commas or spaces, markdown links and arXiv ids. arXiv/OpenReview pages map to their PDF; other landing pages are followed via `citation_pdf_url`. Each URL is a `download` context; URLs already in the library are skipped. `--dry-run` shows what would happen; `--process` then runs triage, promote, extract and enrich on the new files. |
| store | `papyrus store [--check]` | Copies files scanned before the store existed into it; `--check` re-hashes the stored copies. |
| triage | `papyrus triage [--backend B] [--decider D]` | Asks a local model what each file is. A **decider** (Jev/System-One style typed questions answered with probabilities) settles `kind` and `is_library_material`. An **extraction** model returns title, authors, year, venue, verbatim abstract, summary, topics with fields, and language. Identifiers (arXiv/DOI/ISBN) are found by regex in the filename, download URL and page 1. |
| promote | `papyrus promote` | Creates or links `works` from triaged files, deduplicating by identifier, then by normalised title. |
| extract | `papyrus extract [FILE_IDS] [--all] [--no-ocr]` | Reads every page of the files of live works into the full-text index (personal paperwork stays at the 2 pages triage needs). Labels references, index and contents pages, which rank lower; keeps the PDF outline, so hits show their section; OCRs pages without a text layer with Apple Vision (through `tools/afm-bridge`). |
| process | `papyrus process [FILE_IDS] [--backend B] [--decider D]` | Shorthand for `triage && promote && extract && enrich` over whatever is pending (with FILE_IDS: those files and their works). Stops before promoting if triage can't run. |
| enrich | `papyrus enrich [IDS] [--all] [--reset]` | Matches works against arXiv, OpenAlex, Semantic Scholar and Crossref. Adds the abstract, venue, the published DOI next to the arXiv id, authors with ORCID/OpenAlex/S2 ids, and topics. Candidates that are provably other works are excluded. |
| search | `papyrus search QUERY [--kind K] [--files] [--json]` | Works matching in their metadata or anywhere in their pages, ranked by Reciprocal Rank Fusion of the two, each with its best pages (page, section, snippet). Free text or FTS5 syntax. `--files` ranks files instead, personal ones included. |
| browse | `papyrus works [QUERY]`, `papyrus work ID`, `papyrus files`, `papyrus show ID`, `papyrus stats` | |
| web | `cd web && pnpm dev` | Read-only SvelteKit browser at `/papyrus/`: facets, full-text search, TF‑IDF "similar", inline PDF viewer, provenance. See [`web/README.md`](web/README.md). |
| apps | `python3 apps/macos/build.py --install` | `Papyrus.app`, a window on the web browser, and `Papyrus Server.app`, a menu-bar app that runs the server and publishes it with `tailscale serve` at `https://<mac>.<tailnet>.ts.net/papyrus/`. See [`apps/macos/README.md`](apps/macos/README.md). |
| MCP | `papyrus mcp` | stdio MCP server with these tools: `search_library`, `fulltext_search`, `get_work`, `get_file`, `recent_discoveries`, `sql` (read-only), `library_stats`. Resource: `papyrus://schema`. |

Next on the list:

* Open Library for books
* Slack, Safari Reading List and Twitter contexts (the shared resolver can
  reuse `download.fetch` for PDF links)
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
| decisions | LM Studio emulation of the Jev `/v1/systemone` API, using logprobs over option letters | `--decider lmstudio:qwen3.5-4b-mlx` |
| decisions | any Jev-compatible server (TypeSafe, Ollama `tev1`, decider, Lichen…) | `--decider jev:http://host:port` |

`--backend auto` picks LM Studio, starting its server (`lms server start`) if it
is off, otherwise Apple FM. Triage stops if no decider is available rather than
relying on the extraction model's own kind and "library material" answers;
pass `--decider none` to triage anyway. Build the Apple bridge once with:

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

`~/.papyrus/config.toml` (under `PAPYRUS_HOME`; `PAPYRUS_CONFIG` points
elsewhere). Every key is optional. `papyrus config` shows the file's path and
the settings in effect with where each comes from; `papyrus config --init`
writes a commented template.

```toml
backend = "auto"      # auto | lmstudio[:model] | apple
decider = "auto"      # auto | none | lmstudio[:model] | jev[:url] | openai:model@url

[lmstudio]
autostart = true      # start LM Studio's server when it is off
```

Precedence: command-line option > environment variable (`PAPYRUS_BACKEND`,
`PAPYRUS_DECIDER`, `PAPYRUS_LMSTUDIO_AUTOSTART`) > config file > `auto`.

## Data

* Database: `~/.papyrus/papyrus.sqlite`; override with `PAPYRUS_DB` or `--db`.
* Managed store: `~/.papyrus/store/ab/<sha256>.pdf` (under `PAPYRUS_HOME`),
  read-only copies named by content hash; `files.stored_path` points there.
  Copies of local files are APFS clones (no extra space while the original
  exists; plain `cp` would not clone).
* Schema: [`src/papyrus/schema.sql`](src/papyrus/schema.sql), explained in
  [`docs/schema.md`](docs/schema.md).

## Environment

| variable | purpose |
|---|---|
| `PAPYRUS_DB`, `PAPYRUS_HOME` | database location; `PAPYRUS_HOME` (default `~/.papyrus`) also holds the store |
| `PAPYRUS_LMSTUDIO_URL`, `PAPYRUS_LMSTUDIO_MODEL` | extraction via LM Studio |
| `PAPYRUS_BACKEND`, `PAPYRUS_DECIDER` | override `backend` / `decider` from the config |
| `PAPYRUS_DECIDE_MODEL` | decider model via LM Studio |
| `PAPYRUS_LMSTUDIO_AUTOSTART` | `0` stops papyrus starting LM Studio's server |
| `PAPYRUS_CONFIG` | config file (default `$PAPYRUS_HOME/config.toml`) |
| `PAPYRUS_JEV_URL`, `PAPYRUS_JEV_API_KEY` | Jev-compatible decider server |
| `PAPYRUS_AFM_BIN` | Apple bridge binary |
| `OPENALEX_API_KEY`, `S2_API_KEY`, `PAPYRUS_MAILTO` | enrichment APIs |

## Development

```sh
uv run pytest            # live LM Studio / Apple tests skip themselves when unavailable
```
