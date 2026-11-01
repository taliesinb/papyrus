"""`papyrus watch`: add new PDFs as they appear, then process them one at a time, out of the way.

    start ─▶ catch-up batch: scan the watched directories once (events were missed while the
       │     watcher was down), promote what was triaged but never promoted, process
       ▼
    loop ─▶ watcher events ─▶ Debouncer (settled files, gone paths) ─▶ batch
                         └──▶ pending work (after a back-off, every RECHECK_S) ──▶ batch

A **batch** is one `db.recorded_run(conn, "watch", …)`: it ingests the settled files through
`scan.ingest_path` (snapshot, sha256 dedupe, purged tombstones, provenance, exactly as `papyrus
scan`), then works through pending steps, one item each, new files first:

1. **extract** the text layer of a live work's file (`fulltext.process`, `live_only`);
2. **ocr** pages without a text layer (live works' files, and the first pages of image-only files
   before they are triaged);
3. **enrich** a work that has no API data yet;
4. **triage** a pending file, then **promote** it (which purges sure personal paperwork). Triage
   starts with the screen (screen.py), whose embedding model is small enough to load beside the
   language model; what it sets aside is held without asking the language model.

A batch ends when nothing is left, after BATCH_STEPS steps (so runs stay short and new files
don't wait), or with the model steps backing off. Triage's models sit behind the inference
cache (inference.py), with the gate inside it: an answer the cache has costs no model call and
no check. Otherwise model work (triage, OCR) is gated: before every model call the memory
pressure is read, and for LM Studio the `ModelLedger` checks what is loaded (see models.py). Under pressure, with LM Studio off or busy with another model, or when
Apple's model refuses (error 1013, `ModelBusy`), the step is left pending and model work waits
(`Backoff`); extract and enrich go on. What is pending, and when to try again, is the work
queue's (queue.py, `<home>/queue.sqlite`, synced from the library before every step): a step that
fails, or finishes without changing anything, is retried after 1 h, 4 h and 16 h, then left as
`failed` (triage also marks the file `error`, as `papyrus triage` does).

Privacy: the log names file and work ids, counts and states only, never titles, paths, page
text or exception messages (an OSError's message holds the path).

Status: `<library home>/watch.json` (state, pending counts, back-off), read by the web server's
/api/version for the menu bar, and by `papyrus watch --status`.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .. import db, fulltext, inference, purge, scan
from ..llm.base import BackendUnavailable, ModelBusy
from .debounce import Debouncer, is_candidate
from .errors import (IDLE, INGESTING, LMSTUDIO_DELAY, LMSTUDIO_UNAVAILABLE, MEMORY_DELAY, MEMORY_DELAY_MAX, PAUSED,
                     STARTING,
                     STOPPED, WAITING_MEMORY, Backoff)
from .models import ModelLedger
from .queue import STAGES, Queue
from .sources import PollingWatcher, Watcher, make_watcher
from .system import CRITICAL, NORMAL, memory_pressure

BATCH_STEPS = 25
RECHECK_S = 600  # look for work other processes left pending this often
AUTOSTART_EVERY_S = 3600  # let `lms server start` be tried again (lmstudio.autostart) this often
EXIT_LOCKED = 75  # another watcher holds the library's lock (EX_TEMPFAIL): don't restart
EXIT_REPLACED = 70  # a database file was replaced under the watcher; a copy is in <home>/rescued-*
EXIT_RESCUED = 78  # a rescued-* copy waits to be restored: refuse to start (EX_CONFIG)

TRIAGE_PENDING = ("text_status IN ('ok', 'no_text') AND purged_at IS NULL AND triage_status <> 'done'")


def default_log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def resolve_models() -> tuple[Any, Any]:
    """The triage backend and classifier from the config, as `papyrus triage` picks them. Raises
    Backoff when there is none: triage never runs without a classifier unless it is `none`."""
    from .. import config
    from ..llm import get_backend
    from ..llm.classify import get_classifier

    backend_spec, classifier_spec = config.backend(), config.classifier()
    try:
        be = get_backend(backend_spec)
        if not be.available():
            raise BackendUnavailable(f"{be.info().tag} is not available")
    except BackendUnavailable as e:
        raise Backoff(LMSTUDIO_UNAVAILABLE, "no extraction model", delay=LMSTUDIO_DELAY) from e
    dec = None
    if classifier_spec != "none":
        try:
            dec = get_classifier(None if classifier_spec == "auto" else classifier_spec)
            if not dec.available():
                raise BackendUnavailable(f"classifier {dec.info().tag} is not available")
        except BackendUnavailable as e:
            raise Backoff(LMSTUDIO_UNAVAILABLE, "no classifier (triage needs one unless classifier = \"none\")",
                          delay=LMSTUDIO_DELAY) from e
    return be, dec


def resolve_screen() -> Any:
    """The configured screen (screen.py), or None (turned off, or LM Studio lacks its model)."""
    from ..screen import get_screen

    return get_screen()


def model_kind(m: Any) -> tuple[str, str | None]:
    """How the gate treats a model: lmstudio (the ledger decides), remote (a Jev or OpenAI endpoint:
    no local memory at stake) or local (Apple FM and the like); and its model name."""
    info = m.info()
    kind = "lmstudio" if info.backend == "lmstudio" else "remote" if info.backend in ("jev", "openai") else "local"
    return kind, getattr(m, "model", None) or info.model


class Watch:
    def __init__(self, conn, dirs: list[Path], *, watcher: Watcher | None = None,
                 memory: Callable[[], str] | None = None, ledger: ModelLedger | None = None,
                 models: Callable[[], tuple[Any, Any]] | None = None, settle_s: float = 3.0,
                 unload_after_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = default_log, status_path: Path | None = None,
                 store: bool = True, ocr: bool = True, batch_steps: int = BATCH_STEPS,
                 queue: Queue | None = None, wall: Callable[[], float] = time.time,
                 screen: Callable[[], Any] | None = None):
        from ..llm import lmstudio

        self.conn = conn
        self.dirs = [Path(d).expanduser().resolve() for d in dirs]
        self.watcher = watcher
        self.memory = memory or (lambda: memory_pressure())
        self.clock, self.log = clock, log
        self.ledger = ledger or ModelLedger(lmstudio.loaded_models, lmstudio.unload_model, idle_s=unload_after_s,
                                            clock=clock, log=log)
        self.resolve_models = models or (lambda: resolve_models())
        self.resolve_screen = screen or (lambda: resolve_screen())
        self.screen: Any = None  # triage's first stage (screen.py), resolved with the models
        self.debouncer = Debouncer(self.dirs, settle_s, clock)
        self.status_path = status_path or db.library_home() / "watch.json"
        self.opts = scan.ScanOptions(store=store)
        self.ocr = ocr
        self.batch_steps = batch_steps
        self.host = socket.gethostname()
        self.started = db.now_iso()
        self.state, self.detail = STARTING, ""
        # model work waiting, per stage: an OCR that can't run under "warn" mustn't hold triage
        self.backoffs: dict[str, tuple[float, str, str]] = {}  # stage -> (until, state, reason)
        self.memory_delay = MEMORY_DELAY
        self.queue = queue or Queue(db.library_home() / "queue.sqlite", wall)
        self.models: tuple[Any, Any] | None = None
        self.last_level: str | None = None  # memory pressure at the last gate, recorded with the call
        self.ocr_ok: bool | None = None
        self.dirty = True  # there may be pending work: look
        self.last_check = clock()
        self.last_autostart = -AUTOSTART_EVERY_S
        self.last_refresh = clock()
        self.inodes: dict[Path, int] = {}  # the database files as first seen (see _guard)
        self.pause_path = self.status_path.with_name("watch.pause")
        self.was_paused = self.pause_path.exists()  # pump() acts when this changes
        self.unreadable: list[Path] = []  # watched directories macOS won't let us read
        self.last_access_check = clock()
        self.totals: Counter = Counter()

    # --- running ------------------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        """Catch up, then (unless `once`) follow events until interrupted (SIGTERM, ^C)."""
        self._status(STARTING)
        try:
            if not once:
                self._start_watcher()
            self.catch_up(once=once)
            if once:
                while self.dirty and self._next_task() is not None:
                    self.batch("once", limit=None)
                return
            while True:
                self.pump(self._timeout())
        finally:
            self.close()

    def _start_watcher(self) -> None:
        if self.watcher is None:
            self.watcher = make_watcher(self.dirs)
        try:
            self.watcher.start()
        except OSError as e:
            self.log(f"{self.watcher.name} failed to start ({type(e).__name__}); polling instead")
            self.watcher = PollingWatcher(self.dirs)
            self.watcher.start()
        self.log(f"watching {len(self.dirs)} directories ({self.watcher.name})")

    def close(self) -> None:
        if self.watcher is not None:
            self.watcher.close()
        self.queue.close()
        try:
            self.ledger.release_idle(force=True)
        finally:
            self._status(STOPPED)

    def catch_up(self, once: bool = False) -> None:
        self.batch("startup", catch_up=True, limit=None if once else self.batch_steps)

    def pump(self, timeout: float = 0.0) -> None:
        """One turn of the loop: take events (waiting up to `timeout`), run a batch if there is
        anything to do, unload idle models."""
        self._guard()
        if self.watcher is not None:
            for p in self.watcher.get(timeout):
                self.debouncer.touch(p)
        ready, gone = self._due()
        now = self.clock()
        if self.paused != self.was_paused:
            self.was_paused = not self.was_paused
            self.log("model work paused" if self.was_paused else "model work resumed")
            self.dirty = True
            self._settle()
        for stage in [s for s, (until, _, _) in self.backoffs.items() if now >= until]:
            del self.backoffs[stage]
            self.dirty = True
        if now - self.last_check >= RECHECK_S:
            self.dirty = True
        if self.unreadable and now - self.last_access_check >= 60:
            self.last_access_check = now
            if any(os.access(d, os.R_OK) and self._listable(d) for d in self.unreadable):
                self.log("access granted: scanning the watched directories")
                self.batch("access", catch_up=True)
                return
        if ready or gone:
            self.batch("events", ready, gone)
        elif self.dirty:
            if self._next_task() is not None:
                self.batch("pending")
            else:
                self.dirty = False
                self.last_check = now
                self._settle()
        if self.ledger.ours and now - self.last_refresh >= 60:
            self.last_refresh = now
            self.ledger.refresh()
        if self.ledger.release_idle():
            self._status(self.state, self.detail)

    @staticmethod
    def _listable(d: Path) -> bool:
        try:
            os.listdir(d)
            return True
        except OSError:
            return False

    def _timeout(self) -> float:
        if self.dirty:
            return 0.0
        now = self.clock()
        waits = [60.0, self.last_check + RECHECK_S - now]
        if (d := self.debouncer.next_check()) is not None:
            waits.append(d)
        waits += [until - now for until, _, _ in self.backoffs.values()]
        if (d := self.ledger.idle_due()) is not None:
            waits.append(d + 1)
        if self.paused:
            waits.append(5.0)  # notice a resume soon
        return max(0.2, min(waits))

    # --- a batch ------------------------------------------------------------------------------

    def batch(self, trigger: str, ready: list[Path] = (), gone: list[Path] = (), *, catch_up: bool = False,
              limit: int | None = BATCH_STEPS) -> None:
        from ..triage import PROMPT_VERSION

        c: Counter = Counter()
        self.models = None  # resolved again on the batch's first triage (the config may have changed)
        with db.recorded_run(self.conn, "watch", {"trigger": trigger, "files": len(ready), "dirs": len(self.dirs)},
                             prompt_version=PROMPT_VERSION) as (run_id, rs):
            try:
                self._status(INGESTING)
                if catch_up:
                    self._scan_dirs(run_id, c)
                self._take(ready, gone, run_id, c)
                if catch_up:
                    self._promote_backlog(run_id, c)
                steps, more = 0, False
                while True:
                    if self.watcher is not None:  # new files go first
                        for p in self.watcher.get(0):
                            self.debouncer.touch(p)
                    self._take(*self._due(), run_id, c)
                    self._guard()
                    task = self._next_task()
                    if task is None:
                        break
                    if limit is not None and steps >= limit:
                        more = True
                        break
                    self._step(task, run_id, c)
                    steps += 1
                    self._status(INGESTING)
            finally:
                rs.update(c)
                self.totals.update(c)
        summary = " ".join(f"{k}={v}" for k, v in sorted(c.items()) if k not in ("seen", "unchanged"))
        if summary:
            self.log(f"run {run_id} ({trigger}): {summary}")
        self.dirty = more
        self.last_check = self.clock()
        self._settle()

    def _scan_dirs(self, run_id: int, c: Counter) -> None:
        """`papyrus scan` of the watched directories, except that files changed within the last
        few seconds wait in the debouncer (they may still be downloading)."""
        from .. import store

        if self.opts.store:
            store.clean_tmp()
        now, wall = db.now_iso(), time.time()
        self.unreadable = []
        for root in self.dirs:
            if not root.is_dir():
                c["dirs_missing"] += 1
                continue
            try:
                os.listdir(root)
            except PermissionError:
                # macOS privacy: the app running the watcher hasn't been allowed into this folder. Skip
                # it whole: everything in it would otherwise look deleted (mark_missing).
                self.unreadable.append(root)
                c["dirs_unreadable"] += 1
                self.log(f"cannot read {root}: allow access in System Settings > Privacy & Security >"
                         " Files and Folders; trying again every minute")
                continue
            for path in scan.iter_pdfs(root, self.opts):
                if not is_candidate(path.name):
                    continue
                try:
                    recent = wall - path.stat().st_mtime < self.debouncer.settle_s
                except OSError:
                    continue
                if recent:
                    self.debouncer.add(path)
                else:
                    self._ingest(path, run_id, c, now)
                    self.debouncer.seen(path)
            scan.mark_missing(self.conn, root, now, c)

    def _take(self, ready: list[Path], gone: list[Path], run_id: int, c: Counter) -> None:
        now = db.now_iso()
        for path in ready:
            self._ingest(path, run_id, c, now)
        for path in gone:
            n = self.conn.execute("UPDATE contexts SET missing_at=? WHERE kind='filesystem' AND locator=?"
                                  " AND missing_at IS NULL", (now, str(path))).rowcount
            c["went_missing"] += n

    def _ingest(self, path: Path, run_id: int, c: Counter, now: str) -> None:
        outcome, fid = scan.ingest_path(self.conn, path, self.opts, host=self.host, now=now, run_id=run_id, stats=c)
        if outcome in ("new", "dup"):
            self.dirty = True
            self.log(f"file {fid} added ({outcome})")
        elif outcome == "purged_skipped":
            self.log("skipped a file whose bytes were purged as personal paperwork")

    # --- the database files under us -----------------------------------------------------------

    def _databases(self) -> dict[str, tuple[sqlite3.Connection, Path]]:
        main = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
        store = inference.store()
        return {"papyrus.sqlite": (self.conn, main), "inference.sqlite": (store.conn, store.path),
                "queue.sqlite": (self.queue.conn, self.queue.path)}

    def _guard(self) -> None:
        """Stop if a database file (or its WAL) was replaced while we hold it open. A GUI editor
        that "safe-saves" (SQLPro for SQLite did, 2026-11-01) swaps a copy in: our connections keep
        writing to the old, now nameless file, and everything since is lost when we close, while
        new readers pair the copy with the live WAL and find a corrupt database. So: copy what our
        connections see into <home>/rescued-<time>/ and exit without closing them."""
        replaced = []
        for _name, (_conn, path) in self._databases().items():
            for f in (path, Path(f"{path}-wal")):
                try:
                    ino = f.stat().st_ino
                except FileNotFoundError:
                    if f in self.inodes:
                        replaced.append(f)
                    continue
                if self.inodes.setdefault(f, ino) != ino:
                    replaced.append(f)
        if replaced:
            self._rescue(replaced)

    def _rescue(self, replaced: list[Path]) -> None:
        dest = db.library_home() / f"rescued-{time.strftime('%Y%m%d-%H%M%S')}"
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for name, (conn, _path) in self._databases().items():
            try:
                out = sqlite3.connect(dest / name)
                conn.backup(out)
                out.close()
                saved.append(name)
            except Exception as e:  # noqa: BLE001  save what can be saved
                self.log(f"could not rescue {name}: {type(e).__name__}")
        self.log(f"{', '.join(p.name for p in replaced)} replaced while the watcher had them open (another program "
                 f"saved over them?). What the watcher had: {', '.join(saved)} in {dest}. Stopping without closing "
                 "the databases. To restore: stop the web server, set the damaged files aside, copy these in "
                 f"(AGENTS.md, papyrus watch). The watcher won't start while {dest.name} exists.")
        self.state, self.detail = "error", f"database replaced; rescued to {dest.name}"
        try:
            tmp = self.status_path.with_name(self.status_path.name + ".tmp")
            tmp.write_text(json.dumps({"state": "error", "detail": self.detail, "pid": os.getpid(),
                                       "updated": db.now_iso()}) + "\n")
            os.replace(tmp, self.status_path)
        except OSError:
            pass
        os._exit(EXIT_REPLACED)

    def _due(self) -> tuple[list[Path], list[Path]]:
        """The debouncer's settled and gone paths, less those with nothing to record: a file whose
        context already has its size and mtime (Spotlight or Quick Look touching an xattr), a
        gone path that was never recorded."""
        ready, gone = self.debouncer.due()
        return [p for p in ready if not self._unchanged(p)], [p for p in gone if self._has_context(p)]

    def _unchanged(self, path: Path) -> bool:
        """The path's filesystem context already records its current size and mtime."""
        r = self.conn.execute("SELECT c.extra FROM contexts c JOIN context_refs r ON r.context_id=c.id"
                              " WHERE c.kind='filesystem' AND c.locator=? AND c.missing_at IS NULL",
                              (str(path),)).fetchone()
        if not r:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        extra = json.loads(r["extra"] or "{}")
        return extra.get("size") == st.st_size and extra.get("mtime") == db.to_iso(st.st_mtime)

    def _has_context(self, path: Path) -> bool:
        return self.conn.execute("SELECT 1 FROM contexts WHERE kind='filesystem' AND locator=? AND missing_at IS NULL",
                                 (str(path),)).fetchone() is not None

    def _promote_backlog(self, run_id: int, c: Counter) -> None:
        """Files triaged (by `papyrus triage`, or a watcher that stopped) but never promoted."""
        for (fid,) in self.conn.execute(
                "SELECT f.id FROM files f WHERE f.triage_status='done' AND f.purged_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM file_works fw WHERE fw.file_id=f.id) ORDER BY f.id").fetchall():
            self._promote(fid, run_id, c, quiet=True)

    def _promote(self, fid: int, run_id: int, c: Counter, quiet: bool = False) -> None:
        from ..works import promote_file

        action, wid = promote_file(self.conn, fid, run_id=run_id)
        c[f"promote_{action}"] += 1
        if action == "purged":
            purge.checkpoint(self.conn)
        if not (quiet and action == "held"):
            self.log(f"file {fid} {action}" + (f" (work {wid})" if wid else ""))

    # --- steps --------------------------------------------------------------------------------

    def _library_pending(self) -> dict[str, list[int]]:
        """What the library says is pending, per stage (the queue mirrors it)."""
        from ..enrich.pipeline import unenriched_works

        ocr = self.ocr and self.ocr_ok is not False
        return {
            "extract": fulltext.pending_files(self.conn),
            "ocr": list(fulltext.ocr_targets(self.conn)) if ocr else [],  # none while OCR can't run
            "enrich": unenriched_works(self.conn),
            "triage": [r[0] for r in self.conn.execute(f"SELECT id FROM files WHERE {TRIAGE_PENDING} ORDER BY id")],
        }

    @property
    def paused(self) -> bool:
        """Model work (triage, OCR) is paused: `<home>/watch.pause` exists (the menu bar's Pause
        Model Work, `papyrus watch --pause`). Files are still added, extracted and enriched."""
        return self.pause_path.exists()

    def _next_task(self) -> tuple[str, int] | None:
        self.queue.sync(self._library_pending())
        paused = self.paused
        if paused and not self.was_paused:  # noticed mid-batch: pump() then sees the resume
            self.was_paused = True
            self.log("model work paused")
        now = self.clock()
        waiting = {s for s, (until, _, _) in self.backoffs.items() if now < until}
        if paused:
            waiting |= {"ocr", "triage"}
        return self.queue.next([s for s in STAGES if s not in waiting])

    def _still_pending(self, stage: str, i: int) -> bool:
        return i in self._library_pending()[stage]

    def _step(self, task: tuple[str, int], run_id: int, c: Counter) -> None:
        stage, i = task
        what = "work" if stage == "enrich" else "file"
        t0 = time.monotonic()
        try:
            getattr(self, f"_{stage}")(i, run_id, c)
            if self._still_pending(stage, i):  # nothing changed (e.g. enrichment found nothing)
                self._retry(task, "no_progress")
            else:
                self.queue.done(stage, i, round((time.monotonic() - t0) * 1000))
        except Backoff as b:
            self._back_off(stage, b, c)
        except ModelBusy:
            self._back_off(stage, Backoff(WAITING_MEMORY, "the model refused the request under memory pressure"), c)
        except BackendUnavailable:
            self._back_off(stage, Backoff(LMSTUDIO_UNAVAILABLE, "the model is unavailable", delay=LMSTUDIO_DELAY), c)
        except Exception as e:  # noqa: BLE001  the type only: messages may hold paths
            c[f"{stage}_error"] += 1
            if stage == "triage":
                self.conn.execute("UPDATE files SET triage_status='error' WHERE id=?", (i,))
            self.log(f"{what} {i} {stage} failed ({type(e).__name__})")
            self._retry(task, type(e).__name__)

    def _retry(self, task: tuple[str, int], error: str) -> None:
        stage, i = task
        delay = self.queue.retry(stage, i, error)
        what = "work" if stage == "enrich" else "file"
        self.log(f"{what} {i} {stage}: " + (f"{error}, trying again in {delay / 3600:.0f} h" if delay
                                              else f"{error}, giving up (papyrus watch --retry-failed)"))

    def _extract(self, fid: int, run_id: int, c: Counter) -> None:
        s = fulltext.process(self.conn, (fid,), ocr=False, live_only=True)
        if s.get("files"):
            c["extracted"] += s["files"]
        if s.get("error"):
            c["extract_error"] += s["error"]
        self.log(f"file {fid} extracted ({s.get('pages', 0)} pages)" if s.get("files") else f"file {fid} extract failed")

    def _ocr(self, fid: int, run_id: int, c: Counter) -> None:
        self._gate("local", None)  # Apple Vision, in the bridge
        s = fulltext.process(self.conn, (fid,), ocr=True, live_only=True)
        if s.get("ocr_skipped_pages"):
            self.ocr_ok = False
            self.log("OCR is unavailable (tools/afm-bridge not built, or not macOS); image-only pages stay unread")
            return
        self.memory_delay = MEMORY_DELAY
        for k in ("ocr_pages", "retriage"):
            if s.get(k):
                c[k] += s[k]
        self.log(f"file {fid} OCR: {s.get('ocr_pages_with_text', 0)} of {s.get('ocr_pages', 0)} pages read")

    def _enrich(self, wid: int, run_id: int, c: Counter) -> None:
        from ..enrich.pipeline import enrich_work

        r = enrich_work(self.conn, wid, run_id=run_id)
        c[f"enrich_{r['status']}"] += 1
        self.log(f"work {wid} enrich: {r['status']}")

    def _triage(self, fid: int, run_id: int, c: Counter) -> None:
        from ..triage import triage_file

        be, dec = self._models(run_id)
        r = triage_file(self.conn, fid, be, dec, run_id=run_id, screen=self.screen)
        self.memory_delay = MEMORY_DELAY
        c["triaged"] += 1
        if r.get("screened"):
            c["screened"] += 1
            self.log(f"file {fid} set aside by the screen (P(personal)={r['screen']['p_personal']:.2f})")
        else:
            self.log(f"file {fid} triaged")
        self._promote(fid, run_id, c)

    # --- models, memory, back-off -------------------------------------------------------------

    def _models(self, run_id: int) -> tuple[Any, Any]:
        if self.models is None:
            if self.clock() - self.last_autostart >= AUTOSTART_EVERY_S:
                from ..llm import lmstudio

                lmstudio.forget_start_attempt()  # ensure_server may try `lms server start` again
                self.last_autostart = self.clock()
            be, dec = self.resolve_models()
            self.models = (self._cached(be, inference.cached_backend),
                           self._cached(dec, inference.cached_classifier) if dec is not None else None)
            try:
                self.screen = self.resolve_screen()
            except Exception as e:  # noqa: BLE001  the screen only saves work: go on without it
                self.screen = None
                self.log(f"screen unavailable ({type(e).__name__})")
            if self.screen is not None:
                self.screen.embedder = inference.cached_embedder(
                    self.screen.embedder, before_call=lambda: self._gate("embed", None),
                    pressure=lambda: self.last_level)
            tag = be.info().tag
            self.conn.execute("UPDATE runs SET backend=? WHERE id=?", (tag, run_id))
            self.log("triage with " + (f"{self.screen.tag} (P >= {self.screen.threshold:g}), then " if self.screen else "")
                     + tag + (f" + {dec.info().tag}" if dec is not None else " (no classifier)"))
        return self.models

    def _cached(self, m: Any, wrap: Callable) -> Any:
        """The model behind the inference cache, with the gate inside it: a cached answer costs no
        model call, so it needs no memory and no LM Studio."""
        kind, model = model_kind(m)
        return wrap(m, before_call=lambda: self._gate(kind, model), pressure=lambda: self.last_level)

    def _gate(self, kind: str, model: str | None) -> None:
        """Before every model call: may we ask now? Raises Backoff if not."""
        if kind == "remote":
            self.last_level = None
            return
        level = self.last_level = self.memory()
        if level == CRITICAL:
            raise Backoff(WAITING_MEMORY, "memory pressure is critical")
        if kind == "embed":
            # the screen's embedding model (84 MB) loads beside whatever is loaded, even under
            # "warn" (which this machine reads whenever LM Studio holds a model); LM Studio's own
            # idle timeout unloads it
            return
        if kind == "lmstudio" and model:
            # a model that is already loaded costs little more; loading one under pressure would swap
            self.ledger.claim(model, may_load=level == NORMAL)
        elif level != NORMAL:
            raise Backoff(WAITING_MEMORY, f"memory pressure ({level})")

    def _back_off(self, stage: str, b: Backoff, c: Counter) -> None:
        if b.state == WAITING_MEMORY:
            delay = b.delay or self.memory_delay
            self.memory_delay = min(self.memory_delay * 2, MEMORY_DELAY_MAX)
        else:
            delay = b.delay or LMSTUDIO_DELAY
            self.models = None
        self.backoffs[stage] = (self.clock() + delay, b.state, b.reason)
        c[f"backoff_{b.state}"] += 1
        self.log(f"{stage} waits {delay:.0f} s: {b.reason}")

    def _waiting(self) -> tuple[float, str, str] | None:
        """The back-off to show: triage's if it has one and work, else OCR's."""
        counts, now = self.pending(), self.clock()
        for stage in ("triage", "ocr"):
            b = self.backoffs.get(stage)
            if b and now < b[0] and counts[stage]:
                return b
        return None

    # --- status -------------------------------------------------------------------------------

    def pending(self) -> dict[str, int]:
        """Items in the queue per stage (due, or waiting for a retry), and `failed`."""
        return self.queue.counts()

    def eta_s(self) -> float | None:
        """Seconds of work left: triage from the inference cache's timings (what the models took
        for the questions of the latest document), the other stages from the queue's history."""
        try:
            per_doc = inference.store().document_estimate_s()
        except Exception:  # noqa: BLE001  an estimate must never stop the watcher
            per_doc = None
        return self.queue.eta_s({"triage": per_doc})

    def _settle(self) -> None:
        """The state between batches: paused or waiting (if model work is pending), or idle."""
        counts = self.pending()
        if self.paused and (counts["triage"] or counts["ocr"]):
            self._status(PAUSED, "model work paused (papyrus watch --resume)")
        elif waiting := self._waiting():
            self._status(waiting[1], waiting[2])
        elif self.unreadable:
            n = len(self.unreadable)
            self._status(IDLE, f"cannot read {n} watched folder{'s' if n > 1 else ''} (macOS privacy settings)")
        else:
            self._status(IDLE)

    def _status(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        data: dict[str, Any] = {
            "state": state, "detail": detail, "pid": os.getpid(), "since": self.started, "updated": db.now_iso(),
            "watcher": getattr(self.watcher, "name", None), "dirs": len(self.dirs), "paused": self.paused,
            "loaded_by_watch": sorted(self.ledger.ours),
            "totals": {k: self.totals[k] for k in ("new_files", "triaged", "promote_created", "promote_linked",
                                                   "promote_purged", "extracted", "enrich_enriched") if self.totals[k]},
        }
        if state in (WAITING_MEMORY, LMSTUDIO_UNAVAILABLE):
            if waiting := self._waiting():
                data["retry_at"] = db.to_iso(time.time() + max(0.0, waiting[0] - self.clock()))
        if state != STOPPED:
            try:
                counts = self.pending()
                data["failed"] = counts.pop("failed")
                data["pending"] = counts
                data["eta_s"] = self.eta_s() if any(counts.values()) else 0.0
            except Exception:  # noqa: BLE001  a status write must never stop the watcher
                pass
        tmp = self.status_path.with_name(self.status_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1) + "\n")
            os.replace(tmp, self.status_path)
        except OSError:
            pass


def read_status(home: Path) -> dict[str, Any]:
    """The watcher's status for `papyrus watch --status`: watch.json, unless no watcher holds the
    library's lock (then state is stopped)."""
    from .system import InstanceLock

    try:
        data = json.loads((home / "watch.json").read_text())
    except (OSError, ValueError):
        data = {}
    lock = InstanceLock(home / "watch.lock")
    if lock.acquire():
        lock.release()
        data["state"] = STOPPED
    return data or {"state": STOPPED}
