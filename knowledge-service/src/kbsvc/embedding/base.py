"""Embedding protocols."""

from __future__ import annotations

from typing import Protocol


class DenseEmbedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
