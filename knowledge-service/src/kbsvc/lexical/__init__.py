"""Lexical store factory.

Tantivy takes a directory lock, so the process-wide singleton here is
load-bearing, not just an optimisation - the same reason `vector/__init__.py`
keeps one Qdrant client.
"""

from __future__ import annotations

import atexit
from threading import RLock

from .base import LexicalDocument, LexicalStore, SearchFilter, SearchHit
from .tantivy_store import TantivyLexicalStore
from .tokenizer import analyze, tokenize

_store: LexicalStore | None = None
_store_lock = RLock()
_atexit_registered = False


def get_lexical_store() -> LexicalStore:
    global _atexit_registered, _store
    with _store_lock:
        if _store is None:
            _store = TantivyLexicalStore()
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
    "LexicalDocument",
    "LexicalStore",
    "SearchFilter",
    "SearchHit",
    "TantivyLexicalStore",
    "analyze",
    "get_lexical_store",
    "reset_lexical_store",
    "tokenize",
]
