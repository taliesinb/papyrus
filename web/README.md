# papyrus web

A read-only browser for the papyrus library, built with SvelteKit 3 and Svelte 5.

```sh
cd web
pnpm install
pnpm dev                 # http://localhost:5181/papyrus/
# or
pnpm build && pnpm start # node adapter, http://127.0.0.1:5181/papyrus/
```

The app lives under `/papyrus` (`paths.base` in `vite.config.ts`). That is
the path `Papyrus Server.app` publishes it at on the tailnet, so one build
serves loopback and `https://<mac>.<tailnet>.ts.net/papyrus/` alike. Every
link, `goto` and `fetch` goes through `href()` from `#lib/paths.ts`. Set
`PAPYRUS_WEB_BASE=` (empty) at dev or build time to serve the app at the
root instead.

The macOS apps (a window, plus a menu-bar server that also runs
`tailscale serve`) are in [`apps/macos`](../apps/macos/README.md).

It reads `~/.papyrus/papyrus.sqlite`. Override this with `PAPYRUS_DB`, or with
`PAPYRUS_HOME`, as the CLI does.

## Read-only by construction

* The database is opened with Node's built-in `node:sqlite`, using
  `readOnly: true` and `PRAGMA query_only`. The Python pipeline (scan, triage,
  enrich) can keep writing to the same file. In WAL mode readers never block
  the writer, and they always see its latest commit.
* PDFs are served only from paths recorded in the database: `files.stored_path`
  or a live `filesystem` context. The file size must also match.
* The only files written are page thumbnails, rendered with poppler's
  `pdftoppm`. They are cached in `~/.cache/papyrus-web/thumbs`, which you can
  override with `PAPYRUS_WEB_CACHE`. Set `PDFTOPPM` if the binary is not in
  `/opt/homebrew/bin`.
* The UI polls `/api/version`, which reports `PRAGMA data_version`. When the
  pipeline commits something, a "library updated" button appears, and a
  running `triage` or `enrich` run is shown in the header.

## Features

| | |
|---|---|
| **Facets** | Kind, arXiv category, OpenAlex subfield, model-assigned field, author (disambiguated `people` where known), folder, download site and status. Facets combine with AND; values within a facet combine with OR. Each facet's counts are computed against all the other active filters. There is also a year histogram that you can click. |
| **Search** | Uses FTS5 over `works_fts` (title, authors, abstract, topics) and `file_pages_fts` (page text), with highlighted snippets. Page hits link straight to that page in the viewer. Plain words are matched as all-terms with prefix matching on the last word. Queries that contain FTS5 syntax (`"…"`, `NEAR`, `OR`, `*`) are run verbatim. |
| **Similar / more like these** | Inspired by arxiv-sanity: TF‑IDF (unigrams and bigrams, with arXiv categories as features) over title, abstract, summary and topics. `similar` ranks the library by cosine similarity to one work. `+ like` builds a set of works and ranks by the centroid of the set, a read-only stand-in for arxiv-sanity's per-tag SVM. The detail page shows the shared terms and the work's most distinctive terms. |
| **Views** | List, Pages (strips of the first 8 pages, as in the original arxiv-sanity) and Compact. Sorts: best (relevance or similarity, depending on context), recently added (file birth time), year, most cited, title and random (seeded, with reshuffle). |
| **Reading** | The browser's own PDF viewer runs inline, with Range requests, a page-thumbnail strip, a dark-pages toggle (inverted colours) and a wider-PDF mode. "Reveal in Finder" is also available. |
| **Provenance** | Shows where each file was found (path, birth time, browser, download URL and referrer from `kMDItemWhereFroms`), the files with their relations and versions, and every assertion grouped by field. Superseded claims can be shown on request. |
| **Files** | Lists every scanned PDF, including those triage rejected, along with the decider's `kind` and whether each is library material. |
| **Keyboard** | `/` search · `j`/`k` move · `o` open · `s` similar · `x` add to "more like these" · `v` cycle view |

## Layout

```
src/lib/server/db.ts       read-only connection, data_version
src/lib/server/library.ts  in-memory index of works, rebuilt when the DB changes
src/lib/server/query.ts    URL filters → facets, FTS search, sorting
src/lib/server/similar.ts  TF-IDF vectors, centroid similarity, shared/top terms
src/lib/server/detail.ts   work detail, contexts, assertions, pipeline status
src/lib/server/files.ts    path resolution, pdftoppm thumbnails (3 at a time)
src/lib/paths.ts           href(): app paths under the base path
src/routes/                /  /work/[id]  /files  /api/files/[id]/{pdf,thumb/[page],reveal}  /api/version
static/                    favicon.svg (boxicons "scroll", MIT), apple-touch-icon.png (apps/macos/build.py --touch-icon)
```

## Ideas for later

* Writes such as confirm/reject, tags and notes, once papyrus has a `user`
  assertion path. Tags would allow a real arxiv-sanity-style SVM per tag.
* Embedding similarity once `sqlite-vec` lands, alongside TF‑IDF.
* An author page that groups works through `people` ids, and a co-author
  graph.
* pdf.js instead of the native viewer, for highlighting search hits on the
  page.
