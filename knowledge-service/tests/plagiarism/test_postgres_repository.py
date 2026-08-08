"""Repository behaviour against a real PostgreSQL server.

Everything here depends on something SQLite cannot do: array overlap over a GIN
index, `SKIP LOCKED`, advisory locks, half-open snapshot windows. See
`conftest.py` for how a missing server is reported.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import inspect, text

from kbsvc import ids
from kbsvc.plagiarism import repository as repo
from kbsvc.plagiarism.models import (
    PlagCheck,
    PlagCorpusChunk,
    PlagCorpusProjection,
    utcnow,
)
from kbsvc.plagiarism.schema import (
    REQUIRED_TABLES,
    init_plagiarism_schema,
    verify_plagiarism_schema,
)
from kbsvc.plagiarism.types import CheckStatus, CorpusJobStatus

TENANT = "test"
HASH = "algo-hash-v1"


def make_projection(session, *, document_id, version_id="v1", activate=True, tenant=TENANT):
    projection = PlagCorpusProjection(
        id=ids.new_id(),
        tenant_id=tenant,
        document_id=document_id,
        version_id=version_id,
        content_hash="c" * 64,
        language="zh",
        acl=["public"],
        algorithm_config_hash=HASH,
    )
    session.add(projection)
    session.flush()
    if activate:
        repo.activate_projection(session, projection=projection)
    return projection


def add_chunk(session, projection, *, index, fingerprints, text_value="chunk text", start=0):
    chunk = PlagCorpusChunk(
        id=ids.new_id(),
        projection_id=projection.id,
        tenant_id=projection.tenant_id,
        chunk_index=index,
        char_start=start,
        char_end=start + len(text_value),
        sentence_count=1,
        text=text_value,
        fingerprints=fingerprints,
    )
    session.add(chunk)
    session.flush()
    return chunk


# --- schema -------------------------------------------------------------


def test_schema_reports_ready_with_every_table_and_the_gin_index(pg_session):
    report = verify_plagiarism_schema(pg_session)
    assert report["ready"] is True
    assert report["missing_tables"] == []
    assert report["missing_columns"] == []
    assert report["gin_index"] is True


def test_schema_init_is_idempotent(pg_session):
    """Re-running init is the normal way to pick up a newly added table."""
    assert init_plagiarism_schema(pg_session.get_bind()) == []


def test_schema_init_adds_matcher_columns_without_losing_existing_rows(pg_session):
    check = PlagCheck(
        id=ids.new_id(),
        tenant_id=TENANT,
        creator_key_id="key",
        algorithm_config_hash=HASH,
    )
    pg_session.add(check)
    pg_session.flush()
    check_id = check.id

    pg_session.execute(text("ALTER TABLE plag_check DROP COLUMN matcher_version"))
    pg_session.execute(text("ALTER TABLE plag_check DROP COLUMN matcher_config"))
    pg_session.commit()

    assert init_plagiarism_schema(pg_session.get_bind()) == []
    assert pg_session.execute(
        text("SELECT count(*) FROM plag_check WHERE id = :id"), {"id": check_id}
    ).scalar_one() == 1
    columns = {
        column["name"]
        for column in inspect(pg_session.get_bind()).get_columns("plag_check")
    }
    assert {"matcher_version", "matcher_config"} <= columns


def test_required_tables_all_exist(pg_session):
    for table in REQUIRED_TABLES:
        assert pg_session.execute(text(f"SELECT to_regclass('{table}')")).scalar() is not None


# --- snapshot windows ---------------------------------------------------


def test_activation_retires_the_previous_projection(pg_session):
    first = make_projection(pg_session, document_id="doc-1", version_id="v1")
    second = make_projection(pg_session, document_id="doc-1", version_id="v2")
    pg_session.refresh(first)
    assert first.active_until is not None
    assert second.active_until is None


def test_a_snapshot_resolves_to_exactly_one_projection(pg_session):
    """The window is half-open, so the instant of a rebuild belongs to the new
    projection only - never both, never neither."""
    first = make_projection(pg_session, document_id="doc-1", version_id="v1")
    second = make_projection(pg_session, document_id="doc-1", version_id="v2")
    pg_session.refresh(first)
    cutover = second.active_from

    at_cutover = repo.active_projection_ids(
        pg_session, tenant_id=TENANT, algorithm_config_hash=HASH, snapshot_at=cutover
    )
    assert at_cutover == [second.id]

    before = repo.active_projection_ids(
        pg_session,
        tenant_id=TENANT,
        algorithm_config_hash=HASH,
        snapshot_at=cutover - timedelta(microseconds=1),
    )
    assert before == [first.id]


def test_a_rebuild_cannot_change_an_in_flight_checks_corpus(pg_session):
    """The whole point of snapshots: a projection published after the check
    started must not appear in that check's candidate set."""
    original = make_projection(pg_session, document_id="doc-1", version_id="v1")
    snapshot = utcnow()
    make_projection(pg_session, document_id="doc-1", version_id="v2")

    visible = repo.active_projection_ids(
        pg_session, tenant_id=TENANT, algorithm_config_hash=HASH, snapshot_at=snapshot
    )
    assert visible == [original.id]


def test_projections_of_another_algorithm_are_invisible(pg_session):
    projection = make_projection(pg_session, document_id="doc-1")
    assert repo.active_projection_ids(
        pg_session,
        tenant_id=TENANT,
        algorithm_config_hash="a-different-hash",
        snapshot_at=utcnow(),
    ) == []
    assert projection.id in repo.active_projection_ids(
        pg_session, tenant_id=TENANT, algorithm_config_hash=HASH, snapshot_at=utcnow()
    )


def test_projections_of_another_tenant_are_invisible(pg_session):
    make_projection(pg_session, document_id="doc-1", tenant="other-tenant")
    assert repo.active_projection_ids(
        pg_session, tenant_id=TENANT, algorithm_config_hash=HASH, snapshot_at=utcnow()
    ) == []


# --- candidate retrieval ------------------------------------------------


def test_overlap_finds_only_chunks_sharing_a_fingerprint(pg_session):
    projection = make_projection(pg_session, document_id="doc-1")
    hit = add_chunk(pg_session, projection, index=0, fingerprints=[10, 20, 30])
    add_chunk(pg_session, projection, index=1, fingerprints=[40, 50])

    found = repo.find_candidate_chunks(
        pg_session,
        tenant_id=TENANT,
        projection_ids=[projection.id],
        fingerprints=[20, 999],
        limit=10,
    )
    assert [row[0] for row in found] == [hit.id]


def test_empty_probe_or_no_projections_returns_nothing(pg_session):
    projection = make_projection(pg_session, document_id="doc-1")
    add_chunk(pg_session, projection, index=0, fingerprints=[1, 2])
    assert repo.find_candidate_chunks(
        pg_session, tenant_id=TENANT, projection_ids=[projection.id], fingerprints=[], limit=10
    ) == []
    assert repo.find_candidate_chunks(
        pg_session, tenant_id=TENANT, projection_ids=[], fingerprints=[1], limit=10
    ) == []


def test_self_exclusion_drops_every_version_not_just_the_current_one(pg_session):
    """An earlier revision of the same work matching itself is the failure this
    prevents - excluding only the current version would let it through."""
    old = make_projection(pg_session, document_id="doc-self", version_id="v1", activate=False)
    current = make_projection(pg_session, document_id="doc-self", version_id="v2")
    other = make_projection(pg_session, document_id="doc-other")
    add_chunk(pg_session, old, index=0, fingerprints=[7])
    add_chunk(pg_session, current, index=0, fingerprints=[7])
    kept = add_chunk(pg_session, other, index=0, fingerprints=[7])

    found = repo.find_candidate_chunks(
        pg_session,
        tenant_id=TENANT,
        projection_ids=[old.id, current.id, other.id],
        fingerprints=[7],
        limit=10,
        excluded_document_id="doc-self",
    )
    assert [row[0] for row in found] == [kept.id]


def test_candidate_query_uses_the_gin_index(pg_session):
    """ADR-0001 forbids a scan fallback, so a lost index shows up as an
    unbounded slowdown rather than an error. Assert the plan instead."""
    projection = make_projection(pg_session, document_id="doc-1")
    for i in range(200):
        add_chunk(pg_session, projection, index=i, fingerprints=[i, i + 1000])
    pg_session.commit()
    pg_session.execute(text("ANALYZE plag_corpus_chunk"))
    # The planner prefers a sequential scan on tiny tables regardless of
    # indexes; disabling it shows whether the index is usable at all.
    pg_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in pg_session.execute(
            text(
                "EXPLAIN SELECT id FROM plag_corpus_chunk "
                "WHERE fingerprints && ARRAY[5, 7]::bigint[]"
            )
        )
    )
    assert "ix_plag_chunk_fingerprints_gin" in plan, plan


# --- corpus jobs --------------------------------------------------------


def test_registration_is_idempotent(pg_session):
    first = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=3,
    )
    second = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=3,
    )
    assert first.id == second.id


def test_re_registering_a_failed_job_requeues_it(pg_session):
    job = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=1,
    )
    job.status = CorpusJobStatus.FAILED
    job.attempts = 1
    pg_session.flush()

    again = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=1,
    )
    assert again.id == job.id
    assert again.status == CorpusJobStatus.PENDING
    assert again.attempts == 0


def test_expired_lease_is_reclaimed(pg_session):
    job = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=3,
    )
    claimed = repo.claim_corpus_job(pg_session, worker_id="w1", lease_seconds=60)
    assert claimed is not None and claimed.id == job.id

    claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
    pg_session.flush()
    assert repo.reclaim_expired_corpus_jobs(pg_session) == 1
    pg_session.refresh(job)
    assert job.status == CorpusJobStatus.PENDING


def test_failure_with_attempts_left_goes_back_to_pending(pg_session):
    job = repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-1",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=3,
    )
    job.attempts = 1
    repo.finish_corpus_job(
        pg_session, job, status=CorpusJobStatus.FAILED, error="boom", backoff_seconds=0
    )
    assert job.status == CorpusJobStatus.PENDING

    job.attempts = 3
    repo.finish_corpus_job(pg_session, job, status=CorpusJobStatus.FAILED, error="boom")
    assert job.status == CorpusJobStatus.FAILED


# --- readiness ----------------------------------------------------------


def test_readiness_counts_ready_pending_and_failed(pg_session):
    make_projection(pg_session, document_id="doc-ready")
    repo.enqueue_corpus_job(
        pg_session,
        tenant_id=TENANT,
        document_id="doc-failed",
        version_id="v1",
        algorithm_config_hash=HASH,
        max_attempts=1,
    ).status = CorpusJobStatus.FAILED
    pg_session.flush()

    report = repo.projection_readiness(
        pg_session,
        tenant_id=TENANT,
        algorithm_config_hash=HASH,
        document_ids=["doc-ready", "doc-failed", "doc-untouched"],
    )
    assert report == {"total": 3, "ready": 1, "pending": 1, "failed": 1}


def test_readiness_of_an_empty_corpus_is_all_zero(pg_session):
    assert repo.projection_readiness(
        pg_session, tenant_id=TENANT, algorithm_config_hash=HASH, document_ids=[]
    ) == {"total": 0, "ready": 0, "pending": 0, "failed": 0}


# --- events -------------------------------------------------------------


def test_events_replay_in_order_and_resume_exactly(pg_session):
    from kbsvc.plagiarism.types import CheckStage

    check_id = ids.new_id()
    created = [
        repo.append_event(
            pg_session,
            check_id=check_id,
            tenant_id=TENANT,
            stage=stage,
            status=CheckStatus.RUNNING,
        )
        for stage in (CheckStage.QUEUED, CheckStage.STARTED, CheckStage.CHUNKING)
    ]
    ids_in_order = [event.id for event in created]
    assert ids_in_order == sorted(ids_in_order)

    replayed = repo.replay_events(pg_session, check_id=check_id)
    assert [e.id for e in replayed] == ids_in_order

    # Last-Event-ID semantics: strictly after, so no gap and no repeat.
    resumed = repo.replay_events(pg_session, check_id=check_id, after_id=ids_in_order[0])
    assert [e.id for e in resumed] == ids_in_order[1:]


# --- idempotency --------------------------------------------------------


def make_check(session, *, key="", route="", creator="k1", status=CheckStatus.COMPLETED, age=0):
    from kbsvc.plagiarism.models import PlagCheck

    check = PlagCheck(
        id=ids.new_id(),
        tenant_id=TENANT,
        creator_key_id=creator,
        route=route,
        idempotency_key=key,
        algorithm_config_hash=HASH,
        status=str(status),
        created_at=utcnow() - timedelta(days=age),
    )
    session.add(check)
    session.flush()
    return check


def test_several_checks_without_an_idempotency_key_coexist(pg_session):
    """The key is optional. A unique constraint that treats every un-keyed check
    as key `''` makes a caller's second ordinary request collide with the first."""
    first = make_check(pg_session)
    second = make_check(pg_session)
    assert first.id != second.id


def test_the_same_key_twice_on_one_route_is_rejected(pg_session):
    from sqlalchemy.exc import IntegrityError

    make_check(pg_session, key="idem-1", route="text")
    with pytest.raises(IntegrityError):
        make_check(pg_session, key="idem-1", route="text")


def test_the_same_key_on_a_different_route_is_a_different_request(pg_session):
    make_check(pg_session, key="idem-1", route="text")
    other = make_check(pg_session, key="idem-1", route="document")
    assert other.id is not None


def test_lookup_by_key_finds_the_original(pg_session):
    original = make_check(pg_session, key="idem-1", route="text")
    found = repo.find_by_idempotency_key(
        pg_session, tenant_id=TENANT, creator_key_id="k1", route="text", key="idem-1"
    )
    assert found is not None and found.id == original.id


def test_an_empty_key_never_matches_an_existing_check(pg_session):
    make_check(pg_session)
    assert (
        repo.find_by_idempotency_key(
            pg_session, tenant_id=TENANT, creator_key_id="k1", route="text", key=""
        )
        is None
    )


# --- ownership and concurrency -----------------------------------------


def test_a_check_is_invisible_to_another_credential(pg_session):
    """Not-yours and does-not-exist must be indistinguishable, or the endpoint
    becomes a probe for which ids are real."""
    mine = make_check(pg_session, creator="k1")
    assert repo.load_check(
        pg_session, check_id=mine.id, tenant_id=TENANT, creator_key_id="k2"
    ) is None
    assert repo.load_check(
        pg_session, check_id=mine.id, tenant_id=TENANT, creator_key_id="k1"
    ) is not None


def test_active_count_ignores_terminal_checks_and_other_credentials(pg_session):
    make_check(pg_session, creator="k1", status=CheckStatus.PENDING)
    make_check(pg_session, creator="k1", status=CheckStatus.RUNNING)
    make_check(pg_session, creator="k1", status=CheckStatus.COMPLETED)
    make_check(pg_session, creator="k2", status=CheckStatus.PENDING)
    assert repo.count_active_checks(pg_session, tenant_id=TENANT, creator_key_id="k1") == 2


def test_advisory_lock_is_acquired_and_released_with_the_transaction(pg_session):
    repo.lock_creator(pg_session, tenant_id=TENANT, creator_key_id="k1")
    held = pg_session.execute(
        text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
    ).scalar()
    assert held >= 1
    pg_session.commit()
    assert (
        pg_session.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        ).scalar()
        == 0
    )


# --- retention ----------------------------------------------------------


def test_cleanup_removes_aged_checks_but_keeps_recent_ones(pg_session):
    from kbsvc.plagiarism.models import PlagCheck

    old = PlagCheck(
        id=ids.new_id(),
        tenant_id=TENANT,
        creator_key_id="k1",
        algorithm_config_hash=HASH,
        status=str(CheckStatus.COMPLETED),
        created_at=utcnow() - timedelta(days=90),
    )
    recent = PlagCheck(
        id=ids.new_id(),
        tenant_id=TENANT,
        creator_key_id="k1",
        algorithm_config_hash=HASH,
        status=str(CheckStatus.COMPLETED),
    )
    pg_session.add_all([old, recent])
    pg_session.flush()

    removed = repo.cleanup_expired(pg_session, retention_days=30)
    assert removed["checks"] == 1
    assert pg_session.get(PlagCheck, recent.id) is not None
    assert pg_session.get(PlagCheck, old.id) is None


@pytest.mark.parametrize("status", [CheckStatus.PENDING, CheckStatus.RUNNING])
def test_cleanup_never_removes_an_active_check(pg_session, status):
    from kbsvc.plagiarism.models import PlagCheck

    active = PlagCheck(
        id=ids.new_id(),
        tenant_id=TENANT,
        creator_key_id="k1",
        algorithm_config_hash=HASH,
        status=str(status),
        created_at=utcnow() - timedelta(days=90),
    )
    pg_session.add(active)
    pg_session.flush()

    repo.cleanup_expired(pg_session, retention_days=30)
    assert pg_session.get(PlagCheck, active.id) is not None
