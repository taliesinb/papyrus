"""User configuration: `<library home>/config.toml` (default `~/.papyrus/release/config.toml`).

Lives next to the database and the store, so each variant (`~/.papyrus/<variant>`) and a
scratch library (`PAPYRUS_HOME=/tmp/x`) has its own. `PAPYRUS_CONFIG` points at another file. Every key is optional:

    backend = "auto"      # extraction model: auto | lmstudio[:model] | apple
    decider = "auto"      # auto | none | lmstudio[:model] | jev[:url] | openai:model@url

    [lmstudio]
    autostart = true      # run `lms server start` when LM Studio's server is off

    [slack]               # `papyrus slack links` defaults (contexts/slack.py)
    workspace = "symbolica"
    days = 30
    dms = false
    channels = []         # allow list (names or ids); empty = all
    exclude_channels = []
    domains = []          # extra hosts counted as paper links

Precedence for each setting: command-line option > environment variable
(`PAPYRUS_BACKEND`, `PAPYRUS_DECIDER`, `PAPYRUS_LMSTUDIO_AUTOSTART`) > this file > default.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from . import db

TEMPLATE = '''\
# papyrus configuration. Every key is optional; command-line options and the
# PAPYRUS_BACKEND / PAPYRUS_DECIDER / PAPYRUS_LMSTUDIO_AUTOSTART environment
# variables take precedence over this file.

# Extraction model for `papyrus triage`: auto | lmstudio[:model] | apple
# auto = LM Studio (started if needed, see below), else Apple Foundation Models.
backend = "auto"

# Decider for kind / library-vs-personal: auto | none | lmstudio[:model] | jev[:url] | openai:model@url
# auto = PAPYRUS_JEV_URL if set, else LM Studio. Triage refuses to run without a
# decider unless this is "none".
decider = "auto"

[lmstudio]
# Start LM Studio's local server (`lms server start`) when it isn't running.
autostart = true

# Defaults for `papyrus slack links` (command-line options override them).
# [slack]
# workspace = "myco"        # name the token is stored under (papyrus slack login -w myco)
# days = 30
# dms = false               # include direct and group messages
# channels = []             # only these channels (names or ids); empty = all
# exclude_channels = []
# domains = []              # extra hosts (or host/path) counted as paper links
'''


class ConfigError(ValueError):
    pass


def path() -> Path:
    env = os.environ.get("PAPYRUS_CONFIG")
    return Path(env).expanduser() if env else db.library_home() / "config.toml"


def load() -> dict[str, Any]:
    p = path()
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"{p}: {e}") from e


def _str(cfg: dict, key: str) -> str | None:
    v = cfg.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{path()}: {key} must be a non-empty string, got {v!r}")
    return v.strip()


def setting(name: str, cli: str | None = None) -> tuple[str, str]:
    """(value, where it came from) for 'backend' or 'decider'."""
    if cli:
        return cli, "command line"
    env = f"PAPYRUS_{name.upper()}"
    if os.environ.get(env, "").strip():
        return os.environ[env].strip(), env
    if (v := _str(load(), name)) is not None:
        return v, str(path())
    return "auto", "default"


def backend(cli: str | None = None) -> str:
    return setting("backend", cli)[0]


def decider(cli: str | None = None) -> str:
    return setting("decider", cli)[0]


def lmstudio_autostart() -> tuple[bool, str]:
    env = os.environ.get("PAPYRUS_LMSTUDIO_AUTOSTART", "").strip().lower()
    if env:
        return env not in {"0", "false", "no", "off"}, "PAPYRUS_LMSTUDIO_AUTOSTART"
    section = load().get("lmstudio") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path()}: [lmstudio] must be a table")
    v = section.get("autostart")
    if v is None:
        return True, "default"
    if not isinstance(v, bool):
        raise ConfigError(f"{path()}: lmstudio.autostart must be true or false, got {v!r}")
    return v, str(path())


def init() -> tuple[Path, bool]:
    """Write the commented template unless a config file exists. Returns (path, written)."""
    p = path()
    if p.exists():
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE)
    return p, True
