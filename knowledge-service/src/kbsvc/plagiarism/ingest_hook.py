"""The one place ingest touches plagiarism.

Kept to a single tiny adapter so `IngestWorker` never imports the plagiarism
internals, and so the whole feature can be switched off at one point.

The contract in both directions is: **registration must never fail an ingest.**
Plagiarism is an add-on; a document that indexed correctly is indexed correctly
whether or not it also entered the plagiarism corpus. Every call here is wrapped
in a savepoint and swallows its errors after logging.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings

logger = logging.getLogger(__name__)


def enqueue_projection_if_enabled(
    session: Session, *, tenant_id: str, document_id: str, version_id: str
) -> str | None:
    """Register a projection build for a freshly indexed version.

    Returns the job id, or None when the feature is off, the store is not
    PostgreSQL, or registration failed. A None return is never an error the
    caller should act on.
    """
    settings = get_settings()
    if not settings.plag_indexing_enabled:
        return None

    try:
        from .schema import is_postgres

        if not is_postgres(session.get_bind()):
            return None

        from . import repository as repo

        # Savepoint: a failure here rolls back only the registration, leaving
        # the surrounding ingest transaction intact and committable.
        with session.begin_nested():
            job = repo.enqueue_corpus_job(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                algorithm_config_hash=settings.plagiarism_algorithm_config_hash,
                max_attempts=settings.plag_max_attempts,
            )
            return job.id
    except (SQLAlchemyError, ImportError, RuntimeError) as exc:
        logger.warning(
            "plagiarism projection not registered for version %s: %s",
            version_id,
            exc,
            exc_info=True,
        )
        return None


def retire_document_if_enabled(session: Session, *, tenant_id: str, document_id: str) -> int:
    """Drop a deleted document from the plagiarism corpus.

    Same savepoint contract: a document delete succeeds regardless.
    """
    settings = get_settings()
    if not settings.plag_indexing_enabled:
        return 0

    try:
        from .schema import is_postgres

        if not is_postgres(session.get_bind()):
            return 0

        from . import repository as repo

        with session.begin_nested():
            return repo.delete_projections_for_document(
                session, tenant_id=tenant_id, document_id=document_id
            )
    except (SQLAlchemyError, ImportError, RuntimeError) as exc:
        logger.warning(
            "plagiarism projection not retired for document %s: %s",
            document_id,
            exc,
            exc_info=True,
        )
        return 0
