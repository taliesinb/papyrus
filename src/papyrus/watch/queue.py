"""The watcher's work queue: `<library home>/queue.sqlite`, its own database.

What is pending is still decided by the library (a file not triaged, a live work's file not
extracted, a work without API data): `sync` mirrors that into `items`, one row per (stage, id).
What the queue adds is the watcher's own working state, which outlives a restart:

* **retries**: a step that failed, or finished without changing anything (`no_progress`, e.g.
  a work enrichment can find nothing for), is tried again after 1 h, 4 h, then 16 h; after the
  fourth attempt the item is `failed` and left alone (`papyrus watch --retry-failed`);
* **history**: how long each finished step took (`duration_ms`), from which `eta_s` estimates
  the time left;
* **counts** for the status, the menu bar and `papyrus watch --status`.

Rows hold stage names, file and work ids, times and exception type names: no titles, paths or
text. Separate from papyrus.sqlite: working state, not knowledge, and the library's schema is a
public contract. Deleting queue.sqlite only forgets retries and timings.

sync rules, per stage given the library's pending ids P:
    new id in P                  -> pending
    done, but in P again         -> pending, attempts reset (e.g. OCR found text: triage again)
    pending or failed, not in P  -> done (another process did it, or the file was purged)
    failed and in P              -> stays failed
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from pathlib import Path
from typing import Callable, Iterable

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    stage       TEXT NOT NULL,              -- extract | ocr | enrich | classify | metadata
    item_id     INTEGER NOT NULL,           -- files.id (works.id for enrich) in papyrus.sqlite
    state       TEXT NOT NULL,              -- pending | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0, -- failed or fruitless tries so far
    next_at     REAL,                       -- not before this (epoch seconds): a retry
    last_error  TEXT,                       -- exception type name, or 'no_progress'
    enqueued_at REAL NOT NULL,
    updated_at  REAL NOT NULL,
    done_at     REAL,
    duration_ms INTEGER,                    -- of the step that finished it (NULL: finished elsewhere)
    PRIMARY KEY (stage, item_id)
);
CREATE INDEX IF NOT EXISTS items_state ON items(state, stage, next_at);
CREATE INDEX IF NOT EXISTS items_done ON items(stage, done_at) WHERE duration_ms IS NOT NULL;
"""

STAGES = ("extract", "ocr", "enrich", "classify", "metadata")  # the order work is taken in: finish files first
MODEL_STAGES = ("ocr", "classify", "metadata")  # ask a model (paused by watch.pause, gated by memory)
RETRY_S = (3600, 4 * 3600, 16 * 3600)  # after the 1st, 2nd, 3rd failure; the 4th gives up
MAX_ATTEMPTS = len(RETRY_S) + 1


class Queue:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA busy_timeout = 5000")
            c.executescript(SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: queue schema v{row[0]}, this papyrus has v{SCHEMA_VERSION} "
                                   "(it holds only retries and timings: delete it)")
            self._conn = c
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def sync(self, pending: dict[str, Iterable[int]]) -> None:
        """Mirror the library's pending ids, per stage given (stages left out are untouched)."""
        now = self.clock()
        c = self.conn
        c.execute("BEGIN")
        try:
            # stages of an older papyrus (e.g. 'triage', split into classify + metadata): done
            c.execute(f"UPDATE items SET state='done', updated_at=? WHERE state<>'done' AND stage NOT IN "
                      f"({','.join('?' * len(STAGES))})", (now, *STAGES))
            for stage, ids in pending.items():
                ids = set(ids)
                known: dict[int, str] = {}
                chunk = sorted(ids)
                for k in range(0, len(chunk), 500):
                    part = chunk[k:k + 500]
                    known.update(c.execute(f"SELECT item_id, state FROM items WHERE stage=? AND item_id IN "
                                           f"({','.join('?' * len(part))})", (stage, *part)).fetchall())
                for i in ids:
                    state = known.get(i)
                    if state is None:
                        c.execute("INSERT INTO items(stage, item_id, state, enqueued_at, updated_at)"
                                  " VALUES (?,?, 'pending', ?, ?)", (stage, i, now, now))
                    elif state == "done":
                        c.execute("UPDATE items SET state='pending', attempts=0, next_at=NULL, last_error=NULL,"
                                  " enqueued_at=?, updated_at=?, done_at=NULL, duration_ms=NULL"
                                  " WHERE stage=? AND item_id=?", (now, now, stage, i))
                for (i,) in c.execute("SELECT item_id FROM items WHERE stage=? AND state IN ('pending', 'failed')",
                                      (stage,)).fetchall():
                    if i not in ids:
                        c.execute("UPDATE items SET state='done', updated_at=?, done_at=?, duration_ms=NULL"
                                  " WHERE stage=? AND item_id=?", (now, now, stage, i))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def next(self, stages: Iterable[str] = STAGES) -> tuple[str, int] | None:
        """The next pending item that is due, in STAGES order, then by id."""
        stages = [s for s in STAGES if s in set(stages)]
        if not stages:
            return None
        order = " ".join(f"WHEN '{s}' THEN {n}" for n, s in enumerate(STAGES))
        r = self.conn.execute(
            f"SELECT stage, item_id FROM items WHERE state='pending' AND stage IN ({','.join('?' * len(stages))})"
            f" AND (next_at IS NULL OR next_at <= ?) ORDER BY CASE stage {order} END, item_id LIMIT 1",
            (*stages, self.clock())).fetchone()
        return (r["stage"], r["item_id"]) if r else None

    def done(self, stage: str, item_id: int, duration_ms: int | None) -> None:
        now = self.clock()
        self.conn.execute("UPDATE items SET state='done', next_at=NULL, last_error=NULL, updated_at=?, done_at=?,"
                          " duration_ms=? WHERE stage=? AND item_id=?", (now, now, duration_ms, stage, item_id))

    def retry(self, stage: str, item_id: int, error: str) -> float | None:
        """A failed or fruitless attempt. Returns the seconds until the next try, None if it gave up."""
        r = self.conn.execute("SELECT attempts FROM items WHERE stage=? AND item_id=?", (stage, item_id)).fetchone()
        attempts = (r["attempts"] if r else 0) + 1
        now = self.clock()
        if attempts >= MAX_ATTEMPTS:
            self.conn.execute("UPDATE items SET state='failed', attempts=?, last_error=?, next_at=NULL, updated_at=?"
                              " WHERE stage=? AND item_id=?", (attempts, error, now, stage, item_id))
            return None
        delay = RETRY_S[attempts - 1]
        self.conn.execute("UPDATE items SET state='pending', attempts=?, last_error=?, next_at=?, updated_at=?"
                          " WHERE stage=? AND item_id=?", (attempts, error, now + delay, now, stage, item_id))
        return delay

    def retry_failed(self) -> int:
        """Give every failed item another round of attempts."""
        return self.conn.execute("UPDATE items SET state='pending', attempts=0, next_at=NULL, updated_at=?"
                                 " WHERE state='failed'", (self.clock(),)).rowcount

    def counts(self) -> dict[str, int]:
        """Pending items per stage (due or waiting for a retry), and `failed`."""
        out = {s: 0 for s in STAGES}
        for r in self.conn.execute("SELECT stage, count(*) FROM items WHERE state='pending' GROUP BY stage"):
            out[r[0]] = r[1]
        out["failed"] = self.conn.execute("SELECT count(*) FROM items WHERE state='failed'").fetchone()[0]
        return out

    def median_s(self, stage: str, last: int = 50) -> float | None:
        """Median seconds of the last `last` steps of `stage` that the watcher finished."""
        ms = [r[0] for r in self.conn.execute("SELECT duration_ms FROM items WHERE stage=? AND duration_ms IS NOT NULL"
                                              " ORDER BY done_at DESC LIMIT ?", (stage, last))]
        return statistics.median(ms) / 1000 if ms else None

    def eta_s(self, per_item: dict[str, float | None] | None = None) -> float | None:
        """Seconds of work left: pending items per stage times an estimate per item (`per_item`,
        else the median of the stage's history). None while a stage with pending items has no
        estimate yet: a sum over the others would look small and be wrong."""
        per_item = per_item or {}
        total = 0.0
        for stage, n in self.counts().items():
            if stage in ("failed", "triage") or not n:
                continue
            est = per_item.get(stage)
            est = self.median_s(stage) if est is None else est
            if est is None:
                return None
            total += n * est
        return round(total, 1)
