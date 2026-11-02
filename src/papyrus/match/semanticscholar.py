"""Semantic Scholar Academic Graph API client (https://api.semanticscholar.org/graph/v1).

* Optional key in env ``S2_API_KEY`` (sent as ``x-api-key``). With a key: 1 request/s
  on all endpoints. Without: requests share a global unauthenticated pool and 429s are
  common — the HTTP client backs off and retries (several seconds per call is normal).
* Paper ids: 'arXiv:2405.21060', 'DOI:10.1007/...', 'CorpusId:123', or the 40-hex paperId.
* ``embedding.specter_v2`` (768 floats) is available on the paper and batch endpoints
  (verified 2026-10-26); it is kept in ``record.raw['embedding']``.
* ``tldr`` (one-sentence summary) is kept in ``record.raw['tldr']``.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from papyrus import ids as _ids
from papyrus.http import Cooling, HttpClient, HttpError, default_client
from papyrus.models import AuthorRecord, WorkRecord

API = "https://api.semanticscholar.org/graph/v1"
BASE_FIELDS = (
    "paperId,corpusId,externalIds,url,title,abstract,venue,publicationVenue,year,publicationDate,"
    "journal,publicationTypes,citationCount,fieldsOfStudy,s2FieldsOfStudy,openAccessPdf,tldr,"
    "authors.authorId,authors.name,authors.externalIds,authors.affiliations"
)
# search/match and search reject some fields (tldr, embedding); keep them to the base set
SEARCH_FIELDS = (
    "paperId,externalIds,url,title,abstract,venue,publicationVenue,year,publicationDate,journal,"
    "publicationTypes,citationCount,s2FieldsOfStudy,authors.authorId,authors.name"
)
BATCH = 500
KIND = {"Book": "book", "BookSection": "chapter", "Dataset": "dataset", "Thesis": "thesis"}


def api_key() -> str | None:
    k = os.environ.get("S2_API_KEY", "").strip()
    return k or None


def _headers() -> dict[str, str]:
    k = api_key()
    return {"x-api-key": k} if k else {}


# After retries are exhausted on 429 (or 503, or no answer) we stop calling S2 for a while instead
# of making every subsequent lookup wait through its own backoff cycle.
COOLDOWN_S = 120.0
COOLDOWN_STATUSES = (0, 429, 503)
_cooldown_until = 0.0


class S2RateLimited(Cooling):
    pass


def _call(method: str, url: str, *, http: HttpClient | None, **kw: Any) -> Any | None:
    global _cooldown_until
    if time.monotonic() < _cooldown_until:
        raise S2RateLimited(429, url, "skipped: Semantic Scholar rate-limit cooldown")
    http = http or default_client()
    keyed = api_key() is not None
    kw.setdefault("max_retries", 6 if keyed else 4)
    kw.setdefault("backoff_cap", 30.0 if keyed else 20.0)
    try:
        if method == "POST":
            return http.post_json(url, kw.pop("body"), headers=_headers(), **kw)
        return http.get_json(url, headers=_headers(), **kw)
    except HttpError as e:
        if e.status in COOLDOWN_STATUSES:
            _cooldown_until = time.monotonic() + COOLDOWN_S
        raise


def _fields(embedding: bool) -> str:
    return BASE_FIELDS + (",embedding.specter_v2" if embedding else "")


def to_paper_id(s: str) -> str | None:
    """Turn an arXiv id / DOI / S2 id into a Graph API paper id."""
    s = s.strip()
    prefix = s.split(":", 1)[0] if ":" in s else ""
    if prefix in {"CorpusId", "MAG", "ACL", "PMID", "PMCID", "URL", "DBLP"}:
        return s
    if d := _ids.normalize_doi(s):
        a = _ids.arxiv_id_from_doi(d)
        return f"arXiv:{a.id}" if a else f"DOI:{d}"
    if a := _ids.parse_arxiv_id(s):
        return f"arXiv:{a.id}"
    if len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower()):
        return s
    return None


def parse_paper(p: dict[str, Any]) -> WorkRecord:
    authors = []
    for a in p.get("authors") or []:
        name = a.get("name") or ""
        given, family = _ids.split_name(name)
        ext = a.get("externalIds") or {}
        orcid = ext.get("ORCID")
        authors.append(
            AuthorRecord(
                name=name,
                given=given,
                family=family,
                s2_id=a.get("authorId"),
                orcid=orcid if isinstance(orcid, str) else None,
                affiliations=[x for x in a.get("affiliations") or [] if x],
            )
        )
    ext = p.get("externalIds") or {}
    rec_ids: dict[str, str] = {}
    if p.get("paperId"):
        rec_ids["s2"] = p["paperId"]
    corpus = p.get("corpusId") or ext.get("CorpusId")
    if corpus:
        rec_ids["s2_corpus"] = str(corpus)
    if ext.get("ArXiv"):
        a = _ids.parse_arxiv_id(ext["ArXiv"])
        if a:
            rec_ids["arxiv"] = a.id
    if ext.get("DOI"):
        d = _ids.normalize_doi(ext["DOI"])
        if d and _ids.is_arxiv_doi(d):
            a = _ids.arxiv_id_from_doi(d)
            if a:
                rec_ids.setdefault("arxiv", a.id)
        elif d:
            rec_ids["doi"] = d
    for k, scheme in (("DBLP", "dblp"), ("MAG", "mag"), ("PubMed", "pmid"), ("ACL", "acl"), ("PubMedCentral", "pmcid")):
        if ext.get(k):
            rec_ids[scheme] = str(ext[k])
    pv = p.get("publicationVenue") or {}
    journal = p.get("journal") or {}
    venue = p.get("venue") or pv.get("name") or journal.get("name")
    if venue and venue.lower() in {"arxiv", "arxiv.org"}:
        venue = None
    topics = []
    seen = set()
    for f in p.get("s2FieldsOfStudy") or []:
        key = (f.get("category"), f.get("source"))
        if f.get("category") and key not in seen:
            seen.add(key)
            topics.append({"name": f["category"], "scheme": "s2-fos", "source": f.get("source")})
    types = p.get("publicationTypes") or []
    kind = next((KIND[t] for t in types if t in KIND), "paper")
    urls = [u for u in [p.get("url"), (p.get("openAccessPdf") or {}).get("url")] if u]
    raw = dict(p)
    # S2 'year' is often the arXiv year; a DBLP key like 'conf/iclr/YangKH25' gives the venue year
    dk = str(ext.get("DBLP") or "")
    if dk and "/corr/" not in dk and (m := re.fullmatch(r"[A-Za-z][A-Za-z\-']*(\d{2})[a-z]?", dk.rsplit("/", 1)[-1])):
        yy = int(m.group(1))
        raw["venue_year"] = 2000 + yy if yy <= (time.gmtime().tm_year % 100) + 1 else 1900 + yy
    if isinstance(p.get("tldr"), dict):
        raw["tldr"] = p["tldr"].get("text")
    return WorkRecord(
        source="s2",
        title=p.get("title") or "",
        authors=authors,
        abstract=p.get("abstract"),
        year=p.get("year"),
        date=p.get("publicationDate"),
        venue=venue,
        kind=kind,
        ids=rec_ids,
        urls=urls,
        topics=topics,
        citation_count=p.get("citationCount"),
        raw=raw,
    )


def get(paper_id: str, *, embedding: bool = True, http: HttpClient | None = None) -> WorkRecord | None:
    pid = to_paper_id(paper_id)
    if not pid:
        return None
    data = _call("GET", f"{API}/paper/{pid}", params={"fields": _fields(embedding)}, http=http)
    return parse_paper(data) if data and data.get("paperId") else None


def batch(
    paper_ids: Sequence[str], *, embedding: bool = True, http: HttpClient | None = None
) -> list[WorkRecord | None]:
    """POST /paper/batch; result aligned with the input (None for unknown ids)."""
    pids = [to_paper_id(p) for p in paper_ids]
    out: list[WorkRecord | None] = [None] * len(pids)
    valid = [(i, p) for i, p in enumerate(pids) if p]
    for start in range(0, len(valid), BATCH):
        chunk = valid[start : start + BATCH]
        data = _call(
            "POST",
            f"{API}/paper/batch",
            body={"ids": [p for _, p in chunk]},
            params={"fields": _fields(embedding)},
            http=http,
        )
        for (i, _), p in zip(chunk, data or [], strict=False):
            if p and p.get("paperId"):
                out[i] = parse_paper(p)
    return out


def match_title(title: str, *, year: int | None = None, http: HttpClient | None = None) -> WorkRecord | None:
    """/paper/search/match: the single best title match (or None). raw['matchScore'] kept."""
    params: dict[str, Any] = {"query": " ".join(_ids.normalize_title(title).split()[:40]), "fields": SEARCH_FIELDS}
    if year:
        params["year"] = f"{year - 1}-{year + 1}"
    data = _call("GET", f"{API}/paper/search/match", params=params, http=http)
    items = (data or {}).get("data") or []
    return parse_paper(items[0]) if items else None


def search(query: str, *, limit: int = 10, year: int | None = None, http: HttpClient | None = None) -> list[WorkRecord]:
    """/paper/search relevance search."""
    params: dict[str, Any] = {
        "query": " ".join(_ids.normalize_title(query).split()[:40]),
        "limit": limit,
        "fields": SEARCH_FIELDS,
    }
    if year:
        params["year"] = f"{year - 1}-{year + 1}"
    data = _call("GET", f"{API}/paper/search", params=params, http=http)
    return [parse_paper(p) for p in (data or {}).get("data") or []]


def embedding_of(record: WorkRecord) -> list[float] | None:
    e = record.raw.get("embedding") if record.raw else None
    return e.get("vector") if isinstance(e, dict) else None


def get_many(ids: Iterable[str], **kw: Any) -> list[WorkRecord | None]:
    return batch(list(ids), **kw)
