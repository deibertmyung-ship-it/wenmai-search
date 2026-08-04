"""Embedding protocols."""

from __future__ import annotations

from typing import Protocol

SparseVector = dict[int, float]


class DenseEmbedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SparseEmbedder(Protocol):
    name: str

    def encode_document(self, text: str) -> SparseVector: ...

    def encode_query(self, text: str) -> SparseVector: ...
