"""Lexical store factory.

Tantivy takes a directory lock, so the process-wide singleton here is
load-bearing for that backend, not just an optimisation - the same reason
`vector/__init__.py` keeps one Qdrant client. `Fts5LexicalStore` shares the
SQLite engine and `PgSearchLexicalStore` shares the Postgres engine, so
neither has an equivalent lock to defend, but the singleton stays - nothing
else in the tree imports a store module directly, and the A/B gate needs one
rebuild point regardless of backend.

`KB_LEXICAL_BACKEND` picks the implementation. Replacing the BM25 half stops
here: nothing else in the tree imports a store module directly.
"""

from __future__ import annotations

import atexit
from threading import RLock

from ..config import get_settings
from ..errors import KbError
from .base import LexicalDocument, LexicalStore, SearchFilter, SearchHit
from .fts5_store import Fts5LexicalStore
from .pg_search_store import PgSearchLexicalStore
from .tantivy_store import TantivyLexicalStore
from .tokenizer import analyze, tokenize

_store: LexicalStore | None = None
_store_lock = RLock()
_atexit_registered = False


def _build_store() -> LexicalStore:
    """Construct the store named by `KB_LEXICAL_BACKEND`.

    Read here rather than at import time for the reason given in
    `vector/__init__.py`: the A/B gate moves a live process between backends,
    and that needs `reset_settings_cache()` before `reset_lexical_store()`.
    """
    backend = get_settings().lexical_backend
    if backend == "tantivy":
        return TantivyLexicalStore()
    if backend == "fts5":
        return Fts5LexicalStore()
    if backend == "pg-search":
        return PgSearchLexicalStore()
    raise KbError(
        f"KB_LEXICAL_BACKEND={backend!r} is not implemented yet; "
        f"set it to 'tantivy', 'fts5', or 'pg-search'"
    )


def get_lexical_store() -> LexicalStore:
    global _atexit_registered, _store
    with _store_lock:
        if _store is None:
            _store = _build_store()
            _store.ensure_ready()
            if not _atexit_registered:
                atexit.register(reset_lexical_store)
                _atexit_registered = True
        return _store


def reset_lexical_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


__all__ = [
    "Fts5LexicalStore",
    "LexicalDocument",
    "LexicalStore",
    "PgSearchLexicalStore",
    "SearchFilter",
    "SearchHit",
    "TantivyLexicalStore",
    "analyze",
    "get_lexical_store",
    "reset_lexical_store",
    "tokenize",
]
