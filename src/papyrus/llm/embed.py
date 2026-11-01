"""Text embeddings from LM Studio's OpenAI-compatible `/v1/embeddings` (the `Embedder` contract
in base.py), for the screen (screen.py).

The default model, nomic-embed-text v1.5, is 84 MB, about 30 ms per input on a base M4,
and loads just in time (~6 s) next to a language model without crowding it out. It expects a
task prefix on each input ("classification: "), which the caller adds, so that the inference
cache records the exact text sent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from . import lmstudio
from .base import BackendUnavailable, ModelInfo

DEFAULT_MODEL = "text-embedding-nomic-embed-text-v1.5"


class LMStudioEmbedder:
    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: float = 60.0):
        self.base_url = (base_url or lmstudio.default_url()).rstrip("/")
        self.model = model or os.environ.get("PAPYRUS_EMBED_MODEL") or DEFAULT_MODEL
        self._client = httpx.Client(timeout=timeout)
        self.last_usage: dict[str, Any] | None = None

    def info(self) -> ModelInfo:
        return ModelInfo("lmstudio", self.model)

    def available(self) -> bool:
        """The server answers and offers the model (downloaded; it need not be loaded)."""
        try:
            r = self._client.get(f"{self.base_url}/models", timeout=2)
            return r.status_code == 200 and any(m.get("id") == self.model for m in r.json().get("data", []))
        except (httpx.HTTPError, ValueError):
            return False

    # inference cache (inference.py)

    def cache_params(self) -> dict[str, Any]:
        return {"impl": "lmstudio-embed-1"}

    def model_build(self) -> dict[str, Any] | None:
        return lmstudio.model_build(self.base_url, self.model)

    def model_loaded(self) -> bool | None:
        try:
            r = self._client.get(f"{lmstudio.native_root(self.base_url)}/api/v0/models/{self.model}", timeout=3)
            return r.json().get("state") == "loaded" if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self._client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": texts})
        except httpx.HTTPError as e:
            raise BackendUnavailable(f"LM Studio not reachable at {self.base_url}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise BackendUnavailable(f"LM Studio embeddings error {r.status_code}")
        data = r.json()
        self.last_usage = data.get("usage")
        rows = sorted(data["data"], key=lambda d: d["index"])
        if len(rows) != len(texts):
            raise BackendUnavailable(f"LM Studio returned {len(rows)} embeddings for {len(texts)} inputs")
        return [[float(x) for x in d["embedding"]] for d in rows]
