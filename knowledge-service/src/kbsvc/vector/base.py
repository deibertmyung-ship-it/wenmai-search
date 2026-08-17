"""Vector store contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class VectorPoint:
    id: str
    dense: list[float]
    payload: dict


@dataclass
class SearchHit:
    id: str
    score: float
    payload: dict = field(default_factory=dict)


@dataclass
class SearchFilter:
    """Filters pushed down to the vector store - never applied post-hoc."""

    tenant_id: str
    acl_any: list[str] | None = None
    source_ids: list[str] | None = None
    document_ids: list[str] | None = None
    kinds: list[str] | None = None
    current_only: bool = True

    def describe(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "acl_any": self.acl_any,
            "source_ids": self.source_ids,
            "document_ids": self.document_ids,
            "kinds": self.kinds,
            "current_only": self.current_only,
        }


class VectorStore(Protocol):
    def ensure_collection(
        self, dim: int, *, session: Session | None = None
    ) -> None: ...

    def recreate_collection(
        self, dim: int, *, session: Session | None = None
    ) -> None: ...

    def upsert(
        self, points: list[VectorPoint], *, session: Session | None = None
    ) -> None: ...

    def delete_by_ids(
        self, ids: list[str], *, session: Session | None = None
    ) -> None: ...

    def delete_by_document(
        self,
        tenant_id: str,
        document_id: str,
        *,
        session: Session | None = None,
    ) -> None: ...

    def delete_by_versions(
        self,
        tenant_id: str,
        version_ids: list[str],
        *,
        session: Session | None = None,
    ) -> None: ...

    def search_dense(
        self, vector: list[float], *, limit: int, flt: SearchFilter
    ) -> list[SearchHit]: ...

    def count(self, tenant_id: str | None = None) -> int: ...
