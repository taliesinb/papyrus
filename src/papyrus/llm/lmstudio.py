"""LM Studio (or any OpenAI-compatible server) as an extraction backend.

Also `ensure_server`: LM Studio's local API server is separate from the app, and is often
off. When it is, and the URL is local, we start it with `lms server start` (unless
`lmstudio.autostart = false` in the config). Models then load on first request.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import BackendUnavailable, ModelInfo
from .schema_lite import SchemaError, coerce, extract_json_object

DEFAULT_URL = "http://localhost:1234/v1"
# On a base M4 (120 GB/s) the 9B runs ~11 tok/s and ~60-100 tok/s prompt processing (60-75 s per
# triage); the 4B matches it on kind/title/authors/year at ~15 s. One model for extraction and the
# decider also avoids swapping two models on a 24 GB machine.
DEFAULT_MODEL = "qwen3.5-4b-mlx"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_start_attempted: set[str] = set()


def default_url() -> str:
    return (os.environ.get("PAPYRUS_LMSTUDIO_URL") or DEFAULT_URL).rstrip("/")


def lms_path() -> str | None:
    """The `lms` CLI that ships with LM Studio (often not on PATH)."""
    found = shutil.which("lms")
    if found:
        return found
    p = Path.home() / ".lmstudio" / "bin" / "lms"
    return str(p) if p.exists() else None


def server_up(base_url: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"{base_url.rstrip('/')}/models", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_server(base_url: str | None = None, *, autostart: bool | None = None, wait_s: float = 20.0) -> bool:
    """True if LM Studio's API answers at `base_url`, starting the server first if it is off,
    the URL is local and autostart is on (config `lmstudio.autostart`, default true).
    Tries to start it at most once per process and URL."""
    base_url = (base_url or default_url()).rstrip("/")
    if server_up(base_url):
        return True
    if autostart is None:
        from .. import config
        autostart = config.lmstudio_autostart()[0]
    parts = urlsplit(base_url)
    if not autostart or parts.hostname not in LOCAL_HOSTS or base_url in _start_attempted:
        return False
    _start_attempted.add(base_url)
    lms = lms_path()
    if lms is None:
        return False
    cmd = [lms, "server", "start", "--port", str(parts.port or 1234)]
    print(f"LM Studio's server is off; starting it: {' '.join(cmd)}", file=sys.stderr)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  could not start it: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"  could not start it: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if server_up(base_url, timeout=1.0):
            return True
        time.sleep(0.5)
    print(f"  started, but {base_url} still doesn't answer", file=sys.stderr)
    return False


class LMStudioBackend:
    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: float = 180.0):
        self.base_url = (base_url or default_url()).rstrip("/")
        self.model = model or os.environ.get("PAPYRUS_LMSTUDIO_MODEL") or DEFAULT_MODEL
        self._client = httpx.Client(timeout=timeout)
        self._ctx: int | None = None

    def info(self) -> ModelInfo:
        return ModelInfo("lmstudio", self.model, self._context_tokens())

    def _context_tokens(self) -> int | None:
        if self._ctx is None:
            try:  # LM Studio's native REST API exposes context lengths
                root = self.base_url.rsplit("/v1", 1)[0]
                r = self._client.get(f"{root}/api/v0/models/{self.model}", timeout=3)
                if r.status_code == 200:
                    d = r.json()
                    self._ctx = d.get("loaded_context_length") or d.get("max_context_length")
            except httpx.HTTPError:
                pass
        return self._ctx

    def available(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/models", timeout=2)
            return r.status_code == 200 and any(m.get("id") == self.model for m in r.json().get("data", []))
        except (httpx.HTTPError, ValueError):
            return False

    def extract(self, *, instructions: str, prompt: str, schema: dict[str, Any], max_tokens: int = 1024,
                temperature: float = 0.0) -> dict[str, Any]:
        system = (
            instructions.strip()
            + "\n\nRespond with a single JSON object only, matching this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
                # LM Studio (0.4.x) ignores enable_thinking / reasoning_effort for Qwen3.5 MLX
                # models, but continues a trailing assistant message: prefilling an empty
                # think block skips reasoning entirely.
                {"role": "assistant", "content": "<think>\n\n</think>\n\n"},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "result", "strict": True, "schema": schema}},
        }
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                r = self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.HTTPError as e:
                raise BackendUnavailable(f"LM Studio not reachable at {self.base_url}: {e}") from e
            if r.status_code >= 400:
                raise BackendUnavailable(f"LM Studio error {r.status_code}: {r.text[:300]}")
            msg = r.json()["choices"][0]["message"]
            # LM Studio routes output of reasoning models into reasoning_content.
            text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "")
            try:
                return coerce(extract_json_object(text), schema)
            except SchemaError as e:
                last_err = e
                body["temperature"] = 0.3
        raise SchemaError(f"model output did not match schema after retries: {last_err}")
