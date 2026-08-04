"""Rerankers.

`lexical` is the default: offline, sub-millisecond, and on classical Chinese it
reorders usefully because query terms appear near-verbatim in the source. Swap
to `cross-encoder` when you have the GPU budget and want semantic reranking.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import get_settings
from ..embedding.sparse_bm25 import tokenize

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class NoopReranker:
    name = "none"

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [0.0] * len(texts)


class LexicalReranker:
    """Query-term coverage weighted by first-occurrence position.

    coverage = matched distinct query terms / total distinct query terms
    density  = matched occurrences / candidate length (log-damped)
    earliness rewards a match near the top of the chunk.
    """

    name = "lexical"

    def score(self, query: str, texts: list[str]) -> list[float]:
        query_terms = set(tokenize(query))
        if not query_terms:
            return [0.0] * len(texts)

        scores: list[float] = []
        for text in texts:
            tokens = tokenize(text)
            if not tokens:
                scores.append(0.0)
                continue
            token_set = set(tokens)
            matched = query_terms & token_set
            coverage = len(matched) / len(query_terms)
            hits = sum(1 for token in tokens if token in query_terms)
            density = hits / (len(tokens) ** 0.5 + 1.0)
            first = next((i for i, token in enumerate(tokens) if token in query_terms), len(tokens))
            earliness = 1.0 - (first / len(tokens))
            scores.append(round(0.7 * coverage + 0.2 * min(density, 1.0) + 0.1 * earliness, 6))
        return scores


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder. Optional dependency: kbsvc[rerank]."""

    name = "cross-encoder"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        pairs = [(query, text) for text in texts]
        return [float(value) for value in self._model.predict(pairs)]


_cache: dict[str, Reranker] = {}


def get_reranker(kind: str | None = None) -> Reranker:
    settings = get_settings()
    name = kind or settings.reranker
    if name in _cache:
        return _cache[name]

    reranker: Reranker
    if name == "none":
        reranker = NoopReranker()
    elif name == "cross-encoder":
        try:
            reranker = CrossEncoderReranker(settings.rerank_model)
        except Exception as exc:
            logger.warning("cross-encoder unavailable (%s), falling back to lexical", exc)
            reranker = LexicalReranker()
    else:
        reranker = LexicalReranker()
    _cache[name] = reranker
    return reranker


def reset_reranker_cache() -> None:
    _cache.clear()
