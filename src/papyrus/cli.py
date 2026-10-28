"""papyrus command-line interface."""

from __future__ import annotations

import json
import signal
import sqlite3
from pathlib import Path

import click

from . import db


def _sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def _conn(ctx: click.Context) -> sqlite3.Connection:
    return ctx.obj["conn"]


@click.group()
@click.option("--db", "db_path", type=click.Path(path_type=Path), envvar="PAPYRUS_DB",
              help="SQLite database (default ~/.papyrus/papyrus.sqlite).")
@click.pass_context
def main(ctx: click.Context, db_path: Path | None) -> None:
    """Local library of papers, books and other documents."""
    # turn `kill`/SIGTERM into KeyboardInterrupt so runs are closed as 'interrupted'
    signal.signal(signal.SIGTERM, _sigterm)
    ctx.ensure_object(dict)
    ctx.obj["conn"] = db.connect(db_path)


@main.command()
@click.argument("roots", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("-r", "--recursive", is_flag=True, help="Descend into subdirectories.")
@click.option("--max-depth", default=4, show_default=True)
@click.option("--no-store", is_flag=True, help="Don't copy new files into the managed store.")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def scan(ctx: click.Context, roots: tuple[Path, ...], recursive: bool, max_depth: int, no_store: bool,
         verbose: bool) -> None:
    """Find PDFs under ROOTS, hash them, read their first pages and record where they were found.

    New files are also copied into the managed store (~/.papyrus/store, named by hash); on APFS
    the copy is a clone, which takes no extra space."""
    from .scan import ScanOptions, scan as do_scan

    roots = roots or (Path("~/Desktop").expanduser(), Path("~/Downloads").expanduser())

    def progress(state: str, path: Path) -> None:
        if verbose:
            click.echo(f"  {state:4} {path}")

    opts = ScanOptions(recursive=recursive, max_depth=max_depth, store=not no_store)
    stats = do_scan(_conn(ctx), list(roots), opts, progress)
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))


@main.command()
@click.argument("urls", nargs=-1)
@click.option("-c", "--clipboard", is_flag=True,
              help="Read links from the clipboard (e.g. tabs copied in Safari, which keep their titles).")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read links from standard input (also: a lone '-').")
@click.option("-f", "--file", "files", multiple=True, type=click.File("r"), metavar="FILE",
              help="Read links from FILE (repeatable).")
@click.option("-n", "--dry-run", is_flag=True, help="Only show the parsed links and what would be fetched.")
@click.option("--refetch", is_flag=True, help="Fetch even URLs that are already in the library.")
@click.option("-j", "--jobs", default=4, show_default=True, help="Hosts fetched in parallel.")
@click.option("--process", "and_process", is_flag=True,
              help="Then triage, promote and enrich the new files (see `papyrus process`).")
@click.pass_context
def download(ctx: click.Context, urls: tuple[str, ...], clipboard: bool, from_stdin: bool,
             files: tuple, dry_run: bool, refetch: bool, jobs: int, and_process: bool) -> None:
    """Download documents from URLs into the library's store.

    Links can be given as arguments, with -c/--clipboard, - or --stdin, and -f/--file; as
    URLs separated by newlines, commas or spaces, markdown links, or arXiv ids. arXiv and
    OpenReview pages map to their PDF, and other HTML landing pages are followed through
    their citation_pdf_url. Each URL is recorded as a 'download' context.

    \b
    papyrus download -c                 # tabs copied from Safari
    papyrus download https://arxiv.org/abs/2312.00429 1903.05848
    pbpaste | papyrus download -
    """
    import sys

    from .download import Link, dedupe, download as do_download, known_file, parse_text, read_clipboard, resolve

    from_stdin = from_stdin or "-" in urls
    links: list[Link] = []
    for u in urls:
        if u == "-":
            continue
        found = parse_text(u)
        if not found:
            click.echo(f"(ignoring {u!r}: not a URL or arXiv id)", err=True)
        links += found
    for fh in files:
        links += parse_text(fh.read())
    if from_stdin:
        links += parse_text(sys.stdin.read())
    if clipboard:
        found, flavour = read_clipboard()
        click.echo(f"clipboard ({flavour}): {len(found)} links", err=True)
        links += found
    if not (urls or files or from_stdin or clipboard):
        raise click.UsageError("give URLs, or use -c/--clipboard, -/--stdin or -f/--file")
    links = dedupe(links)
    if not links:
        raise click.ClickException("no links found")
    conn = _conn(ctx)
    if dry_run:
        for link in links:
            target, _ = resolve(link.url)
            known = None if refetch else known_file(conn, link.url)
            state = f"known [{known[0]}]" if known else "fetch"
            click.echo(f"{state:12} {link.url}" + (f"\n{'':13}-> {target}" if target != link.url else "")
                       + (f"\n{'':13}“{link.title}”" if link.title else ""))
        return

    def progress(o) -> None:
        label = f"[{o.file_id}]" if o.file_id else ""
        line = f"{o.state:6} {label:>6} {o.link.title or o.link.url}"
        if o.state == "failed":
            line += f"\n{'':14}{o.link.url}\n{'':14}{o.detail[:300]}"
        click.echo(line, err=o.state == "failed")

    stats, outcomes = do_download(conn, links, jobs=jobs, refetch=refetch, progress=progress)
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    new_ids = sorted({o.file_id for o in outcomes if o.state == "new" and o.file_id})
    if and_process and new_ids:
        ctx.invoke(process, file_ids=tuple(new_ids))


@main.command("store")
@click.option("--check", is_flag=True, help="Re-hash stored files instead, and report mismatches.")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def store_cmd(ctx: click.Context, check: bool, verbose: bool) -> None:
    """Copy known files that aren't in the managed store yet (clones on APFS)."""
    from . import store

    def progress(state: str, file_id: int, where: str) -> None:
        if verbose or state in ("no_source", "missing", "corrupt"):
            click.echo(f"  {state:12} [{file_id}] {where}")

    stats = (store.check if check else store.backfill)(_conn(ctx), progress)
    click.echo(f"store: {store.root()}\n" + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))


@main.command()
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--backend", default=None,
              help="Extraction model: auto | lmstudio[:model] | apple  [default: from config, else auto]")
@click.option("--decider", "decider_spec", default=None,
              help="Jev-style decider for kind/is-library: auto | none | lmstudio[:model] | jev:<url>  "
                   "[default: from config, else auto]")
@click.option("--all", "redo_all", is_flag=True, help="Re-triage files already done.")
@click.option("-n", "--limit", type=int)
@click.pass_context
def triage(ctx: click.Context, file_ids: tuple[int, ...], backend: str | None, decider_spec: str | None,
           redo_all: bool, limit: int | None) -> None:
    """Ask a local model what each file is (kind, title, authors, abstract, topics...).

    The backend and decider come from --backend/--decider, else PAPYRUS_BACKEND/PAPYRUS_DECIDER,
    else ~/.papyrus/config.toml (see `papyrus config`), else auto. LM Studio's server is started
    if it is off. Triage stops if no decider is available, unless the decider is 'none'."""
    from . import config
    from .llm import get_backend
    from .llm.base import BackendUnavailable
    from .triage import PROMPT_VERSION, triage_file

    conn = _conn(ctx)
    try:
        backend, decider_spec = config.backend(backend), config.decider(decider_spec)
        be = get_backend(backend)
    except (config.ConfigError, BackendUnavailable) as e:
        raise click.ClickException(str(e)) from e
    dec = None
    if decider_spec != "none":
        from .llm.decide import get_decider
        try:
            dec = get_decider(None if decider_spec == "auto" else decider_spec)
            if not dec.available():
                raise BackendUnavailable(f"decider {dec.info().tag} is not available")
        except BackendUnavailable as e:
            # Without a decider, kind and library-vs-personal rest on the extraction model's own
            # answers, which proved unreliable (exam papers and a CV promoted to works).
            raise click.ClickException(
                f"no decider: {e}.\nTriage without one misjudges personal vs library documents. "
                f"Start LM Studio, or pass --decider none (or set decider = \"none\" in "
                f"{config.path()}) to triage anyway.") from e
    if file_ids:
        ids = list(file_ids)
    else:
        where = "text_status IN ('ok','no_text')" + ("" if redo_all else " AND triage_status<>'done'")
        ids = [r[0] for r in conn.execute(f"SELECT id FROM files WHERE {where} ORDER BY id")]
    ids = ids[:limit] if limit else ids
    click.echo(f"triaging {len(ids)} files with {be.info().tag}" + (f" + {dec.info().tag}" if dec else ""))
    with db.recorded_run(conn, "triage", {"files": len(ids)}, backend=be.info().tag,
                         prompt_version=PROMPT_VERSION) as (run_id, stats):
        stats.update(ok=0, error=0)
        for fid in ids:
            try:
                r = triage_file(conn, fid, be, dec, run_id=run_id)
            except Exception as e:  # keep going; record the failure
                stats["error"] += 1
                conn.execute("UPDATE files SET triage_status='error' WHERE id=?", (fid,))
                click.echo(f"{fid:5} ERROR {type(e).__name__}: {str(e)[:200]}", err=True)
                continue
            stats["ok"] += 1
            e = r["extracted"]
            mark = "+" if e.get("is_library_material") else "-"
            kind = r.get("kind", (e.get("kind"),))[0]  # decider's answer when available
            click.echo(f"{fid:5} {mark} {kind or '?':13} {r['extract_s'] + r.get('decide_s', 0):5.1f}s "
                       f"{r.get('abstract_method', '-'):17} {(e.get('title') or '')[:60]}")
    click.echo(" ".join(f"{k}={v}" for k, v in stats.items()))


@main.command()
@click.pass_context
def promote(ctx: click.Context) -> None:
    """Create/link works from triaged files (dedupes by arXiv/DOI/ISBN and title)."""
    from .works import promote_all
    stats = promote_all(_conn(ctx))
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))


@main.command()
@click.argument("work_ids", nargs=-1, type=int)
@click.option("--all", "redo_all", is_flag=True, help="Also re-enrich works that already have API data.")
@click.option("--reset", is_flag=True, help="Undo previous enrichment of the given works first (keeps it as history).")
@click.option("-n", "--limit", type=int)
@click.pass_context
def enrich(ctx: click.Context, work_ids: tuple[int, ...], redo_all: bool, reset: bool, limit: int | None) -> None:
    """Match works against arXiv / OpenAlex / Semantic Scholar / Crossref; fill abstract, venue,
    identifiers, disambiguated authors (ORCID/OpenAlex/S2 ids) and topics."""
    from .enrich.pipeline import enrich_work, reset_enrichment

    conn = _conn(ctx)
    if reset and not work_ids:
        raise click.UsageError("--reset needs explicit WORK_IDS")
    if work_ids:
        ids = list(work_ids)
    elif redo_all:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM works WHERE status IN ('candidate','confirmed') ORDER BY id")]
    else:
        ids = _unenriched_works(conn)
    ids = ids[:limit] if limit else ids
    with db.recorded_run(conn, "enrich", {"works": len(ids), "reset": reset}) as (run_id, stats):
        for wid in ids:
            try:
                if reset:
                    reset_enrichment(conn, wid)
                r = enrich_work(conn, wid, run_id=run_id)
            except Exception as e:
                r = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            stats[r["status"]] = stats.get(r["status"], 0) + 1
            title = conn.execute("SELECT title FROM works WHERE id=?", (wid,)).fetchone()[0] or ""
            detail = (",".join(r.get("sources", [])) + f" score={r.get('score') or r.get('best_score') or 0:.2f}"
                      + (f" authors={r['authors_linked']}" if "authors_linked" in r else "")
                      if r["status"] != "error" else r["error"][:120])
            click.echo(f"{wid:5} {r['status']:9} {title[:60]:60} {detail}")
    click.echo(" ".join(f"{k}={v}" for k, v in stats.items()))


def _unenriched_works(conn: sqlite3.Connection, file_ids: tuple[int, ...] = ()) -> list[int]:
    """Live works with no API claims or candidates yet; only those of `file_ids` if given."""
    q = ("SELECT DISTINCT w.id FROM works w" + (" JOIN file_works fw ON fw.work_id=w.id" if file_ids else "")
         + " WHERE w.status IN ('candidate','confirmed')"
         + (f" AND fw.file_id IN ({','.join('?' * len(file_ids))})" if file_ids else "")
         + " AND NOT EXISTS (SELECT 1 FROM assertions a WHERE a.subject_type='work' AND a.subject_id=w.id"
           " AND (a.source LIKE 'api:%' OR a.field='enrich_candidates')) ORDER BY w.id")
    return [r[0] for r in conn.execute(q, file_ids)]


@main.command()
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--backend", default=None,
              help="Extraction model for triage: auto | lmstudio[:model] | apple  [default: from config, else auto]")
@click.option("--decider", "decider_spec", default=None,
              help="Decider for triage: auto | none | lmstudio[:model] | jev:<url>  [default: from config, else auto]")
@click.option("-n", "--limit", type=int, help="Triage at most N pending files.")
@click.pass_context
def process(ctx: click.Context, file_ids: tuple[int, ...], backend: str | None, decider_spec: str | None,
            limit: int | None) -> None:
    """Triage, promote and enrich whatever is pending: `triage && promote && enrich`.

    With FILE_IDS, triages those files and enriches only their works. Stops before promoting
    if triage can't run (e.g. no decider available)."""
    click.echo("== triage")
    ctx.invoke(triage, file_ids=file_ids, backend=backend, decider_spec=decider_spec, limit=limit)
    click.echo("== promote")
    ctx.invoke(promote)
    pending = _unenriched_works(_conn(ctx), file_ids)
    click.echo(f"== enrich ({len(pending)} works)")
    if pending:
        ctx.invoke(enrich, work_ids=tuple(pending))


@main.command("works")
@click.argument("query", required=False)
@click.option("--kind")
@click.option("-n", "--limit", default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_works(ctx: click.Context, query: str | None, kind: str | None, limit: int, as_json: bool) -> None:
    """List works, optionally full-text matching QUERY over title/authors/abstract/topics."""
    conn = _conn(ctx)
    sql = ("SELECT w.id, w.kind, w.year, w.title, (SELECT group_concat(raw_name, ', ') FROM "
           "(SELECT raw_name FROM work_authors WHERE work_id=w.id ORDER BY position LIMIT 4)) AS authors FROM works w")
    args: list = []
    where = ["w.status NOT IN ('merged', 'rejected')"]
    if query:
        sql += " JOIN works_fts ON works_fts.rowid=w.id"
        where.append("works_fts MATCH ?")
        args.append(query)
    if kind:
        where.append("w.kind=?")
        args.append(kind)
    sql += " WHERE " + " AND ".join(where) + (" ORDER BY bm25(works_fts)" if query else " ORDER BY w.id") + " LIMIT ?"
    rows = [dict(r) for r in conn.execute(sql, (*args, limit))]
    if as_json:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for r in rows:
        click.echo(f"{r['id']:5} {r['kind'] or '?':8} {r['year'] or '':4}  {r['title'][:80]}\n"
                   f"{'':20}{(r['authors'] or '')[:80]}")


@main.command("work")
@click.argument("work_id", type=int)
@click.pass_context
def show_work(ctx: click.Context, work_id: int) -> None:
    """Show a work with ids, authors, topics, files and discovery contexts."""
    from .works import work_json
    click.echo(json.dumps(work_json(_conn(ctx), work_id), indent=2, ensure_ascii=False))


@main.command("files")
@click.option("--status", help="Filter by triage status (pending/done/error).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_files(ctx: click.Context, status: str | None, as_json: bool) -> None:
    """List known files with their current locations."""
    conn = _conn(ctx)
    q = """SELECT f.id, f.page_count, f.text_status, f.triage_status, f.size, f.stored_path,
                  (SELECT group_concat(c.locator, char(10)) FROM context_refs r JOIN contexts c ON c.id=r.context_id
                    WHERE r.file_id=f.id AND c.kind='filesystem' AND c.missing_at IS NULL) AS paths,
                  (SELECT group_concat(c.locator, char(10)) FROM context_refs r JOIN contexts c ON c.id=r.context_id
                    WHERE r.file_id=f.id AND c.kind='download') AS urls
           FROM files f""" + (" WHERE f.triage_status=?" if status else "") + " ORDER BY f.id"
    rows = [dict(r) for r in conn.execute(q, (status,) if status else ())]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        first = (r["paths"] or r["urls"] or r["stored_path"] or "(no current path)").split("\n")[0]
        click.echo(f"{r['id']:5} {r['page_count'] or '?':>5}p {r['text_status']:8} {r['triage_status']:8} {first}")


@main.command()
@click.argument("query")
@click.option("-n", "--limit", default=20, show_default=True)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Full-text search (SQLite FTS5 syntax) over extracted page text and work metadata."""
    conn = _conn(ctx)
    rows = conn.execute(
        """SELECT p.file_id, p.page, snippet(file_pages_fts, 0, '[', ']', ' … ', 12) AS snip,
                  bm25(file_pages_fts) AS score,
                  (SELECT c.title FROM context_refs r JOIN contexts c ON c.id=r.context_id
                    WHERE r.file_id=p.file_id ORDER BY c.missing_at IS NOT NULL LIMIT 1) AS name
           FROM file_pages_fts JOIN file_pages p ON p.id = file_pages_fts.rowid
           WHERE file_pages_fts MATCH ? ORDER BY score LIMIT ?""",
        (query, limit),
    ).fetchall()
    for r in rows:
        click.echo(f"[{r['file_id']}] p{r['page']} {r['name']}\n    {r['snip'].replace(chr(10), ' ')}")


@main.command()
@click.argument("file_id", type=int)
@click.option("--text", is_flag=True, help="Also print extracted page text.")
@click.pass_context
def show(ctx: click.Context, file_id: int, text: bool) -> None:
    """Show everything known about a file: metadata, contexts, assertions, linked works."""
    conn = _conn(ctx)
    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not f:
        raise click.ClickException(f"no file {file_id}")
    out = {"file": {k: f[k] for k in f.keys() if f[k] is not None}}
    out["contexts"] = [
        {k: (json.loads(r[k]) if k == "extra" and r[k] else r[k]) for k in r.keys() if r[k] is not None}
        for r in conn.execute(
            "SELECT c.kind, c.locator, c.title, c.occurred_at, c.url, c.missing_at, c.extra FROM context_refs r"
            " JOIN contexts c ON c.id=r.context_id WHERE r.file_id=? ORDER BY c.occurred_at", (file_id,))
    ]
    out["assertions"] = db.current_assertions(conn, "file", file_id)
    out["works"] = [dict(r) for r in conn.execute(
        "SELECT w.id, w.kind, w.title, w.year, fw.relation, fw.confidence FROM file_works fw"
        " JOIN works w ON w.id=fw.work_id WHERE fw.file_id=?", (file_id,))]
    click.echo(json.dumps(out, indent=2, ensure_ascii=False))
    if text:
        for r in conn.execute("SELECT page, text FROM file_pages WHERE file_id=? ORDER BY page", (file_id,)):
            click.echo(f"\n----- page {r['page']} -----\n{r['text']}")


@main.command("config")
@click.option("--init", is_flag=True, help="Write a commented config file if there is none.")
def config_cmd(init: bool) -> None:
    """Show where the config file is and the settings in effect (and where each comes from)."""
    from . import config
    from .llm.lmstudio import default_url, lms_path, server_up

    if init:
        p, written = config.init()
        click.echo(f"{'wrote' if written else 'already exists:'} {p}")
    p = config.path()
    click.echo(f"config file: {p}{'' if p.exists() else '  (not present; using defaults)'}")
    try:
        for name in ("backend", "decider"):
            value, source = config.setting(name)
            click.echo(f"  {name:18} {value:28} ({source})")
        auto, source = config.lmstudio_autostart()
        click.echo(f"  {'lmstudio.autostart':18} {str(auto).lower():28} ({source})")
    except config.ConfigError as e:
        raise click.ClickException(str(e)) from e
    url = default_url()
    click.echo(f"LM Studio: {url} {'up' if server_up(url) else 'not running'}; "
               f"lms CLI: {lms_path() or 'not found'}")


@main.command("mcp")
def mcp_cmd() -> None:
    """Run the MCP server on stdio."""
    from .mcp_server import run
    run()


@main.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Counts of things in the library."""
    conn = _conn(ctx)
    for table in ["files", "works", "people", "contexts", "context_refs", "assertions", "runs"]:
        click.echo(f"{table:14} {conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}")
    for r in conn.execute("SELECT text_status, triage_status, count(*) n FROM files GROUP BY 1, 2"):
        click.echo(f"  files text={r[0]} triage={r[1]}: {r[2]}")


if __name__ == "__main__":
    main()
