"""papyrus command-line interface."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import click

from . import db


def _conn(ctx: click.Context) -> sqlite3.Connection:
    return ctx.obj["conn"]


@click.group()
@click.option("--db", "db_path", type=click.Path(path_type=Path), envvar="PAPYRUS_DB",
              help="SQLite database (default ~/.papyrus/papyrus.sqlite).")
@click.pass_context
def main(ctx: click.Context, db_path: Path | None) -> None:
    """Local library of papers, books and other documents."""
    ctx.ensure_object(dict)
    ctx.obj["conn"] = db.connect(db_path)


@main.command()
@click.argument("roots", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("-r", "--recursive", is_flag=True, help="Descend into subdirectories.")
@click.option("--max-depth", default=4, show_default=True)
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def scan(ctx: click.Context, roots: tuple[Path, ...], recursive: bool, max_depth: int, verbose: bool) -> None:
    """Find PDFs under ROOTS, hash them, read their first pages and record where they were found."""
    from .scan import ScanOptions, scan as do_scan

    roots = roots or (Path("~/Desktop").expanduser(), Path("~/Downloads").expanduser())

    def progress(state: str, path: Path) -> None:
        if verbose:
            click.echo(f"  {state:4} {path}")

    stats = do_scan(_conn(ctx), list(roots), ScanOptions(recursive=recursive, max_depth=max_depth), progress)
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))


@main.command()
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--backend", default="auto", show_default=True,
              help="Extraction model: auto | lmstudio[:model] | apple")
@click.option("--decider", "decider_spec", default="none", show_default=True,
              help="Jev-style decider for kind/is-library: none | auto | lmstudio[:model] | jev:<url>")
@click.option("--all", "redo_all", is_flag=True, help="Re-triage files already done.")
@click.option("-n", "--limit", type=int)
@click.pass_context
def triage(ctx: click.Context, file_ids: tuple[int, ...], backend: str, decider_spec: str,
           redo_all: bool, limit: int | None) -> None:
    """Ask a local model what each file is (kind, title, authors, abstract, topics...)."""
    from .llm import get_backend
    from .triage import PROMPT_VERSION, triage_file

    conn = _conn(ctx)
    be = get_backend(backend)
    dec = None
    if decider_spec != "none":
        from .llm.decide import get_decider
        dec = get_decider(None if decider_spec == "auto" else decider_spec)
    if file_ids:
        ids = list(file_ids)
    else:
        where = "text_status IN ('ok','no_text')" + ("" if redo_all else " AND triage_status<>'done'")
        ids = [r[0] for r in conn.execute(f"SELECT id FROM files WHERE {where} ORDER BY id")]
    ids = ids[:limit] if limit else ids
    run_id = db.start_run(conn, "triage", {"files": len(ids)}, backend=be.info().tag, prompt_version=PROMPT_VERSION)
    stats = {"ok": 0, "error": 0}
    click.echo(f"triaging {len(ids)} files with {be.info().tag}" + (f" + {dec.info().tag}" if dec else ""))
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
        click.echo(f"{fid:5} {mark} {e.get('kind', '?'):13} {r['extract_s']:5.1f}s  {e.get('title', '')[:70]}")
    db.finish_run(conn, run_id, stats)
    click.echo(" ".join(f"{k}={v}" for k, v in stats.items()))


@main.command()
@click.pass_context
def promote(ctx: click.Context) -> None:
    """Create/link works from triaged files (dedupes by arXiv/DOI/ISBN and title)."""
    from .works import promote_all
    stats = promote_all(_conn(ctx))
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))


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
    where = ["w.status<>'merged'"]
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
    q = """SELECT f.id, f.page_count, f.text_status, f.triage_status, f.size,
                  (SELECT group_concat(c.locator, char(10)) FROM context_refs r JOIN contexts c ON c.id=r.context_id
                    WHERE r.file_id=f.id AND c.kind='filesystem' AND c.missing_at IS NULL) AS paths
           FROM files f""" + (" WHERE f.triage_status=?" if status else "") + " ORDER BY f.id"
    rows = [dict(r) for r in conn.execute(q, (status,) if status else ())]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        first = (r["paths"] or "(no current path)").split("\n")[0]
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
            "SELECT c.kind, c.locator, c.occurred_at, c.url, c.missing_at, c.extra FROM context_refs r"
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
