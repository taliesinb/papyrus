"""Apple Foundation Models backend (on-device "Apple Intelligence" LLM).

Talks to the Swift bridge in ``tools/afm-bridge`` (binary ``papyrus-afm``), which wraps
``FoundationModels.LanguageModelSession`` with guided generation from a runtime
``DynamicGenerationSchema``. By default a persistent ``papyrus-afm --serve`` subprocess is used
(newline-delimited JSON), so batches do not pay process start-up per document.

Build the bridge first::

    cd tools/afm-bridge && swift build -c release

or point ``PAPYRUS_AFM_BIN`` at a built binary.

Things to know about the model (macOS 27):

* Context window is small: 8192 tokens on macOS 27 ("AFM 3 Core Advanced"); 4096 on macOS 26.
  Instructions + prompt + schema + output must all fit. ``extract`` truncates the prompt (a
  conservative character estimate here, then an exact tokenizer-based fit in the bridge).
* The system may refuse requests from non-app processes under memory pressure (reported as
  ``kind="unavailable", detail="system_busy"``); the bridge retries briefly, then we raise
  ``ModelBusy`` (a ``BackendUnavailable``) so callers can back off or fall back to another backend.
* Content guardrails can reject innocuous documents. The bridge uses
  ``permissiveContentTransformations`` and, if guided generation still trips a guardrail, retries
  unguided (JSON as text) — see ``AppleFMGuardrailError`` for what is left.
* No token log-probabilities are exposed, so this backend implements ``Backend`` but not
  ``Classifier``.
"""

from __future__ import annotations

import json
import math
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from papyrus.llm.base import BackendUnavailable, ModelBusy, ModelInfo

BIN_ENV = "PAPYRUS_AFM_BIN"
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BIN = _REPO_ROOT / "tools" / "afm-bridge" / ".build" / "release" / "papyrus-afm"

# Context size assumed when the bridge cannot report one (macOS 26 shipped with 4096).
FALLBACK_CONTEXT_TOKENS = 4096
# Measured on the AFM tokenizer: English prose ~3.6-4.2 chars/token, author lists / math /
# identifiers ~2.8-3.2, CJK ~1-1.5. We assume 2.5 for ASCII and 1 token per non-ASCII char.
_ASCII_CHARS_PER_TOKEN = 2.5
# Chat template, role markers, schema-in-prompt framing: measured ~60-130 tokens; be generous.
_OVERHEAD_TOKENS = 200


class AppleFMError(RuntimeError):
    """A model-level failure reported by the bridge (not an availability problem)."""

    def __init__(self, message: str, *, kind: str = "other", response: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.response = response or {}


class AppleFMGuardrailError(AppleFMError):
    """The on-device safety guardrails (or a model refusal) blocked this document."""


class AppleFMContextOverflow(AppleFMError):
    """The request did not fit the context window even after truncation."""


def estimate_tokens(text: str) -> int:
    """Conservative (over-)estimate of AFM tokens for `text`, without calling the model.

    Errs high: ASCII at 2.5 chars/token, every non-ASCII character counted as one token.
    Use `AppleFMBackend.count_tokens` for the exact count.
    """
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = len(text) - non_ascii
    return math.ceil(ascii_chars / _ASCII_CHARS_PER_TOKEN) + non_ascii


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Longest prefix of `text` whose `estimate_tokens` is <= max_tokens (cut at a whitespace if near)."""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # binary search on prefix length
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    ws = max(cut.rfind(" "), cut.rfind("\n"))
    if ws > lo * 0.9:
        cut = cut[:ws]
    return cut


def fit_prompt(
    *,
    instructions: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
    context_tokens: int | None,
) -> str:
    """Truncate `prompt` so instructions + schema + prompt + output fit the context (estimated)."""
    ctx = context_tokens or FALLBACK_CONTEXT_TOKENS
    fixed = (
        estimate_tokens(instructions)
        + estimate_tokens(json.dumps(schema, separators=(",", ":")))
        + _OVERHEAD_TOKENS
    )
    budget = ctx - max_tokens - fixed
    if budget <= 0:
        raise AppleFMContextOverflow(
            f"instructions+schema (~{fixed} tokens) plus max_tokens={max_tokens} exceed context {ctx}",
            kind="context_overflow",
        )
    return truncate_to_tokens(prompt, budget)


def find_binary(binary: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate papyrus-afm: explicit path, then $PAPYRUS_AFM_BIN, then the repo build dir."""
    for cand in (binary, os.environ.get(BIN_ENV), DEFAULT_BIN):
        if cand:
            p = Path(cand).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return p
    return None


class AppleFMBackend:
    """`Backend` implementation using Apple's on-device Foundation Model via papyrus-afm."""

    backend_name = "apple"

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        *,
        serve: bool = True,
        guardrails: str = "permissive",  # "permissive" | "default"
        use_case: str = "general",  # "general" | "contentTagging"
        guardrail_fallback: bool = True,
        timeout: float = 120.0,
        busy_retries: int = 2,
    ) -> None:
        self._explicit_binary = binary
        self.serve = serve
        self.guardrails = guardrails
        self.use_case = use_case
        self.guardrail_fallback = guardrail_fallback
        self.timeout = timeout
        self.busy_retries = busy_retries
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._availability: dict[str, Any] | None = None
        self.last_response: dict[str, Any] | None = None
        self.last_usage: dict[str, Any] | None = None

    # ------------------------------------------------------------------ plumbing

    @property
    def binary(self) -> Path | None:
        return find_binary(self._explicit_binary)

    def _require_binary(self) -> Path:
        b = self.binary
        if b is None:
            raise BackendUnavailable(
                f"papyrus-afm not found (set {BIN_ENV} or run `swift build -c release` in tools/afm-bridge)"
            )
        return b

    def _start(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        b = self._require_binary()
        try:
            self._proc = subprocess.Popen(
                [str(b), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as e:
            raise BackendUnavailable(f"cannot start papyrus-afm: {e}") from e
        return self._proc

    def _request_serve(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        for attempt in (1, 2):  # restart once if the server died between requests
            proc = self._start()
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.close()
                if attempt == 2:
                    raise BackendUnavailable("papyrus-afm server exited")
                continue
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if not ready:
                self.close()
                raise AppleFMError(f"papyrus-afm timed out after {timeout:.0f}s", kind="timeout")
            line = proc.stdout.readline()
            if not line:
                self.close()
                if attempt == 2:
                    raise BackendUnavailable("papyrus-afm server exited")
                continue
            return json.loads(line)
        raise BackendUnavailable("papyrus-afm server unusable")  # pragma: no cover

    def _request_once(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        b = self._require_binary()
        try:
            cp = subprocess.run(
                [str(b)], input=json.dumps(req), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            raise AppleFMError(f"papyrus-afm timed out after {timeout:.0f}s", kind="timeout") from e
        except OSError as e:
            raise BackendUnavailable(f"cannot run papyrus-afm: {e}") from e
        if cp.returncode != 0 or not cp.stdout.strip():
            raise BackendUnavailable(f"papyrus-afm failed (exit {cp.returncode}): {cp.stderr[-500:]}")
        return json.loads(cp.stdout.strip().splitlines()[-1])

    def request(self, req: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Send one raw bridge request and return the raw JSON response."""
        t = self.timeout if timeout is None else timeout
        with self._lock:
            if self.serve:
                return self._request_serve(req, t)
            return self._request_once(req, t)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def __enter__(self) -> AppleFMBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # best effort
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ Backend protocol

    def availability(self, *, probe: bool = False, refresh: bool = False) -> dict[str, Any]:
        """Bridge availability report. probe=True also checks the model service accepts work now."""
        if self._availability is not None and not refresh and not probe:
            return self._availability
        if self.binary is None:
            return {"available": False, "reason": "binary_missing"}
        try:
            resp = self.request({"op": "availability", "probe": probe}, timeout=30)
        except (BackendUnavailable, AppleFMError, json.JSONDecodeError) as e:
            return {"available": False, "reason": str(e)}
        self._availability = resp
        return resp

    def available(self) -> bool:
        return bool(self.availability().get("available"))

    def info(self) -> ModelInfo:
        a = self.availability()
        return ModelInfo(
            backend=self.backend_name,
            model=str(a.get("model") or "unknown"),
            context_tokens=a.get("context_tokens"),
        )

    def count_tokens(self, text: str) -> int:
        """Exact token count from the model's tokenizer (macOS 26.4+)."""
        resp = self.request({"op": "count_tokens", "text": text})
        if not resp.get("ok"):
            self._raise(resp)
        return int(resp["tokens"])

    # ------------------------------------------------------------------ inference cache (inference.py)

    def cache_params(self) -> dict[str, Any]:
        """Settings that shape the answer, beyond instructions, prompt and schema."""
        return {"impl": "afm-extract-1", "guardrails": self.guardrails, "use_case": self.use_case,
                "guardrail_fallback": self.guardrail_fallback}

    def model_build(self) -> dict[str, Any]:
        """The model ships with the OS: its release pins the weights."""
        import platform

        return {"macos": platform.mac_ver()[0], "darwin": os.uname().release}

    def seed_honoured(self, build: dict[str, Any]) -> bool:
        return True  # the bridge samples with SamplingMode.random(probabilityThreshold: 1, seed:)

    def extract(
        self,
        *,
        instructions: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        seed: int = 0,
    ) -> dict[str, Any]:
        self.last_usage = None
        a = self.availability()
        if not a.get("available"):
            raise BackendUnavailable(f"Apple Foundation Model unavailable: {a.get('reason')}")
        fitted = fit_prompt(
            instructions=instructions,
            prompt=prompt,
            schema=schema,
            max_tokens=max_tokens,
            context_tokens=a.get("context_tokens"),
        )
        req = {
            "op": "extract",
            "instructions": instructions,
            "prompt": fitted,
            "schema": _with_order(schema),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "seed": seed,
            "fit": True,
            "guardrails": self.guardrails,
            "use_case": self.use_case,
            "guardrail_fallback": self.guardrail_fallback,
            "busy_retries": self.busy_retries,
        }
        t0 = time.monotonic()
        resp = self.request(req)
        resp.setdefault("wall_ms", round((time.monotonic() - t0) * 1000))
        resp["python_truncated"] = len(fitted) < len(prompt)
        self.last_response = resp
        usage = resp.get("usage") or {}
        if usage:
            self.last_usage = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}
        if not resp.get("ok"):
            self._raise(resp)
        result = resp.get("result")
        if not isinstance(result, dict):
            raise AppleFMError(f"bridge returned non-object result: {result!r}", response=resp)
        return _fill_missing(result, schema)

    # ------------------------------------------------------------------ helpers

    def _raise(self, resp: dict[str, Any]) -> None:
        kind = resp.get("kind", "other")
        msg = str(resp.get("error", "unknown error"))
        if kind == "unavailable":
            self._availability = None  # re-check next time
            if resp.get("detail") == "system_busy":  # error 1013: refused under memory pressure
                raise ModelBusy(f"Apple Foundation Model busy: {msg}")
            raise BackendUnavailable(f"Apple Foundation Model unavailable: {msg}")
        if kind == "guardrail":
            raise AppleFMGuardrailError(msg, kind=kind, response=resp)
        if kind == "context_overflow":
            raise AppleFMContextOverflow(msg, kind=kind, response=resp)
        raise AppleFMError(msg, kind=kind, response=resp)


def _with_order(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of `schema` with an "x-order" list on every object, so the bridge (whose JSON parser
    loses key order) generates properties in declaration order — order matters for quality."""
    out = dict(schema)
    props = schema.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: _with_order(v) if isinstance(v, dict) else v for k, v in props.items()}
        out.setdefault("x-order", list(props))
    if isinstance(schema.get("items"), dict):
        out["items"] = _with_order(schema["items"])
    return out


def _fill_missing(value: Any, schema: dict[str, Any]) -> Any:
    """Add None for declared-but-omitted optional object properties (guided generation may skip them)."""
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        for key, sub in schema["properties"].items():
            if key not in value:
                value[key] = None
            elif isinstance(sub, dict):
                value[key] = _fill_missing(value[key], sub)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_fill_missing(v, schema["items"]) for v in value]
    return value
