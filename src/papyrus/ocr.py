"""OCR of PDF pages with Apple Vision, through the Swift bridge in tools/afm-bridge (op "ocr").

The bridge renders each page with CoreGraphics and runs Vision's RecognizeDocumentsRequest
(macOS 26), which keeps reading order across columns. Measured on the base M4: ~57 ms a page
once warm; the first request of a process pays ~25 s of model warm-up.
"""

from __future__ import annotations

from pathlib import Path

from .llm.apple import AppleFMBackend, AppleFMError
from .llm.base import BackendUnavailable

SOURCE = "ocr:vision"
BATCH = 8  # pages per bridge request, recognised concurrently


class VisionOCR:
    def __init__(self, binary: str | None = None, timeout: float = 180.0) -> None:
        # AppleFMBackend is only the transport here: it owns the `papyrus-afm --serve` process.
        self._bridge = AppleFMBackend(binary, timeout=timeout)
        self._ok: bool | None = None
        self.reason = ""

    def available(self) -> bool:
        """True if the bridge is built and new enough to have the ocr op."""
        if self._ok is None:
            try:
                r = self._bridge.request({"op": "ocr", "path": "", "pages": []}, timeout=30)
                self._ok = bool(r.get("ok"))
                self.reason = "" if self._ok else (
                    f"{r.get('error')} (rebuild it: cd tools/afm-bridge && swift build -c release)")
            except (BackendUnavailable, AppleFMError, ValueError) as e:
                self._ok, self.reason = False, str(e)
        return self._ok

    def pages(self, path: Path, pages: list[int]) -> dict[int, str]:
        """Recognised text of the given 1-based pages. Pages that fail are left out."""
        out: dict[int, str] = {}
        for i in range(0, len(pages), BATCH):
            r = self._bridge.request({"op": "ocr", "path": str(path), "pages": pages[i:i + BATCH]})
            if not r.get("ok"):
                raise AppleFMError(f"ocr failed: {r.get('error')}", kind=r.get("kind", "other"), response=r)
            for p in r["pages"]:
                if "text" in p:
                    out[p["page"]] = p["text"]
        return out

    def close(self) -> None:
        self._bridge.close()
