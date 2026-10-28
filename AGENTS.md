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
uv run papyrus --help                  # scan | download | store | triage | promote | enrich | works | work | files | show | search | stats | mcp
cd tools/afm-bridge && swift build -c release                # Apple Foundation Models bridge (CLT is enough)
```

The usual pipeline is `scan` (or `download`) `→ triage → promote → enrich`.
Every stage is idempotent and processes only what is pending (see `--all` /
`--reset`).

## Data safety (read before running anything)

* **`~/.papyrus/papyrus.sqlite` is the user's real library.**
  * Experiment against a scratch copy:
    `mkdir /tmp/ph && sqlite3 ~/.papyrus/papyrus.sqlite ".backup /tmp/ph/papyrus.sqlite"`,
    then set `PAPYRUS_HOME=/tmp/ph` (this moves the managed store too; with
    only `PAPYRUS_DB` set, `scan`/`download` would still write into
    `~/.papyrus/store`). Tests do this automatically (`tests/conftest.py`).
  * Before any bulk change to the real database, take a dated `.backup` next
    to it (`~/.papyrus/papyrus.backup-YYYY-MM-DD-<why>.sqlite`).
* **Never modify, move or delete the user's files on disk.** papyrus only
  reads them. Report odd files instead. For example, a PDF whose filename
  suggests a textbook may turn out to be an unrelated document.
* **The managed store** (`~/.papyrus/store/ab/<sha256>.pdf`) is papyrus's
  own: content-addressed, read-only, written only through `store.commit`.
  Never rewrite or delete a stored file; `files.stored_path` points there.
  Copy local files in with `store.snapshot` (an APFS clone, falling back to a
  copy), never `shutil.copy`/`cp`, which duplicate the bytes.
* **Never delete rows that carry knowledge:**
  * works are rejected (`status='rejected'`, ids released by
    `works.reject_work`) or merged, never deleted;
  * assertions are superseded (`superseded_at`), never deleted;
  * undo an enrichment with `pipeline.reset_enrichment`, which keeps the
    history.
* **Downloaded personal documents live alongside papers in `~/Downloads`:**
  payslips, tax certificates, CVs, exam papers. Don't paste their text into
  commits, tests, fixtures or logs. Triage deliberately stores no abstract or
  topics for non-library documents.

## Invariants

1. **Provenance on every claim.** Model and API output goes into `assertions`
   with a `source` tag:
   * `llm:<backend>:<model>`, `decide:<backend>:<model>`
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
   * bump `db.SCHEMA_VERSION` and add a migration step in `db.init`;
   * update `docs/schema.md` in the same commit.

   The schema is a public contract: users and agents query it with SQL over
   MCP.
7. **Changing a prompt** in `triage.py` (instructions, schema, `KINDS`,
   `DECISIONS`) means bumping `PROMPT_VERSION` and noting what changed in the
   comment above it. It is recorded on every run.
8. **The `Backend` / `Decider` contracts** in `llm/base.py` and the
   `WorkRecord` / `AuthorRecord` types in `models.py` are shared by several
   modules. Change them deliberately, and update every implementation.

## Gotchas (all learned the hard way)

**LM Studio** (0.4.x, `localhost:1234`, shared with the user and other agents):

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
  you loaded (`~/.lmstudio/bin/lms ps / unload`).
* **Speed on this machine:**

  | model | prompt processing | generation | per file |
  |---|---|---|---|
  | qwen3.5-9b | ~60–100 tok/s | ~11 tok/s | 60–100 s |
  | qwen3.5-4b | — | — | 12–30 s, including the decider |

  The 4B is the default for both extraction and decisions. Prompt caching is
  automatic (prefix-based), with no markers to set.

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

**PDF text** (pypdfium2):

* PDFium marks soft hyphens with `\x02`, which splits words in full-text
  search. `pdf.clean_text` strips control characters and zero-width
  characters.
* About 5 of 64 PDFs have no text layer (`text_status='no_text'`); there is
  no OCR yet.

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
