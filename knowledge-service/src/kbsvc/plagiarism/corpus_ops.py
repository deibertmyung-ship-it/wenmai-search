"""Corpus lifecycle operations behind the `kbsvc plagiarism` commands.

These only ever *register* work and report on it. Nothing here computes a
projection - that is the plagiarism worker's job, in its own process. A backfill
over a large corpus is hours of fingerprinting; running it inside a CLI
invocation would make it un-resumable and invisible.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from ..config import get_settings
from ..db.models import Document
from ..db.session import session_scope
from . import repository as repo
from .models import PlagCorpusJob
from .schema import verify_plagiarism_schema
from .types import CorpusJobStatus

logger = logging.getLogger(__name__)

# A worker seen within this window counts as live for readiness purposes.
_HEARTBEAT_WINDOW_SECONDS = 120


def _current_documents(session, tenant_id: str) -> list[tuple[str, str]]:
    """`(document_id, current_version_id)` for every live, indexed document."""
    rows = session.execute(
        select(Document.id, Document.current_version_id).where(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
            Document.current_version_id.is_not(None),
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def register_backfill(tenant_id: str, *, rebuild: bool = False) -> tuple[int, int]:
    """Register a projection job per current document version.

    Returns `(registered, skipped)`. Without `rebuild`, documents that already
    have a live projection under the current algorithm hash are skipped, which
    makes the command safe to re-run after an interruption.
    """
    settings = get_settings()
    config_hash = settings.plagiarism_algorithm_config_hash

    with session_scope() as session:
        documents = _current_documents(session, tenant_id)
        if not documents:
            return 0, 0

        already_built: set[str] = set()
        if not rebuild:
            readiness_ids = [document_id for document_id, _ in documents]
            already_built = set(
                session.scalars(
                    select(repo.PlagCorpusProjection.document_id).where(
                        repo.PlagCorpusProjection.tenant_id == tenant_id,
                        repo.PlagCorpusProjection.algorithm_config_hash == config_hash,
                        repo.PlagCorpusProjection.document_id.in_(readiness_ids),
                        repo.PlagCorpusProjection.active_from.is_not(None),
                        repo.PlagCorpusProjection.active_until.is_(None),
                    )
                )
            )

        registered = 0
        for document_id, version_id in documents:
            if document_id in already_built:
                continue
            repo.enqueue_corpus_job(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                algorithm_config_hash=config_hash,
                max_attempts=settings.plag_max_attempts,
            )
            registered += 1

    return registered, len(already_built)


def rebuild_document_frequencies(tenant_id: str) -> int:
    """Recompute DF over the live corpus and refresh planner statistics.

    ANALYZE matters here: the corpus table changes size by orders of magnitude
    during a backfill, and a stale row estimate is exactly what makes the
    planner abandon the GIN index.
    """
    settings = get_settings()
    with session_scope() as session:
        count = repo.rebuild_fingerprint_df(
            session,
            tenant_id=tenant_id,
            algorithm_config_hash=settings.plagiarism_algorithm_config_hash,
        )
        session.execute(_analyze_statement())
    return count


def _analyze_statement():
    from sqlalchemy import text as sql_text

    return sql_text("ANALYZE plag_corpus_chunk")


def corpus_report(tenant_id: str) -> dict:
    """Everything `plagiarism status` and the readiness probe need."""
    settings = get_settings()
    config_hash = settings.plagiarism_algorithm_config_hash

    with session_scope() as session:
        schema = verify_plagiarism_schema(session)
        document_ids = [document_id for document_id, _ in _current_documents(session, tenant_id)]
        coverage = repo.projection_readiness(
            session,
            tenant_id=tenant_id,
            algorithm_config_hash=config_hash,
            document_ids=document_ids,
        )
        pending_jobs = int(
            session.scalar(
                select(func.count(PlagCorpusJob.id)).where(
                    PlagCorpusJob.tenant_id == tenant_id,
                    PlagCorpusJob.status.in_(
                        [str(CorpusJobStatus.PENDING), str(CorpusJobStatus.RUNNING)]
                    ),
                )
            )
            or 0
        )
        live_workers = repo.live_worker_count(
            session, within_seconds=_HEARTBEAT_WINDOW_SECONDS
        )

    return {
        "algorithm_config_hash": config_hash,
        "schema": schema,
        "coverage": coverage,
        "pending_jobs": pending_jobs,
        "live_workers": live_workers,
        "enabled": settings.plag_enabled,
        "indexing_enabled": settings.plag_indexing_enabled,
    }


def run_cleanup() -> dict[str, int]:
    settings = get_settings()
    with session_scope() as session:
        return repo.cleanup_expired(session, retention_days=settings.plag_retention_days)
