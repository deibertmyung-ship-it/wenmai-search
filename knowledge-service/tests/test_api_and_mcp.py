"""End-to-end tests through the two public surfaces: REST and MCP."""

from __future__ import annotations

import json

import pytest

BOOK = (
    "# 六壬指南\n\n"
    "## 卷一 贼克\n\n"
    "贼克者，取用之首法也。上克下为贼，下贼上为克。凡四课之中，有一下贼上者，即取之为用神。\n\n"
    "## 卷二 涉害\n\n"
    "涉害者，比用不成则涉害。涉害深者为用，历克多者为深。\n"
)


@pytest.fixture(scope="module")
def indexed_corpus(api_client):
    """Register one document through the API and drain the queue."""
    from kbsvc.ingest.worker import IngestWorker

    source = api_client.post(
        "/v1/sources", json={"name": "api-corpus", "kind": "upload"}
    ).json()
    response = api_client.post(
        "/v1/ingest/upload",
        data={"source_id": source["id"], "external_id": "api/liuren.md", "title": "六壬指南"},
        files={"file": ("liuren.md", BOOK.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 202, response.text
    registration = response.json()
    IngestWorker().drain()
    return {"source": source, **registration}


# --- ops ----------------------------------------------------------------


def test_healthz_and_readyz(api_client):
    assert api_client.get("/healthz").json()["status"] == "ok"
    assert api_client.get("/readyz").json()["database"] == "ok"


# --- sources ------------------------------------------------------------


def test_create_and_list_sources(api_client):
    created = api_client.post("/v1/sources", json={"name": "src-list", "kind": "filesystem"})
    assert created.status_code == 201
    names = [item["name"] for item in api_client.get("/v1/sources").json()]
    assert "src-list" in names


def test_creating_the_same_source_twice_is_idempotent(api_client):
    first = api_client.post("/v1/sources", json={"name": "src-idem"}).json()
    second = api_client.post("/v1/sources", json={"name": "src-idem"}).json()
    assert first["id"] == second["id"]


# --- ingestion ----------------------------------------------------------


def test_upload_returns_accepted_with_a_job(indexed_corpus):
    assert indexed_corpus["job_id"]
    assert indexed_corpus["state"] == "pending"
    assert indexed_corpus["deduplicated"] is False


def test_upload_rejects_an_unknown_source(api_client):
    response = api_client.post(
        "/v1/ingest/upload",
        data={"source_id": "does-not-exist"},
        files={"file": ("a.md", b"x", "text/markdown")},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_ingest_path_rejects_a_missing_directory(api_client, indexed_corpus):
    response = api_client.post(
        "/v1/ingest/path",
        json={"source_id": indexed_corpus["source"]["id"], "path": "/no/such/dir"},
    )
    assert response.status_code == 400


def test_job_can_be_inspected_after_completion(api_client, indexed_corpus):
    job = api_client.get(f"/v1/jobs/{indexed_corpus['job_id']}").json()
    assert job["state"] == "completed"
    assert job["attempts"] >= 1


def test_retry_rejects_a_completed_job(api_client, indexed_corpus):
    response = api_client.post(f"/v1/jobs/{indexed_corpus['job_id']}/retry")
    assert response.status_code == 400


# --- documents ----------------------------------------------------------


def test_document_listing_and_detail(api_client, indexed_corpus):
    documents = api_client.get("/v1/documents").json()
    assert any(doc["id"] == indexed_corpus["document_id"] for doc in documents)

    detail = api_client.get(f"/v1/documents/{indexed_corpus['document_id']}").json()
    assert detail["title"] == "六壬指南"
    assert detail["chunk_count"] > 0
    assert detail["version_count"] >= 1


def test_chunks_are_returned_in_order_with_provenance(api_client, indexed_corpus):
    chunks = api_client.get(
        f"/v1/documents/{indexed_corpus['document_id']}/chunks", params={"limit": 50}
    ).json()
    assert chunks
    assert [chunk["ordinal"] for chunk in chunks] == sorted(c["ordinal"] for c in chunks)
    assert all(chunk["content_hash"] for chunk in chunks)
    assert any(chunk["heading_path"] for chunk in chunks)


def test_unknown_document_returns_the_error_envelope(api_client):
    response = api_client.get("/v1/documents/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert set(response.json()["error"]) == {"code", "message", "detail"}


# --- search -------------------------------------------------------------


def test_hybrid_search_finds_the_indexed_passage(api_client, indexed_corpus):
    payload = api_client.post(
        "/v1/search", json={"query": "贼克如何取用神", "top_k": 5}
    ).json()
    assert payload["results"]
    top = payload["results"][0]
    assert top["document_id"] == indexed_corpus["document_id"]
    assert "贼克" in top["snippet"]
    assert top["chunk_id"] and top["source_uri"]
    assert top["rerank_score"] is not None


@pytest.mark.parametrize("mode", ["hybrid", "dense", "sparse"])
def test_every_retrieval_mode_returns_results(api_client, indexed_corpus, mode):
    payload = api_client.post(
        "/v1/search", json={"query": "涉害", "top_k": 3, "mode": mode}
    ).json()
    assert payload["results"], f"mode {mode} returned nothing"


def test_debug_mode_exposes_the_full_retrieval_trace(api_client, indexed_corpus):
    payload = api_client.post(
        "/v1/search", json={"query": "涉害深者为用", "top_k": 3, "debug": True}
    ).json()
    debug = payload["debug"]
    assert set(debug["retrievers"]) == {"dense", "sparse"}
    assert debug["fusion"]["method"] == "rrf"
    assert debug["filter"]["current_only"] is True
    assert debug["timings_ms"]["total"] >= 0
    assert debug["rewritten_queries"][0] == "涉害深者为用"


def test_document_filter_narrows_results(api_client, indexed_corpus):
    payload = api_client.post(
        "/v1/search",
        json={
            "query": "贼克",
            "filters": {"document_ids": ["00000000-0000-0000-0000-000000000000"]},
        },
    ).json()
    assert payload["results"] == []


def test_heading_filter_selects_a_single_chapter(api_client, indexed_corpus):
    payload = api_client.post(
        "/v1/search", json={"query": "涉害", "filters": {"heading_contains": "卷二"}}
    ).json()
    assert payload["results"]
    assert all("卷二" in " ".join(r["heading_path"]) for r in payload["results"])


def test_empty_query_is_rejected_by_validation(api_client):
    assert api_client.post("/v1/search", json={"query": ""}).status_code == 422


# --- stats --------------------------------------------------------------


def test_stats_reports_consistent_counters(api_client, indexed_corpus):
    stats = api_client.get("/v1/stats").json()
    assert stats["documents"] >= 1
    assert stats["chunks"] >= 1
    assert stats["vector_points"] >= stats["chunks"] or stats["vector_points"] == -1


# --- MCP ----------------------------------------------------------------


def _call(tool):
    """MCP tools may be wrapped by the SDK; reach the underlying function."""
    return getattr(tool, "fn", getattr(tool, "__wrapped__", tool))


def test_mcp_search_returns_citations(indexed_corpus):
    from kbsvc.mcp.server import search_knowledge

    output = _call(search_knowledge)("贼克如何取用神", top_k=3)
    assert "cite:" in output
    assert indexed_corpus["document_id"] in output

    body = json.loads(output.split("```json")[1].split("```")[0])
    assert body["results"][0]["chunk_id"]


def test_mcp_fetch_document_chunks_reads_in_order(indexed_corpus):
    from kbsvc.mcp.server import fetch_document_chunks

    output = _call(fetch_document_chunks)(indexed_corpus["document_id"], from_ordinal=0, limit=3)
    assert output.startswith("# 六壬指南")
    assert "--- #0" in output


def test_mcp_fetch_rejects_an_unknown_document():
    from kbsvc.mcp.server import fetch_document_chunks

    assert "not found" in _call(fetch_document_chunks)("nope")


def test_mcp_list_sources_includes_registered_sources(indexed_corpus):
    from kbsvc.mcp.server import list_sources

    output = _call(list_sources)()
    assert indexed_corpus["source"]["name"] in output


def test_mcp_exposes_only_read_only_tools():
    import kbsvc.mcp.server as server

    exported = {
        name
        for name in dir(server)
        if not name.startswith("_") and callable(getattr(server, name))
    }
    forbidden = {"delete_document", "upload_document", "reindex", "ingest"}
    assert not (exported & forbidden)


# --- reindex ------------------------------------------------------------


def test_reindex_rebuilds_from_stored_bytes_without_reupload(api_client, indexed_corpus):
    from kbsvc.ingest.worker import IngestWorker

    document_id = indexed_corpus["document_id"]
    before = api_client.get(f"/v1/documents/{document_id}").json()

    response = api_client.post(f"/v1/documents/{document_id}/reindex")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    IngestWorker().drain()

    assert api_client.get(f"/v1/jobs/{job_id}").json()["state"] == "completed"
    after = api_client.get(f"/v1/documents/{document_id}").json()
    assert after["chunk_count"] == before["chunk_count"]
    assert after["version_count"] == before["version_count"]

    # the index still answers after a rebuild
    payload = api_client.post("/v1/search", json={"query": "贼克", "top_k": 3}).json()
    assert payload["results"]


def test_reindex_of_an_unknown_document_is_rejected(api_client):
    response = api_client.post("/v1/documents/00000000-0000-0000-0000-000000000000/reindex")
    assert response.status_code == 404


def test_search_timings_reconcile(api_client, indexed_corpus):
    """Sub-timings must add up to `search`, or the debug output misleads."""
    payload = api_client.post(
        "/v1/search", json={"query": "贼克", "top_k": 3, "debug": True}
    ).json()
    timings = payload["debug"]["timings_ms"]

    parts = timings["store_init"] + timings["dense_search"] + timings["sparse_search"]
    assert parts <= timings["search"] + 1.0
    assert timings["search"] - parts < max(timings["search"] * 0.2, 5.0)
    assert timings["total"] >= timings["search"]
