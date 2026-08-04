"""Re-embed an existing index in place.

When only the embedding model changes, parsing and chunking results are still
valid - chunk ids, offsets and heading paths do not depend on the model. So
this streams the chunk table straight back into the vector store instead of
re-running the whole ingest pipeline over the original files.

For the 202-book corpus that is the difference between minutes and a full
re-parse. Use a `reindex` job instead when chunking parameters changed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Chunk, Document, DocumentVersion
from ..db.session import session_scope
from ..embedding import get_dense_embedder, get_sparse_embedder
from ..models.events import IndexEventType
from ..vector import VectorPoint, get_vector_store

logger = logging.getLogger(__name__)

ProgressHook = Callable[[int, int], None]


@dataclass
class ReembedResult:
    chunks: int
    documents: int
    dim: int
    model: str
    last_chunk_id: str = ""  # pass to resume_after if the run is interrupted


def reembed_tenant(
    tenant_id: str,
    *,
    batch_size: int = 128,
    recreate: bool = True,
    resume_after: str | None = None,
    progress: ProgressHook | None = None,
) -> ReembedResult:
    """Recompute every vector for a tenant from the persisted chunk rows.

    `resume_after` continues an interrupted run from a chunk id (exclusive).
    Chunks are streamed in ascending id order, so everything at or below that id
    is already written. Resuming implies keeping the collection.

    `ReembedResult.chunks` counts only what this call wrote; the progress hook
    reports absolute position against the whole corpus.
    """
    dense = get_dense_embedder()
    store = get_vector_store()

    if recreate and not resume_after:
        # Dimension and vector space are fixed at creation time, so a model
        # switch cannot reuse the existing collection.
        logger.info("recreating collection with dim=%d", dense.dim)
        store.recreate_collection(dense.dim)
    else:
        store.ensure_collection(dense.dim)

    with session_scope() as session:
        total = int(
            session.scalar(select(func.count(Chunk.id)).where(Chunk.tenant_id == tenant_id)) or 0
        )
        # On a resumed run, progress must count the work already done, otherwise
        # it reports 2% when the index is 92% built - and someone kills it.
        already_done = 0
        if resume_after:
            already_done = int(
                session.scalar(
                    select(func.count(Chunk.id)).where(
                        Chunk.tenant_id == tenant_id, Chunk.id <= resume_after
                    )
                )
                or 0
            )
    if total == 0:
        return ReembedResult(0, 0, dense.dim, getattr(dense, "model_name", dense.name), "")

    done = 0
    documents: set[str] = set()
    last_id = resume_after or ""

    while True:
        with session_scope() as session:
            rows = _next_batch(session, tenant_id, last_id, batch_size)
            if not rows:
                break
            points = _build_points(session, tenant_id, rows, dense)
            last_id = rows[-1][0].id

        store.upsert(points)
        done += len(points)
        documents.update(point.payload["document_id"] for point in points)
        if progress:
            progress(already_done + done, total)

    with session_scope() as session:
        from ..db import repo

        repo.record_event(
            session,
            tenant_id=tenant_id,
            document_id="*",
            event_type=IndexEventType.CHUNKS_UPSERTED,
            payload={
                "reason": "reembed",
                "chunks": done,
                "documents": len(documents),
                "dim": dense.dim,
                "model": getattr(dense, "model_name", dense.name),
            },
        )

    return ReembedResult(
        done, len(documents), dense.dim, getattr(dense, "model_name", dense.name), last_id
    )


def _next_batch(
    session: Session, tenant_id: str, last_id: str, batch_size: int
) -> list[tuple[Chunk, Document, DocumentVersion]]:
    """Keyset pagination by chunk id - stable under concurrent writes."""
    stmt = (
        select(Chunk, Document, DocumentVersion)
        .join(Document, Document.id == Chunk.document_id)
        .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
        .where(Chunk.tenant_id == tenant_id, Chunk.id > last_id)
        .order_by(Chunk.id)
        .limit(batch_size)
    )
    return list(session.execute(stmt).all())


def _build_points(
    session: Session,
    tenant_id: str,
    rows: list[tuple[Chunk, Document, DocumentVersion]],
    dense,
) -> list[VectorPoint]:
    sparse = get_sparse_embedder(tenant_id, session)
    texts = [chunk.text for chunk, _, _ in rows]
    vectors = dense.embed_documents(texts)
    return [
        VectorPoint(
            id=chunk.id,
            dense=vector,
            sparse=sparse.encode_document(chunk.text),
            payload={
                "tenant_id": tenant_id,
                "document_id": document.id,
                "version_id": version.id,
                "version": version.version,
                "chunk_ordinal": chunk.ordinal,
                "kind": chunk.kind,
                "source_id": document.source_id,
                "source_uri": version.source_uri,
                "title": document.title,
                "heading_path": chunk.heading_path or [],
                "heading": (chunk.heading_path or [""])[-1] if chunk.heading_path else "",
                "page_from": chunk.page_from,
                "page_to": chunk.page_to,
                "acl": document.acl or ["public"],
                "is_current": document.current_version_id == version.id,
                "parser": version.parser,
                "parser_version": version.parser_version,
                "lang": document.meta.get("lang", "zh") if document.meta else "zh",
                "content_hash": chunk.content_hash,
                "text": chunk.text,
            },
        )
        for (chunk, document, version), vector in zip(rows, vectors, strict=True)
    ]
