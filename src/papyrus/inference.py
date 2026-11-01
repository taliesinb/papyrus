"""The inference cache: every question asked of a model, in a database of its own.

    ask this model (models)  this question (queries)  about this data (input sha256)  →  calls

`<library home>/inference.sqlite` (DDL: inference.sql) holds hashes, sizes and timings; the
texts are files beside it, named by their hash (`inference/inputs/<hash>.txt`,
`inference/outputs/<hash>.json`, one flat directory each). So the database stays free of document text, the files can
be deleted whenever (`papyrus inference prune`), a repeated question is answered from the cache,
and the timings accumulate into estimates of what the next call will cost.

Use: wrap a Backend, Classifier or Embedder (`cached_backend`, `cached_classifier`, `cached_embedder`).
The wrapper implements the
same contract (llm/base.py) and adds:

* **the cache**: (model, query, input) answered by the newest successful call whose output file
  still exists (`fresh=True` asks the model anyway). A classifier is cached per question, so a new
  question reuses the answers to the old ones;
* **a seed** (default 0), part of the query: another seed is another answer. It is passed to
  backends that take one; whether the model honours it is `models.seed_honoured` (LM Studio's
  MLX engine does not: same seed, different answers, measured 2026-11-01);
* **`before_call`**: run only on a miss, just before the model is asked (`papyrus watch` checks
  memory pressure and LM Studio there, so a cache hit costs neither);
* **the call id** of the answer, as `last_call` (backend) / `last_calls[key]` (classifier), which
  triage stores in `assertions.call_id`; on a hit, it is the original call's id;
* **the subject**: the sha256 of the document the call is about, set with `with subject(sha):`.
  It is what `purge` follows to delete every input and output file about a document.

Model identity is {backend, model, build}: not the endpoint. The build comes from the backend's
`model_build()` (LM Studio: quantization, arch, format; Apple: the OS release). The query is
{kind, instructions, output_schema, examples, params}, params including the backend's own
`cache_params()` (its prompt layout version, classifier method, guardrails…), so a change to any of
them is a new query. Every id and hash is the first 16 hex digits (64 bits) of a sha256, of
canonical JSON for models and queries (key order kept: a schema's property order shapes the
answer). Should two different texts ever share those digits, the second file is named by its
full sha256. The `subject` is the document's full `files.sha256`.

Errors are recorded too (status busy | unavailable | interrupted | error, the exception's type
only), and never served from the cache.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import inspect
import json
import os
import socket
import sqlite3
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterator

from . import db
from .llm.base import Answer, BackendUnavailable, ModelBusy, ModelInfo, Question

SCHEMA_VERSION = 2  # 2: 16-digit ids and hashes, flat directories, calls.input_hash/output_hash
HASH_CHARS = 16
_subject: contextvars.ContextVar[str | None] = contextvars.ContextVar("papyrus_inference_subject", default=None)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def short_hash(data: bytes) -> str:
    """The first HASH_CHARS hex digits of the sha256 of `data`."""
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS]


@contextlib.contextmanager
def subject(document_sha256: str | None) -> Iterator[None]:
    """Calls made inside are about this document (files.sha256)."""
    token = _subject.set(document_sha256)
    try:
        yield
    finally:
        _subject.reset(token)


def call_of(backend: Any) -> str | None:
    """The call that produced the backend's last answer (None if it isn't a cached wrapper)."""
    return getattr(backend, "last_call", None)


def calls_of(classifier: Any) -> dict[str, str | None]:
    return dict(getattr(classifier, "last_calls", None) or {})


# --- the store ----------------------------------------------------------------------------------


class Store:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "inference.sqlite"
        self.root = self.home / "inference"
        self._conn: sqlite3.Connection | None = None
        self._known: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.home.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA busy_timeout = 5000")
            row = None
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: inference schema v{row[0]}, this papyrus has v{SCHEMA_VERSION}")
            c.executescript(resources.files("papyrus").joinpath("inference.sql").read_text())
            if row is None:
                c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._conn = c
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # blobs

    def blob(self, kind: str, sha: str) -> Path:
        return self.root / f"{kind}s" / (f"{sha}.txt" if kind == "input" else f"{sha}.json")

    def _write(self, kind: str, data: bytes) -> str:
        """Store a text under its hash; returns the hash (the full sha256 if the short one is taken
        by other bytes)."""
        sha = short_hash(data)
        p = self.blob(kind, sha)
        if p.exists() and p.read_bytes() != data:
            sha = hashlib.sha256(data).hexdigest()
            p = self.blob(kind, sha)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return sha

    # identities

    def model_id(self, backend: str, model: str, build: dict, seed_honoured: bool | None = None) -> str:
        mid = short_hash(canonical({"backend": backend, "model": model, "build": build}))
        if mid not in self._known:
            self.conn.execute("INSERT OR IGNORE INTO models(id, backend, model, build, seed_honoured, first_seen_at)"
                              " VALUES (?,?,?,?,?,?)", (mid, backend, model, json.dumps(build), seed_honoured,
                                                        db.now_iso()))
            self._known.add(mid)
        return mid

    def query_id(self, kind: str, instructions: str, output_schema: Any, examples: Any, params: dict) -> str:
        q = {"kind": kind, "instructions": instructions, "output_schema": output_schema, "examples": examples,
             "params": params}
        qid = short_hash(canonical(q))
        if qid not in self._known:
            self.conn.execute(
                "INSERT OR IGNORE INTO queries(id, kind, instructions, output_schema, examples, params, first_seen_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (qid, kind, instructions, canonical(output_schema).decode(),
                 None if examples is None else canonical(examples).decode(), canonical(params).decode(),
                 db.now_iso()))
            self._known.add(qid)
        return qid

    # calls

    def lookup(self, model_id: str, query_id: str, input_sha: str) -> tuple[Any, str] | None:
        """(output, call id) of the newest successful call whose output file still exists."""
        for r in self.conn.execute("SELECT id, output_hash FROM calls WHERE model_id=? AND query_id=?"
                                   " AND input_hash=? AND status='ok' ORDER BY started_at DESC",
                                   (model_id, query_id, input_sha)).fetchall():
            try:
                out = json.loads(self.blob("output", r["output_hash"]).read_bytes())
            except (OSError, ValueError):
                continue
            self.conn.execute("UPDATE calls SET hits=hits+1, last_hit_at=? WHERE id=?", (db.now_iso(), r["id"]))
            return out, r["id"]
        return None

    def start(self, model_id: str, query_id: str, text: str, *, subject: str | None = None,
              model_loaded: bool | None = None, memory_pressure: str | None = None,
              endpoint: str | None = None) -> str:
        data = text.encode()
        input_sha = self._write("input", data)
        call_id = uuid.uuid4().hex[:HASH_CHARS]
        self.conn.execute(
            "INSERT INTO calls(id, model_id, query_id, input_hash, input_chars, subject, status, started_at,"
            " model_loaded, memory_pressure, endpoint, host) VALUES (?,?,?,?,?,?, 'running', ?,?,?,?,?)",
            (call_id, model_id, query_id, input_sha, len(text), subject, db.now_iso(), model_loaded, memory_pressure,
             endpoint, socket.gethostname()))
        return call_id

    def finish(self, call_id: str, output: Any, duration_ms: int, usage: dict | None = None,
               extra: dict | None = None) -> None:
        data = canonical(output)
        out_sha = self._write("output", data)
        usage = usage or {}
        self.conn.execute(
            "UPDATE calls SET status='ok', output_hash=?, output_chars=?, finished_at=?, duration_ms=?,"
            " input_tokens=?, output_tokens=?, extra=? WHERE id=?",
            (out_sha, len(data.decode()), db.now_iso(), duration_ms,
             usage.get("prompt_tokens", usage.get("input_tokens")),
             usage.get("completion_tokens", usage.get("output_tokens")),
             json.dumps(extra) if extra else None, call_id))

    def fail(self, call_id: str, exc: BaseException, duration_ms: int) -> None:
        status = ("busy" if isinstance(exc, ModelBusy) else "unavailable" if isinstance(exc, BackendUnavailable)
                  else "interrupted" if isinstance(exc, KeyboardInterrupt) else "error")
        self.conn.execute("UPDATE calls SET status=?, error=?, finished_at=?, duration_ms=? WHERE id=?",
                          (status, type(exc).__name__, db.now_iso(), duration_ms, call_id))

    # forgetting

    def _unlink(self, rows: list[sqlite3.Row], inputs: bool = True, outputs: bool = True) -> int:
        n = 0
        for r in rows:
            for kind, sha in (("input", r["input_hash"] if inputs else None),
                              ("output", r["output_hash"] if outputs else None)):
                if sha:
                    p = self.blob(kind, sha)
                    if p.exists():
                        p.unlink()
                        n += 1
        return n

    def forget_subject(self, document_sha256: str) -> int:
        """Delete the input and output files of every call about this document (purge). The rows
        stay: hashes and timings. Returns the number of files deleted."""
        if not self.path.exists():
            return 0
        rows = self.conn.execute("SELECT input_hash, output_hash FROM calls WHERE subject=?",
                                 (document_sha256,)).fetchall()
        return self._unlink(rows)

    def prune(self, *, inputs: bool, outputs: bool, older_than_days: float = 0.0) -> int:
        """Delete input and/or output files of calls started more than `older_than_days` ago."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        rows = self.conn.execute("SELECT input_hash, output_hash FROM calls WHERE started_at <= ?",
                                 (cutoff,)).fetchall()
        return self._unlink(rows, inputs, outputs)

    # reading

    def show(self, call_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT c.*, m.backend, m.model, m.build, q.kind, q.instructions, q.output_schema, q.params"
            " FROM calls c JOIN models m ON m.id=c.model_id JOIN queries q ON q.id=c.query_id WHERE c.id LIKE ?",
            (call_id + "%",)).fetchone()
        if r is None:
            return None
        out = dict(r)
        for kind, key in (("input", "input_hash"), ("output", "output_hash")):
            p = self.blob(kind, r[key]) if r[key] else None
            out[kind] = p.read_text() if p and p.exists() else None
        return out

    def stats(self) -> list[dict]:
        """Per model and query: calls, cache hits, failures, and how long successful calls took."""
        groups: dict[tuple, dict] = {}
        for r in self.conn.execute(
                "SELECT c.model_id, c.query_id, m.backend, m.model, q.kind, c.status, c.duration_ms, c.input_chars,"
                " c.hits, c.model_loaded FROM calls c JOIN models m ON m.id=c.model_id"
                " JOIN queries q ON q.id=c.query_id ORDER BY c.started_at"):
            g = groups.setdefault((r["model_id"], r["query_id"]), {
                "model": f"{r['backend']}:{r['model']}", "model_id": r["model_id"][:12], "kind": r["kind"],
                "query_id": r["query_id"][:12], "calls": 0, "failed": 0, "hits": 0, "ms": [], "ms_per_kchar": []})
            g["calls"] += 1
            g["hits"] += r["hits"]
            if r["status"] != "ok":
                g["failed"] += r["status"] != "running"
            elif r["duration_ms"] is not None and r["model_loaded"] != 0:
                g["ms"].append(r["duration_ms"])
                g["ms_per_kchar"].append(1000 * r["duration_ms"] / max(1, r["input_chars"]))
        out = []
        for g in groups.values():
            ms, per = g.pop("ms"), g.pop("ms_per_kchar")
            g["median_s"] = round(statistics.median(ms) / 1000, 2) if ms else None
            g["p90_s"] = round(sorted(ms)[int(0.9 * (len(ms) - 1))] / 1000, 2) if ms else None
            g["s_per_kchar"] = round(statistics.median(per) / 1000, 3) if per else None
            out.append(g)
        return out

    def estimate_s(self, model_id: str, query_id: str, input_chars: int, last: int = 50) -> float | None:
        """Seconds the next call should take, from the median time per input character of the last
        `last` successful calls with the model already loaded."""
        rates = [r[0] / max(1, r[1]) for r in self.conn.execute(
            "SELECT duration_ms, input_chars FROM calls WHERE model_id=? AND query_id=? AND status='ok'"
            " AND duration_ms IS NOT NULL AND model_loaded IS NOT 0 ORDER BY started_at DESC LIMIT ?",
            (model_id, query_id, last))]
        return round(statistics.median(rates) * input_chars / 1000, 2) if rates else None


    def document_estimate_s(self, kind: str | None = None, last: int = 50) -> float | None:
        """Seconds the models take for one more typical document (only questions of `kind`, e.g.
        "classify" or "extract", if given): the questions asked about the
        most recently asked-about document (so the current models and prompts), each estimated by
        `estimate_s` at the median input length of its last `last` calls (the latest document's
        own length made it jumpy: a short one halved the estimate). None without history."""
        # the latest document that went through full triage (the screen alone, an embedding,
        # would make every document look like it takes 30 ms)
        r = self.conn.execute("SELECT c.subject FROM calls c JOIN queries q ON q.id=c.query_id WHERE c.status='ok'"
                              " AND c.subject IS NOT NULL AND q.kind<>'embed' ORDER BY c.started_at DESC LIMIT 1"
                              ).fetchone()
        if r is None:
            return None
        pairs = {(c[0], c[1]) for c in self.conn.execute(
            "SELECT c.model_id, c.query_id FROM calls c JOIN queries q ON q.id=c.query_id"
            " WHERE c.subject=? AND c.status='ok' AND (? IS NULL OR q.kind=?)", (r[0], kind, kind))}
        ests = []
        for m, q in pairs:
            chars = [c[0] for c in self.conn.execute(
                "SELECT input_chars FROM calls WHERE model_id=? AND query_id=? AND status='ok'"
                " ORDER BY started_at DESC LIMIT ?", (m, q, last))]
            ests.append(self.estimate_s(m, q, round(statistics.median(chars))))
        ests = [e for e in ests if e is not None]
        return round(sum(ests), 2) if ests else None


_stores: dict[Path, Store] = {}


def store() -> Store:
    """The inference store of the current library (db.library_home())."""
    home = db.library_home()
    if home not in _stores:
        _stores[home] = Store(home)
    return _stores[home]


# --- the wrappers -------------------------------------------------------------------------------


def _accepts_seed(fn: Callable) -> bool:
    try:
        return "seed" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class _Cached:
    def __init__(self, inner: Any, *, seed: int = 0, fresh: bool = False,
                 before_call: Callable[[], None] | None = None, pressure: Callable[[], str | None] | None = None,
                 store: Store | None = None):
        self.inner = inner
        self.seed, self.fresh = seed, fresh
        self.before_call, self.pressure = before_call, pressure
        self._store = store
        self._info: ModelInfo = inner.info()
        self.model = getattr(inner, "model", None) or self._info.model
        self._model_id: str | None = None
        self.last_hit = False

    def __getattr__(self, name: str) -> Any:  # base_url and the like; only called for missing attributes
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    @property
    def store(self) -> Store:
        return self._store or store()

    def info(self) -> ModelInfo:
        return self.inner.info()

    def available(self) -> bool:
        return self.inner.available()

    def _model(self) -> str | None:
        """The model's id, or None while its build can't be told (then calls are not cached)."""
        if self._model_id is None:
            build_fn = getattr(self.inner, "model_build", None)
            build = build_fn() if build_fn else {}
            if build is None:
                return None
            honoured = getattr(self.inner, "seed_honoured", None)
            self._model_id = self.store.model_id(self._info.backend, self.model, build,
                                                 honoured(build) if callable(honoured) else None)
        return self._model_id

    def _params(self, **params: Any) -> dict:
        own = getattr(self.inner, "cache_params", None)
        return {**params, "seed": self.seed, **(own() if callable(own) else {})}

    def _seed_kw(self, fn: Callable) -> dict:
        return {"seed": self.seed} if _accepts_seed(fn) else {}

    def _ask(self, query: Callable[[], str], text: str, call: Callable[[], Any]) -> tuple[Any, str | None]:
        model_id = self._model()
        qid = query() if model_id else None
        if model_id and not self.fresh:
            hit = self.store.lookup(model_id, qid, short_hash(text.encode()))
            if hit:
                self.last_hit = True
                return hit
        self.last_hit = False
        if self.before_call:
            self.before_call()
        if model_id is None:
            return call(), None
        loaded_fn = getattr(self.inner, "model_loaded", None)
        call_id = self.store.start(model_id, qid, text, subject=_subject.get(),
                                   model_loaded=loaded_fn() if loaded_fn else None,
                                   memory_pressure=self.pressure() if self.pressure else None,
                                   endpoint=getattr(self.inner, "base_url", None))
        t0 = time.monotonic()
        try:
            out = call()
        except BaseException as e:
            self.store.fail(call_id, e, round((time.monotonic() - t0) * 1000))
            raise
        self.store.finish(call_id, out, round((time.monotonic() - t0) * 1000),
                          getattr(self.inner, "last_usage", None), getattr(self.inner, "last_params", None))
        return out, call_id


class CachedBackend(_Cached):
    """A `Backend` whose extractions are cached and recorded."""

    last_call: str | None = None

    def extract(self, *, instructions: str, prompt: str, schema: dict[str, Any], max_tokens: int = 1024,
                temperature: float = 0.0) -> dict[str, Any]:
        def query() -> str:
            return self.store.query_id("extract", instructions, schema, None,
                                       self._params(temperature=temperature, max_tokens=max_tokens))

        kw = dict(instructions=instructions, prompt=prompt, schema=schema, max_tokens=max_tokens,
                  temperature=temperature, **self._seed_kw(self.inner.extract))
        out, self.last_call = self._ask(query, prompt, lambda: self.inner.extract(**kw))
        return out


class CachedClassifier(_Cached):
    """A `Classifier` whose answers are cached and recorded one question at a time."""

    def __init__(self, inner: Any, **kw: Any):
        super().__init__(inner, **kw)
        self.last_calls: dict[str, str | None] = {}

    def classify(self, state: str, questions: list[Question]) -> dict[str, Answer]:
        answers: dict[str, Answer] = {}
        self.last_calls = {}
        kw = self._seed_kw(self.inner.classify)
        for i, q in enumerate(questions):
            if hasattr(self.inner, "questions_ahead"):  # so it primes the server's cache only if worth it
                self.inner.questions_ahead = len(questions) - i
            def query(q: Question = q) -> str:
                return self.store.query_id("classify", q.instructions,
                                           {"key": q.key, "type": q.type, "criteria": q.criteria}, None,
                                           self._params())

            probs, call_id = self._ask(query, state,
                                       lambda q=q: self.inner.classify(state, [q], **kw)[q.key].probabilities)
            answers[q.key] = Answer(q.key, {k: float(v) for k, v in probs.items()})
            self.last_calls[q.key] = call_id
        return answers


class CachedEmbedder(_Cached):
    """An `Embedder` whose vectors are cached and recorded, one text per call."""

    last_call: str | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        def query() -> str:
            return self.store.query_id("embed", "", None, None, self._params())

        out = []
        for t in texts:
            v, self.last_call = self._ask(query, t, lambda t=t: self.inner.embed([t])[0])
            out.append([float(x) for x in v])
        return out


def cached_embedder(embedder: Any, **kw: Any) -> CachedEmbedder:
    return embedder if isinstance(embedder, CachedEmbedder) else CachedEmbedder(embedder, **kw)


def cached_backend(backend: Any, **kw: Any) -> CachedBackend:
    return backend if isinstance(backend, CachedBackend) else CachedBackend(backend, **kw)


def cached_classifier(classifier: Any, **kw: Any) -> CachedClassifier:
    return classifier if isinstance(classifier, CachedClassifier) else CachedClassifier(classifier, **kw)
