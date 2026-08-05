"""Ingestion control plane: state machine, idempotency, versioning, deletion."""

from __future__ import annotations

import pytest

from kbsvc.db.models import Chunk, Document, DocumentVersion, IndexEvent, IngestJob
from kbsvc.ingest.states import JobState, backoff_seconds, can_transition
from kbsvc.ingest.uploader import register_bytes
from kbsvc.models.events import IndexEventType

DOC_V1 = "# 六壬\n\n## 卷一\n\n贼克者，取用之首法也。上克下为贼，下贼上为克。\n"
DOC_V2 = "# 六壬\n\n## 卷一\n\n贼克者，取用之首法也。\n\n## 卷二\n\n涉害者，比用不成则涉害。\n"


# --- state machine ------------------------------------------------------


def test_happy_path_transitions_are_allowed():
    chain = [
        (JobState.PENDING, JobState.PARSING),
        (JobState.PARSING, JobState.CHUNKING),
        (JobState.CHUNKING, JobState.EMBEDDING),
        (JobState.EMBEDDING, JobState.INDEXING),
        (JobState.INDEXING, JobState.COMPLETED),
    ]
    assert all(can_transition(current, target) for current, target in chain)


def test_completed_is_terminal_and_failed_can_be_requeued():
    assert not can_transition(JobState.COMPLETED, JobState.PENDING)
    assert can_transition(JobState.FAILED, JobState.PENDING)


def test_unknown_states_are_rejected_rather_than_crashing():
    assert not can_transition("bogus", JobState.PENDING)
    assert not can_transition(JobState.PENDING, "bogus")


def test_backoff_grows_exponentially_and_is_capped():
    assert backoff_seconds(1, base=5, cap=600) == 5
    assert backoff_seconds(3, base=5, cap=600) == 20
    assert backoff_seconds(20, base=5, cap=600) == 600


# --- registration -------------------------------------------------------


def test_registration_only_enqueues_and_does_not_index(session, tenant, source):
    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="reg/only.md",
        data=DOC_V1.encode(),
        filename="only.md",
    )
    session.commit()

    assert result.job_id and result.state == JobState.PENDING
    assert not result.deduplicated
    # nothing parsed yet
    assert session.query(Chunk).filter_by(version_id=result.version_id).count() == 0
    assert session.get(DocumentVersion, result.version_id).status == "pending"


def test_registering_identical_bytes_twice_reuses_the_version(session, tenant, source, worker):
    first = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="dedup/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    second = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="dedup/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()

    assert second.deduplicated is True
    assert second.job_id is None
    assert second.document_id == first.document_id
    assert second.version_id == first.version_id


# --- worker execution ---------------------------------------------------


def test_worker_indexes_a_document_end_to_end(session, tenant, source, worker):
    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="e2e/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()
    assert worker.drain() >= 1

    session.expire_all()
    job = session.get(IngestJob, result.job_id)
    version = session.get(DocumentVersion, result.version_id)
    document = session.get(Document, result.document_id)

    assert job.state == JobState.COMPLETED
    assert version.status == "indexed"
    assert version.chunk_count > 0
    assert version.parser == "text"
    assert document.current_version_id == version.id

    chunks = session.query(Chunk).filter_by(version_id=version.id).all()
    assert chunks and all(chunk.heading_path is not None for chunk in chunks)

    events = session.query(IndexEvent).filter_by(version_id=version.id).all()
    assert any(e.event_type == IndexEventType.CHUNKS_UPSERTED for e in events)


def test_updating_content_creates_a_new_version_and_supersedes_the_old(
    session, tenant, source, worker
):
    first = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="ver/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    second = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="ver/a.md",
        data=DOC_V2.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    session.expire_all()
    assert second.document_id == first.document_id
    assert second.version_id != first.version_id

    old = session.get(DocumentVersion, first.version_id)
    new = session.get(DocumentVersion, second.version_id)
    assert old.status == "superseded"
    assert new.status == "indexed" and new.version == old.version + 1
    assert session.get(Document, first.document_id).current_version_id == new.id

    # old chunks are gone, and the supersede was recorded as an event
    assert session.query(Chunk).filter_by(version_id=old.id).count() == 0
    events = session.query(IndexEvent).filter_by(document_id=first.document_id).all()
    assert any(e.event_type == IndexEventType.VERSION_SUPERSEDED for e in events)


def test_delete_job_removes_chunks_and_emits_an_event(session, tenant, source, worker):
    from kbsvc.ingest.uploader import enqueue_delete

    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="del/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    enqueue_delete(session, tenant_id=tenant, document_id=result.document_id)
    session.commit()
    worker.drain()

    session.expire_all()
    document = session.get(Document, result.document_id)
    assert document.deleted_at is not None
    assert document.current_version_id is None
    assert session.query(Chunk).filter_by(document_id=result.document_id).count() == 0

    events = session.query(IndexEvent).filter_by(document_id=result.document_id).all()
    assert any(e.event_type == IndexEventType.DOCUMENT_DELETED for e in events)


def test_failing_job_retries_then_parks_in_failed(session, tenant, source, worker, monkeypatch):
    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="fail/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()

    import kbsvc.ingest.worker as worker_module

    class ExplodingRegistry:
        def parse(self, *_args, **_kwargs):
            raise RuntimeError("parser exploded")

    monkeypatch.setattr(worker_module, "get_registry", lambda: ExplodingRegistry())

    outcome = worker.run_once()
    assert outcome.state == JobState.RETRY_WAIT

    outcome = worker.run_once()
    assert outcome.state == JobState.FAILED

    session.expire_all()
    job = session.get(IngestJob, result.job_id)
    assert job.attempts == job.max_attempts
    assert "parser exploded" in job.last_error
    assert session.get(DocumentVersion, result.version_id).status == "failed"


def test_worker_returns_none_when_the_queue_is_empty(worker):
    worker.drain()
    assert worker.run_once() is None


def test_registration_rejects_oversized_payloads(session, tenant, source, monkeypatch, settings):
    from kbsvc.errors import PayloadTooLargeError

    monkeypatch.setattr(settings, "max_upload_bytes", 4)
    with pytest.raises(PayloadTooLargeError):
        register_bytes(
            session,
            tenant_id=tenant,
            source_id=source.id,
            external_id="big/a.md",
            data=b"way too many bytes",
            filename="a.md",
        )


def test_registration_requires_an_external_id(session, tenant, source):
    from kbsvc.errors import ValidationError

    with pytest.raises(ValidationError):
        register_bytes(
            session, tenant_id=tenant, source_id=source.id, external_id="", data=b"x"
        )


def test_lexical_index_tracks_the_chunk_table(session, tenant, source, worker):
    """Corpus statistics used to be two hand-maintained tables; the index owns them now."""
    from kbsvc.lexical import get_lexical_store

    before = get_lexical_store().count(tenant)
    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="stats/a.md",
        data=DOC_V2.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    assert get_lexical_store().count(tenant) > before


def test_two_workers_never_claim_the_same_job(session, tenant, source):
    """The lease is a compare-and-swap: exactly one worker may win."""
    from kbsvc.db import repo as repo_module
    from kbsvc.ingest.worker import IngestWorker

    IngestWorker().drain()  # start from an empty queue

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="race/a.md",
        data=DOC_V2.encode(),
        filename="a.md",
    )
    session.commit()

    from kbsvc.db.session import session_scope

    claims = []
    for owner in ("worker-a", "worker-b"):
        with session_scope() as claim_session:
            claims.append(repo_module.claim_job(claim_session, owner=owner, lease_seconds=60))

    assert sum(claim is not None for claim in claims) == 1
    IngestWorker().drain()


def test_an_expired_lease_is_reclaimed_by_another_worker(session, tenant, source):
    from datetime import timedelta

    from kbsvc.db import repo as repo_module
    from kbsvc.db.models import utcnow as now_fn
    from kbsvc.db.session import session_scope
    from kbsvc.ingest.worker import IngestWorker

    IngestWorker().drain()

    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="lease/a.md",
        data=DOC_V1.encode(),
        filename="a.md",
    )
    session.commit()

    with session_scope() as first:
        claimed = repo_module.claim_job(first, owner="dead-worker", lease_seconds=60)
        assert claimed is not None

    # simulate the worker dying mid-flight: its lease lapses
    with session_scope() as expire:
        job = expire.get(IngestJob, result.job_id)
        job.lease_expires_at = now_fn() - timedelta(seconds=1)

    with session_scope() as second:
        reclaimed = repo_module.claim_job(second, owner="live-worker", lease_seconds=60)
        assert reclaimed is not None
        assert reclaimed.id == result.job_id
        assert reclaimed.lease_owner == "live-worker"

    IngestWorker().drain()


# --- re-embedding -------------------------------------------------------


def test_reembed_rebuilds_every_vector_without_reparsing(session, tenant, source, worker):
    """Changing the embedding model must not require re-reading the originals."""
    from kbsvc.db.models import Chunk as ChunkRow
    from kbsvc.ingest.reembed import reembed_tenant

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="reembed/a.md",
        data=DOC_V2.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    session.expire_all()
    before = {row.id: row.content_hash for row in session.query(ChunkRow).all()}
    assert before

    progress: list[tuple[int, int]] = []
    result = reembed_tenant(
        tenant, batch_size=2, progress=lambda done, total: progress.append((done, total))
    )

    assert result.chunks == len(before)
    assert result.documents >= 1
    assert result.dim > 0
    assert progress and progress[-1][0] == result.chunks

    # chunk identity is model-independent: same ids, same hashes
    session.expire_all()
    after = {row.id: row.content_hash for row in session.query(ChunkRow).all()}
    assert after == before


def test_reembed_leaves_the_index_searchable(session, tenant, source, worker):
    from kbsvc.config import get_settings
    from kbsvc.ingest.reembed import reembed_tenant
    from kbsvc.retrieval.pipeline import RetrievalRequest, get_retrieval_service

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="reembed/b.md",
        data=DOC_V2.encode(),
        filename="b.md",
    )
    session.commit()
    worker.drain()

    reembed_tenant(tenant, batch_size=8)

    response = get_retrieval_service().search(
        RetrievalRequest(query="涉害", tenant_id=get_settings().default_tenant, top_k=5)
    )
    assert response.results


def test_reembed_on_an_empty_tenant_is_a_noop():
    from kbsvc.ingest.reembed import reembed_tenant

    result = reembed_tenant("tenant-with-nothing-in-it")
    assert result.chunks == 0
    assert result.documents == 0


def test_reembed_can_resume_after_an_interruption(session, tenant, source, worker):
    """A long rebuild that dies partway must not have to start over."""
    from kbsvc.db.models import Chunk as ChunkRow
    from kbsvc.ingest.reembed import reembed_tenant

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="resume/a.md",
        data=DOC_V2.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()

    session.expire_all()
    ids = sorted(row.id for row in session.query(ChunkRow).all())
    assert len(ids) >= 2, "need at least two chunks to split the run"
    checkpoint = ids[0]

    result = reembed_tenant(tenant, batch_size=8, resume_after=checkpoint)

    # only the chunks after the checkpoint are reprocessed
    assert result.chunks == len(ids) - 1
    assert result.last_chunk_id == ids[-1]


def test_reembed_reports_a_resumable_checkpoint(session, tenant, source, worker):
    from kbsvc.db.models import Chunk as ChunkRow
    from kbsvc.ingest.reembed import reembed_tenant

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="resume/b.md",
        data=DOC_V1.encode(),
        filename="b.md",
    )
    session.commit()
    worker.drain()

    result = reembed_tenant(tenant, batch_size=4)

    session.expire_all()
    highest = max(row.id for row in session.query(ChunkRow).all())
    assert result.last_chunk_id == highest


def test_resumed_progress_reports_absolute_position(session, tenant, source, worker):
    """Misleading progress on a long rebuild gets jobs killed by mistake."""
    from kbsvc.db.models import Chunk as ChunkRow
    from kbsvc.ingest.reembed import reembed_tenant

    register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id="resume/c.md",
        data=DOC_V2.encode(),
        filename="c.md",
    )
    session.commit()
    worker.drain()

    session.expire_all()
    ids = sorted(row.id for row in session.query(ChunkRow).all())
    total = len(ids)

    seen: list[tuple[int, int]] = []
    reembed_tenant(
        tenant,
        batch_size=1,
        resume_after=ids[0],
        progress=lambda done, tot: seen.append((done, tot)),
    )

    # first callback already accounts for the skipped chunk, and the last one
    # lands exactly on the corpus total
    assert seen[0][0] == 2
    assert seen[-1] == (total, total)
