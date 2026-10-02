"""Embedding provider abstraction.

The concrete model is deliberately behind an interface: swapping `bge-m3` for
something else should mean adding a provider and bumping `embedding_version`, not
editing the retrieval code. `available()` never raises, so an unreachable model
degrades retrieval to BM25 instead of failing the command.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from ...core import config


class EmbeddingProvider(Protocol):
    name: str
    model: str

    def available(self) -> bool: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbeddings:
    """Ollama `/api/embed` provider (bge-m3 by default)."""

    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        self.model = model or str(config.get("embedding_model", "bge-m3"))
        self.host = (host or str(config.get("ollama_host"))).rstrip("/")
        self.timeout = int(config.get("embedding_timeout", 120))
        self._available: bool | None = None

    # -- health ---------------------------------------------------------
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return False
        models = payload.get("models")
        if not isinstance(models, list):
            return False
        wanted = self.model.split(":")[0].casefold()
        return any(str(m.get("name", "")).split(":")[0].casefold() == wanted for m in models)

    def missing_reason(self) -> str:
        try:
            urllib.request.urlopen(f"{self.host}/api/tags", timeout=5).close()
        except Exception:
            return f"Ollama unreachable at {self.host} (start it with `ollama serve`)."
        return f"embedding model '{self.model}' not installed (`ollama pull {self.model}`)."

    # -- embedding ------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = max(1, int(config.get("embedding_batch", 16)))
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._embed_batch(texts[start: start + batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self.host}/api/embed",
            data=json.dumps({"model": self.model, "input": batch}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            raise RuntimeError("embedding provider returned an unexpected payload")
        return [[float(x) for x in vector] for vector in vectors]

    # -- identity for index invalidation --------------------------------
    def signature(self) -> str:
        return f"{self.name}:{self.model}:v{config.get('embedding_version', 1)}"


_provider: OllamaEmbeddings | None = None


def provider(refresh: bool = False) -> OllamaEmbeddings:
    global _provider
    desired = (str(config.get("embedding_model")), str(config.get("ollama_host")).rstrip("/"), int(config.get("embedding_timeout", 120)))
    current = (_provider.model, _provider.host, _provider.timeout) if _provider is not None else None
    if _provider is None or refresh or current != desired:
        kind = str(config.get("embedding_provider", "ollama")).casefold()
        if kind != "ollama":
            # One provider exists today; naming an unknown one should be loud
            # rather than silently falling back to a different model.
            raise ValueError(f"unknown embedding_provider: {kind}")
        _provider = OllamaEmbeddings()
    return _provider
