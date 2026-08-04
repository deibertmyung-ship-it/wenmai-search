"""Vector store factory.

Embedded Qdrant holds an exclusive lock on its directory, so the process-wide
singleton here is load-bearing, not just an optimisation.
"""

from __future__ import annotations

import atexit

from .base import SearchFilter, SearchHit, VectorPoint, VectorStore
from .qdrant_store import QdrantVectorStore

_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """Process-wide singleton.

    Embedded Qdrant takes an exclusive lock on its directory, so a second
    instance in the same process would fail outright - this is correctness,
    not caching.
    """
    global _store
    if _store is None:
        _store = QdrantVectorStore()
        # Close before interpreter teardown; Qdrant's own __del__ runs too late.
        atexit.register(reset_vector_store)
    return _store


def reset_vector_store() -> None:
    global _store
    close = getattr(_store, "close", None)
    if close is not None:
        close()
    _store = None


__all__ = [
    "QdrantVectorStore",
    "SearchFilter",
    "SearchHit",
    "VectorPoint",
    "VectorStore",
    "get_vector_store",
    "reset_vector_store",
]
