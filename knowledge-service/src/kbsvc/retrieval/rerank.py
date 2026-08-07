"""Rerankers.

`lexical` is the default: offline, sub-millisecond, and on classical Chinese it
reorders usefully because query terms appear near-verbatim in the source. Swap
to `cross-encoder` when you have the GPU budget and want semantic reranking.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import get_settings
from ..lexical.tokenizer import tokenize

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    name: str
    # Whether `score` can use `tokens`. The pipeline only pays for the lookup
    # when someone will read the result.
    uses_tokens: bool

    def score(
        self, query: str, texts: list[str], *, tokens: list[list[str] | None] | None = None
    ) -> list[float]: ...


class NoopReranker:
    name = "none"
    uses_tokens = False

    def score(
        self, query: str, texts: list[str], *, tokens: list[list[str] | None] | None = None
    ) -> list[float]:
        return [0.0] * len(texts)


class LexicalReranker:
    """Query-term coverage weighted by first-occurrence position.

    coverage = matched distinct query terms / total distinct query terms
    density  = matched occurrences / candidate length (square-root damped)
    earliness rewards a match near the top of the chunk.

    Tokenizing the candidates dominates the cost, so `score` accepts tokens
    precomputed at ingest (`chunk.analyzed`). Falls back to tokenizing per
    candidate when they are absent, which keeps a partial backfill correct.
    """

    name = "lexical"
    uses_tokens = True

    def score(
        self, query: str, texts: list[str], *, tokens: list[list[str] | None] | None = None
    ) -> list[float]:
        query_terms = set(tokenize(query))
        if not query_terms:
            return [0.0] * len(texts)

        scores: list[float] = []
        for index, text in enumerate(texts):
            candidate = tokens[index] if tokens is not None else None
            if not candidate:
                candidate = tokenize(text)
            if not candidate:
                scores.append(0.0)
                continue
            matched = query_terms & set(candidate)
            coverage = len(matched) / len(query_terms)
            hits = sum(1 for token in candidate if token in query_terms)
            density = hits / (len(candidate) ** 0.5 + 1.0)
            first = next(
                (i for i, token in enumerate(candidate) if token in query_terms), len(candidate)
            )
            earliness = 1.0 - (first / len(candidate))
            scores.append(round(0.7 * coverage + 0.2 * min(density, 1.0) + 0.1 * earliness, 6))
        return scores


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder. Optional dependency: kbsvc[rerank]."""

    name = "cross-encoder"
    # A cross-encoder scores raw text pairs; the analyzer's tokens are the wrong
    # input for it.
    uses_tokens = False

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def score(
        self, query: str, texts: list[str], *, tokens: list[list[str] | None] | None = None
    ) -> list[float]:
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
