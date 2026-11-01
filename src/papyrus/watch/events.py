"""The watcher's performance log: `<home>/watch-events.jsonl`, one JSON object per line.

Every line has `t` (UTC, ISO 8601, milliseconds) and `event`:

    start     pid, dirs, prompt_version
    models    extract, classify, screen (model tags, when a batch first needs them)
    scan      id, outcome (new | dup | purged_skipped | ...), ms   (a file added: snapshot, hash, first pages)
    step      stage (read | classify | extract | index | match), id, ms, outcome (done | no_progress |
              backoff | error), phase, and for classify: result (screened | library | not_library),
              p_personal; for extract: cached
    model     msg (a model loaded, unloaded or swapped, from watch/models.ModelLedger)
    backoff   stage, state, reason, delay_s
    drained   the queue went empty: busy_s since the work started, steps per stage, files added
    stop      totals

Like the watcher's text log it holds ids, counts, timings and model names, never titles, paths
or text. `papyrus watch --perf` summarises it (`summary`).
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME = "watch-events.jsonl"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    def __init__(self, path: Path, wall=time.time):
        self.path, self.wall = Path(path), wall

    def emit(self, event: str, **fields: Any) -> None:
        line = json.dumps({"t": _iso(self.wall()), "event": event, **fields}, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(line + "\n")
        except OSError:  # a performance log must never stop the watcher
            pass


def read(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _secs(ms: list[float]) -> str:
    if not ms:
        return "-"
    s = sorted(ms)
    p90 = s[min(len(s) - 1, int(0.9 * len(s)))]
    return (f"{len(s):5}  total {sum(s) / 1000:8.1f} s  median {statistics.median(s) / 1000:6.2f} s"
            f"  p90 {p90 / 1000:6.2f} s  max {s[-1] / 1000:6.2f} s")


def _t(e: dict[str, Any]) -> float:
    return datetime.fromisoformat(e["t"].replace("Z", "+00:00")).timestamp()


def summary(events: list[dict[str, Any]], all_sessions: bool = False) -> str:
    """A report of the latest session (since the last `start`), or of all of them."""
    if not events:
        return "no watcher events yet"
    if not all_sessions:
        starts = [i for i, e in enumerate(events) if e.get("event") == "start"]
        events = events[starts[-1]:] if starts else events
    first, last = _t(events[0]), _t(events[-1])
    work = [e for e in events if e.get("event") in ("scan", "step")]
    lines = [f"session from {events[0]['t']} to {events[-1]['t']} ({last - first:.0f} s)"]
    if work:
        w0, w1 = _t(work[0]), _t(work[-1])
        # a step's line is written when it ends: count the first one's own time as busy too
        w0 -= work[0].get("ms", 0) / 1000
        lines.append(f"busy from the first file to the last step: {w1 - w0:.0f} s ({(w1 - w0) / 60:.1f} min)")
    for e in events:
        if e.get("event") == "models":
            lines.append("models: " + ", ".join(f"{k} {v}" for k, v in e.items() if k not in ("t", "event") and v))
            break
    ingests = [e for e in events if e.get("event") == "scan"]
    if ingests:
        lines.append("")
        lines.append("files added   " + _secs([e.get("ms", 0) for e in ingests]))
        lines.append("              " + ", ".join(f"{k} {v}" for k, v in Counter(e.get("outcome") for e in ingests).most_common()))
    steps = [e for e in events if e.get("event") == "step"]
    if steps:
        lines.append("")
        by = defaultdict(list)
        for e in steps:
            by[e["stage"]].append(e)
        for stage in ("read", "classify", "extract", "index", "match"):
            es = by.get(stage) or []
            if not es:
                continue
            lines.append(f"{stage:13} " + _secs([e.get("ms", 0) for e in es]))
            outcomes = Counter(e.get("outcome") for e in es)
            if stage == "classify":
                results = Counter(e.get("result") for e in es if e.get("outcome") == "done")
                done = [e for e in es if e.get("outcome") == "done"]
                lines.append("              " + ", ".join(f"{k} {v}" for k, v in results.most_common() if k))
                for r in ("screened", "library", "not_library"):
                    ms = [e.get("ms", 0) for e in done if e.get("result") == r]
                    if ms:
                        lines.append(f"  {r:11} " + _secs(ms))
            if set(outcomes) - {"done"}:
                lines.append("              outcomes: " + ", ".join(f"{k} {v}" for k, v in outcomes.most_common()))
    backoffs = [e for e in events if e.get("event") == "backoff"]
    if backoffs:
        lines.append("")
        lines.append(f"back-offs: {len(backoffs)}, " + ", ".join(
            f"{k} {v}" for k, v in Counter(f"{e.get('stage')}: {e.get('reason')}" for e in backoffs).most_common(5)))
    models = [e for e in events if e.get("event") == "model"]
    if models:
        lines.append("")
        lines.append("model events:")
        lines += [f"  {e['t'][11:19]} {e.get('msg')}" for e in models[:20]]
    drained = [e for e in events if e.get("event") == "drained"]
    if drained:
        d = drained[-1]
        lines.append("")
        lines.append(f"queue drained at {d['t']} after {d.get('busy_s', 0):.0f} s busy: "
                     + ", ".join(f"{k} {v}" for k, v in (d.get("steps") or {}).items()) + f"; files added {d.get('added', 0)}")
    return "\n".join(lines)
