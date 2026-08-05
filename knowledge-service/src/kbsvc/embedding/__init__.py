"""Dense embedder factory.

The sparse half of retrieval no longer lives here: it is a real inverted index
under `kbsvc.lexical`, which keeps its own corpus statistics. What used to be a
hand-rolled BM25 plus two statistics tables is now the index's own business.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import DenseEmbedder
from .dense_hash import HashDenseEmbedder


@lru_cache(maxsize=1)
def get_dense_embedder() -> DenseEmbedder:
    settings = get_settings()
    if settings.dense_provider == "fastembed":
        from .dense_fastembed import FastEmbedDenseEmbedder

        cache_dir = settings.resolved_model_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        return FastEmbedDenseEmbedder(
            settings.dense_model,
            settings.dense_dim,
            settings.dense_batch_size,
            cache_dir=str(cache_dir),
        )
    if settings.dense_provider == "openai":
        from .dense_openai import OpenAiDenseEmbedder

        return OpenAiDenseEmbedder(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.dense_model,
            dim=settings.dense_dim,
            batch_size=settings.dense_batch_size,
        )
    return HashDenseEmbedder(settings.dense_dim)


def reset_embedder_cache() -> None:
    get_dense_embedder.cache_clear()


__all__ = [
    "DenseEmbedder",
    "get_dense_embedder",
    "reset_embedder_cache",
]
