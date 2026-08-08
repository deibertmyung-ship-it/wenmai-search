"""Fixtures. The backend is always mocked - kbweb has no business talking to a
real kbsvc in its own test suite."""

from __future__ import annotations

import pytest
from kbweb import create_app
from kbweb.config import Config

API_BASE = "http://kbsvc.test"


@pytest.fixture(params=[False, True], ids=["js", "nojs"])
def config(request) -> Config:
    """Every view test runs twice: once normally, once under KBWEB_NOJS.

    kbweb promises the whole site works with no JavaScript. A promise that
    broad is only worth something if every route carries it, so it is a
    dimension of the suite rather than one spot-check in the e2e tests -
    that is how the debug drawer's `hidden` attribute got caught.
    """
    return Config(
        api_base=API_BASE,
        api_key="kb_test_key",
        timeout=5,
        page_size=10,
        reader_page_size=3,
        debug_ui=True,
        secret_key="test-secret",
        # 8 MiB, not 1: the oversized-plagiarism-input test posts 500,001
        # url-encoded CJK characters (~4.3MB on the wire once percent-encoded),
        # and it wants the *view's* char-count rejection to fire, not Flask's
        # transport-level 413 firing first and hiding it.
        max_content_length=8 * 1024 * 1024,
        nojs=request.param,
    )


@pytest.fixture
def app(config):
    application = create_app(config)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# --- backend payloads ---------------------------------------------------


@pytest.fixture
def sources_payload() -> list[dict]:
    return [
        {
            "id": "src-1",
            "name": "guji",
            "kind": "filesystem",
            "uri": "D:/books",
            "config": {},
            "document_count": 2,
        }
    ]


@pytest.fixture
def search_payload() -> dict:
    return {
        "query": "贼克如何取用神",
        "results": [
            {
                "chunk_id": "chunk-aaaa-bbbb",
                "score": 0.031_25,
                "rerank_score": 0.811_47,
                "document_id": "doc-1111-2222",
                "version": 1,
                "chunk_ordinal": 25,
                "kind": "text",
                "title": "六壬指南",
                "heading_path": ["卷一", "总论"],
                "page": None,
                "source_uri": "file:///books/liuren.txt",
                "snippet": "贼克者，取用之首法也。",
                "highlights": [[0, 2]],
            }
        ],
        "debug": {
            "rewritten_queries": ["贼克如何取用神", "贼克"],
            "retrievers": {
                "dense": [{"id": "chunk-aaaa-bbbb", "score": 0.83, "rank": 1}],
                "sparse": [{"id": "chunk-aaaa-bbbb", "score": 5.1, "rank": 1}],
            },
            "fusion": {
                "method": "rrf",
                "k": 60,
                "weights": {"dense": 1.0, "sparse": 1.0},
                "order": [
                    {
                        "id": "chunk-aaaa-bbbb",
                        "score": 0.032_78,
                        "from": {"dense": 1, "sparse": 1},
                    }
                ],
            },
            "filter": {"tenant_id": "default", "current_only": True},
            "timings_ms": {"rewrite": 0.2, "search": 12.0, "total": 21.2},
        },
    }


@pytest.fixture
def document_payload() -> dict:
    return {
        "id": "doc-1111-2222",
        "source_id": "src-1",
        "external_id": "liuren.txt",
        "title": "六壬指南",
        "acl": ["public"],
        "current_version_id": "ver-1",
        "version_count": 1,
        "chunk_count": 42,
        "created_at": "2026-08-01T10:00:00",
        "updated_at": "2026-08-01T10:00:00",
    }


@pytest.fixture
def chunks_payload() -> list[dict]:
    return [
        {
            "chunk_id": f"chunk-{index}",
            "document_id": "doc-1111-2222",
            "version_id": "ver-1",
            "ordinal": index,
            "kind": "text",
            "text": f"第 {index} 段：贼克者，取用之首法也。",
            "token_count": 20,
            "char_start": index * 100,
            "char_end": index * 100 + 20,
            "page_from": None,
            "page_to": None,
            "heading_path": ["卷一"],
            "content_hash": "abc",
        }
        for index in range(3)
    ]


@pytest.fixture
def jobs_payload() -> list[dict]:
    return [
        {
            "id": "job-ok",
            "document_id": "doc-1111-2222",
            "version_id": "ver-1",
            "job_type": "ingest",
            "state": "completed",
            "attempts": 1,
            "max_attempts": 3,
            "last_error": None,
            "scheduled_at": "2026-08-01T10:00:00",
            "created_at": "2026-08-01T10:00:00",
            "finished_at": "2026-08-01T10:01:00",
        },
        {
            "id": "job-bad",
            "document_id": "doc-3333",
            "version_id": "ver-2",
            "job_type": "ingest",
            "state": "failed",
            "attempts": 3,
            "max_attempts": 3,
            "last_error": "ParserError: no parser could handle x.pdf",
            "scheduled_at": "2026-08-01T10:00:00",
            "created_at": "2026-08-01T10:00:00",
            "finished_at": "2026-08-01T10:02:00",
        },
    ]


@pytest.fixture
def corpus_ready_payload() -> dict:
    return {
        "total_documents": 201,
        "ready_documents": 201,
        "pending_documents": 0,
        "failed_documents": 0,
        "algorithm_config_hash": "cfg-abc123",
        "is_ready": True,
    }


@pytest.fixture
def corpus_pending_payload() -> dict:
    return {
        "total_documents": 1580,
        "ready_documents": 1203,
        "pending_documents": 377,
        "failed_documents": 0,
        "algorithm_config_hash": "cfg-abc123",
        "is_ready": False,
    }


@pytest.fixture
def checks_payload() -> list[dict]:
    return [
        {
            "check_id": "chk-done",
            "status": "completed",
            "created_at": "2026-08-08T10:00:00",
            "snapshot_at": "2026-08-08T10:00:00",
            "algorithm_config_hash": "cfg-abc123",
            "source_document_id": None,
            "query_chars": 1000,
            "matched_chars": 234,
        },
        {
            "check_id": "chk-live",
            "status": "running",
            "created_at": "2026-08-08T10:05:00",
            "snapshot_at": "2026-08-08T10:05:00",
            "algorithm_config_hash": "cfg-abc123",
            "source_document_id": "doc-1111-2222",
            "query_chars": 0,
            "matched_chars": 0,
        },
    ]
