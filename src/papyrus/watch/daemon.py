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
4. **triage** a pending file, then **promote** it (which purges sure personal paperwork).

A batch ends when nothing is left, after BATCH_STEPS steps (so runs stay short and new files
don't wait), or with the model steps backing off. Model work (triage, OCR) is gated: before
every model call the memory pressure is read, and for LM Studio the `ModelLedger` checks what is
loaded (see models.py). Under pressure, with LM Studio off or busy with another model, or when
Apple's model refuses (error 1013, `ModelBusy`), the step is left pending and model work waits
(`Backoff`); extract and enrich go on. A step that fails for another reason is retried after
RETRY_S (triage marks the file `error`, as `papyrus triage` does).

Privacy: the log names file and work ids, counts and states only, never titles, paths, page
text or exception messages (an OSError's message holds the path).

Status: `<library home>/watch.json` (state, pending counts, back-off), read by the web server's
/api/version for the menu bar, and by `papyrus watch --status`.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .. import db, fulltext, purge, scan
from ..llm.base import BackendUnavailable, ModelBusy
from .debounce import Debouncer, is_candidate
from .errors import (IDLE, INGESTING, LMSTUDIO_DELAY, LMSTUDIO_UNAVAILABLE, MEMORY_DELAY, MEMORY_DELAY_MAX, STARTING,
                     STOPPED, WAITING_MEMORY, Backoff)
from .models import ModelLedger
from .sources import PollingWatcher, Watcher, make_watcher
from .system import CRITICAL, NORMAL, memory_pressure

BATCH_STEPS = 25
RETRY_S = 6 * 3600  # a step that failed is tried again after this long
RECHECK_S = 600  # look for work other processes left pending this often
AUTOSTART_EVERY_S = 3600  # let `lms server start` be tried again (lmstudio.autostart) this often
EXIT_LOCKED = 75  # another watcher holds the library's lock (EX_TEMPFAIL): don't restart

TRIAGE_PENDING = ("text_status IN ('ok', 'no_text') AND purged_at IS NULL AND triage_status <> 'done'")


def default_log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def resolve_models() -> tuple[Any, Any]:
    """The triage backend and decider from the config, as `papyrus triage` picks them. Raises
    Backoff when there is none: triage never runs without a decider unless it is `none`."""
    from .. import config
    from ..llm import get_backend
    from ..llm.decide import get_decider

    backend_spec, decider_spec = config.backend(), config.decider()
    try:
        be = get_backend(backend_spec)
        if not be.available():
            raise BackendUnavailable(f"{be.info().tag} is not available")
    except BackendUnavailable as e:
        raise Backoff(LMSTUDIO_UNAVAILABLE, "no extraction model", delay=LMSTUDIO_DELAY) from e
    dec = None
    if decider_spec != "none":
        try:
            dec = get_decider(None if decider_spec == "auto" else decider_spec)
            if not dec.available():
                raise BackendUnavailable(f"decider {dec.info().tag} is not available")
        except BackendUnavailable as e:
            raise Backoff(LMSTUDIO_UNAVAILABLE, "no decider (triage needs one unless decider = \"none\")",
                          delay=LMSTUDIO_DELAY) from e
    return be, dec


class _Gated:
    """A Backend or Decider whose every model call passes the watcher's gate first."""

    def __init__(self, inner: Any, gate: Callable[[str, str | None], None]):
        info = inner.info()
        self.inner, self._gate = inner, gate
        # lmstudio: the ledger decides; remote (a Jev or OpenAI endpoint): no local memory at stake
        self.kind = ("lmstudio" if info.backend == "lmstudio" else
                     "remote" if info.backend in ("jev", "openai") else "local")
        self.model = getattr(inner, "model", None) or info.model

    def info(self):
        return self.inner.info()

    def available(self) -> bool:
        return self.inner.available()

    def extract(self, **kw: Any) -> dict[str, Any]:
        self._gate(self.kind, self.model)
        return self.inner.extract(**kw)

    def decide(self, state: str, questions: list) -> dict:
        self._gate(self.kind, self.model)
        return self.inner.decide(state, questions)


class Watch:
    def __init__(self, conn, dirs: list[Path], *, watcher: Watcher | None = None,
                 memory: Callable[[], str] | None = None, ledger: ModelLedger | None = None,
                 models: Callable[[], tuple[Any, Any]] | None = None, settle_s: float = 3.0,
                 unload_after_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = default_log, status_path: Path | None = None,
                 store: bool = True, ocr: bool = True, batch_steps: int = BATCH_STEPS):
        from ..llm import lmstudio

        self.conn = conn
        self.dirs = [Path(d).expanduser().resolve() for d in dirs]
        self.watcher = watcher
        self.memory = memory or (lambda: memory_pressure())
        self.clock, self.log = clock, log
        self.ledger = ledger or ModelLedger(lmstudio.loaded_models, lmstudio.unload_model, idle_s=unload_after_s,
                                            clock=clock, log=log)
        self.resolve_models = models or (lambda: resolve_models())
        self.debouncer = Debouncer(self.dirs, settle_s, clock)
        self.status_path = status_path or db.library_home() / "watch.json"
        self.opts = scan.ScanOptions(store=store)
        self.ocr = ocr
        self.batch_steps = batch_steps
        self.host = socket.gethostname()
        self.started = db.now_iso()
        self.state, self.detail = STARTING, ""
        self.backoff_state: str | None = None
        self.backoff_reason = ""
        self.backoff_until = 0.0
        self.memory_delay = MEMORY_DELAY
        self.retry_at: dict[tuple[str, int], float] = {}
        self.models: tuple[_Gated, _Gated | None] | None = None
        self.ocr_ok: bool | None = None
        self.dirty = True  # there may be pending work: look
        self.last_check = clock()
        self.last_autostart = -AUTOSTART_EVERY_S
        self.last_refresh = clock()
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
        try:
            self.ledger.release_idle(force=True)
        finally:
            self._status(STOPPED)

    def catch_up(self, once: bool = False) -> None:
        self.batch("startup", catch_up=True, limit=None if once else self.batch_steps)

    def pump(self, timeout: float = 0.0) -> None:
        """One turn of the loop: take events (waiting up to `timeout`), run a batch if there is
        anything to do, unload idle models."""
        if self.watcher is not None:
            for p in self.watcher.get(timeout):
                self.debouncer.touch(p)
        ready, gone = self._due()
        now = self.clock()
        if self.backoff_state and now >= self.backoff_until:
            self.backoff_state = None
            self.dirty = True
        if now - self.last_check >= RECHECK_S:
            self.dirty = True
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

    def _timeout(self) -> float:
        if self.dirty:
            return 0.0
        now = self.clock()
        waits = [60.0, self.last_check + RECHECK_S - now]
        if (d := self.debouncer.next_check()) is not None:
            waits.append(d)
        if self.backoff_state:
            waits.append(self.backoff_until - now)
        if (d := self.ledger.idle_due()) is not None:
            waits.append(d + 1)
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
        for root in self.dirs:
            if not root.is_dir():
                c["dirs_missing"] += 1
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

    def _next_task(self) -> tuple[str, int] | None:
        from ..enrich.pipeline import unenriched_works

        now = self.clock()

        def ok(stage: str, i: int) -> bool:
            return self.retry_at.get((stage, i), 0.0) <= now

        models_ok = self.backoff_state is None or now >= self.backoff_until
        for fid in fulltext.pending_files(self.conn):
            if ok("extract", fid):
                return "extract", fid
        if models_ok and self.ocr and self.ocr_ok is not False:
            for fid in fulltext.ocr_targets(self.conn):
                if ok("ocr", fid):
                    return "ocr", fid
        for wid in unenriched_works(self.conn):
            if ok("enrich", wid):
                return "enrich", wid
        if models_ok:
            for (fid,) in self.conn.execute(f"SELECT id FROM files WHERE {TRIAGE_PENDING} ORDER BY id"):
                if ok("triage", fid):
                    return "triage", fid
        return None

    def _step(self, task: tuple[str, int], run_id: int, c: Counter) -> None:
        stage, i = task
        what = "work" if stage == "enrich" else "file"
        try:
            getattr(self, f"_{stage}")(i, run_id, c)
        except Backoff as b:
            self._back_off(b, c)
        except ModelBusy:
            self._back_off(Backoff(WAITING_MEMORY, "the model refused the request under memory pressure"), c)
        except BackendUnavailable:
            self._back_off(Backoff(LMSTUDIO_UNAVAILABLE, "the model is unavailable", delay=LMSTUDIO_DELAY), c)
        except Exception as e:  # noqa: BLE001  the type only: messages may hold paths
            c[f"{stage}_error"] += 1
            self.retry_at[task] = self.clock() + RETRY_S
            if stage == "triage":
                self.conn.execute("UPDATE files SET triage_status='error' WHERE id=?", (i,))
            self.log(f"{what} {i} {stage} failed ({type(e).__name__}); retrying in {RETRY_S // 3600} h")

    def _extract(self, fid: int, run_id: int, c: Counter) -> None:
        self.retry_at[("extract", fid)] = self.clock() + RETRY_S  # if it is still pending after this
        s = fulltext.process(self.conn, (fid,), ocr=False, live_only=True)
        if s.get("files"):
            c["extracted"] += s["files"]
        if s.get("error"):
            c["extract_error"] += s["error"]
        self.log(f"file {fid} extracted ({s.get('pages', 0)} pages)" if s.get("files") else f"file {fid} extract failed")

    def _ocr(self, fid: int, run_id: int, c: Counter) -> None:
        self._gate("local", None)  # Apple Vision, in the bridge
        self.retry_at[("ocr", fid)] = self.clock() + RETRY_S
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

        self.retry_at[("enrich", wid)] = self.clock() + RETRY_S
        r = enrich_work(self.conn, wid, run_id=run_id)
        c[f"enrich_{r['status']}"] += 1
        self.log(f"work {wid} enrich: {r['status']}")

    def _triage(self, fid: int, run_id: int, c: Counter) -> None:
        from ..triage import triage_file

        be, dec = self._models(run_id)
        triage_file(self.conn, fid, be, dec, run_id=run_id)
        self.memory_delay = MEMORY_DELAY
        c["triaged"] += 1
        self.log(f"file {fid} triaged")
        self._promote(fid, run_id, c)

    # --- models, memory, back-off -------------------------------------------------------------

    def _models(self, run_id: int) -> tuple[_Gated, _Gated | None]:
        if self.models is None:
            if self.clock() - self.last_autostart >= AUTOSTART_EVERY_S:
                from ..llm import lmstudio

                lmstudio.forget_start_attempt()  # ensure_server may try `lms server start` again
                self.last_autostart = self.clock()
            be, dec = self.resolve_models()
            self.models = (_Gated(be, self._gate), _Gated(dec, self._gate) if dec is not None else None)
            tag = be.info().tag
            self.conn.execute("UPDATE runs SET backend=? WHERE id=?", (tag, run_id))
            self.log(f"triage with {tag}" + (f" + {dec.info().tag}" if dec is not None else " (no decider)"))
        return self.models

    def _gate(self, kind: str, model: str | None) -> None:
        """Before every model call: may we ask now? Raises Backoff if not."""
        if kind == "remote":
            return
        level = self.memory()
        if level == CRITICAL:
            raise Backoff(WAITING_MEMORY, "memory pressure is critical")
        if kind == "lmstudio" and model:
            # a model that is already loaded costs little more; loading one under pressure would swap
            self.ledger.claim(model, may_load=level == NORMAL)
        elif level != NORMAL:
            raise Backoff(WAITING_MEMORY, f"memory pressure ({level})")

    def _back_off(self, b: Backoff, c: Counter) -> None:
        if b.state == WAITING_MEMORY:
            delay = b.delay or self.memory_delay
            self.memory_delay = min(self.memory_delay * 2, MEMORY_DELAY_MAX)
        else:
            delay = b.delay or LMSTUDIO_DELAY
            self.models = None
        self.backoff_state, self.backoff_reason = b.state, b.reason
        self.backoff_until = self.clock() + delay
        c[f"backoff_{b.state}"] += 1
        self.log(f"model work waits {delay:.0f} s: {b.reason}")

    # --- status -------------------------------------------------------------------------------

    def pending(self) -> dict[str, int]:
        from ..enrich.pipeline import unenriched_works

        q = self.conn.execute
        return {
            "triage": q(f"SELECT count(*) FROM files WHERE {TRIAGE_PENDING}").fetchone()[0],
            "extract": len(fulltext.pending_files(self.conn)),
            "enrich": len(unenriched_works(self.conn)),
        }

    def _settle(self) -> None:
        """The state between batches: waiting (if model work is pending and backing off) or idle."""
        if self.backoff_state and self.clock() < self.backoff_until and self.pending()["triage"]:
            self._status(self.backoff_state, self.backoff_reason)
        else:
            self._status(IDLE)

    def _status(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        data: dict[str, Any] = {
            "state": state, "detail": detail, "pid": os.getpid(), "since": self.started, "updated": db.now_iso(),
            "watcher": getattr(self.watcher, "name", None), "dirs": len(self.dirs),
            "loaded_by_watch": sorted(self.ledger.ours),
            "totals": {k: self.totals[k] for k in ("new_files", "triaged", "promote_created", "promote_linked",
                                                   "promote_purged", "extracted", "enrich_enriched") if self.totals[k]},
        }
        if state in (WAITING_MEMORY, LMSTUDIO_UNAVAILABLE):
            data["retry_at"] = db.to_iso(time.time() + max(0.0, self.backoff_until - self.clock()))
        if state != STOPPED:
            try:
                data["pending"] = self.pending()
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
