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
