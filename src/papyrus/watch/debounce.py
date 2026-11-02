"""When is a new file finished? When it has stopped changing.

A path reported by a `sources` watcher becomes *pending*. Each `due()` re-reads its size, mtime
and ctime (ctime moves when a browser writes the download URL into an xattr); once they have
stayed the same for `settle_s` seconds, the file is handed out to be ingested.

Ignored:

* hidden files and anything that isn't `*.pdf`;
* browsers' partial files: Safari's `x.pdf.download` bundle, Chrome's `x.pdf.crdownload`,
  Firefox's `x.pdf.part`, and `.partial`/`.tmp`. The rename to `x.pdf` is an event of its own;
* a finished-looking `x.pdf` while `x.pdf.part` (or another partial) sits beside it, and an
  empty `x.pdf`: Firefox creates the final name empty and moves the `.part` over it at the end.

A path that no longer exists is reported as *gone*, so its filesystem context can be marked
missing (a directory under a tree: everything below it). Paths outside the watched directories
are ignored, and so are deeper ones, except under *trees*, directories watched with their
subdirectories (`[watch] dirs` entries ending in `/**`): there a file at any depth counts (as
for `papyrus scan -r`: not in hidden or skipped directories), and a directory reported (moved in,
or to be looked at again) is walked.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PARTIAL_SUFFIXES = (".download", ".crdownload", ".part", ".partial", ".tmp")

Sig = tuple[int, int, int]  # size, mtime_ns, ctime_ns


def is_candidate(name: str) -> bool:
    """A finished PDF's name: `*.pdf`, not hidden, not a browser's partial file."""
    low = name.lower()
    return not name.startswith(".") and low.endswith(".pdf") and not low.endswith(PARTIAL_SUFFIXES)


def _sig(path: Path) -> Sig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns


@dataclass
class _Pending:
    sig: Sig
    since: float  # when this signature was first seen


class Debouncer:
    def __init__(self, dirs: list[Path], settle_s: float = 3.0, clock: Callable[[], float] = time.monotonic,
                 trees: list[Path] = ()):
        self.dirs = {Path(d) for d in dirs}
        self.trees = [Path(t) for t in trees]
        self.settle_s = settle_s
        self.clock = clock
        self.pending: dict[Path, _Pending] = {}
        self._handed: dict[Path, Sig] = {}  # what was last handed out, so a directory rescan skips it
        self._gone: list[Path] = []

    def touch(self, path: Path) -> None:
        """A watcher reported `path` (a file, or a watched directory to look at again)."""
        path = Path(path)
        if path in self.dirs:
            try:
                names = os.listdir(path)
            except OSError:
                return
            for name in names:
                if is_candidate(name):
                    self._consider(path / name)
            return
        tree = self._tree_of(path)
        if tree is not None and not is_candidate(path.name):
            if path.is_dir():  # a directory moved in, or one to look at again: everything below it
                from ..scan import ScanOptions, TREE_DEPTH, iter_pdfs

                depth = TREE_DEPTH - (len(path.relative_to(tree).parts) - 1)
                for p in iter_pdfs(path, ScanOptions(recursive=True, max_depth=max(depth, 0))):
                    if is_candidate(p.name):
                        self._consider(p)
            elif not path.exists() and (path == tree or path.suffix == ""):
                self._gone.append(path)  # a directory went: what was below it went too
            return
        if (path.parent not in self.dirs and tree is None) or not is_candidate(path.name):
            return
        sig = _sig(path)
        if sig is None:
            self.pending.pop(path, None)
            self._handed.pop(path, None)
            if not path.exists():
                self._gone.append(path)
            return
        if self._handed.get(path) == sig:
            return  # unchanged since handed out: reading or hashing it fires events of its own
        self._add(path, sig)

    def _tree_of(self, path: Path) -> Path | None:
        """The tree `path` is in (or is), if any."""
        from ..scan import TREE_DEPTH, in_tree

        for t in self.trees:
            if path == t or in_tree(path, t, TREE_DEPTH):
                return t
        return None

    def _consider(self, p: Path) -> None:
        sig = _sig(p)
        if sig is not None and self._handed.get(p) != sig:
            self._add(p, sig)

    def seen(self, path: Path) -> None:
        """`path` was just ingested by other means (the catch-up scan): ignore its events until it
        changes."""
        sig = _sig(path)
        if sig is not None:
            self._handed[Path(path)] = sig

    def add(self, path: Path) -> None:
        """Wait for `path` to settle (e.g. found by a scan while it is still being written)."""
        sig = _sig(path)
        if sig is not None:
            self._add(Path(path), sig)

    def _add(self, path: Path, sig: Sig) -> None:
        p = self.pending.get(path)
        if p is None or p.sig != sig:
            self.pending[path] = _Pending(sig, self.clock())

    def due(self) -> tuple[list[Path], list[Path]]:
        """(files that have settled, paths that have gone). Both are handed out once."""
        now = self.clock()
        ready: list[Path] = []
        for path, p in list(self.pending.items()):
            sig = _sig(path)
            if sig is None:
                del self.pending[path]
                if not path.exists():
                    self._gone.append(path)
                continue
            if sig != p.sig:
                self.pending[path] = _Pending(sig, now)
                continue
            if now - p.since < self.settle_s:
                continue
            if any(path.with_name(path.name + s).exists() for s in PARTIAL_SUFFIXES):
                continue  # Firefox: the final name exists while x.pdf.part is still being written
            del self.pending[path]
            if sig[0] == 0:
                continue  # an empty placeholder; the finished file brings an event of its own
            self._handed[path] = sig
            ready.append(path)
        gone, self._gone = list(dict.fromkeys(self._gone)), []
        return sorted(ready), gone

    def next_check(self) -> float | None:
        """Seconds until a pending file could settle (None if nothing is pending)."""
        if not self.pending:
            return None
        now = self.clock()
        return max(0.5, min(p.since + self.settle_s - now for p in self.pending.values()))
