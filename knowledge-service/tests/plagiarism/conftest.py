"""PostgreSQL fixtures for the plagiarism suite.

The rest of this repo's tests run on SQLite. Plagiarism cannot: candidate
retrieval is `BIGINT[]` overlap over a GIN index, `SKIP LOCKED` drives job
claiming, and the concurrency limit needs advisory locks. None of those exist
in SQLite, and ADR-0001 rules out a portable fallback on purpose.

So these tests need a real server. Point `KB_TEST_POSTGRES_URL` at one - the
compose stack's postgres is the intended target.

When no server is reachable the tests **skip with the reason attached**. They
must never quietly pass: "did not run" and "ran and was fine" are different
outcomes, and a suite that blurs them reports green on an untested feature.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Default targets the compose stack from inside the compose network. Override
# for a host-side run: postgresql+psycopg://kbsvc:...@localhost:5432/kbsvc_test
_DEFAULT_URL = "postgresql+psycopg://kbsvc:kbsvc@postgres:5432/kbsvc_test"

_SKIP_REASON = (
    "no PostgreSQL reachable at KB_TEST_POSTGRES_URL ({url}): {error}. "
    "Plagiarism is PostgreSQL-only (ADR-0001); these assertions did NOT run."
)


def _target_url() -> str:
    return os.environ.get("KB_TEST_POSTGRES_URL", _DEFAULT_URL)


@pytest.fixture(scope="session")
def pg_engine():
    """Engine against the test database, or a loud skip."""
    url = _target_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any driver/network failure skips
        engine.dispose()
        pytest.skip(_SKIP_REASON.format(url=url, error=type(exc).__name__), allow_module_level=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def pg_schema(pg_engine):
    """Create the `plag_*` schema once, drop it at the end.

    Dropping first as well: a previous crashed run leaves tables behind, and
    silently reusing them would let a stale column definition pass as current.
    """
    from kbsvc.plagiarism.schema import drop_plagiarism_schema, init_plagiarism_schema

    drop_plagiarism_schema(pg_engine)
    init_plagiarism_schema(pg_engine)
    yield pg_engine
    drop_plagiarism_schema(pg_engine)


@pytest.fixture(scope="session")
def kb_schema(pg_schema):
    """The knowledge-base tables, on the same test database.

    Projection and detection read documents, versions and chunks, so both
    schemas have to exist together.
    """
    from kbsvc.db.models import Base

    Base.metadata.create_all(pg_schema)
    yield pg_schema
    Base.metadata.drop_all(pg_schema)


@pytest.fixture
def kb_session(kb_schema, pg_session):
    """`pg_session` plus cleanup of the knowledge-base tables."""
    from kbsvc.db.models import Chunk, Document, DocumentVersion, Source

    yield pg_session
    for model in (Chunk, DocumentVersion, Document, Source):
        pg_session.query(model).delete()
    pg_session.commit()


from _corpus import BODY, TENANT  # noqa: E402  - shared literals


@pytest.fixture
def seed_document(kb_session):
    """Factory for a minimal indexed document: source, document, version, chunk."""
    from kbsvc import ids
    from kbsvc.db.models import Chunk, Document, DocumentVersion, Source

    def _seed(*, text=BODY, document_id=None, version_no=1):
        source = kb_session.get(Source, "src-1") or Source(
            id="src-1", tenant_id=TENANT, name="pytest", kind="upload"
        )
        kb_session.merge(source)

        document_id = document_id or ids.new_id()
        version_id = ids.new_id()
        document = Document(
            id=document_id,
            tenant_id=TENANT,
            source_id="src-1",
            external_id=f"ext-{document_id}",
            title="六壬大全",
            acl=["public"],
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=TENANT,
            document_id=document_id,
            version=version_no,
            # Unique per version: (document_id, content_hash) is constrained.
            content_hash=f"{version_no:064d}",
            mime="text/plain",
            size_bytes=len(text),
            object_key="k",
            status="indexed",
        )
        chunk = Chunk(
            id=ids.new_id(),
            tenant_id=TENANT,
            document_id=document_id,
            version_id=version_id,
            ordinal=0,
            text=text,
            char_start=0,
            char_end=len(text),
        )
        # merge() returns the managed instance; the local object stays detached,
        # so current_version_id has to be set on what merge gave back or it
        # never reaches the database - and a document without it is invisible to
        # every corpus query.
        managed = kb_session.merge(document)
        kb_session.add_all([version, chunk])
        managed.current_version_id = version_id
        kb_session.flush()
        return document_id, version_id

    return _seed


@pytest.fixture
def enqueue_job(kb_session):
    """Factory registering a corpus job under the current algorithm hash."""
    from kbsvc.config import get_settings
    from kbsvc.plagiarism import repository as repo

    def _enqueue(document_id, version_id):
        return repo.enqueue_corpus_job(
            kb_session,
            tenant_id=TENANT,
            document_id=document_id,
            version_id=version_id,
            algorithm_config_hash=get_settings().plagiarism_algorithm_config_hash,
            max_attempts=3,
        )

    return _enqueue


@pytest.fixture
def build_corpus(kb_session, seed_document, enqueue_job):
    """Factory that seeds a document and builds its projection in one step."""
    from kbsvc.plagiarism.projection import ProjectionBuilder

    def _build(*, text=BODY, document_id=None):
        document_id, version_id = seed_document(text=text, document_id=document_id)
        ProjectionBuilder().build(kb_session, enqueue_job(document_id, version_id).id)
        return document_id

    return _build


@pytest.fixture
def app_db_on_test_postgres(pg_engine, monkeypatch):
    """Point the *global* engine at the test database for one test.

    `PlagiarismWorker` opens its own sessions through `session_scope()` rather
    than taking one, because in production it runs in its own process. Tests
    that exercise the worker therefore need the application engine repointed.

    Deliberately scoped to the tests that need it instead of setting
    `KB_DATABASE_URL` for the whole run: that env var makes the entire suite -
    including the pure algorithm tests - depend on a reachable server, turning
    the designed skip into 120 hard errors when the database is down.
    """
    from kbsvc.config import reset_settings_cache
    from kbsvc.db.session import get_engine, init_db, reset_engine_cache

    monkeypatch.setenv("KB_DATABASE_URL", str(pg_engine.url.render_as_string(hide_password=False)))
    reset_settings_cache()
    reset_engine_cache()
    init_db()
    yield get_engine()
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture
def pg_session(pg_schema):
    """A session rolled back after each test, so cases cannot leak into each other."""
    from kbsvc.plagiarism.models import (
        PlagCheck,
        PlagCheckEvent,
        PlagCheckPassage,
        PlagCheckSource,
        PlagCorpusChunk,
        PlagCorpusJob,
        PlagCorpusProjection,
        PlagFingerprintDf,
        PlagWorkerHeartbeat,
    )

    session = Session(bind=pg_schema, expire_on_commit=False, future=True)
    try:
        yield session
    finally:
        session.rollback()
        # Advisory locks and SKIP LOCKED tests commit on purpose, so a rollback
        # alone is not enough to isolate cases.
        for model in (
            PlagCheckPassage,
            PlagCheckSource,
            PlagCheckEvent,
            PlagCheck,
            PlagCorpusChunk,
            PlagCorpusProjection,
            PlagCorpusJob,
            PlagFingerprintDf,
            PlagWorkerHeartbeat,
        ):
            session.query(model).delete()
        session.commit()
        session.close()
