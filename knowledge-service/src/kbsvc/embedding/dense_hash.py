"""Deterministic hashing embedder.

Not a semantic model - it is a signed random-projection of character n-grams.
Its job is to make the whole pipeline runnable and testable with zero downloads
and zero network. Switch KB_DENSE_PROVIDER to fastembed/openai for semantics.
"""

from __future__ import annotations

import math
import zlib

from .sparse_bm25 import tokenize


class HashDenseEmbedder:
    name = "hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = zlib.crc32(token.encode("utf-8"))
            index = digest % self.dim
            sign = 1.0 if (digest >> 31) & 1 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
