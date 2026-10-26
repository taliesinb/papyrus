"""PDF reading: document info and page text (pypdfium2, Apache/BSD licensed)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

PDF_MAGIC = b"%PDF-"


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
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # undo end-of-line hyphenation: "repre-\nsentation" -> "representation"
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)
    return s.strip()


@dataclass
class PdfInfo:
    page_count: int
    meta: dict[str, str | None]  # title, author, subject, keywords, creator, producer, created, modified
    pages: list[str] = field(default_factory=list)  # text of the first N pages (index 0 = page 1)


def read_pdf(path: Path, max_pages: int = 2) -> PdfInfo:
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
        for i in range(min(n, max_pages)):
            page = doc[i]
            tp = page.get_textpage()
            try:
                pages.append(clean_text(tp.get_text_bounded()))
            finally:
                tp.close()
                page.close()
        return PdfInfo(page_count=n, meta=meta, pages=pages)
    finally:
        doc.close()
