"""PDF reading: document info and page text (pypdfium2, Apache/BSD licensed)."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PDF_MAGIC = b"%PDF-"
TRIAGE_PAGES = 2  # pages read when a file is scanned: all that triage sees

# get_toc() warns on stderr for every outline deeper than max_depth (and for cycles); both are harmless.
logging.getLogger("pypdfium2").setLevel(logging.ERROR)


def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def looks_like_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return PDF_MAGIC in f.read(1024)
    except OSError:
        return False


_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")


def pdf_date_to_iso(s: str | None) -> str | None:
    if not s:
        return None
    m = _PDF_DATE.match(s.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (g or dflt for g, dflt in zip(m.groups(), ["", "01", "01", "00", "00", "00"]))
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"


def clean_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    # soft hyphens (PDFium emits \x02 at hyphenation points), zero-width chars, other C0 controls
    s = re.sub("[\x01-\x08\x0b\x0c\x0e-\x1f\u00ad\u200b\u200c\u200d\ufeff\ufffe]", "", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # undo end-of-line hyphenation: "repre-\nsentation" -> "representation"
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)
    return s.strip()


@dataclass
class OutlineEntry:
    level: int  # 0 = top level
    title: str
    page: int  # 1-based


@dataclass
class PdfInfo:
    page_count: int
    meta: dict[str, str | None]  # title, author, subject, keywords, creator, producer, created, modified
    pages: list[str] = field(default_factory=list)  # text of the first N pages (index 0 = page 1)
    outline: list[OutlineEntry] = field(default_factory=list)  # only with outline=True


def _bookmark_page(doc: pdfium.PdfDocument, bm: pdfium.PdfBookmark) -> int | None:
    """0-based target page of a bookmark: its destination, else its GoTo action's."""
    dest = bm.get_dest()
    if dest is not None and (i := dest.get_index()) is not None:
        return i
    action = pdfium_c.FPDFBookmark_GetAction(bm.raw)
    if action:
        d = pdfium_c.FPDFAction_GetDest(doc.raw, action)
        if d:
            i = pdfium_c.FPDFDest_GetDestPageIndex(doc.raw, d)
            return i if i >= 0 else None
    return None


def read_outline(doc: pdfium.PdfDocument, max_entries: int = 5000) -> list[OutlineEntry]:
    out = []
    n = len(doc)
    for bm in doc.get_toc(max_depth=8):
        title = " ".join((bm.get_title() or "").split())
        i = _bookmark_page(doc, bm)
        if title and i is not None and 0 <= i < n:
            out.append(OutlineEntry(bm.level, title[:300], i + 1))
            if len(out) >= max_entries:
                break
    return out


class PageReader:
    """A PDF's pages read one at a time, on demand (clean_text, as in read_pdf), for callers that
    need a few pages of a long document."""

    def __init__(self, path: Path):
        self.doc = pdfium.PdfDocument(str(path))
        self.page_count = len(self.doc)

    def text(self, page: int) -> str:
        """The text of page `page` (1-based)."""
        pg = self.doc[page - 1]
        tp = pg.get_textpage()
        try:
            return clean_text(tp.get_text_bounded())
        finally:
            tp.close()
            pg.close()

    def close(self) -> None:
        self.doc.close()

    def __enter__(self) -> PageReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_pdf(path: Path, max_pages: int | None = 2, outline: bool = False) -> PdfInfo:
    """Document info and the text of the first `max_pages` pages (None = all), optionally the outline."""
    doc = pdfium.PdfDocument(str(path))
    try:
        raw = doc.get_metadata_dict(skip_empty=True)
        meta = {
            "title": raw.get("Title"),
            "author": raw.get("Author"),
            "subject": raw.get("Subject"),
            "keywords": raw.get("Keywords"),
            "creator": raw.get("Creator"),
            "producer": raw.get("Producer"),
            "created": pdf_date_to_iso(raw.get("CreationDate")),
            "modified": pdf_date_to_iso(raw.get("ModDate")),
        }
        meta = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in meta.items()}
        n = len(doc)
        pages = []
        for i in range(n if max_pages is None else min(n, max_pages)):
            page = doc[i]
            tp = page.get_textpage()
            try:
                pages.append(clean_text(tp.get_text_bounded()))
            finally:
                tp.close()
                page.close()
        return PdfInfo(page_count=n, meta=meta, pages=pages, outline=read_outline(doc) if outline else [])
    finally:
        doc.close()
