"""Export one JSON file per ingested document, for training the ingestion classifiers.

    uv run python tools/screen/export.py [--out training] [--db PATH]

(tools/screen/train.py reads the output, from this library or another machine's.)

Without --db, the variant's live database is copied with SQLite's backup API (opened read-only)
into a temporary file and read from there, so the real library is never written to.

Each file is <out>/<first 16 hex digits of sha256>.json with three sections:

  inputs        what the triage classifiers see: filenames, paths, download URLs and link
                titles (filesystem/download contexts), size, page count, the PDF info
                dictionary, the text of the first pages (pdfium text layer or Apple Vision
                OCR, per page), the filename heuristic's hint, and `triage_prompt`, the exact
                state string describe.build_state gives the models.
  ground_truth  the library's current resolved view. `label_status` says where it comes from:
                  work               the file belongs to a live work: fields from `works`,
                                     `work_authors`, `work_ids`, `work_topics`
                  rejected_personal  its work was rejected as not library material
                  not_promoted       triaged and judged personal, so no work was created
                  not_promoted_no_title  judged library material but promote found no title
                  pending_triage     not (re-)triaged since its text changed (e.g. after OCR):
                                     no verdict, labels are null
                  purged             erased as personal paperwork: a tombstone, no inputs left
                                     (train.py recovers them from the originals)
                `sources` gives, per field, the provenance of the value where it can be traced
                (api:*, classify:*, llm:*, heuristic:*, user). No label is user-reviewed unless
                its source is `user` or `work_status` is `confirmed`.
  predictions   the live triage claims on the file (every source, with confidence and the
                classifier's probability distributions): the current classifiers' outputs.

This contains the first pages of personal documents (payslips, CVs, tax certificates): keep the
output out of git (`/training/` is ignored), tests, fixtures and logs.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from papyrus import db, describe, works
from papyrus.pdf import FIRST_PAGES

INPUT_CONTEXTS = ("filesystem", "download")
WORK_FIELDS = ["kind", "title", "subtitle", "abstract", "year", "date", "venue", "language"]
NAME = re.compile(r"^[0-9a-f]{16}\.json$")


def snapshot(src: Path, dest: Path) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def contexts(conn: sqlite3.Connection, file_id: int) -> list[dict[str, Any]]:
    out = []
    for c in conn.execute(  # same order as describe.build_state
            "SELECT c.* FROM context_refs r JOIN contexts c ON c.id=r.context_id"
            f" WHERE r.file_id=? AND c.kind IN {INPUT_CONTEXTS}"
            " ORDER BY c.missing_at IS NOT NULL, c.occurred_at", (file_id,)):
        extra = json.loads(c["extra"] or "{}")
        d = {
            "kind": c["kind"],
            "filename": extra.get("filename"),
            "path": c["locator"] if c["kind"] == "filesystem" else None,
            "dir": extra.get("dir"),
            "url": c["url"] or (c["locator"] if c["kind"] == "download" else None),
            "where_froms": extra.get("where_froms") or [],
            "link_title": c["title"] if c["kind"] == "download" else None,
            "source": c["source"],
            "download_agent": extra.get("download_agent"),
            "content_type": extra.get("content_type"),
            "occurred_at": c["occurred_at"],
            "missing_at": c["missing_at"],
        }
        out.append({k: v for k, v in d.items() if v not in (None, [])})
    return out


def pages(conn: sqlite3.Connection, file_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT page, source, kind, text FROM file_pages WHERE file_id=? AND page<=? ORDER BY page",
        (file_id, FIRST_PAGES))]


def trace(value: Any, claims: dict[str, list[dict]], field: str) -> str | None:
    """Source of the best-ranked live claim whose value equals the resolved value."""
    if value is None:
        return None
    xs = [c for c in claims.get(field) or [] if c["value"] == value]
    return sorted(xs, key=lambda c: (works._rank(c["source"]), -(c["confidence"] or 0)))[0]["source"] if xs else None


def work_truth(conn: sqlite3.Connection, work_id: int, file_claims: dict[str, list[dict]]) -> dict[str, Any]:
    w = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    wclaims = db.current_assertions(conn, "work", work_id)
    gt: dict[str, Any] = {f: w[f] for f in WORK_FIELDS}
    gt["work_id"], gt["work_status"] = work_id, w["status"]
    gt["authors"] = [r["raw_name"] for r in conn.execute(
        "SELECT raw_name FROM work_authors WHERE work_id=? AND role='author' ORDER BY position", (work_id,))]
    gt["editors"] = [r["raw_name"] for r in conn.execute(
        "SELECT raw_name FROM work_authors WHERE work_id=? AND role='editor' ORDER BY position", (work_id,))]
    gt["ids"] = {r["scheme"]: r["value"] for r in conn.execute(
        "SELECT scheme, value FROM work_ids WHERE work_id=? ORDER BY scheme", (work_id,))}
    gt["topics"] = [{k: r[k] for k in r.keys() if r[k] is not None} for r in conn.execute(
        "SELECT t.scheme, t.name, t.level, t.external_id, p.name AS parent, wt.score, wt.source"
        " FROM work_topics wt JOIN topics t ON t.id=wt.topic_id LEFT JOIN topics p ON p.id=t.parent_id"
        " WHERE wt.work_id=? ORDER BY t.scheme, wt.score DESC, t.name", (work_id,))]
    sources: dict[str, Any] = {}
    for f in ["title", "abstract", "year", "venue"]:
        sources[f] = trace(gt[f], wclaims, f) or trace(gt[f], file_claims, f)
    sources["kind"] = trace(gt["kind"], file_claims, "kind")
    sources["language"] = trace(gt["language"], file_claims, "language")
    sources["authors"] = trace(gt["authors"], wclaims, "authors") or trace(gt["authors"], file_claims, "authors")
    sources["ids"] = {r["scheme"]: r["source"] for r in conn.execute(
        "SELECT scheme, source FROM work_ids WHERE work_id=?", (work_id,))}
    gt["sources"] = {k: v for k, v in sources.items() if v}
    return gt


def ground_truth(conn: sqlite3.Connection, f: sqlite3.Row, claims: dict[str, list[dict]]) -> dict[str, Any]:
    links = conn.execute(
        "SELECT fw.work_id, fw.relation, fw.version, w.status FROM file_works fw JOIN works w ON w.id=fw.work_id"
        " WHERE fw.file_id=? ORDER BY w.status IN ('candidate', 'confirmed') DESC, fw.work_id", (f["id"],)).fetchall()
    live = [r for r in links if r["status"] in ("candidate", "confirmed")]
    empty = {k: None for k in WORK_FIELDS}
    if live:
        r = live[0]
        gt = {"label_status": "work", "in_library": True, "is_personal": False,
              **work_truth(conn, r["work_id"], claims), "relation": r["relation"], "version": r["version"]}
        if len(live) > 1:
            gt["other_work_ids"] = [x["work_id"] for x in live[1:]]
        return gt
    personal = {**empty, "kind": "personal"}
    if f["purged_at"]:
        return {"label_status": "purged", "in_library": False, "is_personal": True, **personal}
    if any(r["status"] == "rejected" for r in links):
        rid = next(r["work_id"] for r in links if r["status"] == "rejected")
        return {"label_status": "rejected_personal", "in_library": False, "is_personal": True, **personal,
                "work_id": rid, "work_status": "rejected"}
    if f["triage_status"] != "done":
        return {"label_status": "pending_triage", "in_library": False, "is_personal": None, **empty}
    if not works.is_library(claims):
        return {"label_status": "not_promoted", "in_library": False, "is_personal": True, **personal}
    return {"label_status": "not_promoted_no_title", "in_library": False, "is_personal": False, **empty}


def record(conn: sqlite3.Connection, f: sqlite3.Row) -> dict[str, Any]:
    claims = db.current_assertions(conn, "file", f["id"])
    ctx = contexts(conn, f["id"])
    pg = pages(conn, f["id"])
    hint = claims.get("filename_hint")
    first = next((c for c in ctx if c.get("filename")), {})
    inputs = {
        "filename": first.get("filename"),
        "path": next((c["path"] for c in ctx if c.get("path")), None),
        "filenames": list(dict.fromkeys(c["filename"] for c in ctx if c.get("filename"))),
        "paths": [c["path"] for c in ctx if c.get("path")],
        "where_froms": list(dict.fromkeys(u for c in ctx for u in c.get("where_froms", []))),
        "link_titles": list(dict.fromkeys(c["link_title"] for c in ctx if c.get("link_title"))),
        "contexts": ctx,
        "size": f["size"],
        "media_type": f["media_type"],
        "page_count": f["page_count"],
        "pdf_info": {k: f[f"pdf_{k}"] for k in
                     ["title", "author", "subject", "keywords", "creator", "producer", "created", "modified"]},
        "text_status": f["text_status"],
        "text_sources": sorted({p["source"] for p in pg}),
        "text": "\n\n".join(p["text"] for p in pg),
        "pages": pg,
        "filename_hint": hint[0]["value"] if hint else None,
        "triage_prompt": describe.build_state(conn, f["id"]),
    }
    preds = {k: [{"value": c["value"], "confidence": c["confidence"], "source": c["source"],
                  "created_at": c["created_at"]} for c in v]
             for k, v in sorted(claims.items()) if k != "filename_hint"}
    return {
        "id": f["sha256"][:16],
        "sha256": f["sha256"],
        "file_id": f["id"],
        "first_seen_at": f["first_seen_at"],
        "inputs": inputs,
        "ground_truth": ground_truth(conn, f, claims),
        "predictions": preds,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=Path("training"))
    ap.add_argument("--db", type=Path, help="read this database (default: a fresh backup of the live one)")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        path = a.db
        if path is None:
            path = Path(tmp) / "papyrus.sqlite"
            snapshot(db.default_db_path(), path)
        conn = db.connect(path)
        files = conn.execute("SELECT * FROM files ORDER BY id").fetchall()
        meta = {"triage_pages": FIRST_PAGES, "prompt_version": describe.PROMPT_VERSION,
                "schema_version": db.SCHEMA_VERSION, "exported_at": db.now_iso(),
                "library": str(db.default_db_path()) if a.db is None else str(a.db)}
        recs = [record(conn, f) for f in files]
        conn.close()
    names = {r["id"] for r in recs}
    if len(names) != len(recs):
        sys.exit("sha256 prefix collision; use more digits")
    a.out.mkdir(parents=True, exist_ok=True)
    for r in recs:
        r["export"] = meta
        (a.out / f"{r['id']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    stale = [p for p in a.out.iterdir() if NAME.match(p.name) and p.stem not in names]
    for p in stale:  # only files this script wrote, for documents no longer in the library
        p.unlink()
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["ground_truth"]["label_status"]] = counts.get(r["ground_truth"]["label_status"], 0) + 1
    print(f"wrote {len(recs)} files to {a.out}/ ({', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))})"
          + (f"; removed {len(stale)} stale" if stale else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
