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
from ..embedding import get_dense_embedder
from ..lexical import LexicalDocument, get_lexical_store
from ..lexical.tokenizer import analyze
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
    lexical = get_lexical_store()

    if recreate and not resume_after:
        # Dimension and vector space are fixed at creation time, so a model
        # switch cannot reuse the existing collection. The lexical index has no
        # such constraint, but rebuilding both together is what keeps the two
        # halves of retrieval describing the same corpus.
        logger.info("recreating collection with dim=%d", dense.dim)
        store.recreate_collection(dense.dim)
        lexical.recreate()
    else:
        store.ensure_collection(dense.dim)
        lexical.ensure_ready()

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

    # The lexical index commits once at the end, not per batch: committing on
    # every batch makes tantivy merge segments while later batches are still
    # writing, which on Windows races with an on-access scanner and kills the
    # writer. If reembed is interrupted, the dense side resumes from
    # `last_chunk_id` and the lexical side is rebuilt with `rebuild_lexical` -
    # 20 seconds on a 22k-chunk corpus, so there is nothing to protect here.
    with lexical.bulk():
        while True:
            with session_scope() as session:
                rows = _next_batch(session, tenant_id, last_id, batch_size)
                if not rows:
                    break
                points = _build_points(tenant_id, rows, dense)
                last_id = rows[-1][0].id

            store.upsert(points)
            lexical.upsert(
                [
                    LexicalDocument(id=point.id, text=point.payload["text"], payload=point.payload)
                    for point in points
                ]
            )
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


def rebuild_lexical(
    tenant_id: str,
    *,
    batch_size: int = 512,
    progress: ProgressHook | None = None,
) -> int:
    """Rebuild only the lexical index from the persisted chunk rows.

    The migration path onto tantivy, and the repair path if the two indexes ever
    drift. Deliberately separate from `reembed_tenant`: the dense vectors are
    unaffected by a lexical rebuild, and re-running the embedding model over the
    whole corpus to fix an inverted index would be minutes of wasted GPU/CPU.
    """
    lexical = get_lexical_store()
    lexical.recreate()

    with session_scope() as session:
        total = int(
            session.scalar(select(func.count(Chunk.id)).where(Chunk.tenant_id == tenant_id)) or 0
        )
    if total == 0:
        return 0

    done = 0
    last_id = ""
    with lexical.bulk():
        while True:
            with session_scope() as session:
                rows = _next_batch(session, tenant_id, last_id, batch_size)
                if not rows:
                    break
                documents = [
                    LexicalDocument(
                        id=chunk.id,
                        text=chunk.text,
                        payload=_payload(tenant_id, chunk, document, version),
                    )
                    for chunk, document, version in rows
                ]
                last_id = rows[-1][0].id

            lexical.upsert(documents)
            done += len(documents)
            if progress:
                progress(done, total)

    return done


def backfill_analyzed(
    tenant_id: str,
    *,
    batch_size: int = 1000,
    force: bool = False,
    progress: ProgressHook | None = None,
) -> int:
    """Populate `chunk.analyzed` for rows written before it existed.

    Touches neither index - the analyzer output is derived purely from
    `chunk.text`, so this is a metadata pass, not a re-embed or a lexical
    rebuild. Pass `force` after changing the tokenizer, which invalidates every
    stored value.
    """
    with session_scope() as session:
        pending = select(func.count(Chunk.id)).where(Chunk.tenant_id == tenant_id)
        if not force:
            pending = pending.where(Chunk.analyzed == "")
        total = int(session.scalar(pending) or 0)
    if total == 0:
        return 0

    done = 0
    last_id = ""
    while True:
        with session_scope() as session:
            stmt = select(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.id > last_id)
            if not force:
                stmt = stmt.where(Chunk.analyzed == "")
            rows = list(session.scalars(stmt.order_by(Chunk.id).limit(batch_size)))
            if not rows:
                break
            for chunk in rows:
                chunk.analyzed = analyze(chunk.text)
            last_id = rows[-1].id
            done += len(rows)
        if progress:
            progress(min(done, total), total)

    return done


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
    tenant_id: str,
    rows: list[tuple[Chunk, Document, DocumentVersion]],
    dense,
) -> list[VectorPoint]:
    texts = [chunk.text for chunk, _, _ in rows]
    vectors = dense.embed_documents(texts)
    return [
        VectorPoint(
            id=chunk.id,
            dense=vector,
            payload=_payload(tenant_id, chunk, document, version),
        )
        for (chunk, document, version), vector in zip(rows, vectors, strict=True)
    ]


def _payload(
    tenant_id: str, chunk: Chunk, document: Document, version: DocumentVersion
) -> dict:
    """The denormalized view both indexes carry, so a hit renders without a join."""
    return {
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
    }
