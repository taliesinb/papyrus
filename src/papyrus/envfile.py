"""Private environment settings: `<PAPYRUS_ROOT>/env` (default `~/.papyrus/env`).

API keys and the contact address (`OPENALEX_API_KEY`, `S2_API_KEY`, `PAPYRUS_MAILTO`, …) are read
from the environment. Papyrus Server.app, which runs the watcher, doesn't get the login shell's
environment, so the CLI reads this file at start: one `KEY=VALUE` per line (`#` comments, blank
lines, an optional `export ` and quotes around the value are fine). A variable already set in the
environment wins. Shared by every variant; outside git; keep it readable by you only (chmod 600):
a warning says so otherwise. Values are never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NAME = "env"


def path() -> Path:
    from .db import papyrus_root

    return papyrus_root() / NAME


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load(p: Path | None = None) -> list[str]:
    """Set the file's variables that the environment doesn't set; returns their names."""
    p = p or path()
    try:
        text = p.read_text()
        mode = p.stat().st_mode
    except OSError:
        return []
    if mode & 0o077:
        print(f"papyrus: {p} is readable by others; it holds API keys: chmod 600 {p}", file=sys.stderr)
    added = []
    for key, value in parse(text).items():
        if value and not os.environ.get(key, "").strip():
            os.environ[key] = value
            added.append(key)
    return added
