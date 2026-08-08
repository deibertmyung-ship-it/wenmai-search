"""Service admission, check execution and worker semantics, against real PostgreSQL.

Covers phase 4's exit criteria: worker restart, contention, cancellation,
timeout, and no duplicate results on retry.
"""

from __future__ import annotations

import pytest

from kbsvc.config import get_settings
from kbsvc.plagiarism import repository as repo
from kbsvc.plagiarism.models import PlagCheck, PlagCheckPassage, PlagCheckSource, utcnow
from kbsvc.plagiarism.runner import CheckRunner
from kbsvc.plagiarism.service import (
    ConcurrencyLimitError,
    CorpusEmptyError,
    CorpusNotReadyError,
    FeatureDisabledError,
    IdempotencyConflictError,
    InputTooLargeError,
    InputTooShortError,
    PlagiarismService,
)
from kbsvc.plagiarism.types import CheckStatus, CreateDocumentCheck, CreateTextCheck
from kbsvc.plagiarism.worker import PlagiarismWorker

pytest.importorskip("pysbd", reason="detection needs the `plagiarism` extra")

from _corpus import TENANT

REUSED = (
    "贼克者，取用之首法也。上克下为贼，下贼上为克。"
    "凡四课之中，有上克下者为贼，下贼上者为克。"
    "取用之道，先取贼克，次取比用。涉害者，比用不成则涉害。"
)


@pytest.fixture
def enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "plag_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plag_indexing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plag_min_seed_len", 12, raising=False)
    monkeypatch.setattr(settings, "plag_min_passage_len", 20, raising=False)
    return settings


def service(enabled) -> PlagiarismService:
    return PlagiarismService(enabled)


# --- admission ----------------------------------------------------------


def test_creating_a_check_while_disabled_is_refused(kb_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "plag_enabled", False, raising=False)
    with pytest.raises(FeatureDisabledError):
        PlagiarismService(settings).create_text_check(
            kb_session,
            CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text="anything"),
        )


def test_an_empty_corpus_is_refused_rather_than_reported_as_clean(kb_session, enabled):
    """'No documents to compare against' must not look like 'no reuse found'."""
    with pytest.raises(CorpusEmptyError):
        service(enabled).create_text_check(
            kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
        )


def test_a_partly_built_corpus_is_refused(kb_session, enabled, build_corpus, seed_document):
    build_corpus(text=REUSED)
    # A second document with no projection yet.
    seed_document(text="另一部尚未建成投影的书。" * 5)
    with pytest.raises(CorpusNotReadyError):
        service(enabled).create_text_check(
            kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
        )


def test_oversized_input_is_refused_before_any_work(kb_session, enabled, monkeypatch, build_corpus):
    monkeypatch.setattr(enabled, "plag_max_input_chars", 100, raising=False)
    build_corpus(text=REUSED)
    with pytest.raises(InputTooLargeError):
        service(enabled).create_text_check(
            kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text="x" * 200)
        )


def test_effective_text_shorter_than_twelve_is_refused_before_corpus_lookup(kb_session, enabled):
    with pytest.raises(InputTooShortError) as exc_info:
        service(enabled).create_text_check(
            kb_session,
            CreateTextCheck(
                tenant_id=TENANT,
                creator_key_id="k1",
                text="\u4eba\u7980\u5929\u5730\u3001\u547d\u5c5e\u9634\u9633\u3002",
            ),
        )
    assert exc_info.value.code == "plagiarism_text_too_short"
    assert exc_info.value.detail["effective_chars"] < 12


def test_the_same_idempotency_key_returns_the_original_check(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    command = CreateTextCheck(
        tenant_id=TENANT, creator_key_id="k1", text=REUSED, idempotency_key="idem-1"
    )
    first = svc.create_text_check(kb_session, command)
    second = svc.create_text_check(kb_session, command)
    assert first.check_id == second.check_id


def test_the_same_key_with_different_content_conflicts(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    svc.create_text_check(
        kb_session,
        CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED, idempotency_key="k"),
    )
    with pytest.raises(IdempotencyConflictError):
        svc.create_text_check(
            kb_session,
            CreateTextCheck(
                tenant_id=TENANT, creator_key_id="k1", text=REUSED + "不同", idempotency_key="k"
            ),
        )


def test_the_concurrency_limit_is_enforced(kb_session, enabled, monkeypatch, build_corpus):
    monkeypatch.setattr(enabled, "plag_max_active_checks_per_key", 1, raising=False)
    build_corpus(text=REUSED)
    svc = service(enabled)
    svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    with pytest.raises(ConcurrencyLimitError):
        svc.create_text_check(
            kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
        )
    # A different credential is unaffected.
    assert svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k2", text=REUSED)
    )


# --- detection ----------------------------------------------------------


def test_verbatim_reuse_is_found_with_document_offsets(kb_session, enabled, build_corpus):
    """The end-to-end promise: submit reused text, get back which source it came
    from and where, in coordinates that slice the source document."""
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session,
        CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text="前言。" + REUSED + "后记。"),
    )
    kb_session.commit()

    CheckRunner(enabled).run(kb_session, summary.check_id)
    kb_session.flush()

    sources = kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).all()
    assert len(sources) == 1
    passages = kb_session.query(PlagCheckPassage).filter_by(source_id=sources[0].id).all()
    assert passages
    assert all(p.query_end > p.query_start for p in passages)
    assert sources[0].matched_chars > 0


def test_unrelated_text_finds_nothing(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session,
        CreateTextCheck(
            tenant_id=TENANT,
            creator_key_id="k1",
            text="今天天气很好，我去公园散步，看见许多人在放风筝。" * 4,
        ),
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    assert kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).count() == 0


def test_a_document_check_excludes_every_version_of_itself(kb_session, enabled, build_corpus):
    """Checking a document against the corpus must not match the document."""
    document_id = build_corpus(text=REUSED)
    kb_session.commit()

    svc = service(enabled)
    summary = svc.create_document_check(
        kb_session,
        CreateDocumentCheck(tenant_id=TENANT, creator_key_id="k1", document_id=document_id),
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)

    hits = kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).all()
    assert all(source.document_id != document_id for source in hits)


def test_a_retry_does_not_duplicate_findings(kb_session, enabled, build_corpus):
    """Re-running must replace the previous attempt's rows, not append to them."""
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    kb_session.commit()

    runner = CheckRunner(enabled)
    runner.run(kb_session, summary.check_id)
    kb_session.flush()
    first = kb_session.query(PlagCheckPassage).filter_by(check_id=summary.check_id).count()

    runner.run(kb_session, summary.check_id)
    kb_session.flush()
    assert kb_session.query(PlagCheckPassage).filter_by(check_id=summary.check_id).count() == first


def test_an_exhausted_budget_yields_partial_coverage(
    kb_session, enabled, monkeypatch, build_corpus
):
    """Running out of time must be visibly different from finishing clean."""
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED * 6)
    )
    kb_session.commit()

    monkeypatch.setattr(enabled, "plag_budget_seconds", 0.0, raising=False)
    result = CheckRunner(enabled).run(kb_session, summary.check_id)
    assert result["coverage_reason"] == "time_cap"
    assert result["checked_chunks"] < result["total_chunks"]


# --- ownership ----------------------------------------------------------


def test_another_credential_cannot_see_the_check(kb_session, enabled, build_corpus):
    from kbsvc.plagiarism.service import CheckNotFoundError

    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    with pytest.raises(CheckNotFoundError):
        svc.get_check(
            kb_session, check_id=summary.check_id, tenant_id=TENANT, creator_key_id="k2"
        )


def test_cancelling_a_pending_check_clears_its_text_immediately(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    status = svc.delete_check(
        kb_session, check_id=summary.check_id, tenant_id=TENANT, creator_key_id="k1"
    )
    assert status == "cancelled"
    assert kb_session.get(PlagCheck, summary.check_id).query_text == ""


def test_cancelling_a_running_check_is_cooperative(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    check = kb_session.get(PlagCheck, summary.check_id)
    check.status = str(CheckStatus.RUNNING)
    kb_session.flush()

    status = svc.delete_check(
        kb_session, check_id=summary.check_id, tenant_id=TENANT, creator_key_id="k1"
    )
    assert status == "cancel_requested"
    # The worker owns it; the text survives until the worker settles.
    assert kb_session.get(PlagCheck, summary.check_id).cancel_requested == 1


# --- worker -------------------------------------------------------------


def test_two_workers_never_claim_the_same_check(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    kb_session.commit()

    first = repo.claim_check(kb_session, worker_id="w1", lease_seconds=60)
    kb_session.commit()
    second = repo.claim_check(kb_session, worker_id="w2", lease_seconds=60)
    assert first is not None
    assert second is None


def test_an_expired_check_lease_is_reclaimed(kb_session, enabled, build_corpus):
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    kb_session.commit()

    claimed = repo.claim_check(kb_session, worker_id="w1", lease_seconds=60)
    claimed.lease_expires_at = utcnow() - repo.timedelta(seconds=1)
    kb_session.flush()

    assert repo.reclaim_expired_checks(kb_session) == 1
    assert kb_session.get(PlagCheck, summary.check_id).status == str(CheckStatus.PENDING)


def test_worker_drains_corpus_jobs_before_checks(
    kb_session, enabled, seed_document, enqueue_job, app_db_on_test_postgres
):
    """A document without a projection blocks every check, so builds must not
    queue behind detection work."""
    document_id, version_id = seed_document()
    enqueue_job(document_id, version_id)
    kb_session.commit()

    worker = PlagiarismWorker(enabled)
    assert worker.run_once() is True
    kb_session.expire_all()
    coverage = repo.projection_readiness(
        kb_session,
        tenant_id=TENANT,
        algorithm_config_hash=enabled.plagiarism_algorithm_config_hash,
        document_ids=[document_id],
    )
    assert coverage["ready"] == 1


def test_heartbeat_is_recorded_for_readiness(kb_session, enabled, app_db_on_test_postgres):
    worker = PlagiarismWorker(enabled)
    worker.run_once()
    kb_session.expire_all()
    assert repo.live_worker_count(kb_session, within_seconds=300) >= 1


# --- non-discriminative chunks -----------------------------------------


def test_separator_runs_are_not_reported_as_reuse(kb_session, enabled, build_corpus):
    """Found by acceptance testing on the real corpus: 300 dashes reported 100%
    reuse across four books. A rule line matches every other rule line exactly -
    a real verbatim match, and a meaningless one."""
    build_corpus(text="正文若干。" + "-" * 200 + "又有正文若干。" * 5)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text="-" * 300)
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    assert kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).count() == 0


def test_real_prose_is_still_probed(kb_session, enabled, build_corpus):
    """The guard must not silence genuine content - the failure mode it would
    trade for is worse than the one it fixes."""
    build_corpus(text=REUSED)
    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session, CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text=REUSED)
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    assert kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).count() >= 1


# --- query_text snapshot (ADR-0006) --------------------------------------


def test_document_mode_check_row_carries_the_resolved_snapshot(kb_session, enabled, build_corpus):
    """The runner must write the detection-time snapshot onto `PlagCheck.query_text`
    for document mode too - not just chunk from it and discard it - so the report
    can return it later without ever re-parsing object storage."""
    document_id = build_corpus(text=REUSED)
    kb_session.commit()

    svc = service(enabled)
    summary = svc.create_document_check(
        kb_session,
        CreateDocumentCheck(tenant_id=TENANT, creator_key_id="k1", document_id=document_id),
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    kb_session.flush()

    check = kb_session.get(PlagCheck, summary.check_id)
    assert check.query_text == REUSED
    assert check.query_chars == len(REUSED)


def test_persisted_source_carries_the_real_document_version(
    kb_session, enabled, seed_document, enqueue_job
):
    """`_persist()` used to hardcode `version=0`. Task 7 needs the real
    `DocumentVersion.version` to fetch source excerpts against the exact frozen
    text, not whatever the document looks like today."""
    from kbsvc.plagiarism.projection import ProjectionBuilder

    document_id, version_id = seed_document(text=REUSED, version_no=3)
    ProjectionBuilder(enabled).build(kb_session, enqueue_job(document_id, version_id).id)
    kb_session.commit()

    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session,
        CreateTextCheck(tenant_id=TENANT, creator_key_id="k1", text="前言。" + REUSED + "后记。"),
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    kb_session.flush()

    sources = kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).all()
    assert len(sources) == 1
    assert sources[0].version == 3


def test_a_missing_source_version_fails_the_check_rather_than_completing_empty(
    kb_session, enabled, build_corpus
):
    """A document-mode check whose frozen `source_version_id` can no longer be
    read must fail the run, not silently produce an empty "successful" report
    that reads exactly like a legitimately empty document."""
    from kbsvc.db.models import DocumentVersion
    from kbsvc.plagiarism.service import SourceVersionUnavailableError

    document_id = build_corpus(text=REUSED)
    kb_session.commit()

    svc = service(enabled)
    summary = svc.create_document_check(
        kb_session,
        CreateDocumentCheck(tenant_id=TENANT, creator_key_id="k1", document_id=document_id),
    )
    kb_session.commit()

    check = kb_session.get(PlagCheck, summary.check_id)
    kb_session.query(DocumentVersion).filter_by(id=check.source_version_id).delete()
    kb_session.flush()

    with pytest.raises(SourceVersionUnavailableError):
        CheckRunner(enabled).run(kb_session, summary.check_id)


def test_report_still_flags_revoked_access_after_the_query_text_change(
    kb_session, enabled, build_corpus
):
    """A source revoked before `get_report` runs must still surface as
    `report_visibility_changed` - simplifying `query_text` to a plain
    `check.query_text or ""` must not have disturbed this ACL check."""
    from kbsvc.db.models import Document
    from kbsvc.plagiarism.service import ReportVisibilityChangedError

    document_id = build_corpus(text=REUSED)
    document = kb_session.get(Document, document_id)
    document.acl = ["team-a"]
    kb_session.commit()

    svc = service(enabled)
    summary = svc.create_text_check(
        kb_session,
        CreateTextCheck(
            tenant_id=TENANT,
            creator_key_id="k1",
            text="前言。" + REUSED + "后记。",
            acl=["team-a"],
        ),
    )
    kb_session.commit()
    CheckRunner(enabled).run(kb_session, summary.check_id)
    kb_session.commit()

    assert kb_session.query(PlagCheckSource).filter_by(check_id=summary.check_id).count() >= 1

    document.acl = ["team-b"]
    kb_session.commit()

    with pytest.raises(ReportVisibilityChangedError):
        svc.get_report(
            kb_session, check_id=summary.check_id, tenant_id=TENANT, creator_key_id="k1"
        )
