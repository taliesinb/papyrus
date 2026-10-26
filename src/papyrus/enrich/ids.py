"""Identifier detection and normalisation: arXiv ids, DOIs, ISBNs (+ title/name helpers).

All ``find_*`` functions scan free text (filenames, URLs, first-page text) and return
de-duplicated normalised values in order of first appearance. ``parse_*`` functions
accept a single candidate string and return ``None`` when it is not an identifier.

Normal forms:
    arXiv  '2412.06464' / 'math/0601001' (no version; version returned separately as 'v3')
    DOI    lowercase, no 'https://doi.org/' / 'doi:' prefix
    ISBN   13 digits, no hyphens (ISBN-10 is converted)
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

# --------------------------------------------------------------------------- arXiv

# Archives that issued old-style identifiers (archive/YYMMNNN), before April 2007.
OLD_ARCHIVES = (
    "acc-phys adap-org alg-geom ao-sci astro-ph atom-ph bayes-an chao-dyn chem-ph cmp-lg "
    "comp-gas cond-mat cs dg-ga funct-an gr-qc hep-ex hep-lat hep-ph hep-th math math-ph "
    "mtrl-th nlin nucl-ex nucl-th patt-sol physics plasm-ph q-alg q-bio quant-ph solv-int supr-con"
).split()
_ARCH_RE = "|".join(sorted((re.escape(a) for a in OLD_ARCHIVES), key=len, reverse=True))


@dataclass(frozen=True)
class ArxivId:
    id: str  # normalised, no version
    version: str | None = None  # 'v3'
    explicit: bool = False  # found with 'arXiv:' prefix or in an arxiv.org URL

    @property
    def versioned(self) -> str:
        return self.id + (self.version or "")

    @property
    def doi(self) -> str:
        return arxiv_doi(self.id)

    @property
    def is_old_style(self) -> bool:
        return "/" in self.id


# context words that make a match "explicit"
_EXPLICIT_CTX = re.compile(
    r"(?:arxiv(?:\.org)?(?:/(?:abs|pdf|html|format|src|e-print))?[:/ ]\s*|10\.48550/arxiv\.)$", re.I
)

# new style: YYMM.NNNN (2007-04 .. 2014-12) or YYMM.NNNNN (2015-01 ..)
_NEW_RE = re.compile(r"(\d{2})(\d{2})\.(\d{4,5})(v\d{1,3})?(?!\d|\.\d)", re.I)
_DOI_PREFIX_BEFORE = re.compile(r"10\.\d{4,9}/$")


def _new_style_boundary_ok(text: str, start: int) -> bool:
    """The character before a new-style match must not glue it to a bigger token."""
    if start == 0:
        return True
    c = text[start - 1]
    if c.isalnum() or c in "_-":
        return False
    if c == ".":  # only '...arXiv.2412.06464' (arXiv DOI)
        return text[max(0, start - 6) : start].lower() == "arxiv."
    if c == "/":  # URL path segment, but not the suffix of another DOI ('10.1234/1203.4567')
        return not _DOI_PREFIX_BEFORE.search(text[max(0, start - 16) : start])
    return True


# old style: archive/YYMMNNN, optionally written without the slash in filenames ('math0601001.pdf')
_OLD_RE = re.compile(
    rf"(?<![\w.\-])({_ARCH_RE})(/|(?=\d{{7}}(?:v\d+)?\.pdf))(\d{{2}})(\d{{2}})(\d{{3}})(v\d{{1,3}})?(?!\d)", re.I
)


# words right before a bare number that make it a page / price / phone number, not an id
_NEG_CTX = re.compile(
    r"(?:\b(?:pp?|pages?|vol|no|tel|call|phone|fax|ph|mob|usd|eur|gbp|chf|eq|fig|sec|table|tab)\.?|[$€£¥#+=§])\s*$|\d[ \t\-]+$",
    re.I,
)


def _today() -> _dt.date:
    return _dt.date.today()


def _valid_new(yy: str, mm: str, num: str) -> bool:
    y, m = int(yy), int(mm)
    if not (1 <= m <= 12) or y < 7 or (y == 7 and m < 4):
        return False
    today = _today()
    # allow next month to tolerate clock/timezone skew
    if (y, m) > (today.year % 100, today.month + 1) or y > today.year % 100:
        return False
    if (y, m) < (15, 1):
        return len(num) == 4
    return len(num) == 5


def _valid_old(yy: str, mm: str) -> bool:
    y, m = int(yy), int(mm)
    if not (1 <= m <= 12):
        return False
    if y >= 91:
        return True
    return y < 7 or (y == 7 and m <= 3)


def find_arxiv_ids(text: str, *, explicit_only: bool = False) -> list[ArxivId]:
    """All plausible arXiv ids in `text`, normalised, in order of appearance.

    Bare new-style matches (no 'arXiv:' prefix or arxiv.org URL around them) are kept
    only when the YYMM/number shape is valid; pass ``explicit_only=True`` for noisy text.
    """
    text = unquote(text) if "%2" in text or "%3" in text else text
    out: list[tuple[int, ArxivId]] = []
    for m in _NEW_RE.finditer(text):
        yy, mm, num, ver = m.groups()
        if not _new_style_boundary_ok(text, m.start()) or not _valid_new(yy, mm, num):
            continue
        before = text[max(0, m.start() - 40) : m.start()]
        explicit = bool(_EXPLICIT_CTX.search(before))
        if (explicit_only or _NEG_CTX.search(before)) and not explicit:
            continue
        out.append((m.start(), ArxivId(f"{yy}{mm}.{num}", ver.lower() if ver else None, explicit)))
    for m in _OLD_RE.finditer(text):
        arch, _sep, yy, mm, num, ver = m.groups()
        if not _valid_old(yy, mm):
            continue
        explicit = bool(_EXPLICIT_CTX.search(text[max(0, m.start() - 40) : m.start()]))
        # old style ids are distinctive enough (archive name + 7 digits) to count as explicit-ish
        if explicit_only and not explicit:
            continue
        out.append((m.start(), ArxivId(f"{arch.lower()}/{yy}{mm}{num}", ver.lower() if ver else None, explicit)))
    out.sort(key=lambda t: t[0])
    seen: dict[str, int] = {}
    result: list[ArxivId] = []
    for _, a in out:
        if a.id in seen:
            # keep the most informative occurrence (explicit / with version)
            i = seen[a.id]
            prev = result[i]
            if (a.explicit and not prev.explicit) or (a.version and not prev.version):
                result[i] = ArxivId(a.id, a.version or prev.version, a.explicit or prev.explicit)
            continue
        seen[a.id] = len(result)
        result.append(a)
    return result


def parse_arxiv_id(s: str) -> ArxivId | None:
    """Parse a single arXiv id / URL / filename / arXiv DOI; None if not exactly one id."""
    s = s.strip()
    if not s:
        return None
    found = find_arxiv_ids(s)
    if len(found) != 1:
        return None
    return found[0]


def normalize_arxiv_id(s: str) -> str | None:
    a = parse_arxiv_id(s)
    return a.id if a else None


def arxiv_doi(arxiv_id: str) -> str:
    """DataCite DOI arXiv assigns to every paper (old-style ids keep their slash)."""
    return f"10.48550/arxiv.{arxiv_id}"


def arxiv_id_from_doi(doi: str) -> ArxivId | None:
    d = normalize_doi(doi)
    if d and d.startswith("10.48550/arxiv."):
        return parse_arxiv_id(d)
    return None


# --------------------------------------------------------------------------- DOI

# Unicode quotes/brackets never occur in real DOIs but do follow them in PDF text
# ('10.1177/2399808326146067⟩', '“10.1000/x”'): stop the match there.
_UNI_STOP = "—–⟨⟩〈〉《》«»‹›“”„‟‘’‚‛「」『』【】（）［］｛｝…。，；：  ​"
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>{}\[\]|\\^`" + _UNI_STOP + r"]+)", re.I)
_TRAILING_JUNK = ".,;:!?*'\"–—‐‑†‡·•"
# template placeholders: '10.1017/xxxxx', '10.1000/XXXXXXX', '10.1234/0000000', '10.1093/mnras/xxx'
_DOI_PLACEHOLDER = re.compile(r"^10\.(?:0+|9{4,})/|/[\W_]*[x0#*?]+[\W_]*$|/[x0]{3,}$|[/.\-]x{3,}$", re.I)
_DOI_URL_SUFFIXES = re.compile(r"(?:\.pdf|/(?:full|abstract|pdf|epdf|epub|meta|summary|references))$", re.I)


def _clean_doi_tail(d: str) -> str:
    while True:
        prev = d
        d = d.rstrip(_TRAILING_JUNK)
        # strip unbalanced closing brackets
        for o, c in ("()", "[]", "{}"):
            while d.endswith(c) and d.count(c) > d.count(o):
                d = d[:-1]
        d = _DOI_URL_SUFFIXES.sub("", d)
        if d == prev:
            return d


def find_dois(text: str) -> list[str]:
    """DOIs in free text / URLs (doi.org links, 'doi:' prefixes, %-encoded URLs)."""
    if "%2f" in text.lower():
        text = unquote(text)
    out: list[str] = []
    for m in _DOI_RE.finditer(text):
        d = _clean_doi_tail(m.group(1))
        if "/" not in d or d.endswith("/") or _DOI_PLACEHOLDER.search(d):
            continue
        d = d.lower()
        if d not in out:
            out.append(d)
    return out


def normalize_doi(s: str) -> str | None:
    found = find_dois(s.strip())
    return found[0] if found else None


def is_arxiv_doi(doi: str | None) -> bool:
    return bool(doi) and doi.lower().startswith("10.48550/arxiv.")


# --------------------------------------------------------------------------- ISBN


def _isbn10_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", d):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
    return total % 11 == 0


def _isbn13_ok(d: str) -> bool:
    if not re.fullmatch(r"97[89]\d{10}", d):
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(d))
    return total % 10 == 0


def isbn10_to_13(d: str) -> str:
    core = "978" + d[:9]
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def isbn13_to_10(d: str) -> str | None:
    if not d.startswith("978"):
        return None
    core = d[3:12]
    s = sum((10 - i) * int(c) for i, c in enumerate(core))
    check = (11 - s % 11) % 11
    return core + ("X" if check == 10 else str(check))


def normalize_isbn(s: str) -> str | None:
    """Validated ISBN-13 (no hyphens) from an ISBN-10/13 string, or None."""
    d = re.sub(r"[\s\-‐‑–]", "", s.strip()).upper()
    d = re.sub(r"^ISBN(?:-?1[03])?:?", "", d)
    if len(d) == 13 and _isbn13_ok(d):
        return d
    if len(d) == 10 and _isbn10_ok(d):
        return isbn10_to_13(d)
    return None


_ISBN_LABELLED = re.compile(r"ISBN(?:-?1[03])?[:\s]*((?:97[89][\s\-‐‑–]?)?(?:\d[\s\-‐‑–]?){9}[\dXx])\b", re.I)
_ISBN13_BARE = re.compile(r"(?<![\d\-])(97[89](?:[\-‐‑– ]?\d){10})(?![\d\-])")
_ISBN10_HYPH = re.compile(r"(?<![\d\-])(\d{1,5}-\d{1,7}-\d{1,7}-[\dXx])(?![\d\-])")


def find_isbns(text: str) -> list[str]:
    """Valid ISBNs (as ISBN-13): labelled 'ISBN ...' ones, bare 978/979 13-digit ones, and
    fully hyphenated ISBN-10s ('0-13-461099-7'). Bare unhyphenated 10-digit numbers are
    ignored (too many false positives: phone numbers, ids)."""
    out: list[tuple[int, str]] = []
    for rx in (_ISBN_LABELLED, _ISBN13_BARE, _ISBN10_HYPH):
        for m in rx.finditer(text):
            raw = m.group(1)
            if rx is _ISBN10_HYPH and len(re.sub(r"\D", "", raw.upper().replace("X", "0"))) != 10:
                continue
            n = normalize_isbn(raw)
            if n:
                out.append((m.start(1), n))
    out.sort()
    res: list[str] = []
    for _, n in out:
        if n not in res:
            res.append(n)
    return res


# --------------------------------------------------------------------------- aggregate


@dataclass
class Identifiers:
    arxiv: list[ArxivId]
    dois: list[str]
    isbns: list[str]

    @property
    def best_arxiv(self) -> ArxivId | None:
        if not self.arxiv:
            return None
        return next((a for a in self.arxiv if a.explicit), self.arxiv[0])


def detect(text: str) -> Identifiers:
    """Every identifier in `text`. arXiv DOIs are reported both as DOI and as arXiv id."""
    dois = find_dois(text)
    arx = find_arxiv_ids(text)
    return Identifiers(arxiv=arx, dois=dois, isbns=find_isbns(text))


# --------------------------------------------------------------------------- text helpers

_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\*?(?:\{([^{}]*)\})?")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_title(t: str | None) -> str:
    """Lowercase ascii words separated by single spaces (LaTeX/markup/punctuation removed)."""
    if not t:
        return ""
    t = re.sub(r"<[^>]+>", " ", t)  # JATS / html
    t = _LATEX_CMD.sub(lambda m: m.group(1) or " ", t)
    t = t.replace("$", " ").replace("{", "").replace("}", "")
    t = strip_accents(t).lower().replace("&", " and ")
    return _NON_ALNUM.sub(" ", t).strip()


_PARTICLES = {
    "van",
    "von",
    "der",
    "den",
    "de",
    "del",
    "della",
    "di",
    "da",
    "du",
    "le",
    "la",
    "dos",
    "das",
    "ter",
    "ten",
    "bin",
    "al",
    "el",
    "st",
}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def split_name(name: str) -> tuple[str | None, str | None]:
    """(given, family) from a display name: 'Tri Dao', 'Dao, Tri', 'Ludwig van Beethoven'."""
    name = " ".join(name.split())
    if not name:
        return None, None
    if "," in name:
        fam, _, giv = name.partition(",")
        giv = giv.strip()
        if giv.lower().rstrip(".") in {s.rstrip(".") for s in _SUFFIXES}:
            return None, fam.strip() or None
        return giv or None, fam.strip() or None
    toks = name.split(" ")
    while len(toks) > 1 and toks[-1].lower() in _SUFFIXES:
        toks = toks[:-1]
    if len(toks) == 1:
        return None, toks[0]
    i = len(toks) - 1
    while i > 1 and toks[i - 1].lower() in _PARTICLES:
        i -= 1
    return " ".join(toks[:i]) or None, " ".join(toks[i:])


def surname_key(name_or_family: str, *, is_family: bool = False) -> str:
    """Comparable surname: accents/punctuation/particles stripped ('van der Waals' -> 'waals')."""
    fam = name_or_family if is_family else (split_name(name_or_family)[1] or "")
    words = normalize_title(fam).split()
    words = [w for w in words if w not in _PARTICLES] or words
    return words[-1] if words else ""
