"""papyrus command-line interface."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from pathlib import Path

import click

from . import db


def _sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def _conn(ctx: click.Context) -> sqlite3.Connection:
    return ctx.obj["conn"]


def _filename(conn: sqlite3.Connection, file_id: int) -> str:
    """A file's name where no model gave it a title (personal paperwork gets no metadata)."""
    r = conn.execute("SELECT json_extract(c.extra, '$.filename'), c.locator FROM context_refs r JOIN contexts c"
                     " ON c.id=r.context_id WHERE r.file_id=? AND c.kind IN ('filesystem', 'download')"
                     " ORDER BY c.kind='filesystem' DESC LIMIT 1", (file_id,)).fetchone()
    return (r[0] or Path(r[1] or "").name) if r else ""


def _check_home(db_path: Path | None) -> None:
    """Refuse to create a library by accident: in the old single-library layout, or for a variant
    that was never made (a typo would otherwise start an empty library)."""
    if db_path or os.environ.get("PAPYRUS_HOME"):
        return
    try:
        v = db.variant()
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    root, home = db.papyrus_root(), db.library_home()
    if home.is_dir():
        return
    if (root / "papyrus.sqlite").exists():
        raise click.ClickException(
            f"{root} holds a library in the old layout; move its contents into {root / db.DEFAULT_VARIANT}/ "
            "(every variant now has a home of its own, see apps/macos/README.md)")
    if v != db.DEFAULT_VARIANT:
        have = sorted(p.name for p in root.iterdir() if p.is_dir() and db.VARIANT_ID.match(p.name)) if root.is_dir() else []
        raise click.ClickException(
            f"no variant {v!r}: {home} does not exist{f' (have: {', '.join(have)})' if have else ''}. "
            f"Create it with `mkdir {home}` or `python3 apps/macos/build.py --variant {v}`.")


@click.group()
@click.option("--variant", envvar="PAPYRUS_VARIANT", metavar="ID",
              help="Library variant: its home is ~/.papyrus/<ID> (default release).")
@click.option("--db", "db_path", type=click.Path(path_type=Path), envvar="PAPYRUS_DB",
              help="SQLite database (default ~/.papyrus/<variant>/papyrus.sqlite).")
@click.pass_context
def main(ctx: click.Context, variant: str | None, db_path: Path | None) -> None:
    """Local library of papers, books and other documents."""
    # turn `kill`/SIGTERM into KeyboardInterrupt so runs are closed as 'interrupted'
    signal.signal(signal.SIGTERM, _sigterm)
    if variant:
        os.environ["PAPYRUS_VARIANT"] = variant  # library_home() and the config read it
    _check_home(db_path)
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

    New files are also copied into the managed store (<home>/store, named by hash); on APFS
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
              help="Read links from the clipboard (e.g. tabs copied in Safari, which keep their titles; "
                   "on Linux through wl-paste or xclip).")
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
@click.option("--classifier", "--decider", "classifier_spec", default=None,
              help="Jev-style classifier for kind/is-library: auto | none | lmstudio[:model] | jev:<url>  "
                   "[default: from config, else auto]")
@click.option("--all", "redo_all", is_flag=True, help="Re-triage files already done.")
@click.option("--fresh", is_flag=True, help="Ask the models even where the inference cache has the answer.")
@click.option("--no-screen", is_flag=True, help="Skip the fast first stage: ask the language models about every file.")
@click.option("-n", "--limit", type=int)
@click.pass_context
def triage(ctx: click.Context, file_ids: tuple[int, ...], backend: str | None, classifier_spec: str | None,
           redo_all: bool, fresh: bool, no_screen: bool, limit: int | None) -> None:
    """Ask local models what each file is: first the classifier (kind, library or personal) for
    every pending file, then the extraction model (title, authors, abstract, summary, topics...)
    for those it classified as library material. Personal paperwork gets no metadata.

    The backend and classifier come from --backend/--classifier, else PAPYRUS_BACKEND/PAPYRUS_CLASSIFIER,
    else <home>/config.toml (see `papyrus config`), else auto. LM Studio's server is started
    if it is off. Triage stops if no classifier is available, unless the classifier is 'none'.
    Answers go through the inference cache (<home>/inference.sqlite): a question already asked
    about the same text is answered from it (marked 'cached').

    First, the screen (a small embedding model, ~30 ms a file) sets aside files it is sure are
    personal paperwork: they are held for review (`papyrus purge --dry-run`) without asking the
    language models. Turn it off with --no-screen, `[screen] enabled = false` or PAPYRUS_SCREEN=0."""
    from . import config, inference
    from .llm import get_backend
    from .llm.base import BackendUnavailable
    from .triage import PROMPT_VERSION, classify_file, extract_metadata
    from .watch.system import memory_pressure

    conn = _conn(ctx)
    try:
        backend, classifier_spec = config.backend(backend), config.classifier(classifier_spec)
        be = get_backend(backend)
    except (config.ConfigError, BackendUnavailable) as e:
        raise click.ClickException(str(e)) from e
    dec = None
    if classifier_spec != "none":
        from .llm.classify import get_classifier
        try:
            dec = get_classifier(None if classifier_spec == "auto" else classifier_spec)
            if not dec.available():
                raise BackendUnavailable(f"classifier {dec.info().tag} is not available")
        except BackendUnavailable as e:
            # Without a classifier, kind and library-vs-personal rest on the extraction model's own
            # answers, which proved unreliable (exam papers and a CV promoted to works).
            raise click.ClickException(
                f"no classifier: {e}.\nTriage without one misjudges personal vs library documents. "
                f"Start LM Studio, or pass --classifier none (or set classifier = \"none\" in "
                f"{config.path()}) to triage anyway.") from e
    if file_ids:  # purged files have nothing left to triage
        purged = {r[0] for r in conn.execute(
            f"SELECT id FROM files WHERE purged_at IS NOT NULL AND id IN ({','.join('?' * len(file_ids))})", file_ids)}
        if purged:
            click.echo(f"skipping purged files: {' '.join(map(str, sorted(purged)))}", err=True)
        ids = [i for i in file_ids if i not in purged]
    else:
        where = ("text_status IN ('ok','no_text') AND purged_at IS NULL"
                 + ("" if redo_all else " AND triage_status<>'done'"))
        ids = [r[0] for r in conn.execute(f"SELECT id FROM files WHERE {where} ORDER BY id")]
    # Already classified (an earlier run stopped between the phases): straight to extraction.
    classified = {r[0] for r in conn.execute("SELECT id FROM files WHERE triage_status='classified'")} if not redo_all else set()
    ids = ids[:limit] if limit else ids
    be = inference.cached_backend(be, fresh=fresh, pressure=memory_pressure)
    dec = inference.cached_classifier(dec, fresh=fresh, pressure=memory_pressure) if dec else None
    scr = None
    if not no_screen and ids:
        from .screen import get_screen

        try:
            scr = get_screen()
        except (config.ConfigError, OSError, ValueError) as e:
            raise click.ClickException(f"screen: {e}") from e
        if scr is None and config.screen().enabled:
            click.echo("(screen unavailable: LM Studio doesn't offer its embedding model; asking the language "
                       "models about every file)", err=True)
        if scr is not None:
            scr.embedder = inference.cached_embedder(scr.embedder, fresh=fresh, pressure=memory_pressure)
    click.echo(f"triaging {len(ids)} files with " + (f"{scr.tag} (P >= {scr.threshold:g}), then " if scr else "")
               + be.info().tag + (f" + {dec.info().tag}" if dec else ""))
    with db.recorded_run(conn, "triage", {"files": len(ids)}, backend=be.info().tag,
                         prompt_version=PROMPT_VERSION) as (run_id, stats):
        stats.update(ok=0, error=0)
        # Phase 1: classify every file (one model), phase 2: extract metadata for those classified
        # as library material (possibly another model): each model is used once, not per file.
        to_extract = [fid for fid in ids if fid in classified]
        for fid in [i for i in ids if i not in classified]:
            try:
                r = classify_file(conn, fid, dec, run_id=run_id, screen=scr)
            except Exception as e:  # keep going; record the failure
                stats["error"] += 1
                conn.execute("UPDATE files SET triage_status='error' WHERE id=?", (fid,))
                click.echo(f"{fid:5} ERROR {type(e).__name__}: {str(e)[:200]}", err=True)
                continue
            stats["ok"] += 1
            if r.get("screened"):
                stats["screened"] = stats.get("screened", 0) + 1
                s = r["screen"]
                click.echo(f"{fid:5} - {'personal':13} {s['screen_s']:5.2f}s screen P(personal)={s['p_personal']:.2f}")
                continue
            kind = r.get("kind", ("?",))[0]
            took = f"{r.get('classify_s', 0):5.1f}s" if "classify_s" in r else "     -"
            if r["status"] == "classified":
                to_extract.append(fid)
                click.echo(f"{fid:5} + {kind:13} {took:>6} classified")
            else:
                stats["not_library"] = stats.get("not_library", 0) + 1
                click.echo(f"{fid:5} - {kind:13} {took:>6} P(personal)={r['p_personal']:.2f}: no metadata")
        if to_extract:
            click.echo(f"extracting metadata of {len(to_extract)} files with {be.info().tag}")
        for fid in to_extract:
            try:
                r = extract_metadata(conn, fid, be, run_id=run_id)
            except Exception as e:  # stays 'classified': the next run extracts it
                stats["error"] += 1
                click.echo(f"{fid:5} ERROR {type(e).__name__}: {str(e)[:200]}", err=True)
                continue
            e = r["extracted"]
            took = "cached" if r.get("cached") else f"{r['extract_s']:5.1f}s"
            click.echo(f"{fid:5} + {'metadata':13} {took:>6} {r.get('abstract_method', '-'):17} {(e.get('title') or '')[:60]}")
    click.echo(" ".join(f"{k}={v}" for k, v in stats.items()))


@main.command()
@click.pass_context
def promote(ctx: click.Context) -> None:
    """Create/link works from triaged files (dedupes by arXiv/DOI/ISBN and title); purge files
    that are surely personal paperwork (P(kind=personal) >= 0.9), hold the less sure ones."""
    from .works import promote_all
    stats = promote_all(_conn(ctx))
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())))


@main.command("purge")
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--dry-run", is_flag=True, help="Only list what would be purged, and the files held for review.")
@click.pass_context
def purge_cmd(ctx: click.Context, file_ids: tuple[int, ...], dry_run: bool) -> None:
    """Erase personal paperwork from the library, down to a tombstone (sha256, size, URL).

    Without FILE_IDS: every triaged file the classifier is sure is personal (P(kind=personal) >= 0.9;
    promote already does this as it goes), and a list of the files held for review (personal,
    but less sure). With FILE_IDS: those files, as your decision. Page text, paths, filenames,
    PDF metadata, model claims and the stored copy are erased; the same bytes are skipped from
    then on. `papyrus keep PATH` brings a purged file back from its original."""
    from . import purge
    from .screen import rejected as screen_rejected

    conn = _conn(ctx)
    if file_ids:
        todo: list[tuple[int, dict | None]] = []
        for fid in file_ids:
            r = conn.execute("SELECT purged_at FROM files WHERE id=?", (fid,)).fetchone()
            if r is None or r["purged_at"]:
                click.echo(f"{fid:5} {'no such file' if r is None else 'already purged'}", err=True)
            elif conn.execute("SELECT 1 FROM file_works fw JOIN works w ON w.id=fw.work_id"
                              " WHERE fw.file_id=? AND w.status='confirmed'", (fid,)).fetchone():
                click.echo(f"{fid:5} belongs to a confirmed work: not purged", err=True)
            else:
                todo.append((fid, {"by": "user"}))
        held: list[int] = []
    else:
        found = purge.candidates(conn, held=True)
        todo = [(fid, why) for fid, why in found if why]
        held = [fid for fid, why in found if not why]
    for fid in held:
        claims = db.current_assertions(conn, "file", fid)
        probs = next((c["value"] for c in claims.get("kind_probs", []) if c["source"].startswith("classify:")), {})
        title = next((c["value"] for c in claims.get("title", [])), "") or _filename(conn, fid)
        by = ""
        if not probs and (p := screen_rejected(claims)) is not None:
            probs, by = {"personal": p}, " (screen)"
        click.echo(f"{fid:5} held   P(personal)={probs.get('personal', float('nan')):.2f}{by}  {str(title)[:60]}")
    if dry_run:
        for fid, why in todo:
            click.echo(f"{fid:5} would purge  {json.dumps(why)}")
        return
    with db.recorded_run(conn, "purge", {"files": [f for f, _ in todo]}) as (run_id, stats):
        stats.update(purged=0, held=len(held))
        for fid, why in todo:
            if purge.purge_file(conn, fid, why or {"by": "user"}, run_id=run_id):
                stats["purged"] += 1
                click.echo(f"{fid:5} purged")
        purge.checkpoint(conn)
    click.echo(f"purged={stats['purged']} held={len(held)}"
               + ("  (keep one with `papyrus keep ID`, purge it with `papyrus purge ID`)" if held else ""))


@main.command("keep")
@click.argument("targets", nargs=-1, required=True)
@click.pass_context
def keep_cmd(ctx: click.Context, targets: tuple[str, ...]) -> None:
    """Mark files as library material whatever triage said: TARGETS are file ids, or paths of
    originals (which also brings back a purged file: its copy, first pages and path)."""
    from . import purge

    conn = _conn(ctx)
    with db.recorded_run(conn, "keep", {"targets": len(targets)}) as (run_id, stats):
        for t in targets:
            if t.isdigit():
                r = conn.execute("SELECT purged_at FROM files WHERE id=?", (int(t),)).fetchone()
                if r is None or r["purged_at"]:
                    click.echo(f"{t}: {'no such file' if r is None else 'purged; give the path of the original'}",
                               err=True)
                    continue
                action, wid = purge.keep(conn, int(t), run_id)
                where = f" (work {wid})" if wid else ""
            else:
                p = Path(t)
                if not p.is_file():
                    click.echo(f"{t}: not a file", err=True)
                    continue
                action, fid = purge.restore(conn, p, run_id)
                where = f" (file {fid})" if fid and action == "restored" else ""
            stats[action] = stats.get(action, 0) + 1
            hint = {"restored": "; now `papyrus process` triages it", "no_title": "; `papyrus process` triages it",
                    "retriage": "; the screen had set it aside: `papyrus process` triages it in full",
                    "unknown": "; not in the library: `papyrus scan` it"}.get(action, "")
            click.echo(f"{t}: {action}{where}{hint}")


@main.command()
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--all", "redo_all", is_flag=True, help="Re-read the text layer of files already extracted.")
@click.option("--no-ocr", is_flag=True, help="Skip OCR of pages without a text layer.")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def extract(ctx: click.Context, file_ids: tuple[int, ...], redo_all: bool, no_ocr: bool, verbose: bool) -> None:
    """Read every page of the files of live works into the full-text index.

    Scanning reads only the first pages (for triage). This reads the rest, labels references,
    index and contents pages (they rank lower in search), keeps the PDF outline (to label hits
    with their section), and OCRs pages without a text layer with Apple Vision (needs the bridge
    in tools/afm-bridge). Files of personal paperwork are not extracted unless given as FILE_IDS."""
    from .fulltext import run

    def progress(state: str, file_id: int, detail: str) -> None:
        if state == "no-ocr":
            click.echo(f"OCR unavailable: {detail}", err=True)
        elif state == "error":
            click.echo(f"{file_id:5} error {detail}", err=True)
        elif verbose or state == "ocr":
            click.echo(f"{file_id:5} {state:12} {detail}")

    stats = run(_conn(ctx), file_ids, redo=redo_all, ocr=not no_ocr, progress=progress)
    click.echo(" ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing to extract")


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
    from .enrich.pipeline import unenriched_works
    return unenriched_works(conn, file_ids)


@main.command()
@click.argument("file_ids", nargs=-1, type=int)
@click.option("--backend", default=None,
              help="Extraction model for triage: auto | lmstudio[:model] | apple  [default: from config, else auto]")
@click.option("--classifier", "--decider", "classifier_spec", default=None,
              help="Classifier for triage: auto | none | lmstudio[:model] | jev:<url>  [default: from config, else auto]")
@click.option("-n", "--limit", type=int, help="Triage at most N pending files.")
@click.pass_context
def process(ctx: click.Context, file_ids: tuple[int, ...], backend: str | None, classifier_spec: str | None,
            limit: int | None) -> None:
    """Triage, promote, extract and enrich whatever is pending: `triage && promote && extract && enrich`.
    Promote purges what the classifier is sure is personal paperwork (see `papyrus purge`).

    With FILE_IDS, triages those files and enriches only their works. Stops before promoting
    if triage can't run (e.g. no classifier available)."""
    click.echo("== triage")
    ctx.invoke(triage, file_ids=file_ids, backend=backend, classifier_spec=classifier_spec, limit=limit)
    click.echo("== promote")
    ctx.invoke(promote)
    click.echo("== extract")
    ctx.invoke(extract)  # everything pending: new works' files, whether or not they were in FILE_IDS
    pending = _unenriched_works(_conn(ctx), file_ids)
    click.echo(f"== enrich ({len(pending)} works)")
    if pending:
        ctx.invoke(enrich, work_ids=tuple(pending))


@main.command()
@click.option("--once", is_flag=True, help="Catch up (scan the watched directories, process what is pending) and exit.")
@click.option("--dir", "dirs", multiple=True, type=click.Path(file_okay=False, path_type=Path), metavar="DIR",
              help="Watch DIR instead of the config's [watch] dirs (repeatable).")
@click.option("--poll", is_flag=True, help="Poll the directories instead of using FSEvents / inotify.")
@click.option("--status", "show_status", is_flag=True, help="Print the running watcher's status (JSON) and exit.")
@click.option("--retry-failed", is_flag=True, help="Give the queue's failed items another round of attempts, and exit.")
@click.option("--pause", "pause", flag_value=True, default=None,
              help="Pause model work (triage, OCR) of this library's watcher, running or not, and exit. New "
                   "files are still added. Lasts until --resume.")
@click.option("--resume", "pause", flag_value=False, help="Resume model work, and exit.")
@click.pass_context
def watch(ctx: click.Context, once: bool, dirs: tuple[Path, ...], poll: bool, show_status: bool,
          retry_failed: bool, pause: bool | None) -> None:
    """Add new PDFs from ~/Downloads and ~/Desktop (config: [watch] dirs) as they appear, and
    triage, promote, extract and enrich them one at a time, at background priority.

    Runs in the foreground; Papyrus Server.app (macOS) or a systemd user service (Linux) runs it
    in the background. One watcher per library. Model work waits under memory pressure, while
    LM Studio is off or has another model loaded, and without a classifier (unless classifier =
    "none"). Logs name file ids and counts only. SIGTERM stops it, closing its run as
    interrupted."""
    from . import config
    from .watch import EXIT_LOCKED, Watch, read_status
    from .watch.daemon import EXIT_RESCUED
    from .watch.sources import PollingWatcher
    from .watch.system import InstanceLock, lower_priority

    home = db.library_home()
    if show_status:
        click.echo(json.dumps(read_status(home), indent=2))
        return
    if pause is not None:
        flag = home / "watch.pause"
        if pause:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
        click.echo("model work paused (papyrus watch --resume)" if pause else "model work resumed")
        return
    if retry_failed:
        from .watch.queue import Queue

        click.echo(f"{Queue(home / 'queue.sqlite').retry_failed()} failed items are pending again")
        return
    try:
        settings = config.watch()
        config.backend(), config.classifier(), config.lmstudio_autostart()  # fail now on a bad config
    except config.ConfigError as e:
        raise click.ClickException(str(e)) from e
    roots = [Path(d).expanduser().resolve() for d in (dirs or settings.dirs)]
    missing = [d for d in roots if not d.is_dir()]
    for d in missing:
        click.echo(f"watch: not a directory, skipped: {d}", err=True)
    roots = [d for d in roots if d not in missing]
    if not roots:
        raise click.ClickException("no directory to watch (config: [watch] dirs, or --dir)")
    rescued = sorted(home.glob("rescued-*"))
    if rescued:  # watch/daemon.py, Watch._guard: the files on disk are not what the last watcher had
        click.echo(f"watch: {rescued[-1]} holds the databases as a watcher had them when another program "
                   "replaced the files; restore them, or delete the folder, before starting again "
                   "(AGENTS.md, papyrus watch)", err=True)
        ctx.exit(EXIT_RESCUED)
    lock = InstanceLock(home / "watch.lock")
    if not lock.acquire():
        click.echo(f"another papyrus watch is running for {home}" + (f" (pid {lock.holder})" if lock.holder else ""),
                   err=True)
        ctx.exit(EXIT_LOCKED)
    try:
        prio = lower_priority()
        click.echo(f"watch: {home}, {len(roots)} directories, priority {'+'.join(prio) or 'unchanged'}", err=True)
        w = Watch(_conn(ctx), roots, watcher=PollingWatcher(roots) if poll else None, settle_s=settings.settle_s,
                  unload_after_s=settings.unload_after_s)
        try:
            w.run(once=once)
        except KeyboardInterrupt:
            click.echo("watch: stopped", err=True)
    finally:
        lock.release()


@main.group("inference")
def inference_cmd() -> None:
    """The inference cache (<home>/inference.sqlite): every question asked of a model, with
    hashes and timings; the texts are files in <home>/inference/."""


@inference_cmd.command("stats")
@click.option("--json", "as_json", is_flag=True)
def inference_stats(as_json: bool) -> None:
    """Per model and question: calls, cache hits, failures, and how long calls took (median, 90th
    percentile, seconds per 1,000 input characters; with the model already loaded)."""
    from . import inference

    rows = inference.store().stats()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    click.echo(f"{'model':32} {'kind':7} {'query':12} {'calls':>5} {'hits':>5} {'failed':>6} "
               f"{'median':>7} {'p90':>7} {'s/kchar':>7}")
    for r in rows:
        fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        click.echo(f"{r['model'][:32]:32} {r['kind']:7} {r['query_id']:12} {r['calls']:5} {r['hits']:5} "
                   f"{r['failed']:6} {fmt(r['median_s']):>7} {fmt(r['p90_s']):>7} {fmt(r['s_per_kchar']):>7}")


@inference_cmd.command("show")
@click.argument("call_id")
def inference_show(call_id: str) -> None:
    """One call (id or a prefix, e.g. from assertions.call_id): model, question, timings, and the
    input and output texts if their files are still there."""
    from . import inference

    c = inference.store().show(call_id)
    if c is None:
        raise click.ClickException(f"no call {call_id}")
    text = {k: c.pop(k) for k in ("input", "output", "instructions")}
    click.echo(json.dumps(c, indent=2))
    for k, v in text.items():
        click.echo(f"--- {k}" + (" (deleted)" if v is None else ""))
        if v is not None:
            click.echo(v)


@inference_cmd.command("prune")
@click.option("--inputs/--no-inputs", default=True, show_default=True, help="Delete the input files.")
@click.option("--outputs/--no-outputs", default=False, show_default=True,
              help="Delete the output files too (the cache then asks the models again).")
@click.option("--older-than", "days", type=float, default=30, show_default=True, metavar="DAYS")
def inference_prune(inputs: bool, outputs: bool, days: float) -> None:
    """Delete the texts of old calls; the rows (hashes, timings) stay. Inputs hold documents'
    first pages; outputs are what the cache answers from."""
    from . import inference

    n = inference.store().prune(inputs=inputs, outputs=outputs, older_than_days=days)
    click.echo(f"deleted {n} files")


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
    q = """SELECT f.id, f.page_count, f.text_status, f.triage_status, f.size, f.stored_path, f.purged_at,
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
        triage = "purged" if r["purged_at"] else r["triage_status"]
        click.echo(f"{r['id']:5} {r['page_count'] or '?':>5}p {r['text_status']:8} {triage:8} {first}")


def _page_line(h) -> str:
    where = f"p.{h.page}" + (f" {h.kind}" if h.kind != "body" else "") + (f" · {h.section[:40]}" if h.section else "")
    return f"{'':6}{where}\n{'':10}{h.snippet}"


@main.command()
@click.argument("query")
@click.option("-n", "--limit", default=20, show_default=True)
@click.option("--kind", help="Only works of this kind.")
@click.option("--files", "by_file", is_flag=True,
              help="Rank files by their pages instead, including files that aren't (live) works, "
                   "such as personal paperwork.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, kind: str | None, by_file: bool, as_json: bool) -> None:
    """Search works by title, authors, abstract, topics and the text of their pages.

    QUERY is free text (every word must match) or SQLite FTS5 syntax: "phrases", OR, NOT,
    prefix*, NEAR(a b, 5)."""
    from dataclasses import asdict

    from .search import fill_snippets, rank_files, search_works

    conn = _conn(ctx)
    if by_file:
        files = rank_files(conn, query, live_only=False, limit=limit)
        fill_snippets(conn, query, [h for f in files for h in f.hits])
        if as_json:
            click.echo(json.dumps([asdict(f) for f in files], indent=2, ensure_ascii=False))
            return
        for f in files:
            name = conn.execute("SELECT c.title FROM context_refs r JOIN contexts c ON c.id=r.context_id"
                                " WHERE r.file_id=? ORDER BY c.missing_at IS NOT NULL LIMIT 1", (f.file_id,)).fetchone()
            click.echo(f"[{f.file_id}] {name[0] if name else ''}  ({f.pages} pages match)")
            for h in f.hits:
                click.echo(_page_line(h))
        return
    works = search_works(conn, query, limit=limit, kind=kind)
    if as_json:
        click.echo(json.dumps([{**w, "page_hits": [asdict(h) for h in w["page_hits"]]} for w in works],
                              indent=2, ensure_ascii=False))
        return
    for w in works:
        click.echo(f"{w['id']:5} {w['kind'] or '?':8} {w['year'] or '':4}  {(w['title'] or '')[:80]}")
        if w["authors"]:
            click.echo(f"{'':20}{w['authors'][:80]}")
        for h in w["page_hits"]:
            click.echo(_page_line(h))
        if w["matching_pages"] > len(w["page_hits"]):
            click.echo(f"{'':6}+{w['matching_pages'] - len(w['page_hits'])} more pages")


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
    click.echo(f"variant: {db.variant()}  library: {db.library_home()}")
    p = config.path()
    click.echo(f"config file: {p}{'' if p.exists() else '  (not present; using defaults)'}")
    try:
        for name in ("backend", "classifier"):
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
    for r in conn.execute("SELECT text_status, triage_status, fulltext_status, count(*) n FROM files"
                          " WHERE purged_at IS NULL GROUP BY 1, 2, 3"):
        click.echo(f"  files text={r[0]} triage={r[1]} fulltext={r[2]}: {r[3]}")
    click.echo(f"  files purged: {conn.execute('SELECT count(*) FROM files WHERE purged_at IS NOT NULL').fetchone()[0]}")
    for r in conn.execute("SELECT source, kind, count(*) FROM file_pages GROUP BY 1, 2 ORDER BY 1, 2"):
        click.echo(f"  pages {r[0]}/{r[1]}: {r[2]}")


@main.command("resolve")
@click.option("--kind", "context_kind", metavar="KIND", help="Only refs of contexts of this kind (e.g. slack).")
@click.option("--limit", type=int, help="At most this many identifiers.")
@click.pass_context
def resolve_cmd(ctx: click.Context, context_kind: str | None, limit: int | None) -> None:
    """Link unresolved context refs that name a work (arXiv id, DOI) to works, creating and
    enriching new candidate works where needed (resolve.py)."""
    from .resolve import resolve_refs

    conn = _conn(ctx)
    with db.recorded_run(conn, "resolve", {"kind": context_kind, "limit": limit}) as (run_id, stats):
        stats.update(resolve_refs(conn, context_kind=context_kind, run_id=run_id, limit=limit, progress=lambda i, r:
                                  click.echo(f"{i[0]}:{i[1]:24} {r['status']:16} {r.get('work_id') or ''}")))
    click.echo(" ".join(f"{k}={v}" for k, v in stats.items() if k != "status"))


from .contexts.slack_cli import slack_group  # noqa: E402  (kept out of this file: contexts/slack_cli.py)

main.add_command(slack_group)


if __name__ == "__main__":
    main()
