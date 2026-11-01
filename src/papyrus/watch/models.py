"""Which LM Studio models `papyrus watch` may use, and which it may unload.

LM Studio is shared with the user and other agents, and loading two models on a 24 GB Mac
swaps (AGENTS.md, Gotchas). So before each request for an LM Studio model, `ModelLedger.claim`
looks at what is loaded:

* the model is loaded: use it (whoever loaded it);
* another language model is loaded: don't ask, since the request would load a second one;
  back off;
* nothing is loaded: the request will load the model just in time. It is then *ours*, unless
  memory is already under pressure, in which case we back off instead.

Embedding models (the screen's, 84 MB) don't count: `lmstudio.loaded_models` lists language
models only, and the watcher's gate lets the screen load its model beside one (daemon.py).

`release_idle` unloads our models once the watcher has not used them for `idle_s` seconds (and
at exit). A model someone else loaded is never ours, so it is never unloaded. A model of ours
that went away (LM Studio's own idle timeout, the user) stops being ours, so that a later load by
somebody else isn't mistaken for ours.
"""

from __future__ import annotations

import time
from typing import Callable

from .errors import LMSTUDIO_UNAVAILABLE, WAITING_MEMORY, Backoff


class ModelLedger:
    def __init__(self, list_loaded: Callable[[], list[str] | None], unload: Callable[[str], bool],
                 idle_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = lambda msg: None):
        self.list_loaded = list_loaded
        self.unload = unload
        self.idle_s = idle_s
        self.clock = clock
        self.log = log
        self.ours: set[str] = set()
        self.last_used = 0.0

    def claim(self, model: str, may_load: bool = True) -> None:
        """Called before every request for `model`; raises `Backoff` when we may not ask."""
        loaded = self.list_loaded()
        if loaded is None:
            raise Backoff(LMSTUDIO_UNAVAILABLE, "LM Studio does not answer")
        if model not in loaded:
            self.ours.discard(model)
            others = [m for m in loaded if m != model]
            if others:
                raise Backoff(LMSTUDIO_UNAVAILABLE, "another model is loaded", delay=120)
            if not may_load:
                raise Backoff(WAITING_MEMORY, "loading a model under memory pressure")
            self.ours.add(model)
            self.log(f"loading {model} (the watcher will unload it when idle)")
        self.last_used = self.clock()

    def refresh(self) -> None:
        """Forget models of ours that are no longer loaded (LM Studio's own idle timeout, the
        user), so that a later load by somebody else isn't taken for ours."""
        if self.ours and (loaded := self.list_loaded()) is not None:
            self.ours &= set(loaded)

    def idle_due(self) -> float | None:
        """Seconds until our models count as idle (None if we hold none)."""
        if not self.ours:
            return None
        return max(0.0, self.last_used + self.idle_s - self.clock())

    def release_idle(self, force: bool = False) -> list[str]:
        """Unload our models if idle long enough (or `force`, at exit). Returns what was unloaded."""
        if not self.ours or (not force and self.clock() - self.last_used < self.idle_s):
            return []
        loaded = self.list_loaded() or []
        done = []
        for m in sorted(self.ours):
            if m in loaded and self.unload(m):
                done.append(m)
                self.log(f"unloaded {m}, which the watcher had loaded")
        self.ours.clear()
        return done
