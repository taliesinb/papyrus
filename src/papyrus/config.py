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

    [services]            # how every web service is asked (services.Policy)
    timeout_s = 30        # also interval_s, max_retries, backoff_base, backoff_cap, pause_s,
                          # pause_statuses, cache_ttl (inf: forever), scope
    [services.arxiv]      # one service: arxiv, arxiv-web, openalex, crossref, s2, slack, web, web:<host>
    pause_s = 300         # after a refusal or no answer, ask nothing for this long

The defaults are a file shipped with papyrus, `src/papyrus/data/default-config.toml`, which
also holds the service policies and why they are what they are. A library without a config file
gets a copy of it the first time its config is read, to edit. A setting deleted from the copy
falls back to the shipped default; one kept stays as it is when the defaults change (`papyrus
config` lists the settings that differ from the current defaults). For [services], the order is:
shipped [services] keys < yours < shipped [services.<name>] < yours.

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

DEFAULT_FILE = "default-config.toml"  # in src/papyrus/data/: the defaults, and what a new config file starts as


def default_text() -> str:
    """The shipped default config (src/papyrus/data/default-config.toml)."""
    from importlib import resources

    return resources.files("papyrus").joinpath("data", DEFAULT_FILE).read_text()


def defaults() -> dict[str, Any]:
    """The shipped default config, parsed (once)."""
    global _defaults
    if _defaults is None:
        _defaults = tomllib.loads(default_text())
    return _defaults


_defaults: dict[str, Any] | None = None


class ConfigError(ValueError):
    pass


def path() -> Path:
    env = os.environ.get("PAPYRUS_CONFIG")
    return Path(env).expanduser() if env else db.library_home() / "config.toml"


def _header() -> str:
    from datetime import date

    return (f"# Written by papyrus on {date.today().isoformat()} from its defaults. Delete a setting to follow\n"
            "# papyrus's default for it; `papyrus config` lists the settings that differ from the defaults.\n\n")


def _write_default(p: Path) -> bool:
    """Write the shipped defaults to `p` unless something is there (another process may race us)."""
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(_header() + default_text())
    return True


def load() -> dict[str, Any]:
    """The library's config file, parsed. A library (an existing home) without one gets the shipped
    defaults written there first; a missing PAPYRUS_CONFIG file is not created."""
    p = path()
    if not os.environ.get("PAPYRUS_CONFIG") and not p.exists() and p.parent.is_dir():
        _write_default(p)
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


def _policy_settings(table: dict[str, Any], where: str) -> dict[str, Any]:
    """One [services] or [services.<name>] table's policy settings, checked (`where` names it)."""
    from dataclasses import fields

    from .services import Policy

    known = [f.name for f in fields(Policy)]
    out: dict[str, Any] = {}
    for k, v in table.items():
        if k not in known:
            raise ConfigError(f"{where}.{k} is not a policy setting ({', '.join(known)})")
        if k == "scope":
            if v not in ("service", "credential"):
                raise ConfigError(f'{where}.scope must be "service" or "credential", got {v!r}')
        elif k == "pause_statuses":
            if not isinstance(v, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
                raise ConfigError(f"{where}.pause_statuses must be a list of HTTP statuses (0: no answer), got {v!r}")
            v = tuple(v)
        elif k == "max_retries":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ConfigError(f"{where}.max_retries must be a whole number >= 0, got {v!r}")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not v >= 0:
            what = "a number of seconds >= 0, or inf (forever)" if k == "cache_ttl" else "a number of seconds >= 0"
            raise ConfigError(f"{where}.{k} must be {what}, got {v!r}")
        else:
            v = float(v)
        out[k] = v
    return out


def _services_tables(cfg: dict[str, Any], where: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(the [services] keys, {name: the [services.<name>] keys}) of one config, checked."""
    section = cfg.get("services") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: [services] must be a table")
    base = {k: v for k, v in section.items() if not isinstance(v, dict)}
    per = {k: v for k, v in section.items() if isinstance(v, dict)}
    return (_policy_settings(base, f"{where}: services"),
            {name: _policy_settings(t, f"{where}: services.{name}") for name, t in per.items()})


def default_services_layer() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    return _services_tables(defaults(), f"(papyrus's defaults) {DEFAULT_FILE}")


def services_layers() -> list[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    """The service policy settings in the order they apply: the shipped defaults' ([services],
    [services.<name>]), then this library's config file's (services.policy_for merges them)."""
    return [default_services_layer(), _services_tables(load(), str(path()))]


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def differences() -> list[tuple[str, Any, Any]]:
    """(dotted key, your value, the default or None) for each setting in this library's config
    file that isn't papyrus's current default (a [services.<name>] key without a default of its
    own is compared with the [services] default)."""
    mine, shipped = _flatten(load()), _flatten(defaults())
    out = []
    for k, v in mine.items():
        d = shipped.get(k)
        if d is None and k.startswith("services.") and k.count(".") >= 2:  # services.<name>.<key>; a host has dots
            d = shipped.get("services." + k.rsplit(".", 1)[1])
        if v != d:
            out.append((k, v, d))
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
    """Write the shipped defaults unless a config file exists (creating its directory, which `load`
    doesn't). Returns (path, written)."""
    p = path()
    if p.exists():
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    return p, _write_default(p)
