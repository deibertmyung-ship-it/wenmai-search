"""Vector store factory.

Embedded Qdrant holds an exclusive lock on its directory, so the process-wide
singleton here is load-bearing, not just an optimisation.

`KB_VECTOR_BACKEND` picks the implementation. Replacing the dense half stops
here: nothing else in the tree imports a store module directly.
"""

from __future__ import annotations

import atexit
from threading import RLock

from ..config import get_settings
from ..errors import KbError
from .base import SearchFilter, SearchHit, VectorPoint, VectorStore
from .qdrant_store import QdrantVectorStore
from .sqlite_vec_store import SqliteVecStore

_store: VectorStore | None = None
_store_lock = RLock()
_atexit_registered = False


def _build_store() -> VectorStore:
    """Construct the store named by `KB_VECTOR_BACKEND`.

    The switch is read here rather than captured at import time, so a process
    can be moved onto another backend without restarting - which is what the
    ADR-0008 A/B gate does. Moving it takes both resets: `reset_settings_cache()`
    to re-read the environment, then `reset_vector_store()` to drop the store
    built from the old value.
    """
    backend = get_settings().vector_backend
    if backend == "qdrant":
        return QdrantVectorStore()
    if backend == "sqlite-vec":
        return SqliteVecStore()
    raise KbError(
        f"KB_VECTOR_BACKEND={backend!r} is not implemented yet; "
        f"set it to 'qdrant' or 'sqlite-vec'"
    )


def get_vector_store() -> VectorStore:
    """Process-wide singleton.

    Embedded local mode requires exactly one client and now shares it between
    the API request threads and the API-owned ingest worker thread.
    """
    global _atexit_registered, _store
    with _store_lock:
        if _store is None:
            _store = _build_store()
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
    "SqliteVecStore",
    "VectorPoint",
    "VectorStore",
    "get_vector_store",
    "reset_vector_store",
]
