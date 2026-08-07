"""The ingest hook must never be able to fail an ingest.

Runs on the default SQLite suite - which is itself part of the point: on a
non-PostgreSQL install the hook has to be inert rather than raising.
"""

from __future__ import annotations

import pytest

from kbsvc.db.models import Document, DocumentVersion, IngestJob
from kbsvc.ingest.states import JobState
from kbsvc.ingest.uploader import register_bytes
from kbsvc.plagiarism import ingest_hook

DOC = "# 六壬\n\n## 卷一\n\n贼克者，取用之首法也。上克下为贼，下贼上为克。\n"


def ingest(session, tenant, source, worker, *, external_id="hook/a.md"):
    result = register_bytes(
        session,
        tenant_id=tenant,
        source_id=source.id,
        external_id=external_id,
        data=DOC.encode(),
        filename="a.md",
    )
    session.commit()
    worker.drain()
    session.expire_all()
    return result


def test_hook_is_inert_when_indexing_is_disabled(session, tenant, source, worker):
    """Default-off must mean genuinely no side effects, not merely no output."""
    result = ingest(session, tenant, source, worker)
    job = session.get(IngestJob, result.job_id)
    assert job.state == JobState.COMPLETED
    assert (
        ingest_hook.enqueue_projection_if_enabled(
            session, tenant_id=tenant, document_id=result.document_id, version_id=result.version_id
        )
        is None
    )


def test_hook_is_inert_on_a_non_postgres_store(session, tenant, source, monkeypatch, settings):
    """Turning indexing on under SQLite must not raise - the local profile has
    to keep working even with the flag flipped."""
    monkeypatch.setattr(settings, "plag_indexing_enabled", True, raising=False)
    monkeypatch.setattr("kbsvc.plagiarism.ingest_hook.get_settings", lambda: settings)
    assert (
        ingest_hook.enqueue_projection_if_enabled(
            session, tenant_id=tenant, document_id="doc-1", version_id="ver-1"
        )
        is None
    )
    assert (
        ingest_hook.retire_document_if_enabled(session, tenant_id=tenant, document_id="doc-1") == 0
    )


def test_a_failing_hook_does_not_fail_the_ingest(session, tenant, source, worker, monkeypatch):
    """The guarantee that matters: a document that indexed correctly stays
    indexed even when plagiarism registration blows up."""

    def explode(*args, **kwargs):
        raise RuntimeError("projection registration is broken")

    monkeypatch.setattr("kbsvc.plagiarism.ingest_hook.enqueue_projection_if_enabled", explode)

    result = ingest(session, tenant, source, worker, external_id="hook/resilient.md")
    job = session.get(IngestJob, result.job_id)
    version = session.get(DocumentVersion, result.version_id)
    document = session.get(Document, result.document_id)

    assert job.state == JobState.COMPLETED
    assert version.status == "indexed"
    assert document.current_version_id == version.id


def test_hook_swallows_database_errors_rather_than_propagating(
    session, tenant, monkeypatch, settings
):
    monkeypatch.setattr(settings, "plag_indexing_enabled", True, raising=False)
    monkeypatch.setattr("kbsvc.plagiarism.ingest_hook.get_settings", lambda: settings)
    monkeypatch.setattr("kbsvc.plagiarism.schema.is_postgres", lambda engine: True)

    # With `is_postgres` forced true on a SQLite session, the repository call
    # fails inside the savepoint - which is exactly the case the hook exists to
    # contain.
    assert (
        ingest_hook.enqueue_projection_if_enabled(
            session, tenant_id=tenant, document_id="doc-1", version_id="ver-1"
        )
        is None
    )


@pytest.mark.parametrize("flag", [False, True])
def test_retire_is_safe_in_both_flag_states(session, tenant, monkeypatch, settings, flag):
    monkeypatch.setattr(settings, "plag_indexing_enabled", flag, raising=False)
    monkeypatch.setattr("kbsvc.plagiarism.ingest_hook.get_settings", lambda: settings)
    assert (
        ingest_hook.retire_document_if_enabled(session, tenant_id=tenant, document_id="missing")
        == 0
    )
