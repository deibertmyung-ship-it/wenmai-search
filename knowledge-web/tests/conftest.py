"""Fixtures. The backend is always mocked - kbweb has no business talking to a
real kbsvc in its own test suite."""

from __future__ import annotations

import pytest
from kbweb import create_app
from kbweb.config import Config

API_BASE = "http://kbsvc.test"


@pytest.fixture
def config() -> Config:
    return Config(
        api_base=API_BASE,
        api_key="kb_test_key",
        timeout=5,
        page_size=10,
        reader_page_size=3,
        debug_ui=True,
        secret_key="test-secret",
        max_content_length=1024 * 1024,
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
