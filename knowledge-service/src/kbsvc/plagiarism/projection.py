"""Builds the plagiarism corpus projection for one document version.

A projection is the searchable form of a document: sliding-sentence chunks, each
carrying its winnowing fingerprints and its offsets into the *document* text.

The build is deliberately not part of ingest. Ingest registers a job and returns;
this runs later in a separate process, so fingerprinting never delays a document
becoming searchable and never fails an ingest.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .. import ids
from ..config import Settings, get_settings
from ..db import repo as kb_repo
from ..db.models import Document, DocumentVersion
from . import repository as repo
from .chunking.sliding import chunk_document
from .fingerprinting import fingerprint
from .language import pysbd_language
from .matching_policy import resolve_match_policy
from .models import PlagCorpusChunk, PlagCorpusProjection
from .types import CorpusJobStatus

logger = logging.getLogger(__name__)


def _has_han(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in text)


def _chunk_fingerprints(text: str, settings: Settings) -> list[int]:
    """Store both profiles for mixed-script chunks so either query can recall."""
    profiles = ("generic", "zh") if _has_han(text) else ("generic",)
    fingerprints: set[int] = set()
    for profile in profiles:
        fingerprints.update(
            fingerprint(
                text,
                k=settings.plag_kgram,
                w=settings.plag_winnow_window,
                profile=profile,
            )
        )
    return sorted(fingerprints)


class ProjectionBuilder:
    """Turns one document version into an activated corpus projection."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def algorithm_config_hash(self) -> str:
        return self.settings.plagiarism_algorithm_config_hash

    def build(self, session: Session, job_id: str) -> dict:
        """Build and activate the projection for `job_id`'s version.

        The previous projection stays live for the whole build and is retired
        only in the same transaction that publishes the new one - so the corpus
        never has a window where the document is absent.
        """
        job = session.get(repo.PlagCorpusJob, job_id)
        if job is None:
            raise ValueError(f"corpus job {job_id} not found")

        version = session.get(DocumentVersion, job.version_id)
        document = session.get(Document, job.document_id)
        if version is None or document is None:
            raise ValueError(f"corpus job {job_id} references a missing document/version")

        text = self._load_text(session, version)
        if not text.strip():
            # An empty projection is a legitimate outcome (an image-only PDF,
            # say). Publishing it keeps readiness accurate; refusing would leave
            # the document permanently "pending".
            logger.info("document %s has no extractable text", document.id)

        policy = resolve_match_policy(text, "auto", self.settings)
        language = policy.resolved_language
        chunks = (
            chunk_document(
                text,
                sentences_per_chunk=self.settings.plag_sentences_per_chunk,
                overlap=self.settings.plag_chunk_overlap,
                language=pysbd_language(language),
            )
            if text.strip()
            else []
        )

        projection = PlagCorpusProjection(
            id=ids.new_id(),
            tenant_id=job.tenant_id,
            document_id=document.id,
            version_id=version.id,
            content_hash=version.content_hash,
            language=language,
            acl=document.acl or ["public"],
            algorithm_config_hash=job.algorithm_config_hash,
            total_chars=len(text),
            chunk_count=len(chunks),
        )
        session.add(projection)
        session.flush()

        rows = [
            PlagCorpusChunk(
                id=ids.new_id(),
                projection_id=projection.id,
                tenant_id=job.tenant_id,
                chunk_index=chunk.chunk_index,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                sentence_count=chunk.sentence_count,
                text=chunk.text,
                fingerprints=_chunk_fingerprints(chunk.text, self.settings),
            )
            for chunk in chunks
        ]
        repo.replace_projection_chunks(session, projection_id=projection.id, chunks=rows)

        # Publish and retire the predecessor atomically.
        repo.activate_projection(session, projection=projection)
        return {
            "projection_id": projection.id,
            "chunks": len(rows),
            "language": language,
            "chars": len(text),
        }

    def _load_text(self, session: Session, version: DocumentVersion) -> str:
        """Reassemble the document text from its stored chunks.

        Reuses the knowledge base's own parse output rather than re-parsing the
        original file: re-parsing could disagree with what was indexed, and a
        finding that cites offsets nobody else can reproduce is worse than no
        finding.
        """
        chunks = sorted(
            kb_repo.chunks_for_version(session, version.id), key=lambda c: c.char_start
        )
        if not chunks:
            return ""

        # Walk in offset order, keeping a cursor at the end of what has been
        # emitted. The knowledge-base chunker overlaps adjacent chunks, so a
        # chunk usually starts before the cursor - only its unseen tail is
        # appended. A gap (content the chunker dropped, e.g. a stripped table)
        # is padded with spaces so every later offset still lines up with the
        # parsed text; misaligned offsets would make every reported span wrong.
        parts: list[str] = []
        cursor = 0
        for chunk in chunks:
            if chunk.char_start > cursor:
                parts.append(" " * (chunk.char_start - cursor))
                cursor = chunk.char_start
            if chunk.char_end <= cursor:
                continue  # wholly covered by an earlier chunk
            parts.append(chunk.text[cursor - chunk.char_start :])
            cursor = chunk.char_end
        return "".join(parts)


def run_corpus_job(session: Session, job_id: str, *, settings: Settings | None = None) -> dict:
    """Execute one corpus job, settling its status either way."""
    active = settings or get_settings()
    job = session.get(repo.PlagCorpusJob, job_id)
    if job is None:
        raise ValueError(f"corpus job {job_id} not found")
    try:
        result = ProjectionBuilder(active).build(session, job_id)
    except Exception as exc:  # noqa: BLE001 - settle the job, then re-raise
        repo.finish_corpus_job(
            session,
            job,
            status=CorpusJobStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            backoff_seconds=active.plag_backoff_base,
        )
        raise
    repo.finish_corpus_job(session, job, status=CorpusJobStatus.COMPLETED)
    return result
