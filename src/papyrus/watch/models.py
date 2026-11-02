"""Which LM Studio models `papyrus watch` may use, and which it may unload.

LM Studio is shared with the user and other agents, and loading two models on a 24 GB Mac
swaps (AGENTS.md, Gotchas). So before each request for an LM Studio model, `ModelLedger.claim`
looks at what is loaded:

* the model is loaded: use it (whoever loaded it);
* another language model, loaded by someone else, is loaded: load beside it only if the model
  fits in the memory available now (`room`, system.has_room); else back off;
* nothing is loaded: the request will load the model just in time. It is then *ours*, unless
  memory is already under pressure, in which case we back off instead;
* only models of ours are loaded (the classifier's, when extraction needs another): the new one
  loads beside them if it fits (or memory pressure is normal); else ours are unloaded first (a
  swap, at a phase change; see daemon.py).

Embedding models (the screen's, 84-640 MB) don't count: `lmstudio.loaded_models` lists language
models only, and the watcher's gate lets the screen load its model beside one when memory
pressure is normal, or it fits (daemon.py, `_gate`).

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
                 log: Callable[[str], None] = lambda msg: None, room: Callable[[str], bool] | None = None):
        self.list_loaded = list_loaded
        self.unload = unload
        self.room = room  # model -> whether it fits in memory beside what is loaded (system.has_room)
        self.idle_s = idle_s
        self.clock = clock
        self.log = log
        self.ours: set[str] = set()
        self.last_used = 0.0

    def claim(self, model: str, may_load: bool = True, swap: bool = True) -> None:
        """Called before every request for `model`; raises `Backoff` when we may not ask.

        `may_load`: memory pressure is normal. `room(model)`: the model fits in the memory available
        now (the pressure level reads "warn" on a Mac whenever any model is loaded, so it can't
        tell). The classifier and the extraction model can be different models, and the watcher
        works through one phase, then the other (daemon.py), so at a phase change:

        * the next model fits: it loads beside what is loaded, whoever loaded that;
        * it doesn't, and only models of ours are loaded: they are unloaded first (a swap);
        * it doesn't, and someone else's model is loaded: we wait. Never unloaded what we didn't load."""
        loaded = self.list_loaded()
        if loaded is None:
            raise Backoff(LMSTUDIO_UNAVAILABLE, "LM Studio does not answer")
        if model not in loaded:
            self.ours.discard(model)
            others = [m for m in loaded if m != model]
            theirs = [m for m in others if m not in self.ours]
            fits = self.room(model) if self.room is not None else False
            if theirs and not fits:
                raise Backoff(LMSTUDIO_UNAVAILABLE, f"no room to load {model} beside {theirs[0]}, which the watcher "
                              "did not load", delay=120)
            if others and not theirs and not (fits or may_load):
                if not swap:
                    raise Backoff(WAITING_MEMORY, "loading a second model under memory pressure")
                for m in others:  # all ours: make room
                    if self.unload(m):
                        self.ours.discard(m)
                        self.log(f"unloaded {m} to load {model} (swap)")
                    else:
                        raise Backoff(LMSTUDIO_UNAVAILABLE, "could not unload the watcher's other model", delay=120)
            elif not others and not (fits or may_load):
                raise Backoff(WAITING_MEMORY, "loading a model under memory pressure")
            beside = others if fits or may_load else []  # (after a swap, nothing)
            self.ours.add(model)
            self.log(f"loading {model}" + (f" beside {', '.join(beside)}" if beside else "")
                     + " (the watcher will unload it when idle)")
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
