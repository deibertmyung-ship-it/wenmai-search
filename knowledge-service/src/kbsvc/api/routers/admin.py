"""Jobs and stats."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import repo
from ...db.models import Chunk, Document, DocumentVersion, IngestJob, Source, utcnow
from ...errors import NotFoundError, ValidationError
from ...ingest.states import JobState
from ...lexical import get_lexical_store
from ...vector import get_vector_store
from ..auth import Principal
from ..deps import get_principal, get_session
from ..schemas import JobOut, StatsOut

router = APIRouter(tags=["admin"])

# Simple TTL cache for vector/lexical counts: these can be expensive
# and are called on every /stats and /readyz hit.
_count_cache: dict[str, tuple[float, int]] = {}
_COUNT_TTL = 30.0


def _job_out(job: IngestJob) -> JobOut:
    return JobOut(
        id=job.id,
        document_id=job.document_id,
        version_id=job.version_id,
        job_type=job.job_type,
        state=job.state,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
        scheduled_at=job.scheduled_at.isoformat(),
        created_at=job.created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )


@router.get("/jobs")
def list_jobs(
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> list[JobOut]:
    jobs = repo.list_jobs(
        session,
        tenant_id=principal.tenant_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return [_job_out(job) for job in jobs]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> JobOut:
    job = session.get(IngestJob, job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        raise NotFoundError("job not found", {"job_id": job_id})
    return _job_out(job)


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> JobOut:
    job = session.get(IngestJob, job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        raise NotFoundError("job not found", {"job_id": job_id})
    if job.state not in (JobState.FAILED, JobState.CANCELLED):
        raise ValidationError("only failed or cancelled jobs can be retried", {"state": job.state})
    job.state = str(JobState.PENDING)
    job.attempts = 0
    job.last_error = None
    job.finished_at = None
    job.scheduled_at = utcnow()
    session.flush()
    return _job_out(job)


@router.get("/stats")
def stats(
    principal: Principal = Depends(get_principal), session: Session = Depends(get_session)
) -> StatsOut:
    tenant = principal.tenant_id

    def count(model, *conditions) -> int:
        stmt = select(func.count()).select_from(model).where(*conditions)
        return int(session.scalar(stmt) or 0)

    job_rows = session.execute(
        select(IngestJob.state, func.count(IngestJob.id))
        .where(IngestJob.tenant_id == tenant)
        .group_by(IngestJob.state)
    ).all()

    try:
        vector_points = _cached_count("vector", tenant)
    except Exception:
        vector_points = -1

    try:
        lexical_docs = _cached_count("lexical", tenant)
    except Exception:
        lexical_docs = -1

    return StatsOut(
        tenant_id=tenant,
        sources=count(Source, Source.tenant_id == tenant),
        documents=count(Document, Document.tenant_id == tenant, Document.deleted_at.is_(None)),
        versions=count(DocumentVersion, DocumentVersion.tenant_id == tenant),
        chunks=count(Chunk, Chunk.tenant_id == tenant),
        vector_points=vector_points,
        lexical_docs=lexical_docs,
        jobs_by_state=dict(job_rows),
    )


def _cached_count(kind: str, tenant: str) -> int:
    """Vector/lexical count with a short TTL to avoid full scans on every /stats."""
    cache_key = f"{kind}:{tenant}"
    now = time.monotonic()
    hit = _count_cache.get(cache_key)
    if hit is not None and now - hit[0] < _COUNT_TTL:
        return hit[1]
    if kind == "vector":
        value = get_vector_store().count(tenant)
    else:
        value = get_lexical_store().count(tenant)
    _count_cache[cache_key] = (now, value)
    return value
