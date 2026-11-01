"""Locate a span of page text from short start/end anchor strings.

Instead of having a (slow) model copy a 250-word abstract verbatim, extraction asks
for its first and last few words; we cut the exact text out of the page.
Matching is on normalised word tokens, so it survives ligatures, hyphenation,
line breaks, diacritics and small model paraphrases of punctuation.
"""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"\w+", re.UNICODE)
_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
                            # invisible: soft hyphen, zero-width space/joiners, BOM
                            "\u00ad": "", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
                            **{chr(c): "" for c in [*range(1, 9), 11, 12, *range(14, 32)]}})


def _norm_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w.translate(_LIGATURES)).encode("ascii", "ignore").decode()
    return w.lower()


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(normalised word, start offset, end offset) for each word in `text`."""
    out = []
    for m in _WORD.finditer(text.translate(_LIGATURES)):
        w = _norm_word(m.group())
        if w:
            out.append((w, m.start(), m.end()))
    return out


def _index(seq: list[str], cand: list[str], lo: int) -> int | None:
    k = len(cand)
    for i in range(lo, len(seq) - k + 1):
        if seq[i : i + k] == cand:
            return i
    return None


def _find(seq: list[str], words: list[str], lo: int = 0, *, want: str,
           min_len: int = 3) -> tuple[int, int] | None:
    """Locate anchor `words` in `seq` (at/after `lo`), tolerating a garbled head or tail.

    Returns (boundary index, slack). want='first': index of the anchor's first word
    (start anchors); want='last': index of its last word (end anchors). If only a
    partial match was found and the boundary-side words were the garbled ones,
    `slack` says how many words the true boundary may lie beyond the match.
    """
    n = len(words)
    i = _index(seq, words, lo)
    if i is not None:
        return (i, 0) if want == "first" else (i + n - 1, 0)
    for k in range(n - 1, min(min_len, n) - 1, -1):
        pre, suf = words[:k], words[-k:]
        # the part touching the boundary we care about is the more useful one
        order = [("pre", pre), ("suf", suf)] if want == "first" else [("suf", suf), ("pre", pre)]
        for which, cand in order:
            j = _index(seq, cand, lo)
            if j is None:
                continue
            if want == "first":
                return (j, 0) if which == "pre" else (j, n - k)
            return (j + k - 1, 0) if which == "suf" else (j + k - 1, n - k)
    return None


_HEADINGS = {"abstract", "summary"}


def _extend_back(text: str, toks: list, i: int, slack: int) -> int:
    """Move a start boundary back over up to `slack` words, stopping at headings/sentence ends."""
    while slack > 0 and i > 0:
        prev = toks[i - 1]
        gap = text[prev[2] : toks[i][1]]
        if prev[0] in _HEADINGS or re.search(r"[.!?:]\s|\n\s*\n", gap):
            break
        i, slack = i - 1, slack - 1
    return i


def _extend_forward(text: str, toks: list, i: int, slack: int) -> int:
    """Move an end boundary forward over up to `slack` words, stopping at a sentence end."""
    while slack > 0 and i + 1 < len(toks):
        if re.match(r"[.!?]", text[toks[i][2] : toks[i + 1][1]].strip()[:1] or " "):
            break
        i, slack = i + 1, slack - 1
    return i


def locate(text: str, start: str | None, end: str | None, max_words: int = 700) -> str | None:
    """Return the exact substring of `text` from anchor `start` through anchor `end`."""
    if not text or not start:
        return None
    text = text.translate(_LIGATURES)  # offsets below index into the translated text
    toks = tokens(text)
    seq = [t[0] for t in toks]
    sw = [w for w, *_ in tokens(start)]
    if not sw:
        return None
    s = _find(seq, sw, want="first")
    if s is None:
        return None
    i0 = _extend_back(text, toks, *s)
    ew = [w for w, *_ in tokens(end or "")]
    if not ew:
        return None
    e = _find(seq, ew, lo=s[0], want="last")
    if e is None:
        return None
    i1 = _extend_forward(text, toks, *e)
    if i1 - i0 > max_words:
        return None
    a, b = toks[i0][1], toks[i1][2]
    # include trailing sentence punctuation right after the last word
    while b < len(text) and text[b] in ".!?)]\"'”":
        b += 1
    return re.sub(r"\s+", " ", text[a:b]).strip()


_ABS_HEAD = re.compile(r"(?im)^\s*(?:a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|summary)\b[\s.:—–-]*")
_ABS_STOP = re.compile(
    r"(?im)^\s*(?:(?:1|I)\.?\s+introduction|introduction\b|keywords?\b|key\s+words\b|index terms\b|"
    r"ccs concepts\b|acm reference format\b|mathematics subject classification\b|msc\b|contents\b|"
    r"1\s+[A-Z][a-z]+)"
)


def heuristic_abstract(text: str, max_chars: int = 3000) -> str | None:
    """Text after an 'Abstract' heading up to the next section-like heading."""
    m = _ABS_HEAD.search(text)
    if not m:
        return None
    rest = text[m.end():]
    stop = _ABS_STOP.search(rest)
    body = rest[: stop.start()] if stop else rest[:max_chars]
    body = re.sub(r"\s+", " ", body).strip()
    return body if 200 <= len(body) <= max_chars else None


def _normjoin(s: str) -> str:
    return " ".join(w for w, *_ in tokens(s))


def best_abstract(pages: str, start: str | None, end: str | None) -> tuple[str | None, str]:
    """Combine model anchors with the 'Abstract ... Introduction' heuristic.

    Returns (abstract, method) with method in anchors | heuristic | anchors+heuristic | none.
    The heuristic wins when the anchor span is a truncated piece of it (the model saw a
    clipped page and gave the last words it saw) or clearly over-runs it (the end anchor
    matched text after the abstract).
    """
    span = locate(pages, start, end)
    h = heuristic_abstract(pages)
    if span and h:
        ns, nh = _normjoin(span), _normjoin(h)
        if ns in nh and len(nh) > len(ns) and not span.rstrip().endswith((".", "!", "?")):
            return h, "anchors+heuristic"
        if nh in ns and len(ns) > 1.3 * len(nh):
            return h, "anchors+heuristic"
    if span:
        return span, "anchors"
    if h:
        return h, "heuristic"
    return None, "none"
