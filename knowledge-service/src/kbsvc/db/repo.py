"""Repository helpers. All multi-row reads/writes against the metadata store live here."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .. import ids
from ..models.events import IndexEventType
from .models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexEvent,
    IngestJob,
    Source,
    utcnow,
)

# --- sources ------------------------------------------------------------


def upsert_source(
    session: Session,
    *,
    tenant_id: str,
    name: str,
    kind: str = "upload",
    uri: str = "",
    config: dict | None = None,
) -> Source:
    sid = ids.source_id(tenant_id, name)
    source = session.get(Source, sid)
    if source is None:
        source = Source(
            id=sid, tenant_id=tenant_id, name=name, kind=kind, uri=uri, config=config or {}
        )
        session.add(source)
    else:
        source.kind = kind or source.kind
        source.uri = uri or source.uri
        if config:
            source.config = {**source.config, **config}
    session.flush()
    return source


def list_sources(session: Session, tenant_id: str) -> list[Source]:
    stmt = select(Source).where(Source.tenant_id == tenant_id).order_by(Source.name)
    return list(session.scalars(stmt))


# --- documents & versions ----------------------------------------------


def get_or_create_document(
    session: Session,
    *,
    tenant_id: str,
    source_id: str,
    external_id: str,
    title: str = "",
    acl: list[str] | None = None,
    meta: dict | None = None,
) -> Document:
    did = ids.document_id(tenant_id, source_id, external_id)
    document = session.get(Document, did)
    if document is None:
        document = Document(
            id=did,
            tenant_id=tenant_id,
            source_id=source_id,
            external_id=external_id,
            title=title or external_id,
            acl=acl or ["public"],
            meta=meta or {},
        )
        session.add(document)
        session.flush()
        return document

    if title:
        document.title = title
    if acl:
        document.acl = acl
    if meta:
        document.meta = {**document.meta, **meta}
    document.deleted_at = None
    session.flush()
    return document


def find_version_by_hash(
    session: Session, document_id: str, content_hash: str
) -> DocumentVersion | None:
    stmt = select(DocumentVersion).where(
        DocumentVersion.document_id == document_id,
        DocumentVersion.content_hash == content_hash,
    )
    return session.scalars(stmt).first()


def next_version_number(session: Session, document_id: str) -> int:
    stmt = select(func.max(DocumentVersion.version)).where(
        DocumentVersion.document_id == document_id
    )
    return (session.scalar(stmt) or 0) + 1


def create_version(
    session: Session,
    *,
    document: Document,
    content_hash: str,
    mime: str,
    size_bytes: int,
    object_key: str,
    source_uri: str,
) -> DocumentVersion:
    version = DocumentVersion(
        id=ids.version_id(document.id, content_hash),
        document_id=document.id,
        tenant_id=document.tenant_id,
        version=next_version_number(session, document.id),
        content_hash=content_hash,
        mime=mime,
        size_bytes=size_bytes,
        object_key=object_key,
        source_uri=source_uri,
        status="pending",
    )
    session.add(version)
    session.flush()
    return version


def document_ids_by_title(
    session: Session,
    *,
    tenant_id: str,
    needle: str,
    source_ids: list[str] | None = None,
) -> list[str]:
    """Documents whose title contains `needle`, case-insensitively.

    Retrieval has no title filter, and deliberately so: filtering hits by title
    after fusion would discard whatever the retrievers happened not to return,
    and one book among hundreds is usually not in the overfetched candidates at
    all - the search would look empty rather than narrow. `document_id` has a
    payload index in Qdrant and a raw term in tantivy, so resolving the title to
    ids here pushes the filter down into both stores instead.
    """
    stmt = select(Document.id).where(
        Document.tenant_id == tenant_id,
        Document.deleted_at.is_(None),
        Document.title.ilike(f"%{needle}%"),
    )
    if source_ids:
        stmt = stmt.where(Document.source_id.in_(source_ids))
    return list(session.scalars(stmt))


def list_versions(session: Session, document_id: str) -> list[DocumentVersion]:
    stmt = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version)
    )
    return list(session.scalars(stmt))


def superseded_version_ids(session: Session, document_id: str, keep_version_id: str) -> list[str]:
    stmt = select(DocumentVersion.id).where(
        DocumentVersion.document_id == document_id,
        DocumentVersion.id != keep_version_id,
        DocumentVersion.status != "superseded",
    )
    return list(session.scalars(stmt))


def mark_versions_superseded(session: Session, version_ids: list[str]) -> None:
    if not version_ids:
        return
    session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id.in_(version_ids))
        .values(status="superseded")
    )


# --- chunks -------------------------------------------------------------


def replace_chunks(session: Session, version_id: str, chunks: list[Chunk]) -> None:
    session.execute(delete(Chunk).where(Chunk.version_id == version_id))
    session.add_all(chunks)
    session.flush()


def analyzed_by_ids(session: Session, *, tenant_id: str, chunk_ids: list[str]) -> dict[str, str]:
    """Precomputed tokenizer output for reranking, keyed by chunk id.

    Rows that predate the column - or that a backfill has not reached - are
    left out rather than returned empty, so the caller can tell "no stored
    tokens" from "stored, and genuinely empty".
    """
    if not chunk_ids:
        return {}
    stmt = select(Chunk.id, Chunk.analyzed).where(
        Chunk.tenant_id == tenant_id, Chunk.id.in_(chunk_ids)
    )
    return {chunk_id: analyzed for chunk_id, analyzed in session.execute(stmt) if analyzed}


def delete_chunks_for_versions(session: Session, version_ids: list[str]) -> int:
    if not version_ids:
        return 0
    result = session.execute(delete(Chunk).where(Chunk.version_id.in_(version_ids)))
    return result.rowcount or 0


def fetch_chunks(
    session: Session,
    *,
    document_id: str,
    version_id: str | None = None,
    from_ordinal: int = 0,
    limit: int = 20,
) -> list[Chunk]:
    stmt = select(Chunk).where(Chunk.document_id == document_id, Chunk.ordinal >= from_ordinal)
    if version_id:
        stmt = stmt.where(Chunk.version_id == version_id)
    stmt = stmt.order_by(Chunk.ordinal).limit(limit)
    return list(session.scalars(stmt))


# --- jobs ---------------------------------------------------------------


def enqueue_job(
    session: Session,
    *,
    tenant_id: str,
    document_id: str,
    version_id: str | None,
    job_type: str,
    max_attempts: int,
) -> IngestJob:
    job = IngestJob(
        id=ids.new_id(),
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        job_type=job_type,
        state="pending",
        max_attempts=max_attempts,
        scheduled_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def claim_job(session: Session, *, owner: str, lease_seconds: int) -> IngestJob | None:
    """Atomically lease one runnable job.

    Runnable = pending/retry_wait and due, or an expired lease left by a dead worker.
    """
    now = utcnow()
    stmt = (
        select(IngestJob)
        .where(
            IngestJob.scheduled_at <= now,
            (
                IngestJob.state.in_(["pending", "retry_wait"])
                | (
                    IngestJob.state.in_(["parsing", "chunking", "embedding", "indexing"])
                    & (IngestJob.lease_expires_at < now)
                )
            ),
        )
        .order_by(IngestJob.scheduled_at)
        .limit(1)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None

    # Compare-and-swap on the full pre-read tuple. updated_at alone is not enough:
    # on a coarse clock the winner can rewrite it to the same value, letting a second
    # worker's WHERE still match. state and lease_owner always change on a claim.
    result = session.execute(
        update(IngestJob)
        .where(
            IngestJob.id == job.id,
            IngestJob.updated_at == job.updated_at,
            IngestJob.state == job.state,
            IngestJob.lease_owner.is_(None)
            if job.lease_owner is None
            else IngestJob.lease_owner == job.lease_owner,
        )
        .values(
            state="parsing",
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempts=job.attempts + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        return None  # lost the race, caller retries
    session.commit()
    session.refresh(job)
    return job


def list_jobs(
    session: Session, *, tenant_id: str, state: str | None = None, limit: int = 50
) -> list[IngestJob]:
    stmt = select(IngestJob).where(IngestJob.tenant_id == tenant_id)
    if state:
        stmt = stmt.where(IngestJob.state == state)
    stmt = stmt.order_by(IngestJob.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))


# --- events -------------------------------------------------------------


def record_event(
    session: Session,
    *,
    tenant_id: str,
    document_id: str,
    event_type: IndexEventType,
    version_id: str | None = None,
    chunk_id: str | None = None,
    payload: dict | None = None,
) -> IndexEvent:
    event = IndexEvent(
        id=ids.new_id(),
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        chunk_id=chunk_id,
        event_type=str(event_type),
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
