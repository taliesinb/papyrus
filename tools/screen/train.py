"""Train the screen's weights (src/papyrus/data/screen.bin, the production classifier; see
screen.py) on triaged files.

    uv run python tools/screen/export.py --out /tmp/training-local     # each library, any machine
    sqlite3 ~/.papyrus/release/papyrus.sqlite ".backup /tmp/lib.sqlite"
    uv run --with numpy --with scikit-learn python tools/screen/train.py \\
        --export local=/tmp/training-local --export air=/tmp/training-air \\
        --db /tmp/lib.sqlite --originals ~/Downloads --originals ~/Desktop \\
        [--personal FILE_ID ...] [--library FILE_ID ...] [--write] [--json /tmp/screen-eval.json]

Then commit data/screen.bin. (2026-11-01, input version 3: 157 files, 69 personal and 88 library,
from this Mac and tali-macbook-air, whose schema-2 library was exported from an upgraded .backup
copy (AGENTS.md); --personal 37, a personal document triage had promoted to a work. LOO AUC
0.9998; at P >= 0.9, 68 personal caught and one borderline library file flagged, held for
review rather than purged.)

Labelled files come from:

* `--export NAME=DIR`: the output of export_training.py (one JSON per file) of a library, local
  or another machine's. `ground_truth.is_personal` is the label; files pending triage, without a
  title, without text, or purged are left out.
* `--db` + `--originals DIR`: files purged as personal paperwork keep only their sha256 in the
  library, so their inputs come from the originals, found by hashing the PDFs in DIR (read only).
* `--personal` / `--library`: file ids of `--db` whose label you set yourself (by sha256, so
  they override the exports too).

Each file is embedded at the screen's level (input version 3: triage's state, the export's
`triage_prompt`, or for purged originals triage.render_state on the PDF itself; versions 1 and 2:
screen.screen_input; prefix and model as in screen.py) straight from LM Studio, *not* through the
inference cache: the cache would write the pages of purged documents back to disk.

It reports leave-one-out metrics (C chosen inside each fold), per source and across sources
(trained on one, tested on the others), then fits all files and, with --write, writes the
weights with the standardisation folded in. Output names files as `<source>:<file id>`, with
counts and metrics only: no names, paths or text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import Normalizer, StandardScaler

from papyrus import db, screen, triage
from papyrus.filenames import parse_filename
from papyrus.llm.embed import LMStudioEmbedder
from papyrus.pdf import PageReader, read_pdf

if sys.platform == "darwin":
    from papyrus.macos import where_froms
else:
    from papyrus.linux import where_froms

CS = [0.01, 0.1, 1, 10, 100]
THRESHOLD = 0.9


def from_exports(name: str, d: Path, version: int = 1) -> list[dict]:
    out = []
    for p in sorted(d.glob("*.json")):
        r = json.loads(p.read_text())
        gt, inp = r["ground_truth"], r["inputs"]
        if gt.get("is_personal") is None or gt.get("label_status") in ("not_promoted_no_title", "purged"):
            continue
        if not (inp.get("text") or "").strip():
            continue
        path = inp.get("path")
        if version >= 3:
            if int(r["export"]["prompt_version"].rpartition("-")[2]) < 5:
                sys.exit(f"{d}: exported with {r['export']['prompt_version']}; input version 3 needs triage-5's "
                         "state: export again with the current code")
            out.append({"key": f"{name}:{r['file_id']}", "source": name, "sha256": r["sha256"],
                        "label": 1 if gt["is_personal"] else 0, "input": inp["triage_prompt"]})
            continue
        out.append({"key": f"{name}:{r['file_id']}", "source": name, "sha256": r["sha256"],
                    "label": 1 if gt["is_personal"] else 0,
                    "input": screen.screen_input(inp.get("filename") or (Path(path).name if path else "unknown.pdf"),
                                                 screen.home_relative(str(Path(path).parent)) if path else "~/Downloads",
                                                 inp.get("size") or 0, screen.host_of((inp.get("where_froms") or [None])[0]),
                                                 inp["text"], version=version,
                                                 pages=inp.get("page_count"), meta=inp.get("pdf_info"))})
    return out


def from_originals(name: str, db: Path, dirs: list[Path], version: int = 1) -> list[dict]:
    lib = sqlite3.connect(db)  # SELECT only
    purged = {r[0]: r[1] for r in lib.execute("SELECT sha256, id FROM files WHERE purged_at IS NOT NULL")}
    out, seen = [], set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() != ".pdf" or p.name.startswith(".") or not p.is_file():
                continue
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            if sha not in purged or sha in seen:
                continue
            seen.add(sha)
            try:
                info = read_pdf(p, max_pages=2)
                text = "\n".join(info.pages)
            except Exception:  # noqa: BLE001  unreadable: skip (the message may hold the path)
                continue
            if not text.strip():
                continue
            if version >= 3:
                out.append({"key": f"{name}:{purged[sha]}", "source": name, "sha256": sha, "label": 1,
                            "input": original_state(p, info)})
                continue
            out.append({"key": f"{name}:{purged[sha]}", "source": name, "sha256": sha, "label": 1,
                        "input": screen.screen_input(p.name, screen.home_relative(str(p.parent)), p.stat().st_size,
                                                     screen.host_of((where_froms(p) or [None])[0]), text,
                                                     version=version, pages=info.page_count, meta=info.meta)})
    return out


def original_state(p: Path, info) -> str:
    """Triage's state for a PDF that is not in the library (a purged original), as build_state
    would have given it: name, folder, size, download URL, PDF metadata, filename hint, pages."""
    froms = where_froms(p) or []
    hint = parse_filename(p.stem)
    meta = dict(info.meta)
    d = triage.Document(
        names=[p.name], folders=[screen.home_relative(str(p.parent))],
        origins=[froms[0] + (f" (via {froms[1]})" if len(froms) > 1 else "")] if froms else [],
        size=p.stat().st_size, page_count=info.page_count,
        meta={k: meta.get(k) for k in triage.META_FIELDS},
        hint=db.dumps({"title": hint.title, "surnames": hint.surnames, "year": hint.year}) if hint else None)
    with PageReader(p) as reader:
        return triage.render_state(d, info.page_count, reader.text)


def fit(X, y):
    folds = min(5, int(min(y.sum(), len(y) - y.sum())))
    g = GridSearchCV(make_pipeline(Normalizer(), StandardScaler(), LogisticRegression(max_iter=5000)),
                     {"logisticregression__C": CS}, cv=StratifiedKFold(folds, shuffle=True, random_state=0),
                     scoring="neg_log_loss")
    g.fit(X, y)
    return g.best_estimator_, g.best_params_["logisticregression__C"]


def metrics(keys, y, p) -> dict:
    keys, y, p = np.array(keys), np.array(y), np.array(p)
    m = {"n_personal": int(y.sum()), "n_library": int(len(y) - y.sum())}
    if 0 < y.sum() < len(y):
        lib_max = float(p[y == 0].max())
        m.update(auc=round(float(roc_auc_score(y, p)), 4), zero_fp_threshold=round(lib_max, 4),
                 caught_zero_fp=int((p[y == 1] > lib_max).sum()))
    for t in (0.5, THRESHOLD):
        m[f"at_{t}"] = {"caught": int((p[y == 1] >= t).sum()),
                        "library_flagged": [str(k) for k in keys[(y == 0) & (p >= t)]],
                        "personal_missed": len(keys[(y == 1) & (p < t)])}
    return m


def show(title: str, m: dict) -> None:
    s = f"{title}: personal {m['n_personal']}, library {m['n_library']}"
    if "auc" in m:
        s += f"; AUC {m['auc']:.3f}, caught with no library flagged {m['caught_zero_fp']}"
    for t in (0.5, THRESHOLD):
        a = m[f"at_{t}"]
        s += f"; P>={t}: caught {a['caught']}, library flagged {a['library_flagged'] or 'none'}"
    print(s, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", action="append", default=[], metavar="NAME=DIR")
    ap.add_argument("--db", type=Path, help="the library whose purged files --originals recovers (read only)")
    ap.add_argument("--originals", action="append", type=Path, default=[])
    ap.add_argument("--name", default="local", help="source name for --db files [local]")
    ap.add_argument("--personal", type=int, nargs="*", default=[])
    ap.add_argument("--library", type=int, nargs="*", default=[])
    ap.add_argument("--write", action="store_true", help=f"write {screen.weights_path()}")
    ap.add_argument("--out", type=Path, help="write the weights here instead")
    ap.add_argument("--json", type=Path, help="leave-one-out probabilities and metrics (keys, no text)")
    ap.add_argument("--input-version", type=int, default=3, choices=(1, 2, 3),
                    help="1: name, folder, size, host, first words; 2: also pages and PDF metadata; "
                         "3: triage's state [3]")
    a = ap.parse_args(argv)

    recs: list[dict] = []
    if a.db and a.originals:
        recs += from_originals(a.name, a.db, [d.expanduser() for d in a.originals], a.input_version)
    for spec in a.export:
        name, _, d = spec.partition("=")
        recs += from_exports(name, Path(d).expanduser(), a.input_version)
    override = {}
    if a.personal or a.library:
        lib = sqlite3.connect(a.db)
        for ids, label in ((a.personal, 1), (a.library, 0)):
            for i in ids:
                r = lib.execute("SELECT sha256 FROM files WHERE id=?", (i,)).fetchone()
                if r is None:
                    sys.exit(f"no file {i} in --db")
                override[r[0]] = label
    seen, uniq = set(), []
    for r in recs:  # originals first, then exports in the order given
        if r["sha256"] in seen:
            continue
        seen.add(r["sha256"])
        if r["sha256"] in override:
            r["label"] = override[r["sha256"]]
        uniq.append(r)
    recs = uniq
    sources = sorted({r["source"] for r in recs})
    for s in sources:
        n1 = sum(r["label"] for r in recs if r["source"] == s)
        print(f"{s}: {n1} personal, {sum(1 for r in recs if r['source'] == s) - n1} library", flush=True)

    emb = LMStudioEmbedder()
    t0 = time.perf_counter()
    emb.embed([screen.PREFIX + recs[0]["input"]])
    print(f"first embedding (may load the model): {time.perf_counter() - t0:.1f} s; embedding {len(recs)} files",
          flush=True)
    vecs, times = [], []
    for i, r in enumerate(recs):
        t0 = time.perf_counter()
        (v,) = emb.embed([screen.PREFIX + r["input"]])
        times.append(time.perf_counter() - t0)
        vecs.append(v)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(recs)}, median {statistics.median(times) * 1000:.0f} ms", flush=True)
    X = np.array(vecs, dtype=np.float64)
    y = np.array([r["label"] for r in recs])
    keys = [r["key"] for r in recs]
    src = np.array([r["source"] for r in recs])
    report: dict = {"embed_ms_median": round(statistics.median(times) * 1000, 1), "model": emb.model}

    print("leave-one-out…", flush=True)
    p_loo = np.zeros(len(y))
    for k in range(len(y)):
        tr = np.arange(len(y)) != k
        m, _ = fit(X[tr], y[tr])
        p_loo[k] = m.predict_proba(X[k:k + 1])[0, 1]
    report["loo"] = metrics(keys, y, p_loo)
    show("LOO, all", report["loo"])
    report["loo_by_source"] = {}
    for s in sources:
        sel = src == s
        report["loo_by_source"][s] = metrics(np.array(keys)[sel], y[sel], p_loo[sel])
        show(f"LOO, {s} files", report["loo_by_source"][s])
    report["cross"] = {}
    if len(sources) > 1:
        for s in sources:
            tr, te = src == s, src != s
            if y[tr].sum() < 2 or (len(y[tr]) - y[tr].sum()) < 2:
                continue
            m, c = fit(X[tr], y[tr])
            report["cross"][f"train_{s}"] = metrics(np.array(keys)[te], y[te], m.predict_proba(X[te])[:, 1])
            show(f"trained on {s} only (C={c}), tested on the rest", report["cross"][f"train_{s}"])

    model, c = fit(X, y)
    norm, scaler, lr = model.steps[0][1], model.steps[1][1], model.steps[2][1]
    assert isinstance(norm, Normalizer)
    w = lr.coef_[0] / scaler.scale_
    b = float(lr.intercept_[0] - np.dot(w, scaler.mean_))
    meta = {"model": emb.model, "prefix": screen.PREFIX, "words": screen.WORDS, "threshold": THRESHOLD, "C": c,
            "input_version": a.input_version,
            "label_overrides": len(override),
            "trained_at": time.strftime("%Y-%m-%d"), "sources": {s: {"personal": int(y[src == s].sum()),
                                                                     "library": int((src == s).sum() - y[src == s].sum())}
                                                                 for s in sources},
            "loo": {k: v for k, v in report["loo"].items() if not k.startswith("at_")}
            | {f"at_{t}": {"caught": report["loo"][f"at_{t}"]["caught"],
                           "library_flagged": len(report["loo"][f"at_{t}"]["library_flagged"])} for t in (0.5, THRESHOLD)}}
    # the folded weights reproduce the pipeline
    check = screen.Weights(tuple(float(x) for x in w), b, meta)
    ref = model.predict_proba(X[:5])[:, 1]
    got = [check.p(list(v)) for v in X[:5]]
    assert max(abs(r - g) for r, g in zip(ref, got)) < 1e-3, (ref, got)
    print(f"final model: C={c}, {len(y)} files", flush=True)
    if a.write or a.out:
        out = a.out or screen.weights_path()
        screen.write_weights(out, [float(x) for x in w], b, meta)
        print(f"wrote {out} ({out.stat().st_size} bytes)")
    if a.json:
        report["files"] = [{"key": k, "label": int(l), "p_loo": round(float(p), 4)} for k, l, p in zip(keys, y, p_loo)]
        report["final"] = meta
        a.json.write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
