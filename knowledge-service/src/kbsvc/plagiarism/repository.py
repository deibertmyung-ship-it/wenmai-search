"""All PostgreSQL access for plagiarism detection.

Every query and transaction lives here so the service, the projection builder
and the runner stay free of SQL. Synchronous `Session` throughout - the same
engine and transaction machinery as the rest of kbsvc (ADR-0001 rules out a
second, async engine in the same process).

Candidate retrieval is `BIGINT[]` overlap over a GIN index and has no
scan-based fallback: a fallback would turn a missing index from a loud failure
into a silent, unbounded slowdown.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from .. import ids
from .models import (
    PlagCheck,
    PlagCheckEvent,
    PlagCheckPassage,
    PlagCheckSource,
    PlagCorpusChunk,
    PlagCorpusJob,
    PlagCorpusProjection,
    PlagFingerprintDf,
    PlagWorkerHeartbeat,
    utcnow,
)
from .types import (
    ACTIVE_CHECK_STATUSES,
    CheckStage,
    CheckStatus,
    CorpusJobStatus,
)

# --- corpus jobs --------------------------------------------------------


def enqueue_corpus_job(
    session: Session,
    *,
    tenant_id: str,
    document_id: str,
    version_id: str,
    algorithm_config_hash: str,
    max_attempts: int,
) -> PlagCorpusJob:
    """Register a projection build. Idempotent on (tenant, version, hash).

    Called from the ingest hook, from backfill and from rebuild - all three can
    legitimately fire for the same version, so re-registration returns the
    existing row rather than raising.
    """
    existing = session.scalar(
        select(PlagCorpusJob).where(
            PlagCorpusJob.tenant_id == tenant_id,
            PlagCorpusJob.version_id == version_id,
            PlagCorpusJob.algorithm_config_hash == algorithm_config_hash,
        )
    )
    if existing is not None:
        # A previously failed build should retry when something re-registers it;
        # a completed one is left alone.
        if existing.status == CorpusJobStatus.FAILED:
            existing.status = CorpusJobStatus.PENDING
            existing.attempts = 0
            existing.available_at = utcnow()
            existing.last_error = ""
        return existing

    job = PlagCorpusJob(
        id=ids.new_id(),
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        algorithm_config_hash=algorithm_config_hash,
        status=CorpusJobStatus.PENDING,
        max_attempts=max_attempts,
        available_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def claim_corpus_job(
    session: Session, *, worker_id: str, lease_seconds: int
) -> PlagCorpusJob | None:
    """Lease one pending corpus job.

    `FOR UPDATE SKIP LOCKED` is what lets several workers poll the same table
    without two of them taking the same row: a row already locked by another
    transaction is stepped over rather than waited on.
    """
    now = utcnow()
    job = session.scalar(
        select(PlagCorpusJob)
        .where(
            PlagCorpusJob.status == CorpusJobStatus.PENDING,
            PlagCorpusJob.available_at <= now,
        )
        .order_by(PlagCorpusJob.available_at, PlagCorpusJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = CorpusJobStatus.RUNNING
    job.attempts += 1
    job.leased_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    session.flush()
    return job


def reclaim_expired_corpus_jobs(session: Session) -> int:
    """Return jobs whose lease outlived their worker to the queue."""
    result = session.execute(
        update(PlagCorpusJob)
        .where(
            PlagCorpusJob.status == CorpusJobStatus.RUNNING,
            PlagCorpusJob.lease_expires_at.is_not(None),
            PlagCorpusJob.lease_expires_at < utcnow(),
        )
        .values(
            status=CorpusJobStatus.PENDING,
            leased_by="",
            lease_expires_at=None,
            available_at=utcnow(),
        )
    )
    return result.rowcount or 0


def finish_corpus_job(
    session: Session,
    job: PlagCorpusJob,
    *,
    status: CorpusJobStatus,
    error: str = "",
    backoff_seconds: float = 0.0,
) -> None:
    """Settle a corpus job. A failure with attempts left goes back to pending."""
    if status == CorpusJobStatus.FAILED and job.attempts < job.max_attempts:
        job.status = CorpusJobStatus.PENDING
        job.available_at = utcnow() + timedelta(seconds=backoff_seconds)
    else:
        job.status = status
    job.last_error = error[:2000]
    job.leased_by = ""
    job.lease_expires_at = None
    session.flush()


# --- projections --------------------------------------------------------


def activate_projection(
    session: Session,
    *,
    projection: PlagCorpusProjection,
    now: datetime | None = None,
) -> None:
    """Publish `projection` and retire the previous one, in one transaction.

    The window is half-open: the outgoing projection's `active_until` equals the
    incoming one's `active_from`, so a snapshot taken at that instant resolves
    to exactly one projection - never both, never neither.
    """
    moment = now or utcnow()
    session.execute(
        update(PlagCorpusProjection)
        .where(
            PlagCorpusProjection.tenant_id == projection.tenant_id,
            PlagCorpusProjection.document_id == projection.document_id,
            PlagCorpusProjection.algorithm_config_hash == projection.algorithm_config_hash,
            PlagCorpusProjection.id != projection.id,
            PlagCorpusProjection.active_from.is_not(None),
            PlagCorpusProjection.active_until.is_(None),
        )
        .values(active_until=moment)
    )
    projection.active_from = moment
    projection.active_until = None
    session.flush()


def active_projection_ids(
    session: Session,
    *,
    tenant_id: str,
    algorithm_config_hash: str,
    snapshot_at: datetime,
    document_ids: Sequence[str] | None = None,
) -> list[str]:
    """Projections live at `snapshot_at`, for the given algorithm."""
    stmt = select(PlagCorpusProjection.id).where(
        PlagCorpusProjection.tenant_id == tenant_id,
        PlagCorpusProjection.algorithm_config_hash == algorithm_config_hash,
        PlagCorpusProjection.active_from.is_not(None),
        PlagCorpusProjection.active_from <= snapshot_at,
        or_(
            PlagCorpusProjection.active_until.is_(None),
            PlagCorpusProjection.active_until > snapshot_at,
        ),
    )
    if document_ids is not None:
        stmt = stmt.where(PlagCorpusProjection.document_id.in_(document_ids))
    return list(session.scalars(stmt))


def projection_readiness(
    session: Session,
    *,
    tenant_id: str,
    algorithm_config_hash: str,
    document_ids: Sequence[str],
) -> dict[str, int]:
    """How much of `document_ids` has a live projection under this algorithm.

    A check is refused unless every visible document is ready - running against
    a partially built corpus would report "no matches" for reasons that have
    nothing to do with the submitted text.
    """
    if not document_ids:
        return {"total": 0, "ready": 0, "pending": 0, "failed": 0}

    ready = set(
        session.scalars(
            select(PlagCorpusProjection.document_id).where(
                PlagCorpusProjection.tenant_id == tenant_id,
                PlagCorpusProjection.algorithm_config_hash == algorithm_config_hash,
                PlagCorpusProjection.document_id.in_(document_ids),
                PlagCorpusProjection.active_from.is_not(None),
                PlagCorpusProjection.active_until.is_(None),
            )
        )
    )
    failed = set(
        session.scalars(
            select(PlagCorpusJob.document_id).where(
                PlagCorpusJob.tenant_id == tenant_id,
                PlagCorpusJob.algorithm_config_hash == algorithm_config_hash,
                PlagCorpusJob.document_id.in_(document_ids),
                PlagCorpusJob.status == CorpusJobStatus.FAILED,
            )
        )
    )
    outstanding = set(document_ids) - ready
    return {
        "total": len(set(document_ids)),
        "ready": len(ready),
        "pending": len(outstanding - failed),
        "failed": len(outstanding & failed),
    }


def replace_projection_chunks(
    session: Session, *, projection_id: str, chunks: list[PlagCorpusChunk]
) -> None:
    session.execute(
        delete(PlagCorpusChunk).where(PlagCorpusChunk.projection_id == projection_id)
    )
    session.add_all(chunks)
    session.flush()


def delete_projections_for_document(
    session: Session, *, tenant_id: str, document_id: str
) -> int:
    """Remove a document from the corpus entirely (used on document delete)."""
    projection_ids = list(
        session.scalars(
            select(PlagCorpusProjection.id).where(
                PlagCorpusProjection.tenant_id == tenant_id,
                PlagCorpusProjection.document_id == document_id,
            )
        )
    )
    if not projection_ids:
        return 0
    session.execute(
        delete(PlagCorpusChunk).where(PlagCorpusChunk.projection_id.in_(projection_ids))
    )
    session.execute(
        delete(PlagCorpusProjection).where(PlagCorpusProjection.id.in_(projection_ids))
    )
    return len(projection_ids)


# --- candidate retrieval ------------------------------------------------


def find_candidate_chunks(
    session: Session,
    *,
    tenant_id: str,
    projection_ids: Sequence[str],
    fingerprints: Sequence[int],
    limit: int,
    excluded_document_id: str = "",
) -> list[tuple[str, str, str, int]]:
    """Chunks sharing at least one fingerprint with the probe.

    Returns `(chunk_id, text, projection_id, char_start)`. The `&&` operator is
    what the GIN index serves.

    Ordering is by `(projection_id, chunk_index)` rather than by overlap size.
    Ranking by intersection cardinality reads better but costs a per-row array
    intersection on top of the index scan, and the alignment stage re-scores
    every candidate anyway - so the ordering only decides which candidates
    survive `limit`, and a deterministic order is worth more there than a
    marginally better one.

    `excluded_document_id` drops **every version** of that document - excluding
    only the current one would let an earlier revision match itself.
    """
    if not projection_ids or not fingerprints:
        return []

    stmt = (
        select(
            PlagCorpusChunk.id,
            PlagCorpusChunk.text,
            PlagCorpusChunk.projection_id,
            PlagCorpusChunk.char_start,
        )
        .join(
            PlagCorpusProjection,
            PlagCorpusProjection.id == PlagCorpusChunk.projection_id,
        )
        .where(
            PlagCorpusChunk.tenant_id == tenant_id,
            PlagCorpusChunk.projection_id.in_(projection_ids),
            PlagCorpusChunk.fingerprints.overlap(list(fingerprints)),
        )
        .order_by(PlagCorpusChunk.projection_id, PlagCorpusChunk.chunk_index)
        .limit(limit)
    )
    if excluded_document_id:
        stmt = stmt.where(PlagCorpusProjection.document_id != excluded_document_id)

    return [tuple(row) for row in session.execute(stmt)]


def high_frequency_fingerprints(
    session: Session,
    *,
    tenant_id: str,
    algorithm_config_hash: str,
    ratio_threshold: float,
    corpus_size: int,
) -> set[int]:
    """Fingerprints common enough to carry no signal.

    Boilerplate and stock phrases appear in a large share of the corpus; left
    in the probe they dominate candidate retrieval and drown real matches.
    """
    if corpus_size <= 0:
        return set()
    cutoff = max(1, int(corpus_size * ratio_threshold))
    return set(
        session.scalars(
            select(PlagFingerprintDf.fingerprint).where(
                PlagFingerprintDf.tenant_id == tenant_id,
                PlagFingerprintDf.algorithm_config_hash == algorithm_config_hash,
                PlagFingerprintDf.document_count >= cutoff,
            )
        )
    )


# --- checks -------------------------------------------------------------


def request_digest(*parts: str) -> str:
    """Stable digest of a create request, for idempotency conflict detection."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _advisory_lock_key(tenant_id: str, creator_key_id: str) -> int:
    """A stable 63-bit key for `pg_advisory_xact_lock`."""
    digest = hashlib.sha256(f"{tenant_id}|{creator_key_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_creator(session: Session, *, tenant_id: str, creator_key_id: str) -> None:
    """Serialise concurrent creates by the same credential.

    Held to the end of the transaction. Without it, two simultaneous requests
    both count `n` active checks, both see room, and both insert - overshooting
    the limit. Counting and inserting must be one atomic step.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _advisory_lock_key(tenant_id, creator_key_id)},
    )


def count_active_checks(session: Session, *, tenant_id: str, creator_key_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(PlagCheck.id)).where(
                PlagCheck.tenant_id == tenant_id,
                PlagCheck.creator_key_id == creator_key_id,
                PlagCheck.status.in_([str(s) for s in ACTIVE_CHECK_STATUSES]),
            )
        )
        or 0
    )


def find_by_idempotency_key(
    session: Session, *, tenant_id: str, creator_key_id: str, route: str, key: str
) -> PlagCheck | None:
    if not key:
        return None
    return session.scalar(
        select(PlagCheck).where(
            PlagCheck.tenant_id == tenant_id,
            PlagCheck.creator_key_id == creator_key_id,
            PlagCheck.route == route,
            PlagCheck.idempotency_key == key,
        )
    )


def load_check(
    session: Session, *, check_id: str, tenant_id: str, creator_key_id: str
) -> PlagCheck | None:
    """Load a check for its owner.

    Always scoped by tenant *and* creator: a check not owned by the caller must
    be indistinguishable from one that does not exist, or the endpoint becomes
    a probe for which ids are real.
    """
    return session.scalar(
        select(PlagCheck).where(
            PlagCheck.id == check_id,
            PlagCheck.tenant_id == tenant_id,
            PlagCheck.creator_key_id == creator_key_id,
        )
    )


def list_checks(
    session: Session,
    *,
    tenant_id: str,
    creator_key_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[PlagCheck]:
    return list(
        session.scalars(
            select(PlagCheck)
            .where(
                PlagCheck.tenant_id == tenant_id,
                PlagCheck.creator_key_id == creator_key_id,
            )
            .order_by(PlagCheck.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def claim_check(session: Session, *, worker_id: str, lease_seconds: int) -> PlagCheck | None:
    now = utcnow()
    check = session.scalar(
        select(PlagCheck)
        .where(
            PlagCheck.status == str(CheckStatus.PENDING),
            PlagCheck.available_at <= now,
        )
        .order_by(PlagCheck.available_at, PlagCheck.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if check is None:
        return None
    check.status = str(CheckStatus.RUNNING)
    check.attempts += 1
    check.leased_by = worker_id
    check.lease_expires_at = now + timedelta(seconds=lease_seconds)
    session.flush()
    return check


def reclaim_expired_checks(session: Session) -> int:
    result = session.execute(
        update(PlagCheck)
        .where(
            PlagCheck.status == str(CheckStatus.RUNNING),
            PlagCheck.lease_expires_at.is_not(None),
            PlagCheck.lease_expires_at < utcnow(),
        )
        .values(
            status=str(CheckStatus.PENDING),
            leased_by="",
            lease_expires_at=None,
            available_at=utcnow(),
        )
    )
    return result.rowcount or 0


def clear_results(session: Session, *, check_id: str) -> None:
    """Drop a check's findings before a retry.

    Without this, a retry appends to the previous attempt's rows and the report
    double-counts every passage it rediscovers.
    """
    source_ids = list(
        session.scalars(select(PlagCheckSource.id).where(PlagCheckSource.check_id == check_id))
    )
    if source_ids:
        session.execute(
            delete(PlagCheckPassage).where(PlagCheckPassage.source_id.in_(source_ids))
        )
    session.execute(delete(PlagCheckSource).where(PlagCheckSource.check_id == check_id))
    session.flush()


def purge_sensitive_content(session: Session, *, check_id: str) -> None:
    """Erase submitted text and findings, keeping the row as a tombstone.

    A cancelled or deleted check must stop holding the user's text, but the
    event stream still needs a terminal event to close on.
    """
    clear_results(session, check_id=check_id)
    session.execute(
        update(PlagCheck).where(PlagCheck.id == check_id).values(query_text="")
    )
    session.flush()


# --- events -------------------------------------------------------------


def append_event(
    session: Session,
    *,
    check_id: str,
    tenant_id: str,
    stage: CheckStage,
    status: CheckStatus,
    progress: float = 0.0,
    detail: dict | None = None,
) -> PlagCheckEvent:
    """Append a progress event.

    A terminal event must be committed in the same transaction as the check's
    terminal status; otherwise a subscriber can see a finished check whose
    stream never closes.
    """
    event = PlagCheckEvent(
        check_id=check_id,
        tenant_id=tenant_id,
        stage=str(stage),
        status=str(status),
        progress=progress,
        detail=detail or {},
    )
    session.add(event)
    session.flush()
    return event


def replay_events(
    session: Session, *, check_id: str, after_id: int | None = None, limit: int = 200
) -> list[PlagCheckEvent]:
    """Events for a check, oldest first, strictly after `after_id`.

    `after_id` comes from `Last-Event-ID`; the strict inequality is what makes a
    reconnect neither drop nor repeat an event.
    """
    stmt = select(PlagCheckEvent).where(PlagCheckEvent.check_id == check_id)
    if after_id is not None:
        stmt = stmt.where(PlagCheckEvent.id > after_id)
    return list(session.scalars(stmt.order_by(PlagCheckEvent.id).limit(limit)))


# --- worker heartbeat ---------------------------------------------------


def record_heartbeat(
    session: Session, *, worker_id: str, algorithm_config_hash: str, tenant_id: str = ""
) -> None:
    row = session.get(PlagWorkerHeartbeat, worker_id)
    if row is None:
        row = PlagWorkerHeartbeat(worker_id=worker_id)
        session.add(row)
    row.tenant_id = tenant_id
    row.algorithm_config_hash = algorithm_config_hash
    row.last_seen_at = utcnow()
    session.flush()


def live_worker_count(session: Session, *, within_seconds: int) -> int:
    cutoff = utcnow() - timedelta(seconds=within_seconds)
    return int(
        session.scalar(
            select(func.count(PlagWorkerHeartbeat.worker_id)).where(
                PlagWorkerHeartbeat.last_seen_at >= cutoff
            )
        )
        or 0
    )


# --- retention ----------------------------------------------------------


def cleanup_expired(session: Session, *, retention_days: int) -> dict[str, int]:
    """Delete aged-out checks and retired projections.

    A retired projection is only removable once no check that could still be
    read references its window - dropping it earlier would leave a stored report
    pointing at a source that can no longer be resolved.
    """
    cutoff = utcnow() - timedelta(days=retention_days)

    stale_ids = list(
        session.scalars(
            select(PlagCheck.id).where(
                PlagCheck.created_at < cutoff,
                PlagCheck.status.notin_([str(s) for s in ACTIVE_CHECK_STATUSES]),
            )
        )
    )
    removed_checks = 0
    if stale_ids:
        source_ids = list(
            session.scalars(
                select(PlagCheckSource.id).where(PlagCheckSource.check_id.in_(stale_ids))
            )
        )
        if source_ids:
            session.execute(
                delete(PlagCheckPassage).where(PlagCheckPassage.source_id.in_(source_ids))
            )
        session.execute(delete(PlagCheckSource).where(PlagCheckSource.check_id.in_(stale_ids)))
        session.execute(delete(PlagCheckEvent).where(PlagCheckEvent.check_id.in_(stale_ids)))
        removed_checks = (
            session.execute(delete(PlagCheck).where(PlagCheck.id.in_(stale_ids))).rowcount or 0
        )

    oldest_live_snapshot = session.scalar(
        select(func.min(PlagCheck.snapshot_at)).where(
            PlagCheck.status.in_([str(s) for s in ACTIVE_CHECK_STATUSES])
        )
    )
    retirement_cutoff = cutoff
    if oldest_live_snapshot is not None:
        retirement_cutoff = min(cutoff, oldest_live_snapshot)

    retired_ids = list(
        session.scalars(
            select(PlagCorpusProjection.id).where(
                PlagCorpusProjection.active_until.is_not(None),
                PlagCorpusProjection.active_until < retirement_cutoff,
            )
        )
    )
    removed_projections = 0
    if retired_ids:
        session.execute(
            delete(PlagCorpusChunk).where(PlagCorpusChunk.projection_id.in_(retired_ids))
        )
        removed_projections = (
            session.execute(
                delete(PlagCorpusProjection).where(PlagCorpusProjection.id.in_(retired_ids))
            ).rowcount
            or 0
        )

    session.flush()
    return {"checks": removed_checks, "projections": removed_projections}


__all__ = [
    "activate_projection",
    "active_projection_ids",
    "append_event",
    "claim_check",
    "claim_corpus_job",
    "cleanup_expired",
    "clear_results",
    "count_active_checks",
    "delete_projections_for_document",
    "enqueue_corpus_job",
    "find_by_idempotency_key",
    "find_candidate_chunks",
    "finish_corpus_job",
    "high_frequency_fingerprints",
    "list_checks",
    "live_worker_count",
    "load_check",
    "lock_creator",
    "projection_readiness",
    "purge_sensitive_content",
    "reclaim_expired_checks",
    "reclaim_expired_corpus_jobs",
    "record_heartbeat",
    "replace_projection_chunks",
    "replay_events",
    "request_digest",
    "cleanup_expired",
]
