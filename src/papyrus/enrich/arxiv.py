"""arXiv export API client (Atom feed).

API docs: https://info.arxiv.org/help/api/user-manual.html — at most 1 request every
3 seconds (enforced by the shared HTTP client's per-host limiter), single connection.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence

from papyrus.enrich import ids as _ids
from papyrus.enrich.http import HttpClient, default_client
from papyrus.models import AuthorRecord, WorkRecord

API = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
BATCH = 100

_STOP = set(
    "a an and are as at be by for from in into is it of on or the to with via towards toward "
    "using its their our we can not".split()
)


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = " ".join(el.text.split())
    return t or None


def parse_entry(entry: ET.Element) -> WorkRecord | None:
    raw_id = _text(entry.find("atom:id", NS)) or ""
    title = _text(entry.find("atom:title", NS))
    if not title or title == "Error" or "/api/errors" in raw_id:
        return None
    aid = _ids.parse_arxiv_id(raw_id)
    if aid is None:
        return None
    authors: list[AuthorRecord] = []
    for a in entry.findall("atom:author", NS):
        name = _text(a.find("atom:name", NS))
        if not name:
            continue
        given, family = _ids.split_name(name)
        affs = [t for t in (_text(x) for x in a.findall("arxiv:affiliation", NS)) if t]
        authors.append(AuthorRecord(name=name, given=given, family=family, affiliations=affs))
    published = _text(entry.find("atom:published", NS))
    updated = _text(entry.find("atom:updated", NS))
    doi = _text(entry.find("arxiv:doi", NS))
    doi = _ids.normalize_doi(doi) if doi else None
    journal_ref = _text(entry.find("arxiv:journal_ref", NS))
    comment = _text(entry.find("arxiv:comment", NS))
    prim = entry.find("arxiv:primary_category", NS)
    primary = prim.get("term") if prim is not None else None
    cats = [c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")]
    if primary and primary in cats:
        cats.remove(primary)
        cats.insert(0, primary)
    elif primary:
        cats.insert(0, primary)
    record_ids = {"arxiv": aid.id}
    if doi and not _ids.is_arxiv_doi(doi):
        record_ids["doi"] = doi
    urls = [f"https://arxiv.org/abs/{aid.id}"]
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf" and link.get("href"):
            urls.append(link.get("href").replace("http://", "https://"))
    return WorkRecord(
        source="arxiv",
        title=title,
        authors=authors,
        abstract=_text(entry.find("atom:summary", NS)),
        year=int(published[:4]) if published else None,
        date=published[:10] if published else None,
        venue=journal_ref,
        kind="paper",
        ids=record_ids,
        urls=urls,
        arxiv_categories=[c for c in cats if c],
        arxiv_version=aid.version,
        raw={
            "id": raw_id,
            "published": published,
            "updated": updated,
            "journal_ref": journal_ref,
            "comment": comment,
            "doi": doi,
            "primary_category": primary,
            "versioned_id": aid.versioned,
        },
    )


def parse_feed(xml_text: str) -> list[WorkRecord]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("atom:entry", NS):
        r = parse_entry(e)
        if r is not None:
            out.append(r)
    return out


def fetch(arxiv_ids: Iterable[str], *, http: HttpClient | None = None) -> list[WorkRecord]:
    """Records for the given ids (any accepted form; versions are dropped so the latest
    version is returned). Unknown ids are silently missing from the result."""
    http = http or default_client()
    norm: list[str] = []
    for s in arxiv_ids:
        a = _ids.parse_arxiv_id(s)
        if a and a.id not in norm:
            norm.append(a.id)
    out: list[WorkRecord] = []
    for i in range(0, len(norm), BATCH):
        chunk = norm[i : i + BATCH]
        r = http.get(API, params={"id_list": ",".join(chunk), "max_results": len(chunk)}, ok_statuses=frozenset({200}))
        out.extend(parse_feed(r.text))
    order = {a: i for i, a in enumerate(norm)}
    out.sort(key=lambda w: order.get(w.ids.get("arxiv", ""), 1 << 30))
    return out


def get(arxiv_id: str, *, http: HttpClient | None = None) -> WorkRecord | None:
    res = fetch([arxiv_id], http=http)
    return res[0] if res else None


def _title_terms(title: str, limit: int = 12) -> list[str]:
    words = [w for w in _ids.normalize_title(title).split() if w not in _STOP and len(w) > 1]
    # longest words carry most information; keep original order
    if len(words) > limit:
        keep = set(sorted(words, key=len, reverse=True)[:limit])
        words = [w for w in words if w in keep]
    return words


def build_query(title: str | None = None, authors: Sequence[str] = ()) -> str:
    parts = [f"ti:{w}" for w in _title_terms(title or "")]
    for a in authors[:3]:
        s = _ids.surname_key(a)
        if s:
            parts.append(f"au:{s}")
    return " AND ".join(parts)


def search(
    title: str | None = None,
    authors: Sequence[str] = (),
    *,
    max_results: int = 10,
    http: HttpClient | None = None,
) -> list[WorkRecord]:
    """Search by title words (+ author surnames). Falls back to title-only when the
    author-constrained query finds nothing (names are often transliterated differently)."""
    http = http or default_client()
    queries = [build_query(title, authors)]
    if authors and title:
        queries.append(build_query(title))
    for q in queries:
        if not q:
            continue
        r = http.get(
            API,
            params={"search_query": q, "max_results": max_results, "sortBy": "relevance"},
            ok_statuses=frozenset({200}),
        )
        res = parse_feed(r.text)
        if res:
            return res
    return []


def total_results(xml_text: str) -> int | None:
    m = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", xml_text)
    return int(m.group(1)) if m else None
