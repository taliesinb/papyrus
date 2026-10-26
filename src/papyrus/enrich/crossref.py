"""Crossref REST API client (https://api.crossref.org) — no key needed.

Requests carry ``mailto`` (env PAPYRUS_MAILTO) for the polite pool; the API advertises
its limit in ``x-rate-limit-limit`` / ``x-rate-limit-interval`` (3 req/s seen 2026-10).
Crossref only knows Crossref-registered DOIs: arXiv (DataCite) DOIs are 404 here, and
many books (e.g. Pearson textbooks) are not deposited at all.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from papyrus.enrich import ids as _ids
from papyrus.enrich.http import HttpClient, default_client, mailto
from papyrus.models import AuthorRecord, WorkRecord

API = "https://api.crossref.org"
KIND = {
    "journal-article": "paper",
    "proceedings-article": "paper",
    "posted-content": "paper",
    "peer-review": "paper",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "book-set": "book",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "book-part": "chapter",
    "reference-entry": "chapter",
    "dissertation": "thesis",
    "report": "report",
    "report-component": "report",
    "dataset": "dataset",
    "standard": "standard",
}


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    p = dict(extra)
    if m := mailto():
        p["mailto"] = m
    return p


def _strip_jats(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"<jats:title>.*?</jats:title>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = " ".join(s.split())
    return s or None


def _date(item: dict[str, Any]) -> tuple[int | None, str | None]:
    for k in ("published-print", "published-online", "published", "issued", "created"):
        parts = ((item.get(k) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y = int(parts[0])
            if len(parts) >= 3:
                return y, f"{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            if len(parts) == 2:
                return y, f"{y:04d}-{int(parts[1]):02d}"
            return y, None
    return None, None


def parse_item(item: dict[str, Any]) -> WorkRecord:
    authors = []
    people = item.get("author") or item.get("editor") or []
    for a in people:
        given, family = a.get("given"), a.get("family")
        name = " ".join(x for x in (given, family) if x) or a.get("name") or ""
        orcid = None
        if a.get("ORCID"):
            m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", a["ORCID"])
            orcid = m.group(1) if m else None
        authors.append(
            AuthorRecord(
                name=name,
                given=given,
                family=family or (None if given else a.get("name")),
                orcid=orcid,
                affiliations=[x["name"] for x in a.get("affiliation") or [] if x.get("name")],
            )
        )
    title = " ".join((item.get("title") or [""])[0].split())
    sub = item.get("subtitle") or []
    if sub and sub[0] and sub[0].lower() not in title.lower():
        title = f"{title}: {' '.join(sub[0].split())}"
    year, date = _date(item)
    rec_ids: dict[str, str] = {}
    if doi := _ids.normalize_doi(item.get("DOI") or ""):
        rec_ids["doi"] = doi
    isbns = [n for n in (_ids.normalize_isbn(x) for x in item.get("ISBN") or []) if n]
    if isbns:
        rec_ids["isbn"] = isbns[0]
    if item.get("ISSN"):
        rec_ids["issn"] = item["ISSN"][0]
    venue = (item.get("container-title") or [None])[0]
    if not venue and item.get("event"):
        venue = item["event"].get("name")
    if not venue and item.get("type") in ("book", "monograph", "edited-book", "reference-book"):
        venue = item.get("publisher")
    urls = [u for u in [item.get("URL")] if u]
    for link in item.get("link") or []:
        if link.get("URL") and link["URL"] not in urls:
            urls.append(link["URL"])
    topics = [{"name": s, "scheme": "crossref-subject"} for s in item.get("subject") or []]
    raw = dict(item)
    return WorkRecord(
        source="crossref",
        title=title,
        authors=authors,
        abstract=_strip_jats(item.get("abstract")),
        year=year,
        date=date,
        venue=venue,
        kind=KIND.get(item.get("type") or "", "other" if item.get("type") else None),
        ids=rec_ids,
        urls=urls,
        topics=topics,
        citation_count=item.get("is-referenced-by-count"),
        raw=raw,
    )


def get(doi: str, *, http: HttpClient | None = None) -> WorkRecord | None:
    d = _ids.normalize_doi(doi)
    if not d or _ids.is_arxiv_doi(d):
        return None
    http = http or default_client()
    data = http.get_json(f"{API}/works/{d}", params=_params({}))
    return parse_item(data["message"]) if data and data.get("message") else None


def _list(params: dict[str, Any], http: HttpClient | None) -> list[WorkRecord]:
    http = http or default_client()
    data = http.get_json(f"{API}/works", params=_params(params))
    items = ((data or {}).get("message") or {}).get("items") or []
    out = []
    for it in items:
        r = parse_item(it)
        r.raw["crossref_score"] = it.get("score")
        out.append(r)
    return out


def search(
    title: str | None = None,
    authors: Sequence[str] = (),
    *,
    year: int | None = None,
    rows: int = 10,
    types: Sequence[str] = (),
    http: HttpClient | None = None,
) -> list[WorkRecord]:
    """query.bibliographic search (title + author surnames + year in one string)."""
    bits = [title or ""] + [_ids.surname_key(a) for a in authors[:3]]
    if year:
        bits.append(str(year))
    q = " ".join(b for b in bits if b).strip()
    if not q:
        return []
    params: dict[str, Any] = {"query.bibliographic": q, "rows": rows}
    if types:
        params["filter"] = ",".join(f"type:{t}" for t in types)
    return _list(params, http)


def by_isbn(isbn: str, *, rows: int = 10, http: HttpClient | None = None) -> list[WorkRecord]:
    """Works (books, but also their chapters) carrying this ISBN; books first."""
    n = _ids.normalize_isbn(isbn)
    if not n:
        return []
    res = _list({"filter": f"isbn:{n}", "rows": rows}, http)
    res.sort(key=lambda r: r.kind != "book")
    return res
