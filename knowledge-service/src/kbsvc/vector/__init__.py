"""Vector store factory.

Embedded Qdrant holds an exclusive lock on its directory, so the process-wide
singleton here is load-bearing, not just an optimisation.
"""

from __future__ import annotations

import atexit
from threading import RLock

from .base import SearchFilter, SearchHit, VectorPoint, VectorStore
from .qdrant_store import QdrantVectorStore

_store: VectorStore | None = None
_store_lock = RLock()
_atexit_registered = False


def get_vector_store() -> VectorStore:
    """Process-wide singleton.

    Embedded local mode requires exactly one client and now shares it between
    the API request threads and the API-owned ingest worker thread.
    """
    global _atexit_registered, _store
    with _store_lock:
        if _store is None:
            _store = QdrantVectorStore()
            if not _atexit_registered:
                # Qdrant's own __del__ runs too late during interpreter teardown.
                atexit.register(reset_vector_store)
                _atexit_registered = True
        return _store


def reset_vector_store() -> None:
    global _store
    with _store_lock:
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
