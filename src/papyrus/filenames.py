"""Heuristics for hand-made filenames such as

    Bellard2019 - "Lossless Data Compression with Neural Networks".pdf
    BonchiGadducciKissinger 2022 - "String Diagram Rewrite Theory II- Rewriting ...".pdf
    NavaWyart2026 "Hierarchical Concept Geometry ...".pdf
    "Gated Delta Networks".pdf

Results are weak hints (recorded as assertions from 'heuristic:filename').
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_QUOTED = re.compile(r'^(?P<pre>[^"“”]*?)\s*[“"](?P<title>[^"“”]+)[”"]\s*$')
_PRE = re.compile(r"^(?P<names>[A-Za-zÀ-ÿ'’.\- ]*?)\s*(?P<year>(?:19|20)\d{2})?\s*-?\s*$")
_CAMEL = re.compile(r"[A-ZÀ-Þ][a-zß-ÿ'’]+(?:-[A-ZÀ-Þ][a-zß-ÿ'’]+)?")


@dataclass
class FilenameHint:
    title: str | None = None
    surnames: list[str] = field(default_factory=list)
    year: int | None = None

    def __bool__(self) -> bool:
        return bool(self.title or self.surnames or self.year)


def _fix_title(t: str) -> str:
    t = t.strip()
    # macOS can't show ':' in names, so people write 'Foo- Bar' or 'Foo - Bar'
    t = re.sub(r"(\w)- ", r"\1: ", t)
    t = re.sub(r"\s+-\s+", ": ", t, count=1) if ":" not in t else t
    return re.sub(r"\s+", " ", t).strip()


def parse_filename(stem: str) -> FilenameHint:
    m = _QUOTED.match(stem)
    if not m:
        return FilenameHint()
    hint = FilenameHint(title=_fix_title(m.group("title")))
    pre = m.group("pre").strip()
    pm = _PRE.match(pre)
    if pm:
        if pm.group("year"):
            hint.year = int(pm.group("year"))
        names = (pm.group("names") or "").strip(" -")
        if names:
            hint.surnames = _CAMEL.findall(names) if " " not in names else [n for n in names.split() if n[:1].isupper()]
    return hint
