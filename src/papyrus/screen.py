"""The screen: a fast first stage of triage that sets obvious personal paperwork aside.

Triage asks a 4B language model about every new file (~1 s for the classifier alone, 12-30 s with
extraction). The screen embeds what it sees of a file first, with a small embedding model
(`llm/embed.py`, nomic-embed-text v1.5, ~30 ms), and scores it with a logistic regression:

    input   version 3: triage's own state (`triage.build_state`), the text the classifier and the
            extraction model read. Earlier weights used inputs of their own: version 1
            "File name: …\\nFolder: ~/…\\nSize: N KB\\nDownloaded from: host\\nFirst words: <128 words>",
            version 2 the same with "Pages: N" and "PDF metadata: title=…; creator=…" before the
            first words. The weights say which version they were trained on (`input_version`, 1
            if absent).
    p       sigmoid(w · e/|e| + b), e the embedding of "classification: " + input

When p >= the threshold (default: the weights' own, 0.9), triage stops there: the file gets a
`screen` claim ({p_personal, threshold, rejected: true}, source `screen:<backend>:<model>`), no
extraction and no classifier, and promote *holds* it (no work, never served, its first pages kept
for review: `papyrus purge --dry-run` lists it, `purge ID` erases it, `keep ID` sends it through
full triage). It never purges on its own. Below the threshold triage goes on as before, and the
claim (rejected: false) is kept for evaluation.

The screen is skipped for files without a text layer (from the name alone it misjudged library
files), for files the user has judged (`keep`), with `papyrus triage --no-screen`, and when it
is turned off (`[screen] enabled = false`, `PAPYRUS_SCREEN=0`) or the embedding model is
unavailable: it only ever saves work.

The production weights are `data/screen.bin` (in the package), trained by
`tools/screen/train.py` on the files of triaged libraries (exported by `tools/screen/export.py`)
and committed; the format is in `write_weights`. They hold a direction and a bias, no text: the
standardisation is folded into them.
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

PREFIX = "classification: "
WORDS = 128
MAGIC = b"PAPYSCR1"
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
    from .pdf import TRIAGE_PAGES

    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    text = "\n".join(r[0] for r in conn.execute(
        "SELECT text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page", (file_id, TRIAGE_PAGES)))
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
    w: tuple[float, ...]
    b: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def threshold(self) -> float:
        return float(self.meta.get("threshold", 0.9))

    @property
    def input_version(self) -> int:
        return int(self.meta.get("input_version", 1))

    def p(self, embedding: list[float]) -> float:
        """P(personal) for an embedding (L2-normalised here)."""
        if len(embedding) != len(self.w):
            raise ValueError(f"embedding has {len(embedding)} dimensions, the weights {len(self.w)}")
        norm = math.sqrt(sum(x * x for x in embedding)) or 1.0
        z = sum(a * x for a, x in zip(self.w, embedding)) / norm + self.b
        return 1.0 / (1.0 + math.exp(-z)) if z >= -700 else 0.0


def write_weights(path: Path, w: list[float], b: float, meta: dict[str, Any]) -> None:
    """MAGIC, u32 header length, header JSON (utf-8; `dim`, `bias`, `model`, `prefix`, `words`,
    `threshold`, training counts and metrics), then `dim` little-endian float32 weights."""
    header = json.dumps({**meta, "dim": len(w), "bias": float(b)}, sort_keys=True).encode()
    a = array("f", w)
    if sys.byteorder != "little":
        a.byteswap()
    path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header + a.tobytes())


def read_weights(path: Path | None = None) -> Weights:
    data = (path or weights_path()).read_bytes()
    if data[:8] != MAGIC:
        raise ValueError("not a screen weights file")
    (n,) = struct.unpack("<I", data[8:12])
    meta = json.loads(data[12:12 + n])
    a = array("f")
    a.frombytes(data[12 + n:])
    if sys.byteorder != "little":
        a.byteswap()
    if len(a) != meta["dim"]:
        raise ValueError(f"screen weights: {len(a)} values, header says {meta['dim']}")
    return Weights(tuple(a), float(meta["bias"]), meta)


# --- screening a file ---------------------------------------------------------------------------


@dataclass
class Screen:
    """An embedder (usually behind the inference cache) and the weights."""

    embedder: Any
    weights: Weights
    threshold: float

    @property
    def tag(self) -> str:
        return "screen:" + self.embedder.info().tag

    def p_personal(self, text: str) -> tuple[float, str | None]:
        """(P(personal), inference call id) for a screen input."""
        from . import inference

        (e,) = self.embedder.embed([PREFIX + text])
        return self.weights.p(e), inference.call_of(self.embedder)


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
    return Screen(emb, weights, threshold if threshold is not None else cfg.threshold or weights.threshold)


def screen_file(conn: sqlite3.Connection, file_id: int, screen: Screen, run_id: int | None = None
                ) -> dict[str, Any] | None:
    """Screen one pending file and record the claim. Returns {p_personal, threshold, rejected,
    screen_s}, or None when the screen does not apply (no text layer, the user has judged it).
    A rejected file is marked triaged; the caller stops there."""
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
        p, call_id = screen.p_personal(text)
    value = {"p_personal": round(p, 4), "threshold": screen.threshold, "rejected": p >= screen.threshold}
    with db.tx(conn):
        db.assert_(conn, "file", file_id, "screen", value, source=screen.tag, confidence=round(p, 4),
                   run_id=run_id, call_id=call_id)
        if value["rejected"]:
            conn.execute("UPDATE files SET triage_status='done' WHERE id=?", (file_id,))
    return {**value, "screen_s": round(time.monotonic() - t0, 3)}


def rejected(claims: dict[str, list[dict]]) -> float | None:
    """P(personal) of the screen claim that set this file aside, if one did."""
    for c in claims.get("screen") or []:
        if c["source"].startswith("screen:") and isinstance(c["value"], dict) and c["value"].get("rejected"):
            return float(c["value"].get("p_personal", 1.0))
    return None
