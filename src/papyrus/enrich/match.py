"""Resolve a partial description of a document into scored candidate records, and merge.

    resolve(hint)      -> list[WorkRecord] from all sources, sorted by match_score (0..1)
    cluster(records)   -> groups of records describing the same work (preprint + published)
    merge(records)     -> one WorkRecord combining the best field from each source
    resolve_best(hint) -> merge(top cluster) or None

A hint is a `Hint` or a mapping with optional keys arxiv_id, doi, isbn, title,
authors (list of names), year.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any

import httpx

from papyrus.enrich import arxiv, crossref, openalex, semanticscholar
from papyrus.enrich import ids as _ids
from papyrus.enrich.http import HttpClient, HttpError
from papyrus.models import AuthorRecord, WorkRecord

log = logging.getLogger(__name__)

ALL_SOURCES = ("arxiv", "openalex", "s2", "crossref")


@dataclass
class Hint:
    arxiv_id: str | None = None
    doi: str | None = None
    isbn: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None

    @classmethod
    def of(cls, h: Hint | Mapping[str, Any]) -> Hint:
        if isinstance(h, Hint):
            return h
        authors = h.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r";| and ", authors) if a.strip()]
        year = h.get("year")
        return cls(
            arxiv_id=h.get("arxiv_id") or h.get("arxiv"),
            doi=h.get("doi"),
            isbn=h.get("isbn"),
            title=h.get("title"),
            authors=list(authors),
            year=int(year) if year else None,
        )


# --------------------------------------------------------------------------- scoring

_EDITION = re.compile(
    r"\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"revised|updated|international|global|new)?\s*(?:edition|ed)\b\.?",
    re.I,
)


def match_title_key(t: str | None) -> str:
    """normalize_title + edition statements removed (books)."""
    return _ids.normalize_title(_EDITION.sub(" ", t or ""))


def title_similarity(a: str | None, b: str | None) -> float:
    ka, kb = match_title_key(a), match_title_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ratio = SequenceMatcher(None, ka, kb, autojunk=False).ratio()
    wa, wb = ka.split(), kb.split()
    sa, sb = set(wa), set(wb)
    jacc = len(sa & sb) / len(sa | sb)
    # one title is the other minus a subtitle ('Transformers are SSMs' vs the full title)
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    prefix = 0.0
    if len(short) >= 3 and long_[: len(short)] == short:
        prefix = 0.93 if len(short) >= 0.3 * len(long_) else 0.88
    return max(ratio, jacc, prefix)


def _surnames(names: Iterable[str]) -> list[str]:
    return [s for s in (_ids.surname_key(n) for n in names) if s]


def _rec_surnames(rec: WorkRecord) -> list[str]:
    out = []
    for a in rec.authors:
        s = _ids.surname_key(a.family, is_family=True) if a.family else _ids.surname_key(a.name)
        if s:
            out.append(s)
    return out


def _same_surname(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.85)


def author_overlap(hint_authors: Sequence[str], rec: WorkRecord) -> float | None:
    hs = _surnames(hint_authors)
    rs = _rec_surnames(rec)
    if not hs or not rs:
        return None
    hits = sum(1 for h in hs if any(_same_surname(h, r) for r in rs))
    return hits / len(hs)


def year_score(hint_year: int | None, rec_year: int | None) -> float | None:
    if not hint_year or not rec_year:
        return None
    d = abs(hint_year - rec_year)
    return {0: 1.0, 1: 0.8, 2: 0.5}.get(d, 0.15)


def score(hint: Hint, rec: WorkRecord) -> float:
    """Weighted title (0.6) / authors (0.25) / year (0.15) score over available signals."""
    parts: list[tuple[float, float]] = []
    tsim = None
    if hint.title:
        tsim = title_similarity(hint.title, rec.title)
        parts.append((0.6, tsim))
    if (ao := author_overlap(hint.authors, rec)) is not None:
        parts.append((0.25, ao))
    if (ys := year_score(hint.year, rec.year)) is not None:
        parts.append((0.15, ys))
    if not parts:
        return 0.0
    s = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    if tsim is not None and tsim < 0.6:
        s = min(s, tsim)  # authors/year must not rescue a different title
    return round(s, 4)


def _id_score(ref_title: str | None, rec: WorkRecord) -> float:
    """Score for a record reached through an identifier: ~1, unless the title disagrees
    with the reference title (corrupt index entries exist)."""
    if not ref_title:
        return 0.97
    t = title_similarity(ref_title, rec.title)
    if t >= 0.85:
        return round(0.95 + 0.05 * t, 4)
    return round(0.5 * t, 4)


# --------------------------------------------------------------------------- resolve


class _Collector:
    def __init__(self, sources: Sequence[str]):
        self.sources = set(sources)
        self.records: list[WorkRecord] = []
        self.errors: list[str] = []
        self.rate_limited: dict[str, float] = {}  # source -> seconds it asked us to wait (429/503)

    def call(self, source: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        if source not in self.sources:
            return None
        try:
            return fn(*args, **kw)
        except (HttpError, httpx.HTTPError, ET.ParseError, ValueError, KeyError) as e:
            if isinstance(e, HttpError) and e.status in (429, 503):
                self.rate_limited[source] = max(self.rate_limited.get(source, 0.0), getattr(e, "retry_after", 0.0))
            # the status, not the message: an HttpError's names its URL, which holds search terms (a
            # title), and this goes to the watcher's log, which names ids and counts only
            what = f"HTTP {e.status}" if isinstance(e, HttpError) else type(e).__name__
            msg = f"{source}.{getattr(fn, '__name__', fn)}: {what}"
            log.warning("%s", msg)
            self.errors.append(msg)
            return None

    def add(self, rec: WorkRecord | None, s: float) -> WorkRecord | None:
        if rec is None:
            return None
        rec.match_score = s
        key = _primary_key(rec)
        for i, old in enumerate(self.records):
            if _primary_key(old) == key:
                if (old.match_score or 0) < s:
                    self.records[i] = rec
                return self.records[i]
        self.records.append(rec)
        return rec


def _primary_key(rec: WorkRecord) -> tuple[str, str]:
    scheme = {"arxiv": "arxiv", "openalex": "openalex", "s2": "s2", "crossref": "doi"}.get(rec.source, "")
    return rec.source, rec.ids.get(scheme) or _ids.normalize_title(rec.title)


def _is_preprint(rec: WorkRecord) -> bool:
    if rec.source == "arxiv":
        return True
    if rec.source == "openalex":
        return (rec.raw or {}).get("type") == "preprint" or ("arxiv" in rec.ids and "doi" not in rec.ids)
    return False


def resolve(
    hint: Hint | Mapping[str, Any],
    *,
    sources: Sequence[str] = ALL_SOURCES,
    find_published: bool = True,
    http: HttpClient | None = None,
    report: dict[str, Any] | None = None,
) -> list[WorkRecord]:
    """Candidate records from all sources for `hint`, best first (match_score set).
    `report`, if given, receives `rate_limited`: {source: seconds to wait} for sources that refused
    with 429/503 (so "nothing found" may only mean "not asked").

    Identifier route first (arXiv id → arXiv + OpenAlex + S2, then the published
    version via its DOI or an OpenAlex title search; DOI → OpenAlex + Crossref + S2;
    ISBN → Crossref), then title search across sources when ids are absent or found
    nothing. Errors from individual sources are logged and skipped.
    """
    h = Hint.of(hint)
    c = _Collector(sources)
    kw = {"http": http}

    arx_id = _ids.parse_arxiv_id(h.arxiv_id) if h.arxiv_id else None
    doi = _ids.normalize_doi(h.doi) if h.doi else None
    if doi and _ids.is_arxiv_doi(doi):
        arx_id = arx_id or _ids.arxiv_id_from_doi(doi)
        doi = None
    isbn = _ids.normalize_isbn(h.isbn) if h.isbn else None
    ref = replace(h)  # reference description used for scoring; refined by authoritative records

    def refine(rec: WorkRecord | None) -> None:
        if rec is None:
            return
        if not ref.title or title_similarity(ref.title, rec.title) >= 0.85:
            ref.title = rec.title
            ref.authors = ref.authors or [a.name for a in rec.authors]
            ref.year = ref.year or rec.year

    published_dois: list[str] = [doi] if doi else []
    seen_arxiv: set[str] = set()

    def via_arxiv(aid: str) -> None:
        if aid in seen_arxiv:
            return
        seen_arxiv.add(aid)
        a = c.call("arxiv", arxiv.get, aid, **kw)
        if a:
            refine(a)
            c.add(a, _id_score(h.title, a) if h.title else 1.0)
            if d := a.ids.get("doi"):
                published_dois.append(d)
        o = c.call("openalex", openalex.get_by_arxiv, aid, **kw)
        if o:
            c.add(o, _id_score(ref.title, o))
            if (d := o.ids.get("doi")) and _id_score(ref.title, o) > 0.9:
                published_dois.append(d)
        s = c.call("s2", semanticscholar.get, f"arXiv:{aid}", **kw)
        if s:
            if not a:
                refine(s)
            c.add(s, _id_score(ref.title, s))
            if (d := s.ids.get("doi")) and _id_score(ref.title, s) > 0.9:
                published_dois.append(d)

    if arx_id:
        via_arxiv(arx_id.id)

    fetched_dois: set[str] = set()
    for d in list(dict.fromkeys(published_dois)):
        if d in fetched_dois:
            continue
        fetched_dois.add(d)
        cr = c.call("crossref", crossref.get, d, **kw)
        if cr:
            refine(cr)
            c.add(cr, _id_score(ref.title, cr))
        o = c.call("openalex", openalex.get_by_doi, d, **kw)
        if o:
            c.add(o, _id_score(ref.title, o))
            if not arx_id and (a := o.ids.get("arxiv")):
                via_arxiv(a)
        if not arx_id:
            s = c.call("s2", semanticscholar.get, f"DOI:{d}", **kw)
            if s:
                c.add(s, _id_score(ref.title, s))
                if a := s.ids.get("arxiv"):
                    via_arxiv(a)

    if isbn:
        for r in c.call("crossref", crossref.by_isbn, isbn, **kw) or []:
            if r.kind == "book":
                refine(r)
                c.add(r, _id_score(ref.title, r) if ref.title else 0.95)

    found_by_id = any((r.match_score or 0) >= 0.9 for r in c.records)
    if (not found_by_id or find_published) and ref.title:
        _title_route(ref, c, found_by_id=found_by_id, http=http)

    recs = sorted(c.records, key=lambda r: -(r.match_score or 0))
    if report is not None:
        report["rate_limited"] = dict(c.rate_limited)
    return recs


def _title_route(ref: Hint, c: _Collector, *, found_by_id: bool, http: HttpClient | None) -> None:
    kw = {"http": http}
    have_published = any(not _is_preprint(r) and (r.match_score or 0) >= 0.9 for r in c.records)
    if found_by_id:
        # only look for a published version we have not seen yet (one OpenAlex search, 10 credits)
        if have_published and any(r.source == "openalex" and not _is_preprint(r) for r in c.records):
            return
        for r in c.call("openalex", openalex.search, ref.title, **kw) or []:
            s = score(ref, r)
            if s >= 0.75:
                c.add(r, s)
        return
    for r in c.call("arxiv", arxiv.search, ref.title, ref.authors, max_results=5, **kw) or []:
        c.add(r, score(ref, r))
    for r in c.call("openalex", openalex.search, ref.title, **kw) or []:
        c.add(r, score(ref, r))
    s2 = c.call("s2", semanticscholar.match_title, ref.title, **kw)
    if s2:
        c.add(s2, score(ref, s2))
    for r in c.call("crossref", crossref.search, ref.title, ref.authors, rows=5, **kw) or []:
        c.add(r, score(ref, r))
    # follow up the best hit's arXiv id when no arXiv record turned up
    best = max(c.records, key=lambda r: r.match_score or 0, default=None)
    if best and (best.match_score or 0) >= 0.85 and (aid := best.ids.get("arxiv")):
        if not any(r.source == "arxiv" and r.ids.get("arxiv") == aid for r in c.records):
            a = c.call("arxiv", arxiv.get, aid, **kw)
            if a:
                c.add(a, score(ref, a))


# --------------------------------------------------------------------------- clustering


def same_work(a: WorkRecord, b: WorkRecord) -> bool:
    t = title_similarity(a.title, b.title)
    for k in ("arxiv", "doi", "isbn", "openalex", "s2"):
        if a.ids.get(k) and a.ids.get(k) == b.ids.get(k):
            return t >= 0.6  # shared id, but guard against corrupt index entries
    if t < 0.9:
        return False
    if a.year and b.year and abs(a.year - b.year) > 2 and a.kind != "book":
        return False
    ao = author_overlap([x.name for x in a.authors], b)
    return ao is None or ao >= 0.5


def cluster(records: Sequence[WorkRecord]) -> list[list[WorkRecord]]:
    """Union-find over `same_work`; clusters ordered by their best match_score."""
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if same_work(records[i], records[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[WorkRecord]] = {}
    for i, r in enumerate(records):
        groups.setdefault(find(i), []).append(r)
    out = list(groups.values())
    for g in out:
        g.sort(key=lambda r: -(r.match_score or 0))
    out.sort(key=lambda g: -(g[0].match_score or 0))
    return out


# --------------------------------------------------------------------------- merge

_PREF = {
    "title": ("arxiv", "crossref", "s2", "openalex", "dblp"),
    "abstract": ("arxiv", "s2", "openalex", "crossref"),
    "authors": ("arxiv", "crossref", "openalex", "s2", "dblp"),
    "venue": ("crossref", "openalex", "s2", "dblp", "arxiv"),
    "kind": ("crossref", "openalex", "s2", "dblp", "arxiv"),
    "ids": ("arxiv", "crossref", "openalex", "s2", "dblp"),
}


def _ordered(records: Sequence[WorkRecord], what: str) -> list[WorkRecord]:
    pref = _PREF[what]
    rank = {s: i for i, s in enumerate(pref)}
    # within a source: published versions before preprints, then higher match score
    return sorted(records, key=lambda r: (rank.get(r.source, len(pref)), _is_preprint(r), -(r.match_score or 0)))


def _initial(a: AuthorRecord) -> str:
    g = a.given or _ids.split_name(a.name)[0] or ""
    g = _ids.normalize_title(g)
    return g[:1]


def _akey(a: AuthorRecord) -> str:
    return _ids.surname_key(a.family, is_family=True) if a.family else _ids.surname_key(a.name)


def _align(base: list[AuthorRecord], other: list[AuthorRecord]) -> list[AuthorRecord | None]:
    """For each base author, the matching author of `other` (surname + initial, or position)."""
    used: set[int] = set()
    out: list[AuthorRecord | None] = []
    for i, a in enumerate(base):
        ka, ia = _akey(a), _initial(a)
        hit = None
        for j, b in enumerate(other):
            if j in used:
                continue
            if _same_surname(ka, _akey(b)) and (not ia or not _initial(b) or ia == _initial(b)):
                hit = j
                break
        if hit is None and len(base) == len(other) and i not in used:
            b = other[i]
            if _same_surname(ka, _akey(b)):
                hit = i
        if hit is not None:
            used.add(hit)
            out.append(other[hit])
        else:
            out.append(None)
    return out


def _merge_authors(records: Sequence[WorkRecord]) -> list[AuthorRecord]:
    ordered = [r for r in _ordered(records, "authors") if r.authors]
    if not ordered:
        return []
    most = max(len(r.authors) for r in ordered)
    base_rec = next(r for r in ordered if len(r.authors) * 2 > most)
    base = [replace(a, affiliations=list(a.affiliations)) for a in base_rec.authors]
    for a in base:
        if not a.family:
            a.given, a.family = _ids.split_name(a.name)
    for r in ordered:
        if r is base_rec:
            continue
        for a, b in zip(base, _align(base, r.authors), strict=True):
            if b is None:
                continue
            a.orcid = a.orcid or b.orcid
            a.openalex_id = a.openalex_id or b.openalex_id
            a.s2_id = a.s2_id or b.s2_id
            a.dblp_pid = a.dblp_pid or b.dblp_pid
            if r.source == "crossref" and b.given and b.family:
                # Crossref splits names authoritatively
                if len(b.given) >= len(a.given or ""):
                    a.given, a.family = b.given, b.family
            for aff in b.affiliations:
                if aff not in a.affiliations:
                    a.affiliations.append(aff)
    return base


def _first(records: Sequence[WorkRecord], what: str, attr: str, ok: Callable[[Any], bool] = bool) -> Any:
    for r in _ordered(records, what):
        v = getattr(r, attr)
        if ok(v):
            return v
    return None


def merge(records: Sequence[WorkRecord]) -> WorkRecord:
    """Combine records of one work (e.g. a `cluster`) into a 'merged' WorkRecord.

    Preferences: title arXiv > Crossref > S2 > OpenAlex; abstract arXiv > S2 > OpenAlex
    > Crossref; authors from arXiv/Crossref enriched with OpenAlex ids/ORCID/affiliations
    and S2 ids; venue/date from the published version (Crossref, OpenAlex non-preprint)
    when there is one, else arXiv. raw = {'sources': {source: raw}, ...}.
    """
    if not records:
        raise ValueError("merge() needs at least one record")
    records = list(records)
    published = [r for r in records if not _is_preprint(r) and (r.venue or r.source == "crossref")]
    dated = _ordered(published, "venue") or _ordered(records, "ids")
    date_rec = next((r for r in dated if r.year), None)
    year = date_rec.year if date_rec else None
    date = date_rec.date if date_rec else None
    if date_rec is not None and date_rec.source == "s2" and (vy := (date_rec.raw or {}).get("venue_year")):
        if vy != year:  # S2's year/date are often the preprint's; DBLP key has the venue year
            year, date = vy, None

    rec_ids: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for r in _ordered(records, "ids"):
        for k, v in r.ids.items():
            if k not in rec_ids:
                rec_ids[k] = v
            elif rec_ids[k] != v and v not in conflicts.setdefault(k, []):
                conflicts[k].append(v)
    urls: list[str] = []
    for r in records:
        for u in r.urls:
            if u not in urls:
                urls.append(u)
    topics: list[dict[str, Any]] = []
    seen_topics: set[tuple[Any, Any]] = set()
    for r in records:
        for t in r.topics:
            key = (t.get("scheme"), t.get("name"))
            if key not in seen_topics:
                seen_topics.add(key)
                topics.append(t)
    arx = next((r for r in records if r.source == "arxiv"), None)
    raw_sources: dict[str, Any] = {}
    for r in records:
        k, n = r.source, 2
        while k in raw_sources:
            k, n = f"{r.source}#{n}", n + 1
        raw_sources[k] = r.raw
    raw: dict[str, Any] = {
        "sources": raw_sources,
        "match_scores": {k: r.match_score for k, r in zip(raw_sources, records, strict=True)},
        "dates": {k: r.date or r.year for k, r in zip(raw_sources, records, strict=True)},
    }
    if conflicts:
        raw["id_conflicts"] = {k: v for k, v in conflicts.items() if v}
    s2 = next((r for r in records if r.source == "s2"), None)
    if s2 and s2.raw:
        if s2.raw.get("tldr"):
            raw["tldr"] = s2.raw["tldr"]
        if s2.raw.get("embedding"):
            raw["embedding"] = s2.raw["embedding"]
    if arx and arx.raw.get("journal_ref"):
        raw["journal_ref"] = arx.raw["journal_ref"]
    first_dates = [r.date for r in records if r.date]
    if first_dates:
        raw["first_seen"] = min(first_dates)
    cites = [r.citation_count for r in records if r.citation_count is not None]
    scores = [r.match_score for r in records if r.match_score is not None]
    return WorkRecord(
        source="merged",
        title=_first(records, "title", "title") or "",
        authors=_merge_authors(records),
        abstract=_first(records, "abstract", "abstract", lambda v: bool(v) and len(v) > 40),
        year=year,
        date=date,
        venue=_first(published, "venue", "venue") or _first(records, "venue", "venue"),
        kind=_first(records, "kind", "kind"),
        ids=rec_ids,
        urls=urls,
        topics=topics,
        arxiv_categories=list(arx.arxiv_categories) if arx else [],
        arxiv_version=arx.arxiv_version if arx else None,
        citation_count=max(cites) if cites else None,
        raw=raw,
        match_score=max(scores) if scores else None,
    )


def resolve_best(hint: Hint | Mapping[str, Any], *, min_score: float = 0.75, **kw: Any) -> WorkRecord | None:
    """merge() of the best cluster among resolve() results scoring >= min_score."""
    recs = [r for r in resolve(hint, **kw) if (r.match_score or 0) >= min_score]
    if not recs:
        return None
    return merge(cluster(recs)[0])
