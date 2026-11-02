"""Text embeddings from LM Studio's OpenAI-compatible `/v1/embeddings` (the `Embedder` contract
in base.py), for the screen (screen.py).

The screen's weights name their model (`model` in data/screen.bin), so this default only serves
callers without weights. Measured on a base M4 for the ~1000-token document description:
nomic-embed-text v1.5 (84 MB) ~110 ms, Qwen3-Embedding-0.6B (640 MB, the per-kind weights'
model) ~420 ms; both load just in time in 1-6 s beside a language model. Each model expects its
own task prefix on every input, which the caller adds (the weights' `prefix`), so that the
inference cache records the exact text sent. LM Studio serves only models it indexes as
embedding models: jina-embeddings-v5, granite-embedding R2 and LFM2.5-Embedding GGUFs load as
language models, and /v1/embeddings won't take them.
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
