"""Embeddings via Ollama (nomic-embed-text, 768-dim, local).

Optional by design: when Ollama isn't running, embed() returns None and
callers degrade — facts are stored without vectors and recall falls back to
keyword search. There is no cloud fallback for embeddings: all vectors in
the store must come from one model or similarity scores are meaningless.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

DEFAULT_EMBED_MODEL = "nomic-embed-text"
NOMIC_DIM = 768


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed texts, or None when the backend is unavailable."""
        ...


class OllamaEmbedder:
    dim = NOMIC_DIM

    def __init__(self, base_url: str | None = None, model: str = DEFAULT_EMBED_MODEL):
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_HOST")
            or "http://127.0.0.1:11434"
        ).rstrip("/")
        self.model = model
        #: Why the last embed() returned None — surfaced by callers in tool
        #: output / traces; the unavailable state itself is expected, not an error.
        self.last_error: str | None = None

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=2.0)
            ) as client:
                resp = await client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model, "input": texts},
                )
                resp.raise_for_status()
                embeddings = resp.json().get("embeddings")
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

        if (
            not isinstance(embeddings, list)
            or len(embeddings) != len(texts)
            or any(len(v) != self.dim for v in embeddings)
        ):
            self.last_error = f"unexpected embed response shape from {self.model}"
            return None
        self.last_error = None
        return embeddings
