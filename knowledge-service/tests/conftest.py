"""Test fixtures.

Everything runs against a throwaway data dir: SQLite + local object store +
embedded Qdrant. No network, no Docker, no model downloads.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

TEST_ROOT = Path(__file__).resolve().parent / ".pytest-kbdata"


@pytest.fixture(scope="session", autouse=True)
def _isolated_environment() -> None:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)

    os.environ.update(
        {
            "KB_DATA_DIR": str(TEST_ROOT),
            "KB_PROFILE": "local",
            "KB_DEFAULT_TENANT": "test",
            "KB_DENSE_PROVIDER": "hash",
            "KB_DENSE_DIM": "128",
            "KB_CHUNK_TARGET_TOKENS": "120",
            "KB_CHUNK_OVERLAP_TOKENS": "12",
            "KB_CHUNK_MIN_CHARS": "20",
            "KB_WORKER_MAX_ATTEMPTS": "2",
            "KB_WORKER_BACKOFF_BASE": "0",
            "KB_AUTH_REQUIRED": "false",
            "KB_QDRANT_COLLECTION": "kb_test_chunks",
        }
    )

    from kbsvc.config import reset_settings_cache
    from kbsvc.db.session import init_db, reset_engine_cache
    from kbsvc.embedding import reset_embedder_cache
    from kbsvc.storage import reset_object_store_cache
    from kbsvc.vector import reset_vector_store

    reset_settings_cache()
    reset_engine_cache()
    reset_object_store_cache()
    reset_embedder_cache()
    reset_vector_store()
    init_db()

    yield

    reset_vector_store()


@pytest.fixture
def settings():
    from kbsvc.config import get_settings

    return get_settings()


@pytest.fixture
def tenant(settings) -> str:
    return settings.default_tenant


@pytest.fixture
def session():
    from kbsvc.db.session import session_scope

    with session_scope() as db_session:
        yield db_session


@pytest.fixture
def source(session, tenant):
    from kbsvc.db import repo

    return repo.upsert_source(session, tenant_id=tenant, name="pytest-source", kind="upload")


@pytest.fixture
def worker():
    from kbsvc.ingest.worker import IngestWorker

    return IngestWorker()


@pytest.fixture(scope="session")
def api_client():
    from fastapi.testclient import TestClient

    from kbsvc.api.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def sample_markdown() -> str:
    return (
        "# 六壬指南\n\n"
        "卷首总说，此为引言部分，用以说明全书主旨与体例。\n\n"
        "## 卷一\n\n"
        "贼克者，取用之首法也。上克下为贼，下贼上为克。\n"
        "凡四课之中，有一下贼上者，即取之为用神。\n\n"
        "## 卷二\n\n"
        "涉害者，比用不成则涉害。涉害深者为用。\n"
    )
