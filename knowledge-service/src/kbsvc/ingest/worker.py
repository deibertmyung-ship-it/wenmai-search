"""Ingest worker: the state machine executor.

One job at a time, leased. Every stage transition is persisted, so a crashed
worker leaves a job that another worker can reclaim once the lease expires.
Failures retry with exponential backoff up to max_attempts, then park in
`failed` with the error recorded.
"""

from __future__ import annotations

import logging
import socket
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ids
from ..config import Settings, get_settings
from ..db import repo
from ..db.models import Chunk as ChunkRow
from ..db.models import Document, DocumentVersion, IngestJob, utcnow
from ..db.session import session_scope
from ..embedding import get_dense_embedder, get_sparse_embedder
from ..embedding.sparse_bm25 import tokenize
from ..models.events import IndexEventType
from ..models.ir import Chunk as ChunkIR
from ..parsing.registry import get_registry
from ..storage import get_object_store
from ..vector import VectorPoint, get_vector_store
from .states import JobState, JobType, backoff_seconds

logger = logging.getLogger(__name__)


@dataclass
class JobOutcome:
    job_id: str
    state: str
    detail: dict


class IngestWorker:
    def __init__(self, settings: Settings | None = None, owner: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.owner = owner or f"{socket.gethostname()}:{id(self)}"

    # --- loop -----------------------------------------------------------

    def run_forever(self, *, max_jobs: int | None = None) -> int:
        processed = 0
        while max_jobs is None or processed < max_jobs:
            outcome = self.run_once()
            if outcome is None:
                if max_jobs is not None:
                    break
                time.sleep(self.settings.worker_poll_interval)
                continue
            processed += 1
        return processed

    def drain(self, *, limit: int = 10_000) -> int:
        """Process every runnable job and return; used by CLI and tests."""
        processed = 0
        while processed < limit and self.run_once() is not None:
            processed += 1
        return processed

    def run_once(self) -> JobOutcome | None:
        with session_scope() as session:
            job = repo.claim_job(
                session, owner=self.owner, lease_seconds=self.settings.worker_lease_seconds
            )
            if job is None:
                return None
            job_id = job.id

        try:
            with session_scope() as session:
                job = session.get(IngestJob, job_id)
                detail = self._execute(session, job)
                self._finish(session, job, JobState.COMPLETED)
            return JobOutcome(job_id, str(JobState.COMPLETED), detail)
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            with session_scope() as session:
                job = session.get(IngestJob, job_id)
                state = self._fail(session, job, exc)
            return JobOutcome(job_id, str(state), {"error": str(exc)})

    # --- execution ------------------------------------------------------

    def _execute(self, session: Session, job: IngestJob) -> dict:
        if job.job_type == JobType.DELETE:
            return self._run_delete(session, job)
        return self._run_ingest(session, job)

    def _run_ingest(self, session: Session, job: IngestJob) -> dict:
        version = session.get(DocumentVersion, job.version_id)
        document = session.get(Document, job.document_id)
        if version is None or document is None:
            raise ValueError(f"job {job.id} references a missing document/version")

        # -- parse
        self._advance(session, job, JobState.PARSING)
        data = get_object_store().get(version.object_key)
        parsed = get_registry().parse(data, filename=version.object_key, mime=version.mime)
        version.parser = parsed.meta.parser
        version.parser_version = parsed.meta.parser_version
        version.page_count = parsed.meta.page_count
        if parsed.meta.title and not document.title:
            document.title = parsed.meta.title

        # -- chunk
        self._advance(session, job, JobState.CHUNKING)
        from ..chunking.structural import StructuralChunker

        chunks = StructuralChunker(self.settings).chunk(parsed)
        if not chunks:
            raise ValueError(f"parser produced no chunks for {version.object_key}")

        self._retract_version_stats(session, document.tenant_id, [version.id])
        rows = self._persist_chunks(session, document, version, chunks)
        self._apply_stats_delta(session, document.tenant_id, [row.text for row in rows], sign=1)

        # -- embed
        self._advance(session, job, JobState.EMBEDDING)
        points = self._build_points(session, document, version, parsed.meta.lang, rows)

        # -- index
        self._advance(session, job, JobState.INDEXING)
        store = get_vector_store()
        store.ensure_collection(get_dense_embedder().dim)
        store.upsert(points)

        version.status = "indexed"
        version.chunk_count = len(rows)
        document.current_version_id = version.id
        repo.record_event(
            session,
            tenant_id=document.tenant_id,
            document_id=document.id,
            version_id=version.id,
            event_type=IndexEventType.CHUNKS_UPSERTED,
            payload={"chunk_count": len(rows), "parser": version.parser},
        )

        superseded = self._supersede_older(session, document, version)
        return {"chunks": len(rows), "superseded_versions": superseded}

    def _run_delete(self, session: Session, job: IngestJob) -> dict:
        document = session.get(Document, job.document_id)
        if document is None:
            return {"deleted": 0, "note": "document already gone"}

        version_ids = [version.id for version in repo.list_versions(session, document.id)]
        texts = [
            row.text
            for row in session.scalars(
                select(ChunkRow).where(ChunkRow.document_id == document.id)
            )
        ]
        self._apply_stats_delta(session, document.tenant_id, texts, sign=-1)
        removed = repo.delete_chunks_for_versions(session, version_ids)
        get_vector_store().delete_by_document(document.tenant_id, document.id)

        document.deleted_at = utcnow()
        document.current_version_id = None
        repo.mark_versions_superseded(session, version_ids)
        repo.record_event(
            session,
            tenant_id=document.tenant_id,
            document_id=document.id,
            event_type=IndexEventType.DOCUMENT_DELETED,
            payload={"chunks_removed": removed, "versions": len(version_ids)},
        )
        return {"deleted": removed}

    # --- helpers --------------------------------------------------------

    def _persist_chunks(
        self, session: Session, document: Document, version: DocumentVersion, chunks: list[ChunkIR]
    ) -> list[ChunkRow]:
        rows = [
            ChunkRow(
                id=ids.chunk_id(document.id, version.version, chunk.ordinal, chunk.content_hash),
                tenant_id=document.tenant_id,
                document_id=document.id,
                version_id=version.id,
                ordinal=chunk.ordinal,
                kind=chunk.kind,
                text=chunk.text,
                token_count=chunk.token_count,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                section_id=chunk.section_id,
                heading_path=chunk.heading_path,
                bbox=chunk.bbox or None,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        ]
        repo.replace_chunks(session, version.id, rows)
        return rows

    def _build_points(
        self,
        session: Session,
        document: Document,
        version: DocumentVersion,
        lang: str,
        rows: list[ChunkRow],
    ) -> list[VectorPoint]:
        dense = get_dense_embedder()
        sparse = get_sparse_embedder(document.tenant_id, session)
        texts = [row.text for row in rows]
        vectors = dense.embed_documents(texts)
        return [
            VectorPoint(
                id=row.id,
                dense=vector,
                sparse=sparse.encode_document(row.text),
                payload={
                    "tenant_id": document.tenant_id,
                    "document_id": document.id,
                    "version_id": version.id,
                    "version": version.version,
                    "chunk_ordinal": row.ordinal,
                    "kind": row.kind,
                    "source_id": document.source_id,
                    "source_uri": version.source_uri,
                    "title": document.title,
                    "heading_path": row.heading_path,
                    "heading": row.heading_path[-1] if row.heading_path else "",
                    "page_from": row.page_from,
                    "page_to": row.page_to,
                    "acl": document.acl or ["public"],
                    "is_current": True,
                    "parser": version.parser,
                    "parser_version": version.parser_version,
                    "lang": lang,
                    "content_hash": row.content_hash,
                    "text": row.text,
                },
            )
            for row, vector in zip(rows, vectors, strict=True)
        ]

    def _supersede_older(
        self, session: Session, document: Document, current: DocumentVersion
    ) -> list[str]:
        stale = repo.superseded_version_ids(session, document.id, current.id)
        if not stale:
            return []
        texts = [
            row.text
            for row in session.scalars(select(ChunkRow).where(ChunkRow.version_id.in_(stale)))
        ]
        self._apply_stats_delta(session, document.tenant_id, texts, sign=-1)
        repo.delete_chunks_for_versions(session, stale)
        get_vector_store().delete_by_versions(document.tenant_id, stale)
        repo.mark_versions_superseded(session, stale)
        repo.record_event(
            session,
            tenant_id=document.tenant_id,
            document_id=document.id,
            version_id=current.id,
            event_type=IndexEventType.VERSION_SUPERSEDED,
            payload={"superseded_version_ids": stale},
        )
        return stale

    def _retract_version_stats(
        self, session: Session, tenant_id: str, version_ids: list[str]
    ) -> None:
        """Undo statistics from a previous indexing attempt of the same version."""
        texts = [
            row.text
            for row in session.scalars(select(ChunkRow).where(ChunkRow.version_id.in_(version_ids)))
        ]
        if texts:
            self._apply_stats_delta(session, tenant_id, texts, sign=-1)

    def _apply_stats_delta(
        self, session: Session, tenant_id: str, texts: list[str], *, sign: int
    ) -> None:
        """Keep BM25 doc-freq and corpus length in step with the chunk table."""
        if not texts:
            return
        delta: Counter[str] = Counter()
        total_length = 0
        for text in texts:
            tokens = tokenize(text)
            total_length += len(tokens)
            for term in set(tokens):
                delta[term] += sign
        repo.bump_term_stats(session, tenant_id, dict(delta))
        repo.bump_corpus_stat(
            session,
            tenant_id,
            chunk_delta=sign * len(texts),
            length_delta=float(sign * total_length),
        )

    # --- state transitions ----------------------------------------------

    def _advance(self, session: Session, job: IngestJob, state: JobState) -> None:
        job.state = str(state)
        job.updated_at = utcnow()
        job.lease_expires_at = utcnow() + timedelta(seconds=self.settings.worker_lease_seconds)
        session.flush()

    def _finish(self, session: Session, job: IngestJob, state: JobState) -> None:
        job.state = str(state)
        job.finished_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = None
        session.flush()

    def _fail(self, session: Session, job: IngestJob, exc: Exception) -> JobState:
        job.last_error = f"{type(exc).__name__}: {exc}"[:4000]
        job.lease_owner = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.state = str(JobState.FAILED)
            job.finished_at = utcnow()
            if job.version_id:
                version = session.get(DocumentVersion, job.version_id)
                if version is not None:
                    version.status = "failed"
            repo.record_event(
                session,
                tenant_id=job.tenant_id,
                document_id=job.document_id,
                version_id=job.version_id,
                event_type=IndexEventType.INDEX_FAILED,
                payload={"attempts": job.attempts, "error": job.last_error},
            )
            return JobState.FAILED

        delay = backoff_seconds(
            job.attempts,
            base=self.settings.worker_backoff_base,
            cap=self.settings.worker_backoff_cap,
        )
        job.state = str(JobState.RETRY_WAIT)
        job.scheduled_at = utcnow() + timedelta(seconds=delay)
        session.flush()
        return JobState.RETRY_WAIT
