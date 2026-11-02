"""SQLite access: connection, schema bootstrap, small helpers."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "0.5"  # 0.x until the schema is stable (1.0); an old bare "4" reads as 0.4


def version(v: object) -> tuple[int, ...]:
    """'0.5' -> (0, 5); a bare '4' (before 0.x numbering) -> (0, 4)."""
    parts = tuple(int(x) for x in str(v).split("."))
    return (0, *parts) if len(parts) == 1 else parts


# Files whose model stages are over: classified, and extracted or skipped (not library material).
MODELS_DONE = "classify_status = 'done' AND extract_status IN ('done', 'skipped')"


# A variant id: lowercase letters, digits and single dashes (as DeepSea's). No dots, so the
# variant name `io.github.taliesinb.papyrus.<id>` and its components (`.server`) never collide.
VARIANT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_VARIANT = "release"


def papyrus_root() -> Path:
    """Where every variant's home lives: `PAPYRUS_ROOT`, default `~/.papyrus`."""
    return Path(os.environ.get("PAPYRUS_ROOT") or Path.home() / ".papyrus").expanduser()


def variant() -> str:
    """The variant this process works on: `PAPYRUS_VARIANT`, default `release`."""
    v = os.environ.get("PAPYRUS_VARIANT", "").strip() or DEFAULT_VARIANT
    if not VARIANT_ID.match(v):
        raise ValueError(f"PAPYRUS_VARIANT={v!r} is not a variant id (lowercase letters, digits and single dashes)")
    return v


def library_home() -> Path:
    """The library: `PAPYRUS_HOME` if set, else the variant's home `<root>/<variant>`."""
    if os.environ.get("PAPYRUS_HOME"):
        return Path(os.environ["PAPYRUS_HOME"]).expanduser()
    return papyrus_root() / variant()


def default_db_path() -> Path:
    return Path(os.environ.get("PAPYRUS_DB", library_home() / "papyrus.sqlite")).expanduser()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; use tx() for batches
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create the schema in a new database; refuse one made by another schema version.

    The version is checked before the DDL runs, so an old database is never half-upgraded
    (CREATE ... IF NOT EXISTS would add new tables but not new columns). There are no automatic
    migrations: papyrus has one user, who migrates by hand when SCHEMA_VERSION changes."""
    row = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is not None and version(row[0]) != version(SCHEMA_VERSION):
        newer = version(row[0]) > version(SCHEMA_VERSION)
        raise RuntimeError(f"database schema v{row[0]} is {'newer' if newer else 'older'} than this "
                           f"papyrus (v{SCHEMA_VERSION})" + ("" if newer else "; migrate it first"))
    conn.executescript(resources.files("papyrus").joinpath("schema.sql").read_text())
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def start_run(conn: sqlite3.Connection, command: str, args: dict | None = None,
              backend: str | None = None, prompt_version: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(command, args, backend, prompt_version) VALUES (?,?,?,?)",
        (command, dumps(args or {}), backend, prompt_version),
    )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, stats: dict) -> None:
    conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?", (now_iso(), dumps(stats), run_id))


@contextmanager
def recorded_run(conn: sqlite3.Connection, command: str, args: dict | None = None,
                 backend: str | None = None, prompt_version: str | None = None) -> Iterator[tuple[int, dict]]:
    """A `runs` row that is always closed: stats gets status completed | interrupted | crashed: ..."""
    from . import services

    run_id = start_run(conn, command, args, backend, prompt_version)
    # the web services store's calls name the run they belong to (and its library: runs are per library)
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    prev_run = services.set_run(run_id, str(Path(db_file).parent) if db_file else None)
    stats: dict = {}
    status = "completed"
    try:
        yield run_id, stats
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except BaseException as e:
        status = f"crashed: {type(e).__name__}: {e}"[:300]
        raise
    finally:
        services.set_run(*prev_run)
        finish_run(conn, run_id, {**stats, "status": status})


def assert_(conn: sqlite3.Connection, subject_type: str, subject_id: int, field: str, value: Any,
            source: str, confidence: float | None = None, run_id: int | None = None,
            supersede: bool = True, call_id: str | None = None) -> int:
    """Record a claim. By default supersedes earlier claims from the same source. `call_id`: the
    model call it came from (inference.py)."""
    if supersede:
        conn.execute(
            "UPDATE assertions SET superseded_at=? WHERE subject_type=? AND subject_id=? AND field=?"
            " AND source=? AND superseded_at IS NULL",
            (now_iso(), subject_type, subject_id, field, source),
        )
    cur = conn.execute(
        "INSERT INTO assertions(subject_type, subject_id, field, value, confidence, source, run_id, call_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (subject_type, subject_id, field, dumps(value), confidence, source, run_id, call_id),
    )
    return cur.lastrowid


def current_assertions(conn: sqlite3.Connection, subject_type: str, subject_id: int) -> dict[str, list[dict]]:
    """field -> list of live claims (newest first), each {value, confidence, source, created_at}."""
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT field, value, confidence, source, created_at FROM assertions"
        " WHERE subject_type=? AND subject_id=? AND superseded_at IS NULL ORDER BY id DESC",
        (subject_type, subject_id),
    ):
        out.setdefault(r["field"], []).append(
            {"value": json.loads(r["value"]), "confidence": r["confidence"],
             "source": r["source"], "created_at": r["created_at"]}
        )
    return out
