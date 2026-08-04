"""Embedder factories and the DB-backed BM25 statistics provider."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import repo
from ..db.session import session_scope
from .base import DenseEmbedder, SparseEmbedder, SparseVector
from .dense_hash import HashDenseEmbedder
from .sparse_bm25 import Bm25SparseEmbedder, StaticTermStats


class DbTermStats:
    """Reads BM25 corpus statistics from the metadata store.

    Query-time stats are cached per instance so a single search does one round
    trip; indexing constructs a fresh instance per batch.
    """

    def __init__(self, tenant_id: str, session: Session | None = None) -> None:
        self.tenant_id = tenant_id
        self._session = session
        self._corpus: tuple[int, float] | None = None

    def _load_corpus(self) -> tuple[int, float]:
        if self._corpus is None:
            if self._session is not None:
                stat = repo.get_corpus_stat(self._session, self.tenant_id)
            else:
                with session_scope() as session:
                    stat = repo.get_corpus_stat(session, self.tenant_id)
            self._corpus = (stat.chunk_count, stat.avg_length)
        return self._corpus

    def doc_freq(self, terms: list[str]) -> dict[str, int]:
        if self._session is not None:
            return repo.load_term_stats(self._session, self.tenant_id, terms)
        with session_scope() as session:
            return repo.load_term_stats(session, self.tenant_id, terms)

    def chunk_count(self) -> int:
        return self._load_corpus()[0]

    def avg_length(self) -> float:
        return self._load_corpus()[1]


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


def get_sparse_embedder(tenant_id: str, session: Session | None = None) -> SparseEmbedder:
    return Bm25SparseEmbedder(DbTermStats(tenant_id, session))


def reset_embedder_cache() -> None:
    get_dense_embedder.cache_clear()


__all__ = [
    "Bm25SparseEmbedder",
    "DbTermStats",
    "DenseEmbedder",
    "SparseEmbedder",
    "SparseVector",
    "StaticTermStats",
    "get_dense_embedder",
    "get_sparse_embedder",
    "reset_embedder_cache",
]
