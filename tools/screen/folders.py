"""Export PDFs from a folder tree sorted by kind (e.g. ~/Dropbox/doc/{paper,book,thesis,...}), read
only, in export.py's format, labelled by the classifier: more training files for the screen's
per-kind weights (tools/screen/train.py), above all for the kinds a library has few of.

    uv run python tools/screen/folders.py --root ~/Dropbox/doc --out /tmp/training-folders \\
        --map paper=paper --map book=book --map thesis=thesis --map slides=slides --map poster=slides \\
        --map notes=lecture_notes --map reference=manual --map blogpost=article,tutorial \\
        [--sample paper=400] [--classifier lmstudio:qwen3.5-4b-mlx] [--as-folder ~/Downloads]

Each PDF under <root>/<folder>/ (recursively) for a mapped folder becomes <out>/<sha16>.json:

  inputs        `triage_prompt`: the document description (describe.render_state), as it would be
                if the file had been downloaded into --as-folder: the sorted folder's name, which
                is the label, never reaches the description. Also filename and first pages' text.
  ground_truth  label_status `folder`, the folder and the kinds it maps to (`kinds`, the first is
                the one-hot label train.py --folder-labels uses), is_personal false.
  predictions   `kind_probs`: the classifier's distribution (source classify:<tag>), the label
                train.py uses by default.

The classifier is asked straight, not through the inference cache: these files belong to no
library, so their calls would sit in some library's cache about documents it doesn't have. Files
already in --out are skipped, so an interrupted run resumes. Files without a text layer are left
out (the screen skips those). Run it with nothing else using LM Studio: ~4 s a file with the 4B.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

from papyrus import db, describe
from papyrus.classify import CLASSIFICATIONS, KINDS
from papyrus.filenames import parse_filename
from papyrus.llm.classify import get_classifier
from papyrus.pdf import PageReader, read_pdf

if sys.platform == "darwin":
    from papyrus.macos import where_froms
else:
    from papyrus.linux import where_froms


def state_of(p: Path, data: bytes, info, folder: str) -> str:
    froms = where_froms(p) or []
    hint = parse_filename(p.stem)
    meta = dict(info.meta)
    d = describe.Document(
        names=[p.name], folders=[folder],
        origins=[froms[0] + (f" (via {froms[1]})" if len(froms) > 1 else "")] if froms else [],
        size=len(data), page_count=info.page_count,
        meta={k: meta.get(k) for k in describe.META_FIELDS},
        hint=db.dumps({"title": hint.title, "surnames": hint.surnames, "year": hint.year}) if hint else None)
    with PageReader(p) as reader:
        return describe.render_state(d, info.page_count, reader.text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--map", action="append", default=[], metavar="FOLDER=KIND[,KIND]", required=True)
    ap.add_argument("--sample", action="append", default=[], metavar="FOLDER=N", help="at most N files of FOLDER")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--classifier", default="lmstudio:qwen3.5-4b-mlx")
    ap.add_argument("--as-folder", default="~/Downloads", help="the folder the description shows [~/Downloads]")
    a = ap.parse_args(argv)

    mapping: dict[str, list[str]] = {}
    for m in a.map:
        f, _, ks = m.partition("=")
        kinds = [k.strip() for k in ks.split(",") if k.strip()]
        if not kinds or any(k not in KINDS for k in kinds):
            sys.exit(f"--map {m}: kinds must be among {', '.join(KINDS)}")
        mapping[f] = kinds
    sample = {f: int(n) for f, _, n in (s.partition("=") for s in a.sample)}
    root = a.root.expanduser()
    a.out.mkdir(parents=True, exist_ok=True)
    done = {p.stem for p in a.out.glob("*.json")}

    picks: list[tuple[str, Path]] = []
    for folder in sorted(mapping):
        fs = sorted(p for p in (root / folder).rglob("*")
                    if p.suffix.lower() == ".pdf" and p.is_file() and not p.name.startswith("."))
        if folder in sample and len(fs) > sample[folder]:
            fs = sorted(random.Random(a.seed).sample(fs, sample[folder]))
        picks += [(folder, p) for p in fs]
    print(f"{len(picks)} files in {len(mapping)} folders", flush=True)

    clf = get_classifier(a.classifier)
    if not clf.available():
        sys.exit(f"classifier {a.classifier} is not available")
    tag = "classify:" + clf.info().tag
    meta = {"prompt_version": describe.PROMPT_VERSION, "schema_version": db.SCHEMA_VERSION,
            "exported_at": db.now_iso(), "source": "folders", "as_folder": a.as_folder}
    counts: dict[str, int] = {}
    t0 = time.monotonic()
    for i, (folder, p) in enumerate(picks):
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha[:16] in done:
            continue
        try:
            info = read_pdf(p, max_pages=2)
            if not "".join(info.pages).strip():
                counts["no_text"] = counts.get("no_text", 0) + 1
                continue
            state = state_of(p, data, info, a.as_folder)
        except Exception:  # noqa: BLE001  unreadable: skip (the message may hold the path)
            counts["unreadable"] = counts.get("unreadable", 0) + 1
            continue
        probs = clf.classify(state, CLASSIFICATIONS)["kind"].probabilities
        rec = {"id": sha[:16], "sha256": sha, "file_id": None,
               "inputs": {"filename": p.name, "size": len(data), "page_count": info.page_count,
                          "text": "\n\n".join(info.pages), "triage_prompt": state},
               "ground_truth": {"label_status": "folder", "folder": folder, "kinds": mapping[folder],
                                "kind": mapping[folder][0], "is_personal": False, "in_library": False},
               "predictions": {"kind_probs": [{"value": probs, "confidence": None, "source": tag}]},
               "export": meta}
        (a.out / f"{sha[:16]}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        done.add(sha[:16])
        counts[folder] = counts.get(folder, 0) + 1
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(picks)} ({time.monotonic() - t0:.0f} s)", flush=True)
    print("wrote " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
