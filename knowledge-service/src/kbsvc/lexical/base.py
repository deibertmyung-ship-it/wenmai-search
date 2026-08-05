"""Lexical store contracts.

The lexical half of retrieval used to be a hand-rolled BM25 encoded into Qdrant
sparse vectors. It is now a real inverted index. `SearchFilter` and `SearchHit`
are shared with the vector side deliberately: both retrievers answer the same
question and the fusion stage must not care which one produced a hit.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from ..vector.base import SearchFilter, SearchHit

__all__ = ["LexicalDocument", "LexicalStore", "SearchFilter", "SearchHit"]


@dataclass
class LexicalDocument:
    """One indexable chunk.

    `text` is the raw chunk text - tokenization is the store's business, so the
    ingest path never has to know how the corpus is analysed.
    """

    id: str
    text: str
    payload: dict


class LexicalStore(Protocol):
    def ensure_ready(self) -> None: ...

    def bulk(self) -> AbstractContextManager[None]:
        """Defer commits until the block exits; for full rebuilds only."""
        ...

    def upsert(self, documents: list[LexicalDocument]) -> None: ...

    def delete_by_ids(self, ids: list[str]) -> None: ...

    def delete_by_document(self, tenant_id: str, document_id: str) -> None: ...

    def delete_by_versions(self, tenant_id: str, version_ids: list[str]) -> None: ...

    def search(self, query: str, *, limit: int, flt: SearchFilter) -> list[SearchHit]: ...

    def count(self, tenant_id: str | None = None) -> int: ...

    def recreate(self) -> None: ...

    def close(self) -> None: ...
