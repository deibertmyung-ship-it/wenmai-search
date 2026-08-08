"""Deterministic kbsvc stand-in for E2E runs.

Mirrors the real API's response shapes (see knowledge-service/docs/03-api.md).
Payloads are fixed so assertions about ordering, highlighting and pagination
describe frontend behaviour rather than corpus drift.
"""

from __future__ import annotations

SOURCES = [
    {
        "id": "src-guji",
        "name": "guji",
        "kind": "filesystem",
        "uri": "book/",
        "config": {},
        "document_count": 202,
    },
    {
        "id": "src-notes",
        "name": "notes",
        "kind": "upload",
        "uri": "",
        "config": {},
        "document_count": 3,
    },
]

DOC_ID = "5f26adf6-33c6-5e13-9cad-e67f51846400"
TOTAL_CHUNKS = 30

HIT_TERM = "贼克"

# Plagiarism requests may construct a fresh FakeKbClient, so the state that
# survives an SSE request and the following browser reload lives at module
# scope. E2E tests reset this dictionary before each journey.
_CHECK_STATE = {
    "check_id": "chk-e2e",
    "done": False,
    "fallback_ready": False,
    "stream_fail": False,
}

QUERY_TEXT = "夫天地者，万物之逆旅也。古人秉烛夜游。"

PLAG_REPORT = {
    "check_id": "chk-e2e",
    "status": "completed",
    "snapshot_at": "2026-08-08T10:00:00",
    "algorithm_config_hash": "cfg-e2e",
    "matcher_version": "seed-extend-v2",
    "matcher_config": {
        "resolved_language": "zh",
        "profile": "zh",
        "effective_chars": 20,
        "min_seed_len": 12,
        "min_passage_len": 20,
        "normalizer_version": "plag-normalizer-v2",
    },
    "query_chars": len(QUERY_TEXT),
    "matched_chars": 11,
    "checked_chunks": 4,
    "total_chunks": 4,
    "coverage_reason": None,
    "is_complete": True,
    "query_text": QUERY_TEXT,
    "sources": [
        {
            "document_id": "doc-1111-2222",
            "version_id": "ver-1",
            "version": 1,
            "content_hash": "abc",
            "title": "春夜宴从弟桃花园序",
            "matched_chars": 11,
            "score": 0.94,
            "passages": [
                {
                    "query_start": 0,
                    "query_end": 11,
                    "source_start": 0,
                    "source_end": 11,
                    "score": 0.94,
                    "preview": "夫天地者，万物之逆旅也",
                }
            ],
        }
    ],
    "unique_passages": [[0, 11]],
}


def _offsets(snippet: str, term: str = HIT_TERM) -> list[list[int]]:
    """Derive highlight spans the way the backend does, so they always line up."""
    spans: list[list[int]] = []
    start = snippet.find(term)
    while start != -1:
        spans.append([start, start + len(term)])
        start = snippet.find(term, start + len(term))
    return spans


_RESULTS = [
    (
        "六壬存验-清-吴师青",
        ["一、断例"],
        0.015625,
        0.81147,
        "…比用者，因上克下，有多下贼上，不成重审，又不成元首，贼克纷纷，难以取用，则寻比…",
        25,
    ),
    (
        "六壬灵觉经--佚名",
        ["卷二", "涉害"],
        0.016393,
        0.79568,
        "…贼克不成，则入涉害课，卯加辛，戌是季也，未加寅是孟也，当以孟上神为用…",
        41,
    ),
    (
        "六壬指南注解-明-陈公献",
        ["卷一", "总论"],
        0.014925,
        0.77102,
        "凡四课之中，有一下贼上者，即取之为用神。上克下为贼，下贼上为克，贼克者取用之首法也。",
        3,
    ),
]

_DENSE_ORDER = list(range(6))
_SPARSE_ORDER = [1, 0, 2, 4, 3, 5]


def _rank_in(order: list[int], chunk: int) -> int:
    return order.index(chunk) + 1


SEARCH_PAYLOAD = {
    "query": "贼克如何取用神",
    "results": [
        {
            "chunk_id": f"chunk-{i}",
            "score": score,
            "rerank_score": rerank,
            "document_id": DOC_ID if i == 0 else f"{DOC_ID[:-1]}{i}",
            "version": 1,
            "chunk_ordinal": ordinal,
            "kind": "text",
            "title": title,
            "heading_path": headings,
            "page": None,
            "source_uri": f"file:///book/{title}.txt",
            "snippet": snippet,
            "highlights": _offsets(snippet),
        }
        for i, (title, headings, score, rerank, snippet, ordinal) in enumerate(_RESULTS)
    ],
    "debug": {
        "rewritten_queries": ["贼克如何取用神", "贼克"],
        "retrievers": {
            "dense": [
                {"id": f"chunk-{i}", "score": 0.83 - n * 0.04, "rank": n + 1}
                for n, i in enumerate(_DENSE_ORDER)
            ],
            "sparse": [
                {"id": f"chunk-{i}", "score": 5.1 - n * 0.3, "rank": n + 1}
                for n, i in enumerate(_SPARSE_ORDER)
            ],
        },
        "fusion": {
            "method": "rrf",
            "k": 60,
            "weights": {"dense": 1.0, "sparse": 1.0},
            "order": [
                {
                    "id": f"chunk-{i}",
                    "score": 0.0328 - i * 0.001,
                    "from": {
                        "dense": _rank_in(_DENSE_ORDER, i),
                        "sparse": _rank_in(_SPARSE_ORDER, i),
                    },
                }
                for i in range(3)
            ],
        },
        "filter": {"tenant_id": "default", "current_only": True, "acl_any": None},
        "timings_ms": {
            "rewrite": 0.03,
            "search": 5762.0,
            "store_init": 0.0,
            "dense_search": 362.0,
            "sparse_search": 5393.0,
            "fuse": 0.09,
            "rerank": 6.1,
            "total": 5768.2,
        },
    },
}

DOCUMENT = {
    "id": DOC_ID,
    "source_id": "src-guji",
    "external_id": "六壬/六壬存验-清-吴师青.txt",
    "title": "六壬存验-清-吴师青",
    "acl": ["public"],
    "current_version_id": "ver-1",
    "version_count": 1,
    "chunk_count": TOTAL_CHUNKS,
    "created_at": "2026-08-01T19:00:00",
    "updated_at": "2026-08-01T19:00:00",
}

JOBS = [
    {
        "id": "job-done",
        "document_id": DOC_ID,
        "version_id": "ver-1",
        "job_type": "ingest",
        "state": "completed",
        "attempts": 1,
        "max_attempts": 3,
        "last_error": None,
        "scheduled_at": "2026-08-01T19:00:00",
        "created_at": "2026-08-01T19:00:00",
        "finished_at": "2026-08-01T19:01:00",
    },
    {
        "id": "job-failed",
        "document_id": "aaaabbbb-cccc-dddd-eeee-ffff00001111",
        "version_id": "ver-2",
        "job_type": "ingest",
        "state": "failed",
        "attempts": 3,
        "max_attempts": 3,
        "last_error": "ParserError: no parser could handle scan.pdf",
        "scheduled_at": "2026-08-01T19:00:00",
        "created_at": "2026-08-01T19:00:00",
        "finished_at": "2026-08-01T19:02:00",
    },
]

STATS = {
    "tenant_id": "default",
    "sources": 2,
    "documents": 202,
    "versions": 202,
    "chunks": 22350,
    "vector_points": 22350,
    "jobs_by_state": {"completed": 202},
}


def _chunk(ordinal: int, document_id: str = DOC_ID, version: int = 1) -> dict:
    return {
        "chunk_id": f"chunk-ord-{ordinal}",
        "document_id": document_id,
        "version_id": f"ver-{version}",
        "ordinal": ordinal,
        "kind": "text",
        "text": (
            f"第{ordinal}段。贼克者，取用之首法也。"
            "上克下为贼，下贼上为克，凡四课之中有一下贼上者即取之为用神。"
        ),
        "token_count": 40,
        "char_start": ordinal * 120,
        "char_end": ordinal * 120 + 45,
        "page_from": None,
        "page_to": None,
        "heading_path": ["一、断例"] if ordinal % 2 == 0 else [],
        "content_hash": f"hash{ordinal}",
    }


class FakeKbClient:
    """Implements the surface of kbweb.client.KbClient used by the views."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass

    def list_sources(self) -> list[dict]:
        return SOURCES

    def create_source(self, name: str, kind: str = "upload", uri: str = "") -> dict:
        return {
            "id": "src-new",
            "name": name,
            "kind": kind,
            "uri": uri,
            "config": {},
            "document_count": 0,
        }

    def search(self, query, **kwargs) -> dict:
        payload = {**SEARCH_PAYLOAD, "query": query}
        if not kwargs.get("debug"):
            payload = {k: v for k, v in payload.items() if k != "debug"}
        if query.strip() == "nothing":
            payload = {"query": query, "results": []}
        return payload

    def list_documents(self, **kwargs) -> list[dict]:
        query = (kwargs.get("q") or "").strip()
        if query and query not in DOCUMENT["title"]:
            return []
        return [DOCUMENT]

    def get_document(self, document_id: str) -> dict:
        return {**DOCUMENT, "id": document_id}

    def get_chunks(
        self,
        document_id: str,
        *,
        from_ordinal: int = 0,
        limit: int = 20,
        version: int | None = None,
    ) -> list[dict]:
        end = min(from_ordinal + limit, TOTAL_CHUNKS)
        return [
            _chunk(i, document_id=document_id, version=version or 1)
            for i in range(from_ordinal, end)
        ]

    def reindex_document(self, document_id: str) -> dict:
        return {"job_id": "job-reindex", "document_id": document_id, "state": "pending"}

    def delete_document(self, document_id: str) -> dict:
        return {"job_id": "job-delete", "document_id": document_id, "state": "pending"}

    def upload(self, **kwargs) -> dict:
        return {
            "document_id": DOC_ID,
            "version_id": "ver-9",
            "job_id": "job-upload",
            "state": "pending",
            "deduplicated": False,
        }

    def ingest_path(self, **kwargs) -> dict:
        return {"registered": 12, "deduplicated": 3, "failed": 0, "items": [], "errors": []}

    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[dict]:
        rows = JOBS if not state else [job for job in JOBS if job["state"] == state]
        return rows[:limit]

    def retry_job(self, job_id: str) -> dict:
        return {**JOBS[1], "state": "pending"}

    def stats(self) -> dict:
        return STATS

    def health(self) -> dict:
        return {"status": "ok", "profile": "local"}

    # --- plagiarism -----------------------------------------------------

    def corpus_status(self) -> dict:
        return {
            "total_documents": 1,
            "ready_documents": 1,
            "pending_documents": 0,
            "failed_documents": 0,
            "algorithm_config_hash": "cfg-e2e",
            "is_ready": True,
        }

    def create_text_check(self, **_kwargs) -> dict:
        _CHECK_STATE["done"] = False
        _CHECK_STATE["fallback_ready"] = False
        return {"check_id": _CHECK_STATE["check_id"], "status": "pending"}

    def create_document_check(self, _document_id: str, **_kwargs) -> dict:
        return self.create_text_check()

    def list_checks(self, **_kwargs) -> list[dict]:
        return []

    def get_check(self, _check_id: str) -> dict:
        done = bool(_CHECK_STATE["done"] or _CHECK_STATE["fallback_ready"])
        return {
            "check_id": _CHECK_STATE["check_id"],
            "status": "completed" if done else "running",
            "created_at": "2026-08-08T10:00:00",
            "snapshot_at": "2026-08-08T10:00:00",
            "algorithm_config_hash": "cfg-e2e",
            "source_document_id": None,
            "query_chars": PLAG_REPORT["query_chars"],
            "matched_chars": PLAG_REPORT["matched_chars"] if done else 0,
        }

    def get_plag_report(self, _check_id: str) -> dict:
        return {**PLAG_REPORT, "check_id": _CHECK_STATE["check_id"]}

    def delete_check(self, _check_id: str) -> int:
        return 204

    def stream_progress(self, _check_id: str, **_kwargs):
        return _FakeSseResponse(should_fail=bool(_CHECK_STATE["stream_fail"]))


class _FakeSseResponse:
    """Small context-managed response matching httpx's streaming surface."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def iter_lines(self):
        if self.should_fail:
            # The proxy returns a short-lived 200 stream. EventSource observes
            # the close, retries, and the browser's bounded fallback reload
            # then reads the completed state below.
            _CHECK_STATE["fallback_ready"] = True
            return

        progress = 'id: 1\nevent: retrieving\ndata: {"progress": 0.6}\n\n'
        terminal = 'id: 2\nevent: completed\ndata: {"progress": 1.0}\n\n'
        yield from progress.splitlines()
        _CHECK_STATE["done"] = True
        yield from terminal.splitlines()
