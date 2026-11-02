"""User configuration: `<library home>/config.toml` (default `~/.papyrus/release/config.toml`).

Lives next to the database and the store, so each variant (`~/.papyrus/<variant>`) and a
scratch library (`PAPYRUS_HOME=/tmp/x`) has its own. `PAPYRUS_CONFIG` points at another file. Every key is optional:

    backend = "auto"      # extraction model: auto | lmstudio[:model] | apple
    classifier = "auto"      # auto | none | lmstudio[:model] | jev[:url] | openai:model@url

    [lmstudio]
    autostart = true      # run `lms server start` when LM Studio's server is off

    [screen]              # the classify stage's fast first step (screen.py)
    enabled = true        # also PAPYRUS_SCREEN=0
    threshold = 0.9       # P(personal) at which a file is set aside without asking the 4B model
    accept = 0.9          # P(kind) at which the screen's kind is taken without the 4B (false: never)

    [describe]            # the document description every model reads (describe.build_state, pagetext.py)
    state_tokens = 1000   # about this many tokens: the header, then pages from the front and the back
    tail_fraction = 0.2   # share of the page budget kept for the last pages

    [watch]               # `papyrus watch` (watch/)
    dirs = ["~/Downloads", "~/Desktop"]   # "DIR/**": DIR with its subdirectories
    settle = 3            # seconds a new file must stay unchanged before it is added
    unload_after = 600    # seconds idle before unloading a model the watcher loaded itself
    background = true     # run at background priority (false: normal, e.g. to benchmark ingestion)

    [slack]               # `papyrus slack links` defaults (contexts/slack.py)
    workspace = "symbolica"
    days = 30
    dms = false
    channels = []         # allow list (names or ids); empty = all
    exclude_channels = []
    domains = []          # extra hosts counted as paper links

    [services.arxiv]      # how a web service is asked (services.Policy; defaults in services.SERVICES)
    pause_s = 300         # after a refusal or no answer, ask nothing for this long
    interval_s = 3        # also timeout_s, max_retries, backoff_base, backoff_cap, reuse_s, pause_statuses, scope

Precedence for each setting: command-line option > environment variable
(`PAPYRUS_BACKEND`, `PAPYRUS_CLASSIFIER`, `PAPYRUS_LMSTUDIO_AUTOSTART`, `PAPYRUS_SCREEN`) > this file
> default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db

TEMPLATE = '''\
# papyrus configuration. Every key is optional; command-line options and the
# PAPYRUS_BACKEND / PAPYRUS_CLASSIFIER / PAPYRUS_LMSTUDIO_AUTOSTART environment
# variables take precedence over this file.

# Extraction model (`papyrus extract`): auto | lmstudio[:model] | apple
# auto = LM Studio (started if needed, see below), else Apple Foundation Models.
backend = "auto"

# Classifier for kind / library-vs-personal: auto | none | lmstudio[:model] | jev[:url] | openai:model@url
# auto = PAPYRUS_JEV_URL if set, else LM Studio. `papyrus classify` refuses to run without a
# classifier unless this is "none".
classifier = "auto"

[lmstudio]
# Start LM Studio's local server (`lms server start`) when it isn't running.
autostart = true

# The classify stage's fast first step: a small embedding model (0.1-0.4 s a file) sets obvious
# personal paperwork aside, held for review, and settles the kind of the documents it is sure
# about, before the language model is asked.
# [screen]
# enabled = true
# threshold = 0.9         # P(personal) needed to set a file aside (default: the weights' own)
# accept = 0.9            # P(kind) needed to take the screen's kind without the classifier
#                         # (default: the weights' own; false: always ask the classifier)

# What every model reads of a document (the document description): its names, folder, origin and PDF metadata, then
# pages from the front and, for a longer document, the back, within about this many tokens.
# [describe]
# state_tokens = 1000
# tail_fraction = 0.2       # share of the page budget kept for the last pages

# `papyrus watch`: add new PDFs from these directories (not their subdirectories; end an entry
# in /** to include them, e.g. "~/Dropbox/doc/**") as they appear, then ingest them in the
# background: read, classify, extract, promote, index, match.
[watch]
dirs = ["~/Downloads", "~/Desktop"]
# settle = 3            # seconds a file must stay unchanged before it is added
# unload_after = 600    # seconds idle before unloading a model the watcher loaded itself

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


LEGACY = {"classifier": ("decider",)}  # old names still read: key `decider`, PAPYRUS_DECIDER


def setting(name: str, cli: str | None = None) -> tuple[str, str]:
    """(value, where it came from) for 'backend' or 'classifier'."""
    if cli:
        return cli, "command line"
    names = [name, *LEGACY.get(name, ())]  # the classifier was called the decider until 2026-11-01
    for n in names:
        env = f"PAPYRUS_{n.upper()}"
        if os.environ.get(env, "").strip():
            return os.environ[env].strip(), env
    for n in names:
        if (v := _str(load(), n)) is not None:
            return v, str(path())
    return "auto", "default"


def backend(cli: str | None = None) -> str:
    return setting("backend", cli)[0]


def classifier(cli: str | None = None) -> str:
    return setting("classifier", cli)[0]


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


@dataclass(frozen=True)
class ScreenSettings:
    enabled: bool = True
    threshold: float | None = None  # None: the weights' own
    accept: float | bool | None = None  # P(kind) to accept the screen's kind; None: the weights' own; False: never


def screen() -> ScreenSettings:
    """The [screen] table; PAPYRUS_SCREEN=0 turns the screen off."""
    section = load().get("screen") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path()}: [screen] must be a table")
    env = os.environ.get("PAPYRUS_SCREEN", "").strip().lower()
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path()}: screen.enabled must be true or false, got {enabled!r}")
    if env:
        enabled = env not in {"0", "false", "no", "off"}
    t = section.get("threshold")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 1):
        raise ConfigError(f"{path()}: screen.threshold must be a probability in (0, 1], got {t!r}")
    a = section.get("accept")
    if a is not None and a is not False and (a is True or not isinstance(a, (int, float)) or not 0 < a <= 1):
        raise ConfigError(f"{path()}: screen.accept must be a probability in (0, 1] or false, got {a!r}")
    return ScreenSettings(enabled, None if t is None else float(t), a if a is None or a is False else float(a))


@dataclass(frozen=True)
class DescribeSettings:
    state_tokens: int = 1000
    tail_fraction: float = 0.2


def describe() -> DescribeSettings:
    """The [describe] table: the size of the document description and the share of it kept for the
    last pages."""
    section = load().get("describe") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path()}: [describe] must be a table")
    n = section.get("state_tokens", DescribeSettings.state_tokens)
    if isinstance(n, bool) or not isinstance(n, int) or n < 300:
        raise ConfigError(f"{path()}: describe.state_tokens must be a whole number of tokens >= 300, got {n!r}")
    f = section.get("tail_fraction", DescribeSettings.tail_fraction)
    if isinstance(f, bool) or not isinstance(f, (int, float)) or not 0 <= f < 1:
        raise ConfigError(f"{path()}: describe.tail_fraction must be in [0, 1), got {f!r}")
    return DescribeSettings(n, float(f))


WATCH_DIRS = ("~/Downloads", "~/Desktop")


@dataclass(frozen=True)
class WatchSettings:
    dirs: tuple[Path, ...]  # watched without their subdirectories
    trees: tuple[Path, ...] = ()  # entries ending in /**: watched with their subdirectories
    settle_s: float = 3.0
    unload_after_s: float = 600.0
    background: bool = True  # background priority (PRIO_DARWIN_BG / nice + idle I/O): yield to everything else


def watch() -> WatchSettings:
    """The [watch] table: directories to watch (default ~/Downloads, ~/Desktop) and timings."""
    section = load().get("watch") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path()}: [watch] must be a table")
    dirs = section.get("dirs", list(WATCH_DIRS))
    if not isinstance(dirs, list) or not all(isinstance(d, str) and d.strip() for d in dirs):
        raise ConfigError(f"{path()}: watch.dirs must be a list of directories, got {dirs!r}")
    out = {}
    for key, name in (("settle", "settle_s"), ("unload_after", "unload_after_s")):
        v = section.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"{path()}: watch.{key} must be a number of seconds, got {v!r}")
        out[name] = float(v)
    background = section.get("background", True)
    if not isinstance(background, bool):
        raise ConfigError(f"{path()}: watch.background must be true or false, got {background!r}")
    plain, trees = split_watch_dirs(dirs)
    return WatchSettings(dirs=plain, trees=trees, background=background, **out)


def services_overrides() -> dict[str, dict[str, Any]]:
    """The [services.<name>] tables: per service (arxiv, openalex, crossref, s2, slack, arxiv-web),
    policy settings that replace the defaults in services.SERVICES, e.g.

        [services.arxiv]
        pause_s = 600        # ask nothing for 10 min after a refusal or a timeout
        interval_s = 3.5
    """
    from dataclasses import fields

    from .services import Policy

    section = load().get("services") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path()}: [services] must be a table of [services.<name>] tables")
    known = {f.name: f.type for f in fields(Policy)}
    out: dict[str, dict[str, Any]] = {}
    for name, table in section.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path()}: [services.{name}] must be a table")
        o: dict[str, Any] = {}
        for k, v in table.items():
            if k not in known:
                raise ConfigError(f"{path()}: services.{name}.{k} is not a policy setting ({', '.join(known)})")
            if k == "scope":
                if v not in ("service", "credential"):
                    raise ConfigError(f"{path()}: services.{name}.scope must be \"service\" or \"credential\"")
            elif k == "pause_statuses":
                if not isinstance(v, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
                    raise ConfigError(f"{path()}: services.{name}.pause_statuses must be a list of HTTP statuses")
                v = tuple(v)
            elif k == "max_retries":
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    raise ConfigError(f"{path()}: services.{name}.max_retries must be a whole number >= 0")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ConfigError(f"{path()}: services.{name}.{k} must be a number of seconds >= 0, got {v!r}")
            else:
                v = float(v)
            o[k] = v
        out[name] = o
    return out


def split_watch_dirs(entries: list[str] | tuple[str, ...]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """[watch] dirs entries -> (directories, trees): `~/Dropbox/doc/**` is ~/Dropbox/doc with its
    subdirectories, `~/Downloads` the directory alone."""
    plain, trees = [], []
    for e in entries:
        e = str(e).strip()
        if e.endswith("/**"):
            trees.append(Path(e[:-3] or "/").expanduser())
        else:
            plain.append(Path(e).expanduser())
    return tuple(plain), tuple(trees)


def init() -> tuple[Path, bool]:
    """Write the commented template unless a config file exists. Returns (path, written)."""
    p = path()
    if p.exists():
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE)
    return p, True
