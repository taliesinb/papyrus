"""OpenAlex client (https://api.openalex.org).

Authentication / pricing (checked 2026-10-26, https://help.openalex.org/api/authentication/):
* Keyless requests still work but share a $0.10/day budget per IP (1,000 credits);
  a free key (env ``OPENALEX_API_KEY``) gives $1/day (10,000 credits) and 30 req/s.
* Cost per call: singleton ``/works/<id>`` (incl. ``/works/doi:...``) = free;
  list+filter = 1 credit; ``search=`` / ``*.search`` filters = 10 credits.
  So id lookups are free, title searches cost $0.001 each (~100/day keyless).
* The key is sent as ``Authorization: Bearer`` (keeps it out of URLs and the cache).
  A not-yet-activated key gets ``403 key_not_activated``; we then fall back to keyless.

arXiv preprints are indexed as works of type 'preprint' whose ``doi`` is the DataCite
arXiv DOI ``10.48550/arxiv.<id>`` (source "arXiv (Cornell University)"), with locations
pointing at arxiv.org. The published version is frequently a *separate* work (with
the publisher DOI) — find it with a title search. Records can be wrong: e.g. the work
behind 10.48550/arxiv.2405.21060 carried another paper's title in Oct 2026, so callers
should verify titles (``match.resolve`` does).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from typing import Any

from papyrus import ids as _ids
from papyrus.http import HttpClient, HttpError, default_client, mailto
from papyrus.models import AuthorRecord, WorkRecord

log = logging.getLogger(__name__)

API = "https://api.openalex.org"
SELECT = (
    "id,doi,title,display_name,publication_year,publication_date,ids,language,type,"
    "primary_location,locations,best_oa_location,authorships,biblio,primary_topic,topics,"
    "keywords,abstract_inverted_index,cited_by_count,is_retracted,open_access"
)
KIND = {
    "article": "paper",
    "preprint": "paper",
    "review": "paper",
    "letter": "paper",
    "editorial": "paper",
    "book": "book",
    "book-chapter": "chapter",
    "dissertation": "thesis",
    "report": "report",
    "dataset": "dataset",
    "standard": "standard",
    "libguides": "other",
    "paratext": "other",
    "erratum": "other",
}

_key_disabled = False  # set when the configured key is rejected


def api_key() -> str | None:
    if _key_disabled:
        return None
    k = os.environ.get("OPENALEX_API_KEY", "").strip()
    return k or None


def _bare(oa_id: str | None) -> str | None:
    if not oa_id:
        return None
    return oa_id.rstrip("/").rsplit("/", 1)[-1]


def _bare_orcid(o: str | None) -> str | None:
    if not o:
        return None
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", o)
    return m.group(1) if m else None


def reconstruct_abstract(inv: dict[str, list[int]] | None) -> str | None:
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, places in inv.items():
        for p in places:
            pos[p] = word
    if not pos:
        return None
    text = " ".join(pos[i] for i in sorted(pos))
    return text.strip() or None


def _is_repository(loc: dict[str, Any] | None) -> bool:
    src = (loc or {}).get("source") or {}
    return src.get("type") == "repository" or "arxiv" in (src.get("display_name") or "").lower()


def _published_location(work: dict[str, Any]) -> dict[str, Any] | None:
    """A non-repository location (journal / conference) if the work has one."""
    locs = [work.get("primary_location")] + list(work.get("locations") or [])
    for loc in locs:
        if loc and loc.get("source") and not _is_repository(loc):
            return loc
    return None


def parse_work(w: dict[str, Any]) -> WorkRecord:
    authors: list[AuthorRecord] = []
    for au in w.get("authorships") or []:
        a = au.get("author") or {}
        name = a.get("display_name") or au.get("raw_author_name") or ""
        given, family = _ids.split_name(name)
        affs = [i.get("display_name") for i in au.get("institutions") or [] if i.get("display_name")]
        if not affs:
            affs = [s for s in au.get("raw_affiliation_strings") or [] if s]
        authors.append(
            AuthorRecord(
                name=name,
                given=given,
                family=family,
                orcid=_bare_orcid(a.get("orcid")),
                openalex_id=_bare(a.get("id")),
                affiliations=affs,
            )
        )
    rec_ids: dict[str, str] = {}
    if oid := _bare(w.get("id")):
        rec_ids["openalex"] = oid
    doi = _ids.normalize_doi(w.get("doi") or "") if w.get("doi") else None
    arx = None
    if doi and _ids.is_arxiv_doi(doi):
        arx = _ids.arxiv_id_from_doi(doi)
        doi = None
    pub_loc = _published_location(w)
    if not doi and pub_loc and pub_loc.get("doi"):
        d = _ids.normalize_doi(pub_loc["doi"])
        if d and not _ids.is_arxiv_doi(d):
            doi = d
    if doi:
        rec_ids["doi"] = doi
    urls: list[str] = []
    for loc in w.get("locations") or []:
        for k in ("landing_page_url", "pdf_url"):
            u = loc.get(k)
            if u and u not in urls:
                urls.append(u)
            if not arx and u and "arxiv.org" in u:
                arx = _ids.parse_arxiv_id(u)
    if arx:
        rec_ids["arxiv"] = arx.id
    xids = w.get("ids") or {}
    if pmid := xids.get("pmid"):
        rec_ids["pmid"] = pmid.rstrip("/").rsplit("/", 1)[-1]
    if mag := xids.get("mag"):
        rec_ids["mag"] = str(mag)
    topics = []
    for t in w.get("topics") or []:
        topics.append(
            {
                "name": t.get("display_name"),
                "id": _bare(t.get("id")),
                "score": t.get("score"),
                "scheme": "openalex",
                "subfield": (t.get("subfield") or {}).get("display_name"),
                "field": (t.get("field") or {}).get("display_name"),
                "domain": (t.get("domain") or {}).get("display_name"),
            }
        )
    for k in w.get("keywords") or []:
        topics.append(
            {
                "name": k.get("display_name"),
                "id": _bare(k.get("id")),
                "score": k.get("score"),
                "scheme": "openalex-keyword",
            }
        )
    venue_loc = pub_loc or w.get("primary_location") or {}
    venue = ((venue_loc.get("source") or {}).get("display_name")) if venue_loc else None
    if venue and "arxiv" in venue.lower():
        venue = None
    date = w.get("publication_date")
    return WorkRecord(
        source="openalex",
        title=w.get("title") or w.get("display_name") or "",
        authors=authors,
        abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
        year=w.get("publication_year"),
        date=date,
        venue=venue,
        kind=KIND.get(w.get("type") or "", "paper" if w.get("type") else None),
        ids=rec_ids,
        urls=urls,
        topics=topics,
        citation_count=w.get("cited_by_count"),
        raw=w,
    )


def _request(path: str, params: dict[str, Any] | None = None, *, http: HttpClient | None = None) -> Any | None:
    global _key_disabled
    http = http or default_client()
    params = dict(params or {})
    if mailto():
        params.setdefault("mailto", mailto())
    key = api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        return http.get_json(API + path, params=params, headers=headers)
    except HttpError as e:
        if key and e.status in (401, 403):
            log.warning("OpenAlex rejected OPENALEX_API_KEY (%s); continuing without a key", e.body[:200])
            _key_disabled = True
            return http.get_json(API + path, params=params)
        raise


def get(work_id: str, *, http: HttpClient | None = None) -> WorkRecord | None:
    """Singleton lookup (free): 'W123', 'doi:10....', 'https://doi.org/...', 'pmid:..'."""
    w = _request(f"/works/{work_id}", {"select": SELECT}, http=http)
    return parse_work(w) if w and w.get("id") else None


def get_by_doi(doi: str, *, http: HttpClient | None = None) -> WorkRecord | None:
    d = _ids.normalize_doi(doi)
    return get(f"doi:{d}", http=http) if d else None


def get_by_arxiv(arxiv_id: str, *, http: HttpClient | None = None) -> WorkRecord | None:
    a = _ids.parse_arxiv_id(arxiv_id)
    return get_by_doi(a.doi, http=http) if a else None


def get_many_by_doi(dois: Iterable[str], *, http: HttpClient | None = None) -> list[WorkRecord]:
    """Batch DOI lookup via filter=doi:a|b|... (1 credit per 100)."""
    norm = [d for d in (_ids.normalize_doi(x) for x in dois) if d]
    out: list[WorkRecord] = []
    for i in range(0, len(norm), 50):
        chunk = norm[i : i + 50]
        data = _request("/works", {"filter": "doi:" + "|".join(chunk), "per_page": 100, "select": SELECT}, http=http)
        out.extend(parse_work(w) for w in (data or {}).get("results") or [])
    return out


def _clean_query(s: str) -> str:
    # filter values cannot contain ',' '|' ':'; keep plain words
    return " ".join(_ids.normalize_title(s).split()[:30])


def search(
    title: str,
    *,
    year: int | None = None,
    per_page: int = 10,
    http: HttpClient | None = None,
) -> list[WorkRecord]:
    """Title search (10 credits). Optionally restrict to year±1."""
    q = _clean_query(title)
    if not q:
        return []
    flt = f"title.search:{q}"
    if year:
        flt += f",from_publication_date:{year - 1}-01-01,to_publication_date:{year + 1}-12-31"
    data = _request("/works", {"filter": flt, "per_page": per_page, "select": SELECT}, http=http)
    return [parse_work(w) for w in (data or {}).get("results") or []]
