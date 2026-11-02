"""`papyrus watch`: ingest new PDFs as they appear, one step at a time, out of the way.

    start ─▶ catch-up batch: scan the watched directories once (events were missed while the
       │     watcher was down), promote what was classified and extracted but never promoted
       ▼
    loop ─▶ watcher events ─▶ Debouncer (settled files, gone paths) ─▶ batch
                         └──▶ pending work (after a back-off, every RECHECK_S) ──▶ batch

A **batch** is one `db.recorded_run(conn, "watch", …)`: it **scans** the settled files through
`scan.scan_path` (snapshot, sha256 dedupe, purged tombstones, provenance, exactly as `papyrus
scan`), then works through the pipeline's pending steps, one item each, new files first:

1. **index** a live work's file for full-text search (`index.process`, `live_only`);
2. **read** pages without a text layer with OCR (live works' files, and the first pages of
   image-only files before they are classified);
3. **match** a work that has no API data yet (match/);
4. **classify** a pending file (the screen first, screen.py, then the classifier), and promote it
   at once if it isn't library material (which purges sure personal paperwork);
5. **extract** the metadata of a file classified as library material, then **promote** it.

Classify and extract run in phases (`_next_task`): every pending file is classified, then every
classified file extracted, so the two models can differ and each is loaded once per phase.

A batch ends when nothing is left, after BATCH_STEPS steps (so runs stay short and new files
don't wait), or with the model steps backing off. The models sit behind the inference cache
(inference.py), with the gate inside it: an answer the cache has costs no model call and no
check. Otherwise model work (read, classify, extract) is gated: before every model call the memory
pressure is read, and for LM Studio the `ModelLedger` checks what is loaded (see models.py). Under
pressure, with LM Studio off or busy with another model, or when Apple's model refuses (error
1013, `ModelBusy`), the step is left pending and model work waits (`Backoff`); index and match go
on. What is pending, and when to try again, is the work queue's (queue.py, `<home>/queue.sqlite`,
synced from the library before every step): a step that fails, or finishes without changing
anything, is retried after 1 h, 4 h and 16 h, then left as `failed` (a failed classify also marks
the file `error`, as `papyrus classify` does).

The log says when each step starts and how long it took, naming files and works by id and the
first HASH_CHARS hex digits of the file's sha256 (`file 1148 [3fa2c1d9]`; a work by its first
file's), with the screen's and the classifier's likeliest kinds, the extraction model's time and
tokens, and the match's score and sources.

Privacy: the log names ids, hashes, kinds, counts, timings and states only, never titles, paths,
page text, identifier values or exception messages (an OSError's message holds the path).

Status: `<library home>/watch.json` (state, per-stage lines, ingesting N/M, back-off), read by
the web server's /api/version for the menu bar, and by `papyrus watch --status`. Performance:
`<library home>/watch-events.jsonl` (events.py, `papyrus watch --perf`).
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

from .. import db, index, inference, purge, scan
from ..llm.base import BackendUnavailable, ModelBusy
from .debounce import Debouncer, is_candidate
from .errors import (IDLE, INGESTING, LMSTUDIO_DELAY, LMSTUDIO_UNAVAILABLE, MEMORY_DELAY, MEMORY_DELAY_MAX, PAUSED,
                     STARTING,
                     STOPPED, WAITING_MEMORY, Backoff, MATCH_PATIENCE_S, RATE_LIMIT_DELAY, RATE_LIMITED)
from .models import ModelLedger
from .events import NAME as EVENTS_NAME, EventLog
from .queue import MODEL_STAGES, STAGES, Queue
from .sources import PollingWatcher, Watcher, make_watcher
from . import system
from .system import CRITICAL, NORMAL, memory_pressure

BATCH_STEPS = 25
RECHECK_S = 600  # look for work other processes left pending this often
AUTOSTART_EVERY_S = 3600  # let `lms server start` be tried again (lmstudio.autostart) this often
EXIT_LOCKED = 75  # another watcher holds the library's lock (EX_TEMPFAIL): don't restart
EXIT_REPLACED = 70  # a database file was replaced under the watcher; a copy is in <home>/rescued-*
EXIT_RESCUED = 78  # a rescued-* copy waits to be restored: refuse to start (EX_CONFIG)

CLASSIFY_PENDING = "read_status IN ('ok', 'no_text') AND purged_at IS NULL AND classify_status IN ('pending', 'error')"
EXTRACT_PENDING = "purged_at IS NULL AND classify_status = 'done' AND extract_status = 'pending'"
# the menu's lines (Watch.stages), in pipeline order
STAGE_ORDER = ("read", "classify", "extract", "index", "match")
STAGE_LABELS = {"read": "Reading", "classify": "Classifying", "extract": "Extracting", "index": "Indexing",
                "match": "Matching"}
EMA_ALPHA = 0.2  # weight of the newest step in a stage's seconds-per-item average
# a stage worked within this many seconds is "active": steps of other stages come in between
# (each extracted document's indexing and matching run before the next document's extraction)
ACTIVE_S = 30.0


def default_log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


HASH_CHARS = 8  # hex digits of a file's sha256 in the log (the store's names have 16: store/<hash>*.pdf)


def secs(s: float) -> str:
    """A duration for the log: 0.42 s, 6.2 s, 54 s, 3 min 05 s."""
    if s < 1:
        return f"{s:.2f} s"
    if s < 10:
        return f"{s:.1f} s"
    if s < 120:
        return f"{s:.0f} s"
    return f"{int(s // 60)} min {int(s % 60):02d} s"


def n_of(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def top_kinds(probs: dict[str, float] | None, n: int = 3, floor: float = 0.01) -> str:
    """The likeliest kinds of a distribution, e.g. `paper 0.94, report 0.03` (labels and numbers only)."""
    if not probs:
        return "?"
    best = sorted(probs.items(), key=lambda kv: -kv[1])
    shown = [(k, p) for i, (k, p) in enumerate(best[:n]) if i == 0 or p >= floor]
    return ", ".join(f"{k} {p:.2f}" for k, p in shown)


def resolve_models() -> tuple[Any, Any]:
    """The extraction model and classifier from the config, as `papyrus extract` / `classify` pick
    them. Raises Backoff when there is no classifier: none is used only if the config says `none`."""
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
            raise Backoff(LMSTUDIO_UNAVAILABLE, "no classifier (classifying needs one unless classifier = \"none\")",
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
                 screen: Callable[[], Any] | None = None, trees: list[Path] = ()):
        from ..llm import lmstudio

        self.conn = conn
        self.dirs = [Path(d).expanduser().resolve() for d in dirs]
        self.trees = [Path(d).expanduser().resolve() for d in trees]  # watched with their subdirectories
        self.watcher = watcher
        self.memory = memory or (lambda: memory_pressure())
        self.clock, self.log = clock, log
        self.ledger = ledger or ModelLedger(lmstudio.loaded_models, lmstudio.unload_model, idle_s=unload_after_s,
                                            clock=clock, log=log,
                                            room=lambda m: system.has_room(lmstudio.model_size(m)))
        self.resolve_models = models or (lambda: resolve_models())
        self.resolve_screen = screen or (lambda: resolve_screen())
        self.screen: Any = None  # the classify stage's first step (screen.py), resolved with the models
        self.debouncer = Debouncer(self.dirs, settle_s, clock, trees=self.trees)
        self.status_path = status_path or db.library_home() / "watch.json"
        self.opts = scan.ScanOptions(store=store)
        self.ocr = ocr
        self.batch_steps = batch_steps
        self.host = socket.gethostname()
        self.started = db.now_iso()
        self.state, self.detail = STARTING, ""
        # model work waiting, per stage: an OCR that can't run under "warn" mustn't hold classifying
        self.backoffs: dict[str, tuple[float, str, str]] = {}  # stage -> (until, state, reason)
        self.memory_delay = MEMORY_DELAY
        self.queue = queue or Queue(db.library_home() / "queue.sqlite", wall)
        self.models: tuple[Any, Any] | None = None
        self.phase = "classify"  # the model phase in progress: classify | extract
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
        # the performance log (events.py): every step, file, model change and back-off, timed
        self.events = EventLog(self.status_path.with_name(EVENTS_NAME), wall)
        self.wall = wall
        ledger_log = self.ledger.log
        self.ledger.log = lambda msg: (ledger_log(msg), self.events.emit("model", msg=msg))
        self.busy: dict[str, Any] | None = None  # since the queue was last empty: start time, counts
        self.step_detail: dict[str, Any] = {}  # what the last step found, for its event
        self.step_t0 = time.monotonic()  # when the step in progress began (its log lines say how long it took)
        # seconds per item, per stage: an exponential moving average of finished steps, started from
        # the stage's history (the queue's, else the performance log's); and the stage worked last
        self.ema: dict[str, float | None] = {}
        self.last_worked: dict[str, float] = {}  # stage -> clock() of its last step
        self.scanning: dict[str, Any] | None = None  # the catch-up scan in progress: done, total

    # --- running ------------------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        """Catch up, then (unless `once`) follow events until interrupted (SIGTERM, ^C)."""
        from ..describe import PROMPT_VERSION

        self._status(STARTING)
        self.events.emit("start", pid=os.getpid(), dirs=len(self.dirs) + len(self.trees), prompt_version=PROMPT_VERSION)
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
            self.watcher = make_watcher(self.dirs, trees=self.trees)
        try:
            self.watcher.start()
        except OSError as e:
            self.log(f"{self.watcher.name} failed to start ({type(e).__name__}); polling instead")
            self.watcher = PollingWatcher(self.dirs, trees=self.trees)
            self.watcher.start()
        self.log(f"watching {len(self.dirs) + len(self.trees)} directories"
                 + (f" ({len(self.trees)} with their subdirectories)" if self.trees else "") + f" ({self.watcher.name})")

    def close(self) -> None:
        if self.watcher is not None:
            self.watcher.close()
        self.queue.close()
        try:
            self.ledger.release_idle(force=True)
        finally:
            self._status(STOPPED)
            self.events.emit("stop", totals={k: v for k, v in self.totals.items() if v and k not in ("seen", "unchanged")})

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
        from ..describe import PROMPT_VERSION

        c: Counter = Counter()
        t0 = time.monotonic()
        self.models = None  # resolved again on the batch's first model step (the config may have changed)
        with db.recorded_run(self.conn, "watch", {"trigger": trigger, "files": len(ready),
                                                   "dirs": len(self.dirs) + len(self.trees)},
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
                        self._drained()
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
            self.log(f"run {run_id} ({trigger}) took {secs(time.monotonic() - t0)}: {summary}")
        self.dirty = more
        self.last_check = self.clock()
        self._settle()

    def _scan_dirs(self, run_id: int, c: Counter) -> None:
        """`papyrus scan` of the watched directories (`scan -r` of the trees), except that files
        changed within the last few seconds wait in the debouncer (they may still be downloading)."""
        from dataclasses import replace

        from .. import store

        if self.opts.store:
            store.clean_tmp()
        now, wall = db.now_iso(), time.time()
        self.unreadable = []
        tree_opts = replace(self.opts, recursive=True, max_depth=scan.TREE_DEPTH)
        roots = [(d, self.opts) for d in self.dirs] + [(t, tree_opts) for t in self.trees]
        # a big new tree takes minutes to add (hash, first pages, a clone each): the menu says so
        self.scanning = {"done": 0, "total": 0, "t0": time.monotonic(), "shown": time.monotonic()}
        try:
            self._scan_roots(roots, run_id, c, now, wall)
        finally:
            self.scanning = None

    def _scan_roots(self, roots: list, run_id: int, c: Counter, now: str, wall: float) -> None:
        listed = []
        for root, opts in roots:
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
            listed.append((root, [p for p in scan.iter_pdfs(root, opts) if is_candidate(p.name)]))
        self.scanning["total"] = sum(len(ps) for _, ps in listed)
        for root, paths in listed:
            for path in paths:
                self.scanning["done"] += 1
                if time.monotonic() - self.scanning["shown"] >= 2:
                    self.scanning["shown"] = time.monotonic()
                    self._status(INGESTING)
                try:
                    recent = wall - path.stat().st_mtime < self.debouncer.settle_s
                except OSError:
                    continue
                if recent:
                    self.debouncer.add(path)
                else:
                    self._scan_file(path, run_id, c, now)
                    self.debouncer.seen(path)
            scan.mark_missing(self.conn, root, now, c)

    def _take(self, ready: list[Path], gone: list[Path], run_id: int, c: Counter) -> None:
        now = db.now_iso()
        for path in ready:
            self._scan_file(path, run_id, c, now)
        for path in gone:  # a file, or a directory of a tree: everything below it
            prefix = str(path).rstrip("/") + "/"
            n = self.conn.execute("UPDATE contexts SET missing_at=? WHERE kind='filesystem' AND missing_at IS NULL"
                                  " AND (locator=? OR substr(locator, 1, ?)=?)",
                                  (now, str(path), len(prefix), prefix)).rowcount
            c["went_missing"] += n

    def _busy(self) -> dict[str, Any]:
        if self.busy is None:
            self.busy = {"since": self.wall(), "steps": Counter(), "added": 0, "files": set()}
            self.busy["files"] = self._pending_files()
        return self.busy

    def _drained(self) -> None:
        """The queue is empty: log how long the work since it was last empty took (if any)."""
        if self.busy is None or self.backoffs:
            return
        b, self.busy = self.busy, None
        self.events.emit("drained", busy_s=round(self.wall() - b["since"], 1), steps=dict(b["steps"]), added=b["added"])

    def _scan_file(self, path: Path, run_id: int, c: Counter, now: str) -> None:
        t0 = time.monotonic()
        outcome, fid = scan.scan_path(self.conn, path, self.opts, host=self.host, now=now, run_id=run_id, stats=c)
        if outcome not in ("unchanged", "seen", None):
            busy = self._busy()
            busy["added"] += outcome == "new"
            if outcome == "new" and fid is not None:
                busy["files"].add(fid)
            self.events.emit("scan", id=fid, outcome=outcome, ms=round((time.monotonic() - t0) * 1000))
        if outcome in ("new", "dup"):
            self.dirty = True
            self.log(f"{self._file(fid)} added ({outcome}) in {secs(time.monotonic() - t0)}")
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
        """A live filesystem context at `path`, or below it (a tree's directory that went)."""
        prefix = str(path).rstrip("/") + "/"
        return self.conn.execute("SELECT 1 FROM contexts WHERE kind='filesystem' AND missing_at IS NULL"
                                 " AND (locator=? OR substr(locator, 1, ?)=?) LIMIT 1",
                                 (str(path), len(prefix), prefix)).fetchone() is not None

    def _promote_backlog(self, run_id: int, c: Counter) -> None:
        """Files classified and extracted (by the CLI, or a watcher that stopped) but never promoted."""
        for (fid,) in self.conn.execute(
                f"SELECT f.id FROM files f WHERE {db.MODELS_DONE} AND f.purged_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM file_works fw WHERE fw.file_id=f.id) ORDER BY f.id").fetchall():
            try:
                self._promote(fid, run_id, c, quiet=True)
            except Exception as e:  # noqa: BLE001  one bad file must not stop the watcher at every start
                c["promote_error"] += 1
                self.log(f"{self._file(fid)} promote failed ({type(e).__name__})")

    def _promote(self, fid: int, run_id: int, c: Counter, quiet: bool = False) -> None:
        from ..works import promote_file

        ref = self._file(fid)  # before a purge (the tombstone keeps the hash, but ask while it is plainly there)
        t0 = time.monotonic()
        action, wid = promote_file(self.conn, fid, run_id=run_id)
        c[f"promote_{action}"] += 1
        if action == "purged":
            purge.checkpoint(self.conn)
        took = time.monotonic() - t0
        if not (quiet and action == "held"):
            self.log(f"{ref} {action}" + (f" (work {wid})" if wid else "") + (f" in {secs(took)}" if took >= 0.1 else ""))

    # --- naming things in the log: ids and the short hash of the bytes ------------------------

    def _file(self, fid: int | None) -> str:
        """`file 1148 [3fa2c1d9]`: the id and the first digits of the file's sha256."""
        if fid is None:
            return "file ?"
        r = self.conn.execute("SELECT sha256 FROM files WHERE id=?", (fid,)).fetchone()
        return f"file {fid} [{r[0][:HASH_CHARS]}]" if r else f"file {fid}"

    def _work(self, wid: int) -> str:
        """`work 1048 [3fa2c1d9]`: the id and the short hash of its first file."""
        r = self.conn.execute("SELECT f.sha256 FROM file_works fw JOIN files f ON f.id=fw.file_id WHERE fw.work_id=?"
                              " ORDER BY f.id LIMIT 1", (wid,)).fetchone()
        return f"work {wid} [{r[0][:HASH_CHARS]}]" if r else f"work {wid}"

    def _ref(self, stage: str, i: int) -> str:
        return self._work(i) if stage == "match" else self._file(i)

    def _took(self) -> str:
        """How long the step in progress has taken so far."""
        return secs(time.monotonic() - self.step_t0)

    # --- steps --------------------------------------------------------------------------------

    def _library_pending(self) -> dict[str, list[int]]:
        """What the library says is pending, per stage (the queue mirrors it)."""
        from ..match.pipeline import unmatched_works

        ocr = self.ocr and self.ocr_ok is not False
        return {
            "index": index.pending_files(self.conn),
            "read": list(index.ocr_targets(self.conn)) if ocr else [],  # none while OCR can't run
            "match": unmatched_works(self.conn),
            "classify": [r[0] for r in self.conn.execute(f"SELECT id FROM files WHERE {CLASSIFY_PENDING} ORDER BY id")],
            "extract": [r[0] for r in self.conn.execute(f"SELECT id FROM files WHERE {EXTRACT_PENDING} ORDER BY id")],
        }

    @property
    def paused(self) -> bool:
        """Model work (read, classify, extract) is paused: `<home>/watch.pause` exists (the menu bar's Pause
        Model Work, `papyrus watch --pause`). Files are still scanned, indexed and matched."""
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
            waiting |= set(MODEL_STAGES)
        free = [s for s in STAGES if s not in waiting]
        # In phases: classify everything pending, then extract metadata for everything
        # classified, then classify what arrived meanwhile, and so on. The two may be different
        # models; each phase runs as far as it can, so a model is loaded (or swapped in) once
        # per phase, not once per file.
        other = {"classify": "extract", "extract": "classify"}[self.phase]
        free = [s for s in free if s != other]
        task = self.queue.next(free)
        if task is None and other not in waiting:
            task = self.queue.next([other])
            if task is not None:
                self.log(f"model phase: {other}")
                self.phase = other
        return task

    def _still_pending(self, stage: str, i: int) -> bool:
        return i in self._library_pending()[stage]

    def _step(self, task: tuple[str, int], run_id: int, c: Counter) -> None:
        stage, i = task
        ref = self._ref(stage, i)
        t0 = self.step_t0 = time.monotonic()
        self.step_detail = {}
        outcome = "done"
        try:
            left = self.pending().get(stage, 0)
        except Exception:  # noqa: BLE001  a count for the log must never stop the step
            left = None
        self.log(f"{ref} {stage}: starting" + (f" ({left} pending)" if left else ""))
        try:
            getattr(self, f"_{stage}")(i, run_id, c)
            if self._still_pending(stage, i):  # nothing changed (e.g. matching found nothing)
                outcome = "no_progress"
                self._retry(task, "no_progress")
            else:
                self.queue.done(stage, i, round((time.monotonic() - t0) * 1000))
        except Backoff as b:
            outcome = "backoff"
            self._back_off(stage, b, c)
        except ModelBusy:
            outcome = "backoff"
            self._back_off(stage, Backoff(WAITING_MEMORY, "the model refused the request under memory pressure"), c)
        except BackendUnavailable:
            outcome = "backoff"
            self._back_off(stage, Backoff(LMSTUDIO_UNAVAILABLE, "the model is unavailable", delay=LMSTUDIO_DELAY), c)
        except Exception as e:  # noqa: BLE001  the type only: messages may hold paths
            outcome = f"error:{type(e).__name__}"
            c[f"{stage}_error"] += 1
            if stage == "classify":  # (a failed metadata step stays `classified` and is retried)
                self.conn.execute("UPDATE files SET classify_status='error' WHERE id=?", (i,))
            self.log(f"{ref} {stage} failed after {self._took()} ({type(e).__name__})")
            self._retry(task, type(e).__name__)
        finally:
            if outcome != "backoff":
                self._busy()["steps"][stage] += 1
                self.last_worked[stage] = self.clock()
            if outcome == "done":
                self._average(stage, time.monotonic() - t0)
            self.events.emit("step", stage=stage, id=i, ms=round((time.monotonic() - t0) * 1000), outcome=outcome,
                             phase=self.phase, **self.step_detail)

    def _retry(self, task: tuple[str, int], error: str) -> None:
        stage, i = task
        delay = self.queue.retry(stage, i, error)
        self.log(f"{self._ref(stage, i)} {stage}: " + (f"{error}, trying again in {delay / 3600:.0f} h" if delay
                                              else f"{error}, giving up (papyrus watch --retry-failed)"))

    def _index(self, fid: int, run_id: int, c: Counter) -> None:
        s = index.process(self.conn, (fid,), ocr=False, live_only=True)
        if s.get("files"):
            c["indexed"] += s["files"]
        if s.get("error"):
            c["extract_error"] += s["error"]
        self.log(f"{self._file(fid)} indexed in {self._took()} ({n_of(s.get('pages', 0), 'page')})" if s.get("files")
                 else f"{self._file(fid)} index failed after {self._took()}")

    def _read(self, fid: int, run_id: int, c: Counter) -> None:
        self._gate("local", None)  # Apple Vision, in the bridge
        s = index.process(self.conn, (fid,), ocr=True, live_only=True)
        if s.get("ocr_skipped_pages"):
            self.ocr_ok = False
            self.log("OCR is unavailable (tools/afm-bridge not built, or not macOS); image-only pages stay unread")
            return
        self.memory_delay = MEMORY_DELAY
        for k in ("ocr_pages", "requeued"):
            if s.get(k):
                c[k] += s[k]
        self.log(f"{self._file(fid)} OCR in {self._took()}: {s.get('ocr_pages_with_text', 0)} of {s.get('ocr_pages', 0)}"
                 " pages read")

    def _match(self, wid: int, run_id: int, c: Counter) -> None:
        from .. import http
        from ..match.pipeline import match_work

        # Don't sit through a rate limit's retries (minutes, with everything else waiting): give up
        # on a refusal that would cost more than MATCH_PATIENCE_S, record no verdict, come back.
        with http.patience(MATCH_PATIENCE_S):
            r = match_work(self.conn, wid, run_id=run_id)
        c[f"match_{r['status']}"] += 1
        self.log(f"{self._work(wid)} match: {r['status']} in {self._took()}" + self._match_detail(r))
        if r["status"] == "rate_limited":
            names = {"arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "Crossref", "s2": "Semantic Scholar"}
            raise Backoff(RATE_LIMITED, "rate limited by " + ", ".join(names.get(x, x) for x in r["sources"]),
                          delay=max(RATE_LIMIT_DELAY, r.get("retry_after") or 0))

    @staticmethod
    def _match_detail(r: dict[str, Any]) -> str:
        """Scores, sources and counts of a match result (no titles: candidates' titles are search terms)."""
        status = r.get("status")
        if status == "matched":
            parts = []
            if r.get("score") is not None:
                parts.append(f"score {r['score']:.2f}")
            if r.get("sources"):
                parts.append("from " + "+".join(r["sources"]))
            if r.get("ids"):  # the schemes, not the values
                parts.append("ids: " + ", ".join(sorted(r["ids"])))
            if r.get("authors_linked"):
                parts.append(n_of(r['authors_linked'], 'author') + " linked")
            if r.get("possible_duplicates"):
                parts.append(n_of(len(r['possible_duplicates']), 'possible duplicate'))
            return f" ({'; '.join(parts)})" if parts else ""
        if status == "ambiguous":
            return f" (best score {r['best_score']:.2f} of {n_of(r.get('candidates', 0), 'candidate')})"
        if status == "rate_limited":
            return " (" + ", ".join(r.get("sources") or []) + ")"
        if status == "skipped" and r.get("kind"):
            return f" (a {r['kind']} without identifiers)"
        return ""

    def _classify(self, fid: int, run_id: int, c: Counter) -> None:
        from ..classify import classify_file

        _, clf = self._models(run_id)
        r = classify_file(self.conn, fid, clf, run_id=run_id, screen=self.screen)
        ref, took = self._file(fid), self._took()
        scr = r.get("screen") or {}
        screen_note = (f"screen {secs(scr['screen_s'])}" if scr.get("screen_s") is not None else None)
        model_note = (f"classifier {secs(r['classify_s'])}" + (", cached" if r.get("classify_cached") else "")
                      if r.get("classify_s") is not None else None)
        times = "; ".join(x for x in (screen_note, model_note) if x)
        times = f" [{times}]" if times else ""
        self.memory_delay = MEMORY_DELAY
        c["classified"] += 1
        self.step_detail = {"result": "screened" if r.get("screened") else
                            "not_library" if r["status"] == "done" else "library"}
        if r.get("accepted"):
            self.step_detail["by"] = "screen"
        if r.get("p_personal") is not None:
            self.step_detail["p_personal"] = round(r["p_personal"], 3)
        if r.get("screened"):
            c["screened"] += 1
            self.step_detail["p_personal"] = r["screen"]["p_personal"]
            kinds = f"; {top_kinds(r['screen_probs'])}" if r.get("screen_probs") else ""
            self.log(f"{ref} set aside by the screen in {took} (P(personal)={r['screen']['p_personal']:.2f}{kinds})")
        elif r.get("accepted"):
            c["accepted"] += 1
            self.log(f"{ref} classified by the screen in {took}: {top_kinds(r.get('screen_probs'))}")
        else:
            if r.get("screen_probs"):
                unsure = f"; the screen was unsure: {top_kinds(r['screen_probs'])}"
            elif scr.get("p_personal") is not None:
                unsure = f"; the screen: P(personal)={scr['p_personal']:.2f}"
            else:
                unsure = ""
            probs = f": {top_kinds(r['kind_probs'])}" if r.get("kind_probs") else ""
            verdict = " not library" if r["status"] == "done" else ""  # not library material: no metadata extraction
            self.log(f"{ref} classified{verdict} in {took}{probs}{unsure}{times}")
        if r["status"] == "done":
            self._promote(fid, run_id, c)

    def _extract(self, fid: int, run_id: int, c: Counter) -> None:
        from ..extract import extract_metadata

        be, _ = self._models(run_id)
        r = extract_metadata(self.conn, fid, be, run_id=run_id)
        self.step_detail = {"cached": bool(r.get("cached"))}
        self.memory_delay = MEMORY_DELAY
        c["extracted"] += 1
        self.log(f"{self._file(fid)} metadata extracted in {self._took()}{self._extract_detail(r, be)}")
        self._promote(fid, run_id, c)

    @staticmethod
    def _extract_detail(r: dict[str, Any], be: Any) -> str:
        """The model's time and tokens, and what came back, as counts (no titles or names)."""
        parts = []
        if r.get("cached"):
            parts.append("cached")
        elif r.get("extract_s") is not None:
            note = f"model {secs(r['extract_s'])}"
            usage = getattr(be, "last_usage", None)
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                out = usage.get("completion_tokens") or 0
                note += f", {usage['prompt_tokens']} tokens in, {out} out"
                if out and r["extract_s"] > 0:
                    note += f" ({out / r['extract_s']:.0f} tok/s overall)"
            parts.append(note)
        e = r.get("extracted") or {}
        got = []
        if isinstance(e.get("authors"), list):
            got.append(n_of(len(e["authors"]), "author"))
        if isinstance(e.get("topics"), list) and e["topics"]:
            got.append(n_of(len(e["topics"]), "topic"))
        got.append(f"abstract ({r['abstract_method']})" if e.get("abstract") else "no abstract")
        parts.append(", ".join(got))
        return " (" + "; ".join(parts) + ")"

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
            self.events.emit("models", extract=tag, classify=dec.info().tag if dec is not None else None,
                             screen=self.screen.tag if self.screen else None)
            self.log("classify with " + (f"{self.screen.tag} (P(personal) >= {self.screen.threshold:g}"
                                         + (f", P(kind) >= {self.screen.accept:g}" if self.screen.accept else "")
                                         + "), then " if self.screen else "")
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
            # the screen's embedding model (84-640 MB) loads beside whatever is loaded; under "warn"
            # (which this machine reads whenever LM Studio holds a model) only if it is loaded
            # already or fits (system.has_room). LM Studio's own idle timeout unloads it.
            if level == NORMAL or self._embedder_fits():
                return
            raise Backoff(WAITING_MEMORY, f"memory pressure ({level}): no room for the screen's model")
        if kind == "lmstudio" and model:
            # a model that is already loaded costs little more; loading one beside another under
            # pressure would swap, so then the watcher swaps its own instead (models.ModelLedger)
            self.ledger.claim(model, may_load=level == NORMAL)
        elif level != NORMAL:
            raise Backoff(WAITING_MEMORY, f"memory pressure ({level})")

    def _embedder_fits(self) -> bool:
        emb = getattr(self.screen.embedder, "inner", self.screen.embedder) if self.screen else None
        if emb is None:
            return True
        if getattr(emb, "model_loaded", lambda: None)():
            return True
        return self.ledger.room is not None and self.ledger.room(emb.info().model)

    def _back_off(self, stage: str, b: Backoff, c: Counter) -> None:
        if b.state == WAITING_MEMORY:
            delay = b.delay or self.memory_delay
            self.memory_delay = min(self.memory_delay * 2, MEMORY_DELAY_MAX)
        else:
            delay = b.delay or LMSTUDIO_DELAY
            if b.state != RATE_LIMITED:  # (the models are fine)
                self.models = None
        self.backoffs[stage] = (self.clock() + delay, b.state, b.reason)
        c[f"backoff_{b.state}"] += 1
        self.log(f"{stage} waits {delay:.0f} s: {b.reason}")
        self.events.emit("backoff", stage=stage, state=b.state, reason=b.reason, delay_s=round(delay))

    def _waiting(self) -> tuple[float, str, str] | None:
        """The back-off to show: the model phases' if one has work, else reading's (OCR)."""
        counts, now = self.pending(), self.clock()
        for stage in ("classify", "extract", "read"):
            b = self.backoffs.get(stage)
            if b and now < b[0] and counts[stage]:
                return b
        return None

    # --- status -------------------------------------------------------------------------------

    def _history_s(self, stage: str) -> float | None:
        """Mean seconds per item of the stage's last 50 finished steps: from the queue, else from
        the performance log (which outlives a reset library)."""
        ms = [r[0] for r in self.queue.conn.execute(
            "SELECT duration_ms FROM items WHERE stage=? AND duration_ms IS NOT NULL ORDER BY done_at DESC LIMIT 50",
            (stage,))]
        if not ms:
            from .events import read

            ms = [e["ms"] for e in read(self.events.path)
                  if e.get("event") == "step" and e.get("stage") == stage and e.get("outcome") == "done"][-50:]
        return sum(ms) / len(ms) / 1000 if ms else None

    def per_item_s(self, stage: str) -> float | None:
        if stage not in self.ema:
            self.ema[stage] = self._history_s(stage)
        return self.ema[stage]

    def _average(self, stage: str, seconds: float) -> None:
        prev = self.per_item_s(stage)
        self.ema[stage] = seconds if prev is None else EMA_ALPHA * seconds + (1 - EMA_ALPHA) * prev

    def stages(self, counts: dict[str, int]) -> list[dict[str, Any]]:
        """One entry per stage with work left, in pipeline order, for the menu: what it is doing
        (`active`: worked in the last ACTIVE_S seconds; `pending`: waiting for its turn, since the
        stages share one loop and the local models; `waiting`: backed off; `paused`), how many
        items, seconds per item (moving average) and seconds left."""
        out, now = [], self.clock()
        if (s := self.scanning) and s["total"] > s["done"]:  # the catch-up scan: files still to add
            per = (time.monotonic() - s["t0"]) / s["done"] if s["done"] else None
            left = s["total"] - s["done"]
            out.append({"stage": "scan", "label": "Adding", "unit": "files", "pending": left, "state": "active",
                        "per_item_s": round(per, 2) if per is not None else None,
                        "eta_s": round(left * per) if per is not None else None})
        for stage in STAGE_ORDER:
            n = counts.get(stage, 0)
            if not n:
                continue
            per = self.per_item_s(stage)
            b = self.backoffs.get(stage)
            if self.paused and stage in MODEL_STAGES:
                state, reason = "paused", "model work paused"
            elif b and now < b[0]:
                state, reason = "waiting", b[2]
            else:
                worked = self.last_worked.get(stage)
                state, reason = ("active" if worked is not None and now - worked <= ACTIVE_S else "pending"), None
            # the menu counts files; matching works on works (a work's files are matched together)
            files = len(self._files_of(self._library_pending()["match"])) if stage == "match" else n
            entry = {"stage": stage, "label": STAGE_LABELS[stage], "unit": "files",
                     "pending": files, "state": state, "per_item_s": round(per, 2) if per is not None else None,
                     "eta_s": round(n * per) if per is not None else None}
            if reason:
                entry["reason"] = reason
            out.append(entry)
        return out

    def pending(self) -> dict[str, int]:
        """Items in the queue per stage (due, or waiting for a retry), and `failed`."""
        return self.queue.counts()

    def _files_of(self, works: list[int]) -> set[int]:
        if not works:
            return set()
        return {r[0] for r in self.conn.execute(
            f"SELECT file_id FROM file_works WHERE work_id IN ({','.join('?' * len(works))})", works)}

    def _pending_files(self) -> set[int]:
        """Files some stage still has to do something with (matching: the files of its works)."""
        lp = self._library_pending()
        files = set().union(*(set(lp[s]) for s in ("read", "classify", "extract", "index")))
        return files | self._files_of(lp["match"])

    def ingesting(self) -> dict[str, int] | None:
        """Since the watcher woke up (the queue was last empty): `total` files in the pipeline, what
        was pending then plus what arrived since, and `done`, those no stage has left (matched, or
        set aside: held, purged, not library material). None while there is nothing to do."""
        if self.busy is None:
            return None
        pending = self._pending_files()
        self.busy["files"] |= pending
        return {"done": len(self.busy["files"] - pending), "total": len(self.busy["files"])}

    def eta_s(self) -> float | None:
        """Seconds of work left: classify and extract from the inference cache's timings (what the models took
        for the questions of the latest document), the other stages from the queue's history."""
        try:
            store = inference.store()
            per = {"classify": store.document_estimate_s("classify"), "extract": store.document_estimate_s("extract")}
        except Exception:  # noqa: BLE001  an estimate must never stop the watcher
            per = {}
        return self.queue.eta_s(per)

    def _settle(self) -> None:
        """The state between batches: paused or waiting (if model work is pending), or idle."""
        counts = self.pending()
        if self.paused and any(counts.get(s) for s in MODEL_STAGES):
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
            "watcher": getattr(self.watcher, "name", None), "dirs": len(self.dirs) + len(self.trees), "paused": self.paused,
            "loaded_by_watch": sorted(self.ledger.ours),
            "totals": {k: self.totals[k] for k in ("new_files", "classified", "extracted", "promote_created", "promote_linked",
                                                   "promote_purged", "indexed", "match_matched") if self.totals[k]},
        }
        if state in (WAITING_MEMORY, LMSTUDIO_UNAVAILABLE):
            if waiting := self._waiting():
                data["retry_at"] = db.to_iso(time.time() + max(0.0, waiting[0] - self.clock()))
        if state != STOPPED:
            try:
                counts = self.pending()
                data["failed"] = counts.pop("failed")
                data["pending"] = counts
                data["phase"] = self.phase
                data["stages"] = self.stages(counts)
                data["eta_s"] = self.eta_s() if any(counts.values()) else 0.0
                data["ingesting"] = self.ingesting()
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
