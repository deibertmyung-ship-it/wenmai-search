"""Registration side of ingestion.

The contract from the design: an upload only persists bytes, derives ids and
hashes, and enqueues. It never parses, never embeds, never blocks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .. import ids
from ..config import get_settings
from ..db import repo
from ..errors import PayloadTooLargeError, ValidationError
from ..parsing.registry import guess_mime
from ..storage import get_object_store
from .states import JobType

logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    document_id: str
    version_id: str
    job_id: str | None
    state: str
    deduplicated: bool


def register_bytes(
    session: Session,
    *,
    tenant_id: str,
    source_id: str,
    external_id: str,
    data: bytes,
    filename: str = "",
    title: str = "",
    acl: list[str] | None = None,
    meta: dict | None = None,
    source_uri: str = "",
) -> RegistrationResult:
    """Persist bytes, create/advance a version, enqueue an ingest job."""
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            "file exceeds max upload size",
            {"size": len(data), "limit": settings.max_upload_bytes},
        )
    if not external_id:
        raise ValidationError("external_id is required")

    name = filename or external_id
    mime = guess_mime(name)
    if settings.allowed_mimes and mime not in settings.allowed_mimes:
        raise ValidationError("mime type not allowed", {"mime": mime})

    content_hash = ids.hash_bytes(data)
    document = repo.get_or_create_document(
        session,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id=external_id,
        title=title or Path(name).stem,
        acl=acl,
        meta=meta,
    )

    existing = repo.find_version_by_hash(session, document.id, content_hash)
    if existing is not None and existing.status == "indexed":
        return RegistrationResult(document.id, existing.id, None, existing.status, True)

    key = ids.object_key(tenant_id, document.id, content_hash, Path(name).suffix)
    store = get_object_store()
    if not store.exists(key):
        store.put(key, data, content_type=mime)

    version = existing or repo.create_version(
        session,
        document=document,
        content_hash=content_hash,
        mime=mime,
        size_bytes=len(data),
        object_key=key,
        source_uri=source_uri or store.uri(key),
    )

    job = repo.enqueue_job(
        session,
        tenant_id=tenant_id,
        document_id=document.id,
        version_id=version.id,
        job_type=str(JobType.INGEST),
        max_attempts=settings.worker_max_attempts,
    )
    return RegistrationResult(document.id, version.id, job.id, job.state, False)


def register_path(
    session: Session,
    *,
    tenant_id: str,
    source_id: str,
    path: Path,
    external_id: str,
    title: str = "",
    acl: list[str] | None = None,
) -> RegistrationResult:
    return register_bytes(
        session,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id=external_id,
        data=path.read_bytes(),
        filename=path.name,
        title=title,
        acl=acl,
        source_uri=path.resolve().as_uri(),
    )


def enqueue_delete(session: Session, *, tenant_id: str, document_id: str) -> str:
    settings = get_settings()
    job = repo.enqueue_job(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=None,
        job_type=str(JobType.DELETE),
        max_attempts=settings.worker_max_attempts,
    )
    return job.id


def enqueue_reindex(session: Session, *, tenant_id: str, document_id: str, version_id: str) -> str:
    settings = get_settings()
    job = repo.enqueue_job(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        job_type=str(JobType.REINDEX),
        max_attempts=settings.worker_max_attempts,
    )
    return job.id
