"""Lexical store factory.

`KB_LEXICAL_BACKEND` picks fts5 (local) or pg-search (server).
Nothing else in the tree imports a store module directly.
"""

from __future__ import annotations

import atexit
from threading import RLock

from ..config import get_settings
from ..errors import KbError
from .base import LexicalDocument, LexicalStore, SearchFilter, SearchHit
from .fts5_store import Fts5LexicalStore
from .pg_search_store import PgSearchLexicalStore
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
    if backend == "fts5":
        return Fts5LexicalStore()
    if backend == "pg-search":
        return PgSearchLexicalStore()
    raise KbError(
        f"KB_LEXICAL_BACKEND={backend!r} is not implemented; "
        f"set it to 'fts5' or 'pg-search'"
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
    "analyze",
    "get_lexical_store",
    "reset_lexical_store",
    "tokenize",
]
