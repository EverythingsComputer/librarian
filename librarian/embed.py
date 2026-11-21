"""Embedding backends: local ONNX via fastembed (default, CPU-light) or any
OpenAI-compatible /v1/embeddings endpoint (LMStudio etc.)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class Embedder:
    def __init__(self, backend: str, model: str, base_url: str = "", api_key: str = ""):
        self.backend = backend
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._m = None
        self.dims: int = 0

    # -- fastembed ----------------------------------------------------------
    def _fast(self):
        if self._m is None:
            os.environ.setdefault("HF_HOME", str(Path(self._cache_dir()) / "hf"))
            from fastembed import TextEmbedding
            self._m = TextEmbedding(self.model)
            probe = next(iter(self._m.embed(["probe"])))
            self.dims = len(probe)
        return self._m

    def _cache_dir(self):
        from .config import DEFAULT_DATA_DIR
        return os.environ.get("LIBRARIAN_DATA", str(DEFAULT_DATA_DIR))

    # -- openai-compatible --------------------------------------------------
    def _openai(self, texts: list[str]) -> list[list[float]]:
        import httpx
        r = httpx.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers={"Authorization": f"Bearer {self.api_key or 'none'}"},
            timeout=300,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vecs = [[float(x) for x in d["embedding"]] for d in data]
        if vecs and not self.dims:
            self.dims = len(vecs[0])
        return vecs

    # -- common -------------------------------------------------------------
    def embed_batch(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        if not texts:
            return []
        if self.backend == "openai":
            out = []
            for i in range(0, len(texts), batch_size):
                out.extend(self._openai(texts[i:i + batch_size]))
            return out
        m = self._fast()
        out = []
        for vec in m.embed(texts, batch_size=batch_size):
            out.append([float(x) for x in vec])
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]


def cache_key_embed(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\x00{text}".encode("utf-8", "ignore")).hexdigest()
