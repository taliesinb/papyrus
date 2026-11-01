"""`papyrus watch`: a background ingestion daemon.

* `sources.py`: FSEvents (macOS) / inotify (Linux) / polling, reporting paths that may have changed;
* `debounce.py`: when a file is finished (size and times stable; browsers' partial files ignored);
* `system.py`: low priority, memory pressure, the single-instance lock;
* `models.py`: which LM Studio models the watcher may ask for, and which it may unload;
* `daemon.py`: the loop, its recorded runs, the status file.

The pipeline itself is the CLI's (`papyrus ingest`): `scan.scan_path` (scan) → `index.process`
with OCR (read, for pages without a text layer) → `classify.classify_file` (classify) →
`extract.extract_metadata` (extract) → `works.promote_file` (promote) → `index.process` (index)
→ `match.pipeline.match_work` (match).
"""

from .daemon import EXIT_LOCKED, Watch, read_status
from .errors import Backoff

__all__ = ["EXIT_LOCKED", "Backoff", "Watch", "read_status"]
