"""Train the screen's per-kind weights (src/papyrus/data/screen.bin, the production classifier; see
screen.py) on classified files.

    uv run python tools/screen/export.py --out /tmp/training-local     # each library, any machine
    uv run python tools/screen/folders.py --root ~/Dropbox/doc --out /tmp/training-folders --map …
    sqlite3 ~/.papyrus/release/papyrus.sqlite ".backup /tmp/lib.sqlite"
    uv run --with numpy --with scikit-learn python tools/screen/train.py \\
        --export local=/tmp/training-local --export folders=/tmp/training-folders \\
        --db /tmp/lib.sqlite --originals ~/Downloads --originals ~/Desktop \\
        [--ask-classifier] [--personal FILE_ID ...] [--library FILE_ID ...] [--folder-labels] \\
        [--model text-embedding-qwen3-embedding-0.6b] [--write] [--json /tmp/screen-eval.json]

Then commit data/screen.bin (its header records the training counts and cross-validated metrics).

The target is the classifier's kind distribution (the 4B language model's, over classify.KINDS):
the screen learns to say what the classifier would, and is trusted only where it is sure (the
accept threshold, `accept_threshold` in the weights). Labelled files come from:

* `--export NAME=DIR`: export.py's output (one JSON per file) of a library, local or another
  machine's, or folders.py's. The label is the file's `kind_probs` from a `classify:` source.
  Files without one (the screen set them aside, or accepted their kind: the screen's own word is
  no label) get one from the classifier with `--ask-classifier`; else those judged personal count
  as one-hot personal and the rest are left out. So are files without text, and purged ones
  (next). With `--folder-labels`, folders.py's files are labelled by their folder instead
  (one-hot, the folder's first kind).
* `--db` + `--originals DIR`: files purged as personal paperwork keep only their sha256 in the
  library, so their inputs come from the originals, found by hashing the PDFs in DIR (read only).
  One-hot personal.
* `--personal` / `--library`: file ids of `--db` whose label you set yourself (by sha256, so they
  override the exports too): one-hot personal, or the classifier's distribution without personal.

Each file is embedded (the document description: the export's `triage_prompt`, or for purged
originals describe.render_state on the PDF itself; behind the model's task prefix) straight from
LM Studio, *not* through the inference cache: the cache would write the pages of purged documents
back to disk. Same for --ask-classifier.

It reports cross-validated metrics: 10 folds, grouped so that a series of files ("Columbia graph
theory 1 … 23", payslips) is never split between training and test, C chosen inside each fold:
agreement with the label's top kind, per kind, P(personal) as the old screen's, and the accept
threshold's coverage, agreement and personal files let through (must be none). Then it fits all
files and, with --write, writes the weights with the standardisation folded in. Output names files
as `<source>:<file id or sha16>`, with counts and metrics only: no names, paths or text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold, StratifiedGroupKFold
from sklearn.preprocessing import StandardScaler

from papyrus import db, describe, screen
from papyrus.classify import CLASSIFICATIONS, KINDS
from papyrus.filenames import parse_filename
from papyrus.llm.embed import LMStudioEmbedder
from papyrus.pdf import PageReader, read_pdf

if sys.platform == "darwin":
    from papyrus.macos import where_froms
else:
    from papyrus.linux import where_froms

KINDS = list(KINDS)
CS = [0.01, 0.1, 1, 10]
THRESHOLD = 0.9  # P(personal): set aside
ACCEPT = 0.9  # P(kind): accepted without the classifier
MODEL = "text-embedding-qwen3-embedding-0.6b"
PREFIXES = {  # each model's task instruction
    "text-embedding-qwen3-embedding-0.6b":
        "Instruct: Classify the kind of document (research paper, book, chapter, thesis, slides, tutorial, "
        "lecture notes, article, report, manual, personal paperwork, other)\nQuery: ",
    "text-embedding-embeddinggemma-2": "task: classification | query: ",
    "text-embedding-nomic-embed-text-v1.5": screen.PREFIX,
}
PERSONAL = {k: 1.0 if k == "personal" else 0.0 for k in KINDS}


def classify_probs(r: dict) -> dict[str, float] | None:
    for c in (r.get("predictions") or {}).get("kind_probs") or []:
        if c["source"].startswith("classify:") and isinstance(c["value"], dict):
            return c["value"]
    return None


def from_exports(name: str, d: Path, folder_labels: bool) -> list[dict]:
    out = []
    for p in sorted(d.glob("*.json")):
        r = json.loads(p.read_text())
        gt, inp = r["ground_truth"], r["inputs"]
        if gt.get("label_status") == "purged" or not (inp.get("text") or "").strip():
            continue
        pv = str(r["export"]["prompt_version"])  # "triage-5" before 0.x numbering, now "0.6"
        if db.version(pv.rpartition("-")[2] if pv.startswith("triage-") else pv) < (0, 5):
            sys.exit(f"{d}: exported with {pv}; the screen reads 0.5's document description: export again")
        rec = {"key": f"{name}:{r['file_id'] if r.get('file_id') is not None else r['id']}", "source": name,
               "sha256": r["sha256"], "input": inp["triage_prompt"], "name": inp.get("filename") or "",
               "folder_kinds": gt.get("kinds"), "personal": gt.get("is_personal"), "label_from": "classifier"}
        probs = classify_probs(r)
        if gt.get("label_status") == "rejected_personal":  # its work was rejected: not library material
            probs, rec["label_from"] = dict(PERSONAL), "rejected"
        elif probs is not None and gt.get("label_status") == "work" and max(probs, key=probs.get) == "personal":
            q = {k: (0.0 if k == "personal" else v) for k, v in probs.items()}  # kept (`keep`): library
            probs, rec["label_from"] = {k: v / (sum(q.values()) or 1.0) for k, v in q.items()}, "kept"
        if folder_labels and gt.get("label_status") == "folder":
            probs, rec["label_from"] = {k: 1.0 if k == gt["kinds"][0] else 0.0 for k in KINDS}, "folder"
        if probs is None and gt.get("is_personal") is None:
            continue  # not classified yet
        out.append({**rec, "probs": probs})
    return out


def from_originals(name: str, lib: Path, dirs: list[Path]) -> list[dict]:
    conn = sqlite3.connect(f"file:{lib}?mode=ro", uri=True)
    purged = {r[0]: r[1] for r in conn.execute("SELECT sha256, id FROM files WHERE purged_at IS NOT NULL")}
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
                if not "".join(info.pages).strip():
                    continue
                state = original_state(p, info)
            except Exception:  # noqa: BLE001  unreadable: skip (the message may hold the path)
                continue
            out.append({"key": f"{name}:{purged[sha]}", "source": name, "sha256": sha, "input": state,
                        "name": p.name, "probs": dict(PERSONAL), "label_from": "purged", "personal": True})
    return out


def original_state(p: Path, info) -> str:
    """The document description of a PDF that is not in the library (a purged original), as
    build_state would have given it: name, folder, size, download URL, PDF metadata, filename hint, pages."""
    froms = where_froms(p) or []
    hint = parse_filename(p.stem)
    meta = dict(info.meta)
    d = describe.Document(
        names=[p.name], folders=[screen.home_relative(str(p.parent))],
        origins=[froms[0] + (f" (via {froms[1]})" if len(froms) > 1 else "")] if froms else [],
        size=p.stat().st_size, page_count=info.page_count,
        meta={k: meta.get(k) for k in describe.META_FIELDS},
        hint=db.dumps({"title": hint.title, "surnames": hint.surnames, "year": hint.year}) if hint else None)
    with PageReader(p) as reader:
        return describe.render_state(d, info.page_count, reader.text)


def group_of(r: dict) -> str:
    """Files of one series share a group: the name without digits and punctuation, when that
    leaves a word or two (an arXiv id leaves nothing much: its own group)."""
    letters = re.sub(r"[\d\W_]+", "", Path(r["name"]).stem.lower())
    return letters[:14] if len(letters) >= 6 else r["sha256"]


# --- fitting -------------------------------------------------------------------------------------


def fit(X, Y, C):
    """Logistic regression on the label distributions Y (each file repeated per kind, weighted by
    its probability: this minimises the cross-entropy to Y)."""
    sc = StandardScaler().fit(X)
    Z = sc.transform(X)
    rows, labs, w = [], [], []
    for i in range(len(X)):
        for k in np.nonzero(Y[i] > 1e-3)[0]:
            rows.append(i)
            labs.append(k)
            w.append(Y[i, k])
    lr = LogisticRegression(C=C, max_iter=5000).fit(Z[rows], np.array(labs), sample_weight=np.array(w))
    return sc, lr


def proba(model, X):
    sc, lr = model
    P = np.full((len(X), len(KINDS)), 1e-9)
    P[:, lr.classes_] = lr.predict_proba(sc.transform(X))
    return P / P.sum(1, keepdims=True)


def choose_c(X, Y, groups) -> float:
    folds = list(GroupKFold(min(5, len(set(groups)))).split(X, groups=groups))
    best = None
    for C in CS:
        ll = [-np.mean(np.sum(Y[b] * np.log(proba(fit(X[a], Y[a], C), X[b])), 1)) for a, b in folds]
        if best is None or np.mean(ll) < best[0]:
            best = (float(np.mean(ll)), C)
    return best[1]


def cross_validate(X, Y, groups):
    P, Cs = np.zeros_like(Y), []
    split = StratifiedGroupKFold(10, shuffle=True, random_state=0).split(X, Y.argmax(1), groups)
    for k, (tr, te) in enumerate(split):
        C = choose_c(X[tr], Y[tr], groups[tr])
        Cs.append(C)
        P[te] = proba(fit(X[tr], Y[tr], C), X[te])
        print(f"  fold {k + 1}/10 (C={C})", flush=True)
    return P, Cs


# --- metrics -------------------------------------------------------------------------------------


def metrics(keys, Y, P, folder_kinds) -> dict:
    keys = np.array(keys)
    y, pred, top = Y.argmax(1), P.argmax(1), P.max(1)
    pers = KINDS.index("personal")
    lib = y != pers
    m: dict = {"n": len(y), "n_personal": int((~lib).sum()),
               "agree": round(float(np.mean(pred == y)), 4),
               "agree_library": round(float(np.mean(pred[lib] == y[lib])), 4) if lib.any() else None,
               "mean_kl": round(float(np.mean(np.sum(Y * (np.log(np.clip(Y, 1e-12, 1)) - np.log(P)), 1))), 4),
               "per_kind": {KINDS[k]: f"{int(np.sum((pred == k) & (y == k)))}/{int(np.sum(y == k))}"
                            for k in sorted(set(y))}}
    if 0 < (~lib).sum() < len(y):
        m["personal_auc"] = round(float(roc_auc_score((~lib).astype(int), P[:, pers])), 4)
    for t in (0.5, THRESHOLD):
        m[f"personal_at_{t}"] = {"caught": int(((P[:, pers] >= t) & ~lib).sum()),
                                 "library_flagged": [str(k) for k in keys[(P[:, pers] >= t) & lib]]}
    m["accept"] = {}
    for t in (0.8, ACCEPT, 0.95):
        acc = (top >= t) & (pred != pers) & (P[:, pers] < THRESHOLD)
        m["accept"][str(t)] = {
            "accepted_share_of_library": round(float(np.mean(acc[lib])), 3) if lib.any() else None,
            "agree_where_accepted": round(float(np.mean(pred[acc] == y[acc])), 4) if acc.any() else None,
            "personal_accepted": [str(k) for k in keys[acc & ~lib]]}  # personal paperwork let through: must be none
    sel = [i for i, f in enumerate(folder_kinds) if f]
    if sel:
        ok = [set(KINDS.index(k) for k in folder_kinds[i]) for i in sel]
        acc = (top >= ACCEPT) & (pred != pers)
        hybrid = np.where(acc, pred, y)  # the screen where it is sure, else the classifier
        m["folders"] = {"n": len(sel),
                        "screen": round(float(np.mean([pred[i] in o for i, o in zip(sel, ok)])), 4),
                        "label": round(float(np.mean([y[i] in o for i, o in zip(sel, ok)])), 4),
                        "screen_if_sure_else_label": round(float(np.mean([hybrid[i] in o for i, o in zip(sel, ok)])), 4)}
    return m


def show(title: str, m: dict) -> None:
    print(f"{title}: {m['n']} files ({m['n_personal']} personal); agrees with the label's kind {m['agree']:.3f} "
          f"(library {m['agree_library']}), KL {m['mean_kl']:.3f}", flush=True)
    print(f"  per kind (label's kind found / files): {m['per_kind']}")
    if "personal_auc" in m:
        a = m[f"personal_at_{THRESHOLD}"]
        print(f"  P(personal): AUC {m['personal_auc']:.4f}; >= {THRESHOLD}: caught {a['caught']}, "
              f"library flagged {a['library_flagged'] or 'none'}")
    for t, a in m["accept"].items():
        print(f"  accept at {t}: {a['accepted_share_of_library']} of library files, agreeing {a['agree_where_accepted']};"
              f" personal accepted: {a['personal_accepted'] or 'none'}")
    if "folders" in m:
        f = m["folders"]
        print(f"  vs the folders ({f['n']}): screen {f['screen']:.3f}, label {f['label']:.3f}, "
              f"screen if sure (>= {ACCEPT}) else label {f['screen_if_sure_else_label']:.3f}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", action="append", default=[], metavar="NAME=DIR")
    ap.add_argument("--db", type=Path, help="the library whose purged files --originals recovers (read only)")
    ap.add_argument("--originals", action="append", type=Path, default=[])
    ap.add_argument("--name", default="local", help="source name for --db files [local]")
    ap.add_argument("--personal", type=int, nargs="*", default=[])
    ap.add_argument("--library", type=int, nargs="*", default=[])
    ap.add_argument("--ask-classifier", nargs="?", const="lmstudio:qwen3.5-4b-mlx", default=None, metavar="SPEC",
                    help="label files without a classifier claim by asking it [lmstudio:qwen3.5-4b-mlx]")
    ap.add_argument("--folder-labels", action="store_true", help="label folders.py's files by their folder")
    ap.add_argument("--model", default=MODEL, help=f"embedding model [{MODEL}]")
    ap.add_argument("--prefix", help="its task instruction (default: known per model)")
    ap.add_argument("--accept", type=float, default=ACCEPT, help=f"accept threshold written to the weights [{ACCEPT}]")
    ap.add_argument("--write", action="store_true", help=f"write {screen.weights_path()}")
    ap.add_argument("--out", type=Path, help="write the weights here instead")
    ap.add_argument("--json", type=Path, help="cross-validated predictions and metrics (keys, no text)")
    a = ap.parse_args(argv)
    prefix = a.prefix if a.prefix is not None else PREFIXES.get(a.model)
    if prefix is None:
        sys.exit(f"no known task prefix for {a.model}: pass --prefix")

    recs: list[dict] = []
    if a.db and a.originals:
        recs += from_originals(a.name, a.db, [d.expanduser() for d in a.originals])
    for spec in a.export:
        name, _, d = spec.partition("=")
        recs += from_exports(name, Path(d).expanduser(), a.folder_labels)
    override = {}
    if a.personal or a.library:
        lib = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        for ids, label in ((a.personal, True), (a.library, False)):
            for i in ids:
                r = lib.execute("SELECT sha256 FROM files WHERE id=?", (i,)).fetchone()
                if r is None:
                    sys.exit(f"no file {i} in --db")
                override[r[0]] = label
    seen, uniq = set(), []
    for r in recs:  # originals first, then exports in the order given
        if r["sha256"] not in seen:
            seen.add(r["sha256"])
            uniq.append(r)
    recs = uniq

    unlabelled = [r for r in recs if r["probs"] is None and r["sha256"] not in override]
    if unlabelled and a.ask_classifier:
        from papyrus.llm.classify import get_classifier

        clf = get_classifier(a.ask_classifier)
        print(f"asking {clf.info().tag} about {len(unlabelled)} files without a classifier label", flush=True)
        for r in unlabelled:
            r["probs"], r["label_from"] = clf.classify(r["input"], CLASSIFICATIONS)["kind"].probabilities, "asked"
    for r in recs:
        if r["sha256"] in override:
            if override[r["sha256"]]:
                r["probs"], r["label_from"] = dict(PERSONAL), "user"
            elif r["probs"] is not None:
                q = {k: (0.0 if k == "personal" else v) for k, v in r["probs"].items()}
                s = sum(q.values()) or 1.0
                r["probs"], r["label_from"] = {k: v / s for k, v in q.items()}, "user"
        elif r["probs"] is None and r.get("personal"):
            r["probs"], r["label_from"] = dict(PERSONAL), "personal"
    recs = [r for r in recs if r["probs"] is not None]
    for s in sorted({r["source"] for r in recs}):
        rs = [r for r in recs if r["source"] == s]
        top = [max(r["probs"], key=r["probs"].get) for r in rs]
        print(f"{s}: {len(rs)} files, {top.count('personal')} personal; labels from "
              + ", ".join(f"{k} {sum(1 for r in rs if r['label_from'] == k)}" for k in sorted({r['label_from'] for r in rs})),
              flush=True)

    emb = LMStudioEmbedder(model=a.model)
    t0 = time.perf_counter()
    emb.embed([prefix + recs[0]["input"]])
    print(f"first embedding (may load the model): {time.perf_counter() - t0:.1f} s; embedding {len(recs)} files", flush=True)
    vecs, times = [], []
    for i, r in enumerate(recs):
        t0 = time.perf_counter()
        (v,) = emb.embed([prefix + r["input"]])
        times.append(time.perf_counter() - t0)
        vecs.append(v)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(recs)}, median {statistics.median(times) * 1000:.0f} ms", flush=True)
    X = np.array(vecs, dtype=np.float64)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    Y = np.array([[float(r["probs"].get(k, 0.0)) for k in KINDS] for r in recs])
    Y /= Y.sum(1, keepdims=True)
    keys = [r["key"] for r in recs]
    groups = np.array([group_of(r) for r in recs])
    folder_kinds = [r.get("folder_kinds") for r in recs]
    src = np.array([r["source"] for r in recs])
    report: dict = {"embed_ms_median": round(statistics.median(times) * 1000, 1), "model": a.model, "prefix": prefix}

    print(f"cross-validating ({len(set(groups))} groups)…", flush=True)
    P, Cs = cross_validate(X, Y, groups)
    report["cv"] = metrics(keys, Y, P, folder_kinds)
    show("cross-validated, all", report["cv"])
    report["cv_by_source"] = {}
    for s in sorted(set(src)):
        sel = src == s
        report["cv_by_source"][s] = metrics(np.array(keys)[sel], Y[sel], P[sel],
                                            [f for f, t in zip(folder_kinds, sel) if t])
        show(f"cross-validated, {s} files", report["cv_by_source"][s])

    C = statistics.mode(Cs)
    sc, lr = fit(X, Y, C)
    classes = [KINDS[k] for k in lr.classes_]
    W = lr.coef_ / sc.scale_
    b = lr.intercept_ - W @ sc.mean_
    cv = report["cv"]
    meta = {"model": a.model, "prefix": prefix, "input_version": 3, "classes": classes,
            "threshold": THRESHOLD, "accept_threshold": a.accept, "C": C, "trained_at": time.strftime("%Y-%m-%d"),
            "labels": {lf: sum(1 for r in recs if r["label_from"] == lf) for lf in sorted({r["label_from"] for r in recs})},
            "sources": {s: int((src == s).sum()) for s in sorted(set(src))},
            "cv": {k: cv[k] for k in ("n", "n_personal", "agree", "agree_library", "personal_auc") if k in cv}
            | {"accept": {t: {k: (len(v) if isinstance(v, list) else v) for k, v in x.items()}
                          for t, x in cv["accept"].items()}}}
    check = screen.Weights(tuple(tuple(float(x) for x in row) for row in W), tuple(float(x) for x in b), meta)
    ref = proba((sc, lr), X[:5])
    got = np.array([[check.probs(list(v)).get(k, 0.0) for k in KINDS] for v in X[:5]])
    assert np.max(np.abs(ref - got)) < 1e-3, (ref, got)  # the folded weights reproduce the pipeline
    print(f"final model: C={C}, {len(recs)} files, classes {classes}", flush=True)
    if a.write or a.out:
        out = a.out or screen.weights_path()
        screen.write_weights(out, W.tolist(), b.tolist(), meta)
        print(f"wrote {out} ({out.stat().st_size} bytes)")
    if a.json:
        report["files"] = [{"key": k, "label": KINDS[int(y)], "pred": KINDS[int(p)], "p": round(float(q), 4)}
                           for k, y, p, q in zip(keys, Y.argmax(1), P.argmax(1), P.max(1))]
        report["final"] = meta
        a.json.write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
