"""The screen: the classify stage's fast first step. It sets obvious personal paperwork aside and,
with per-kind weights, settles the kind of the documents it is sure about, so that the classifier
(a 4B language model, ~1-4 s a file) is asked only about the rest.

It embeds what it sees of a file with a small embedding model (`llm/embed.py`, through LM Studio)
and scores the embedding with a logistic regression:

    input   version 3: the document description (`describe.build_state`), the text the classifier
            and the extraction model read. Earlier weights used inputs of their own: version 1
            "File name: …\\nFolder: ~/…\\nSize: N KB\\nDownloaded from: host\\nFirst words: <128 words>",
            version 2 the same with "Pages: N" and "PDF metadata: title=…; creator=…" before the
            first words. The weights say which version they were trained on (`input_version`, 1
            if absent).
    e       the embedding of the weights' `prefix` (the model's task instruction) + input,
            L2-normalised
    p       per-kind weights (`classes`, the classifier's KINDS): softmax(W e + b), a distribution
            over the kinds, P(personal) among them; one-row (binary) weights: sigmoid(w · e + b) =
            P(personal) alone

Three outcomes, recorded in a `screen` claim ({p_personal, threshold, rejected, and with per-kind
weights kind, p_kind, accept, accepted, kind_probs}; source `screen:<backend>:<model>`):

* P(personal) >= the threshold (default: the weights' own, 0.9): *set aside*. Classifying stops
  there (no classifier, no extraction) and promote *holds* the file (no work, never served, its
  first pages kept for review: `papyrus purge --dry-run` lists it, `purge ID` erases it, `keep ID`
  sends it through the classifier and extraction). The screen never purges on its own.
* the most likely kind is not personal and its P >= the accept threshold (per-kind weights only;
  default: the weights' own `accept_threshold`, `[screen] accept`): *accepted*. classify.py
  records the screen's `kind` and `kind_probs` (source `screen:…`) as the classifier would, and the
  file goes on to extraction without the classifier.
* otherwise the classifier is asked as before; the claim is kept for evaluation.

The screen is skipped for files without a text layer (from the name alone it misjudged library
files), for files the user has judged (`keep`), with `papyrus classify --no-screen`, and when it
is turned off (`[screen] enabled = false`, `PAPYRUS_SCREEN=0`) or the embedding model is
unavailable: it only ever saves work.

The production weights are `data/screen.bin` (in the package), trained by
`tools/screen/train.py` on the files of classified libraries (exported by `tools/screen/export.py`,
and sorted folders by `tools/screen/folders.py`) and committed; the format is in `write_weights`.
They hold directions and biases, no text: the standardisation is folded into them.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import sys
from array import array
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import db

PREFIX = "classification: "  # nomic-embed-text's task prefix: weights without their own `prefix`
WORDS = 128
MAGIC = b"PAPYSCR1"  # one row: P(personal)
MAGIC_KINDS = b"PAPYSCR2"  # a row per kind
HOME = re.compile(r"^(/Users/[^/]+|/home/[^/]+)(?=/|$)")


def weights_path() -> Path:
    return Path(str(resources.files("papyrus").joinpath("data", "screen.bin")))


# --- the input ----------------------------------------------------------------------------------


def home_relative(path: str) -> str:
    """'/Users/x/Downloads' -> '~/Downloads' (any user's home, so that libraries on other
    machines give the same inputs)."""
    return HOME.sub("~", path)


def host_of(url: str | None) -> str | None:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else None


META_FIELDS = ("title", "author", "subject", "keywords", "creator", "producer")


def screen_input(name: str, folder: str, size_bytes: int, host: str | None, text: str, *, version: int = 1,
                 pages: int | None = None, meta: dict[str, Any] | None = None) -> str:
    """What the screen sees of a file (the text it embeds, without the task prefix). Version 2
    adds the page count and the PDF's own metadata."""
    from .describe import _cap

    lines = [f"File name: {name}", f"Folder: {folder}", f"Size: {round(size_bytes / 1024)} KB"]
    if host:
        lines.append(f"Downloaded from: {host}")
    if version >= 2:
        if pages:
            lines.append(f"Pages: {pages}")
        fields = [f"{k}={_cap(str(meta[k]))}" for k in META_FIELDS if meta and meta.get(k)]
        if fields:
            lines.append("PDF metadata: " + "; ".join(fields))
    lines.append("First words: " + (" ".join(text.split()[:WORDS]) or "(no text layer)"))
    return "\n".join(lines)


def file_input(conn: sqlite3.Connection, file_id: int, version: int = 1) -> str | None:
    """The screen's input for a file in the library, or None if it has no text to go on (none in
    the pages scanning read)."""
    from .pdf import FIRST_PAGES

    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    text = "\n".join(r[0] for r in conn.execute(
        "SELECT text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page", (file_id, FIRST_PAGES)))
    if f is None or not text.strip():
        return None
    if version >= 3:
        from .describe import build_state

        return build_state(conn, file_id)
    c = conn.execute(
        "SELECT c.kind, c.locator, c.extra FROM context_refs r JOIN contexts c ON c.id=r.context_id"
        " WHERE r.file_id=? AND c.kind IN ('filesystem', 'download')"
        " ORDER BY c.kind='filesystem' DESC, c.missing_at IS NOT NULL, c.occurred_at", (file_id,)).fetchone()
    extra = json.loads(c["extra"] or "{}") if c else {}
    path = c["locator"] if c and c["kind"] == "filesystem" else None
    name = extra.get("filename") or (Path(path).name if path else "unknown.pdf")
    folder = home_relative(str(Path(path).parent)) if path else "~/Downloads"
    return screen_input(name, folder, f["size"] or 0, host_of((extra.get("where_froms") or [None])[0]), text,
                        version=version, pages=f["page_count"], meta={k: f[f"pdf_{k}"] for k in META_FIELDS})


# --- the weights --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Weights:
    """`w`: one row (P(personal)) or a row per class (`meta['classes']`); `b`: a bias per row."""

    w: tuple[tuple[float, ...], ...]
    b: tuple[float, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def classes(self) -> list[str]:
        return list(self.meta.get("classes") or ["personal"])

    @property
    def kinds(self) -> bool:
        """Per-kind weights (a distribution over the kinds), not P(personal) alone."""
        return len(self.w) > 1

    @property
    def threshold(self) -> float:
        return float(self.meta.get("threshold", 0.9))

    @property
    def accept_threshold(self) -> float | None:
        """P(kind) at which the screen's kind is taken without the classifier (per-kind weights)."""
        t = self.meta.get("accept_threshold")
        return float(t) if t is not None and self.kinds else None

    @property
    def input_version(self) -> int:
        return int(self.meta.get("input_version", 1))

    @property
    def prefix(self) -> str:
        """The task instruction embedded before the input (the model's own convention)."""
        return self.meta.get("prefix", PREFIX)

    def probs(self, embedding: list[float]) -> dict[str, float]:
        """Class -> P for an embedding (L2-normalised here); one-row weights: {'personal': P}."""
        if len(embedding) != len(self.w[0]):
            raise ValueError(f"embedding has {len(embedding)} dimensions, the weights {len(self.w[0])}")
        norm = math.sqrt(sum(x * x for x in embedding)) or 1.0
        z = [sum(a * x for a, x in zip(row, embedding)) / norm + b for row, b in zip(self.w, self.b)]
        if not self.kinds:
            return {"personal": 1.0 / (1.0 + math.exp(-z[0])) if z[0] >= -700 else 0.0}
        m = max(z)
        e = [math.exp(v - m) for v in z]
        s = sum(e)
        return {c: v / s for c, v in zip(self.classes, e)}

    def p(self, embedding: list[float]) -> float:
        """P(personal) for an embedding."""
        return self.probs(embedding).get("personal", 0.0)


def write_weights(path: Path, w: list[float] | list[list[float]], b: float | list[float],
                  meta: dict[str, Any]) -> None:
    """One row (P(personal)): MAGIC, u32 header length, header JSON (utf-8; `dim`, `bias`, `model`,
    `prefix`, `threshold`, training counts and metrics), then `dim` little-endian float32 weights.
    A row per class: MAGIC_KINDS, the same header with `classes` and `bias` a list (one per class),
    then len(classes) x dim float32, row by row."""
    rows = [list(w)] if not w or not isinstance(w[0], (list, tuple)) else [list(r) for r in w]  # type: ignore[arg-type]
    biases = [float(b)] if isinstance(b, (int, float)) else [float(x) for x in b]
    if len(rows) != len(biases) or len({len(r) for r in rows}) != 1:
        raise ValueError("screen weights: one bias per row, rows of one length")
    if len(rows) > 1 and len(meta.get("classes") or []) != len(rows):
        raise ValueError("screen weights: per-kind weights need `classes`, one per row")
    one = len(rows) == 1
    header = json.dumps({**meta, "dim": len(rows[0]), "bias": biases[0] if one else biases},
                        sort_keys=True).encode()
    a = array("f", [x for r in rows for x in r])
    if sys.byteorder != "little":
        a.byteswap()
    path.write_bytes((MAGIC if one else MAGIC_KINDS) + struct.pack("<I", len(header)) + header + a.tobytes())


def read_weights(path: Path | None = None) -> Weights:
    data = (path or weights_path()).read_bytes()
    if data[:8] not in (MAGIC, MAGIC_KINDS):
        raise ValueError("not a screen weights file")
    (n,) = struct.unpack("<I", data[8:12])
    meta = json.loads(data[12:12 + n])
    a = array("f")
    a.frombytes(data[12 + n:])
    if sys.byteorder != "little":
        a.byteswap()
    dim = meta["dim"]
    biases = [meta["bias"]] if data[:8] == MAGIC else list(meta["bias"])
    if len(a) != dim * len(biases):
        raise ValueError(f"screen weights: {len(a)} values, header says {len(biases)} x {dim}")
    rows = tuple(tuple(a[i * dim:(i + 1) * dim]) for i in range(len(biases)))
    return Weights(rows, tuple(float(x) for x in biases), meta)


# --- screening a file ---------------------------------------------------------------------------


@dataclass
class Screen:
    """An embedder (usually behind the inference cache), the weights and the two thresholds:
    P(personal) to set a file aside, and P(kind) to accept the screen's kind (None: never)."""

    embedder: Any
    weights: Weights
    threshold: float
    accept: float | None = None

    @property
    def tag(self) -> str:
        return "screen:" + self.embedder.info().tag

    def score(self, text: str) -> tuple[dict[str, float], str | None]:
        """(class -> P, inference call id) for a screen input."""
        from . import inference

        (e,) = self.embedder.embed([self.weights.prefix + text])
        return self.weights.probs(e), inference.call_of(self.embedder)

    def p_personal(self, text: str) -> tuple[float, str | None]:
        """(P(personal), inference call id) for a screen input."""
        probs, call = self.score(text)
        return probs.get("personal", 0.0), call

    def decide(self, probs: dict[str, float]) -> dict[str, Any]:
        """The claim's value for a distribution: set aside, accepted, or neither."""
        p = probs.get("personal", 0.0)
        value: dict[str, Any] = {"p_personal": round(p, 4), "threshold": self.threshold,
                                 "rejected": p >= self.threshold}
        if self.weights.kinds:
            kind = max(probs, key=probs.__getitem__)
            value.update(kind=kind, p_kind=round(probs[kind], 4), accept=self.accept,
                         accepted=(not value["rejected"] and kind != "personal" and self.accept is not None
                                   and probs[kind] >= self.accept),
                         kind_probs={k: round(v, 6) for k, v in probs.items()})
        return value


def get_screen(threshold: float | None = None, weights: Weights | None = None) -> Screen | None:
    """The configured screen, or None when it is turned off or its model is unavailable."""
    from . import config
    from .llm.embed import LMStudioEmbedder

    cfg = config.screen()
    if not cfg.enabled:
        return None
    weights = weights or read_weights()
    emb = LMStudioEmbedder(model=weights.meta.get("model"))
    if not emb.available():
        return None
    accept = None if cfg.accept is False or not weights.kinds else (
        weights.accept_threshold if cfg.accept is None else float(cfg.accept))
    return Screen(emb, weights, threshold if threshold is not None else cfg.threshold or weights.threshold, accept)


def screen_file(conn: sqlite3.Connection, file_id: int, screen: Screen, run_id: int | None = None
                ) -> dict[str, Any] | None:
    """Screen one pending file and record the claim. Returns the claim's value (`decide`) with
    `screen_s` and `call_id`, or None when the screen does not apply (no text layer, the user has
    judged it). A rejected file is marked classified and its extraction skipped; the caller stops
    there. An accepted one is left to the caller (classify.classify_file records its kind)."""
    import time

    from . import inference
    from .purge import user_says

    if user_says(conn, file_id) is not None:
        return None
    text = file_input(conn, file_id, screen.weights.input_version)
    if text is None:
        return None
    sha = conn.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()[0]
    t0 = time.monotonic()
    with inference.subject(sha):
        probs, call_id = screen.score(text)
    value = screen.decide(probs)
    with db.tx(conn):
        db.assert_(conn, "file", file_id, "screen", value, source=screen.tag, confidence=value["p_personal"],
                   run_id=run_id, call_id=call_id)
        if value["rejected"]:
            conn.execute("UPDATE files SET classify_status='done', extract_status='skipped' WHERE id=?", (file_id,))
    return {**value, "screen_s": round(time.monotonic() - t0, 3), "call_id": call_id}


def rejected(claims: dict[str, list[dict]]) -> float | None:
    """P(personal) of the screen claim that set this file aside, if one did."""
    for c in claims.get("screen") or []:
        if c["source"].startswith("screen:") and isinstance(c["value"], dict) and c["value"].get("rejected"):
            return float(c["value"].get("p_personal", 1.0))
    return None
