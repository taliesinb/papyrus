"""Plain data records exchanged between pipeline stages (no DB coupling)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuthorRecord:
    name: str  # display name as given by the source
    given: str | None = None
    family: str | None = None
    orcid: str | None = None  # bare 0000-0000-0000-0000
    openalex_id: str | None = None  # bare 'A1234567'
    s2_id: str | None = None  # Semantic Scholar authorId
    dblp_pid: str | None = None
    affiliations: list[str] = field(default_factory=list)


@dataclass
class WorkRecord:
    """A normalised bibliographic record from one external source."""

    source: str  # 'arxiv' | 'openalex' | 's2' | 'crossref' | 'dblp'
    title: str
    authors: list[AuthorRecord] = field(default_factory=list)
    abstract: str | None = None
    year: int | None = None
    date: str | None = None  # ISO yyyy-mm-dd when known
    venue: str | None = None
    kind: str | None = None  # paper|book|chapter|thesis|... (see papyrus.schema KINDS)
    # identifiers, normalised: arxiv w/o version ('2412.06464', 'math/0601001'), doi lowercase
    ids: dict[str, str] = field(default_factory=dict)  # scheme -> value
    urls: list[str] = field(default_factory=list)  # landing pages, pdf urls
    topics: list[dict[str, Any]] = field(default_factory=list)  # [{'name','field','score','scheme','id'}]
    arxiv_categories: list[str] = field(default_factory=list)
    arxiv_version: str | None = None  # 'v3'
    citation_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # original payload, for provenance
    match_score: float | None = None  # how well this matched the query, 0..1
