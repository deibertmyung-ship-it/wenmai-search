"""Document and chunk reads, plus deletion (which is enqueued, never inline)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...access import document_is_visible
from ...db import repo
from ...db.models import Chunk, Document, DocumentVersion
from ...errors import NotFoundError
from ...ingest.uploader import enqueue_delete, enqueue_reindex
from ...reading import PassageLocator
from ..auth import Principal
from ..deps import get_principal, get_session
from ..schemas import (
    ChunkOut,
    DocumentOut,
    HighlightRangeOut,
    PassageChunkOut,
    PassageWindowOut,
)

router = APIRouter(prefix="/documents", tags=["documents"])


def _chunk_payload(chunk: Chunk) -> dict:
    return {
        "chunk_id": chunk.id,
        "document_id": chunk.document_id,
        "version_id": chunk.version_id,
        "ordinal": chunk.ordinal,
        "kind": chunk.kind,
        "text": chunk.text,
        "token_count": chunk.token_count,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "page_from": chunk.page_from,
        "page_to": chunk.page_to,
        "heading_path": list(chunk.heading_path or []),
        "content_hash": chunk.content_hash,
    }


def _load_document(
    session: Session, tenant_id: str, document_id: str, allowed_acl: list[str] | None = None
) -> Document:
    document = session.get(Document, document_id)
    if (
        document is None
        or document.tenant_id != tenant_id
        or document.deleted_at is not None
        or not document_is_visible(document.acl, allowed_acl)
    ):
        raise NotFoundError("document not found", {"document_id": document_id})
    return document


def _to_out(session: Session, document: Document) -> DocumentOut:
    versions = repo.list_versions(session, document.id)
    chunk_count = session.scalar(
        select(func.count(Chunk.id)).where(Chunk.document_id == document.id)
    )
    return DocumentOut(
        id=document.id,
        source_id=document.source_id,
        external_id=document.external_id,
        title=document.title,
        acl=list(document.acl or []),
        current_version_id=document.current_version_id,
        version_count=len(versions),
        chunk_count=int(chunk_count or 0),
        created_at=document.created_at.isoformat(),
        updated_at=document.updated_at.isoformat(),
    )


@router.get("")
def list_documents(
    source_id: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[DocumentOut]:
    # Counts come from two grouped subqueries rather than from `_to_out` per row.
    # Serialising one document at a time costs two queries each, which on a
    # 200-document listing is 400 round trips and ~1.8s - and this endpoint is on
    # the load path of both the library page and the search page's book filter.
    versions = (
        select(DocumentVersion.document_id, func.count().label("n"))
        .group_by(DocumentVersion.document_id)
        .subquery()
    )
    chunks = (
        select(Chunk.document_id, func.count().label("n"))
        .group_by(Chunk.document_id)
        .subquery()
    )
    stmt = (
        select(Document, versions.c.n, chunks.c.n)
        .outerjoin(versions, versions.c.document_id == Document.id)
        .outerjoin(chunks, chunks.c.document_id == Document.id)
        .where(Document.tenant_id == principal.tenant_id, Document.deleted_at.is_(None))
    )
    if source_id:
        stmt = stmt.where(Document.source_id == source_id)
    if q:
        stmt = stmt.where(Document.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(Document.title).limit(limit).offset(offset)

    return [
        DocumentOut(
            id=document.id,
            source_id=document.source_id,
            external_id=document.external_id,
            title=document.title,
            acl=list(document.acl or []),
            current_version_id=document.current_version_id,
            version_count=int(version_count or 0),
            chunk_count=int(chunk_count or 0),
            created_at=document.created_at.isoformat(),
            updated_at=document.updated_at.isoformat(),
        )
        for document, version_count, chunk_count in session.execute(stmt)
    ]


@router.get("/{document_id}")
def get_document(
    document_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> DocumentOut:
    return _to_out(
        session, _load_document(session, principal.tenant_id, document_id, principal.acl)
    )


@router.get("/{document_id}/chunks")
def get_chunks(
    document_id: str,
    from_ordinal: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    version: int | None = None,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[ChunkOut]:
    document = _load_document(session, principal.tenant_id, document_id, principal.acl)
    version_id = document.current_version_id
    if version is not None:
        row = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id, DocumentVersion.version == version
            )
        ).first()
        if row is None:
            raise NotFoundError("version not found", {"version": version})
        version_id = row.id

    chunks = repo.fetch_chunks(
        session,
        document_id=document.id,
        version_id=version_id,
        from_ordinal=from_ordinal,
        limit=limit,
    )
    return [
        ChunkOut(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            ordinal=chunk.ordinal,
            kind=chunk.kind,
            text=chunk.text,
            token_count=chunk.token_count,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
            heading_path=list(chunk.heading_path or []),
            content_hash=chunk.content_hash,
        )
        for chunk in chunks
    ]


@router.get("/{document_id}/passage-window")
def get_passage_window(
    document_id: str,
    version: int = Query(..., ge=1),
    start: int = Query(..., ge=0),
    end: int = Query(..., gt=0),
    context: int = Query(default=2, ge=0, le=10),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> PassageWindowOut:
    window = PassageLocator().locate(
        session,
        tenant_id=principal.tenant_id,
        document_id=document_id,
        version=version,
        start=start,
        end=end,
        context=context,
        allowed_acl=principal.acl,
    )
    return PassageWindowOut(
        document_id=window.document_id,
        version_id=window.version_id,
        version=window.version,
        requested_start=window.requested_start,
        requested_end=window.requested_end,
        focus_ordinal=window.focus_ordinal,
        from_ordinal=window.from_ordinal,
        next_from=window.next_from,
        has_more=window.has_more,
        exact=True,
        chunks=[
            PassageChunkOut(
                **_chunk_payload(item.chunk),
                highlights=[
                    HighlightRangeOut(
                        local_start=highlight.local_start,
                        local_end=highlight.local_end,
                        document_start=highlight.document_start,
                        document_end=highlight.document_end,
                    )
                    for highlight in item.highlights
                ],
            )
            for item in window.chunks
        ],
    )


@router.post("/{document_id}/reindex", status_code=202)
def reindex_document(
    document_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Re-parse and re-embed the current version from the stored original bytes.

    This is the path to take after changing the embedding model or chunk sizes -
    no re-upload needed, because the raw file is still in object storage.
    """
    document = _load_document(session, principal.tenant_id, document_id, principal.acl)
    if not document.current_version_id:
        raise NotFoundError("document has no indexed version", {"document_id": document_id})
    job_id = enqueue_reindex(
        session,
        tenant_id=principal.tenant_id,
        document_id=document.id,
        version_id=document.current_version_id,
    )
    return {"job_id": job_id, "document_id": document.id, "state": "pending"}


@router.delete("/{document_id}", status_code=202)
def delete_document(
    document_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> dict:
    document = _load_document(session, principal.tenant_id, document_id, principal.acl)
    job_id = enqueue_delete(session, tenant_id=principal.tenant_id, document_id=document.id)
    return {"job_id": job_id, "document_id": document.id, "state": "pending"}
