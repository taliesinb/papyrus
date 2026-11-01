"""The watcher's states (shown by the menu bar) and the `Backoff` that pauses model work."""

from __future__ import annotations

STARTING = "starting"
IDLE = "idle"
INGESTING = "ingesting"  # scanning new files, or reading / classifying / extracting / indexing / matching
WAITING_MEMORY = "waiting-memory"
LMSTUDIO_UNAVAILABLE = "lmstudio-unavailable"  # off, not serving the model, or busy with another
PAUSED = "paused"  # model work paused by the user (<home>/watch.pause); files are still added
RATE_LIMITED = "rate-limited"  # a matching API refused (429/503): matching waits, the rest goes on
STOPPED = "stopped"

# How long to wait before trying model work again (seconds). Memory doubles up to the cap.
MEMORY_DELAY, MEMORY_DELAY_MAX = 30.0, 600.0
LMSTUDIO_DELAY = 300.0
RATE_LIMIT_DELAY = 300.0  # when the server didn't say how long (Retry-After)
MATCH_PATIENCE_S = 15.0  # seconds of retry waits one match may spend before backing off


class Backoff(Exception):
    """Model work can't go on right now (memory pressure, LM Studio): wait, don't fail the file."""

    def __init__(self, state: str, reason: str, delay: float | None = None):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.delay = delay
