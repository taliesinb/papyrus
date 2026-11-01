"""Which links found in a message, tweet or bookmark count as papers.

`classify(url)` answers, most precise first:

* ``arxiv``: an arXiv id in an arXiv-like URL (arxiv.org, alphaxiv, ar5iv, Hugging Face papers);
* ``doi``: a DOI anywhere in the URL (doi.org and most publisher article URLs);
* ``pdf``: the path ends in ``.pdf``, or a known PDF route (OpenReview);
* ``domain``: the host is in the allow list (`DEFAULT_DOMAINS` plus the user's own).

Anything else is not a paper link (``None``). The allow list is deliberately hand-made and
short; see docs/slack.md, "Which links count", for larger sources (Zotero translators,
OpenAlex, OpenDOAR) to derive one from.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .. import ids

# Hosts whose arXiv-shaped path segment is an arXiv id.
ARXIV_HOSTS = ("arxiv.org", "alphaxiv.org", "ar5iv.org", "ar5iv.labs.arxiv.org", "huggingface.co/papers",
               "arxiv-sanity-lite.com", "scirate.com/arxiv", "paperswithcode.com/paper")

# Scholarly hosts, matched as the host itself or any subdomain; an entry with a path
# matches only URLs under that path.
DEFAULT_DOMAINS: tuple[str, ...] = (
    # preprints and repositories
    "arxiv.org", "alphaxiv.org", "biorxiv.org", "medrxiv.org", "chemrxiv.org", "psyarxiv.com", "osf.io/preprints",
    "ssrn.com", "zenodo.org", "hal.science", "hal.archives-ouvertes.fr", "eprint.iacr.org", "philpapers.org",
    "europepmc.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov/pmc", "researchgate.net/publication",
    "semanticscholar.org/paper", "huggingface.co/papers", "paperswithcode.com/paper",
    # ML / CS venues
    "openreview.net", "proceedings.mlr.press", "jmlr.org", "aclanthology.org", "papers.nips.cc",
    "proceedings.neurips.cc", "openaccess.thecvf.com", "ecva.net", "ijcai.org", "ojs.aaai.org", "aaai.org/papers",
    "dl.acm.org", "ieeexplore.ieee.org", "usenix.org/conference", "vldb.org/pvldb", "drops.dagstuhl.de",
    "lipics.dagstuhl.de", "distill.pub", "transformer-circuits.pub",
    # publishers
    "nature.com", "science.org", "sciencedirect.com", "cell.com", "pnas.org", "link.springer.com",
    "onlinelibrary.wiley.com", "tandfonline.com", "academic.oup.com", "cambridge.org/core", "journals.aps.org",
    "iopscience.iop.org", "royalsocietypublishing.org", "journals.plos.org", "frontiersin.org", "mdpi.com",
    "elifesciences.org", "peerj.com", "annualreviews.org", "jstor.org/stable", "journals.sagepub.com",
    "pubs.acs.org", "ams.org/journals", "projecteuclid.org", "epubs.siam.org", "worldscientific.com",
    "mitpressjournals.org", "direct.mit.edu", "quantum-journal.org",
)

KINDS = ("arxiv", "doi", "pdf", "domain")


@dataclass(frozen=True)
class LinkClass:
    kind: str  # one of KINDS
    id: str | None = None  # arXiv id (no version) or DOI, when known


def _host_path(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, p.path or "/"


def matches(url: str, entry: str) -> bool:
    """Does `url` fall under the allow-list `entry` (``host`` or ``host/path-prefix``)?"""
    host, path = _host_path(url)
    e_host, _, e_path = entry.lower().removeprefix("www.").partition("/")
    if not (host == e_host or host.endswith("." + e_host)):
        return False
    return not e_path or path.lower().startswith("/" + e_path.rstrip("/"))


def classify(url: str, domains: Iterable[str] = DEFAULT_DOMAINS) -> LinkClass | None:
    if not url.lower().startswith(("http://", "https://")):
        return None
    if any(matches(url, h) for h in ARXIV_HOSTS):
        found = ids.find_arxiv_ids(url)
        if found:
            return LinkClass("arxiv", found[0].id)
    dois = ids.find_dois(url)
    if dois:
        a = ids.arxiv_id_from_doi(dois[0])
        return LinkClass("arxiv", a.id) if a else LinkClass("doi", dois[0])
    host, path = _host_path(url)
    if path.lower().endswith(".pdf") or (host == "openreview.net" and path.startswith("/pdf")):
        return LinkClass("pdf")
    if any(matches(url, d) for d in domains):
        return LinkClass("domain")
    return None
