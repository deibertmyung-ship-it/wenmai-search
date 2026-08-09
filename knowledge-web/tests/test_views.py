"""View tests. The backend is stubbed with respx so nothing hits the network."""

from __future__ import annotations

import httpx
import pytest
import respx

from .conftest import API_BASE


def html(response) -> str:
    return response.data.decode("utf-8")


# --- search -------------------------------------------------------------


@respx.mock
def test_landing_page_renders_without_a_query(client, sources_payload, document_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    response = client.get("/")
    assert response.status_code == 200
    body = html(response)
    assert "在典籍中查找" in body
    assert "guji" in body  # source filter populated
    assert 'class="workspace workspace--search"' in body
    # The primary action must be reachable before the long advanced-filter
    # list; this is the regression that prompted the sidebar redesign.
    assert body.index("search-rail__submit") < body.index("范围与策略")
    assert '<details class="rail-disclosure">' in body
    assert '<details class="rail-disclosure" open>' not in body


@respx.mock
def test_search_renders_results_with_citations(
    client, sources_payload, search_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(return_value=httpx.Response(200, json=search_payload))

    response = client.get("/?q=贼克如何取用神")
    body = html(response)
    assert response.status_code == 200
    assert "六壬指南" in body
    assert "0.0312" in body               # score, formatted
    assert "doc-1111#25" in body          # citation shorthand
    assert "/read/doc-1111-2222" in body  # deep link into the reader


@respx.mock
def test_hits_are_marked_from_backend_offsets(
    client, sources_payload, search_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(return_value=httpx.Response(200, json=search_payload))

    body = html(client.get("/?q=贼克"))
    assert "<mark>贼克</mark>" in body


@respx.mock
def test_search_forwards_filters_and_flags_to_the_backend(
    client, sources_payload, search_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    route = respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=search_payload)
    )

    client.get("/?f=1&q=涉害&mode=sparse&source_id=src-1&document_id=doc-1&top_k=7&debug=1")
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["mode"] == "sparse"
    assert body["top_k"] == 7
    assert body["debug"] is True
    assert body["filters"]["source_ids"] == ["src-1"]
    assert body["filters"]["document_ids"] == ["doc-1"]
    # unchecked checkboxes must read as false, not as "absent means default"
    assert body["rerank"] is False
    assert body["rewrite"] is False


@respx.mock
def test_bare_link_without_the_form_marker_keeps_the_defaults_on(
    client, sources_payload, search_payload, document_payload
):
    """A shared /?q=... URL must still rerank and rewrite."""
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    route = respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=search_payload)
    )
    client.get("/?q=贼克")
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["rerank"] is True
    assert body["rewrite"] is True


@respx.mock
def test_invalid_mode_falls_back_to_hybrid(
    client, sources_payload, search_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    route = respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=search_payload)
    )
    client.get("/?q=x&mode=evil")
    import json

    assert json.loads(route.calls.last.request.content)["mode"] == "hybrid"


@respx.mock
def test_debug_drawer_shows_both_retriever_runs(
    client, sources_payload, search_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(return_value=httpx.Response(200, json=search_payload))

    body = html(client.get("/?q=贼克&debug=1"))
    assert "检索轨迹" in body
    assert "dense" in body and "sparse" in body
    assert "RRF" in body


@respx.mock
def test_debug_ui_can_be_disabled(app, config, sources_payload, search_payload, document_payload):
    config.debug_ui = False
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    route = respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json=search_payload)
    )
    app.test_client().get("/?q=贼克&debug=1")
    import json

    assert json.loads(route.calls.last.request.content)["debug"] is False


@respx.mock
def test_empty_result_set_gets_a_real_empty_state(client, sources_payload, document_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"query": "x", "results": []})
    )
    body = html(client.get("/?q=zzzz"))
    assert "未检索到相关段落" in body


# --- auth forwarding ----------------------------------------------------


@respx.mock
def test_api_key_is_sent_to_the_backend_but_never_to_the_browser(
    client, sources_payload, document_payload
):
    route = respx.get(f"{API_BASE}/v1/sources").mock(
        return_value=httpx.Response(200, json=sources_payload)
    )
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    response = client.get("/")
    assert route.calls.last.request.headers["authorization"] == "Bearer kb_test_key"
    assert "kb_test_key" not in html(response)


# --- reader -------------------------------------------------------------


@respx.mock
def test_reader_renders_chunks_in_order(client, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/read/doc-1111-2222"))
    assert "六壬指南" in body
    assert 'id="c0"' in body and 'id="c2"' in body
    assert body.index('id="c0"') < body.index('id="c2"')


@respx.mock
def test_reader_marks_the_focused_chunk(client, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/read/doc-1111-2222?focus=1"))
    assert 'data-focus="true"' in body


@respx.mock
def test_reader_keeps_a_no_js_next_page_link(client, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/read/doc-1111-2222"))
    assert "from=3" in body  # page_size is 3 in the test config


@respx.mock
def test_reader_renders_a_historical_passage_mark(client, document_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    passage = {
        "document_id": "doc-1111-2222",
        "version_id": "ver-3",
        "version": 3,
        "requested_start": 102,
        "requested_end": 106,
        "focus_ordinal": 2,
        "from_ordinal": 1,
        "next_from": 4,
        "has_more": False,
        "exact": True,
        "chunks": [
            {
                "chunk_id": "chunk-2",
                "document_id": "doc-1111-2222",
                "version_id": "ver-3",
                "ordinal": 2,
                "kind": "text",
                "text": "历史命中正文",
                "token_count": 6,
                "char_start": 100,
                "char_end": 106,
                "page_from": None,
                "page_to": None,
                "heading_path": ["卷一"],
                "content_hash": "h3",
                "highlights": [
                    {"local_start": 2, "local_end": 6, "document_start": 102, "document_end": 106}
                ],
            }
        ],
    }
    route = respx.get(
        f"{API_BASE}/v1/documents/doc-1111-2222/passage-window"
    ).mock(return_value=httpx.Response(200, json=passage))

    body = html(
        client.get(
            "/read/doc-1111-2222?version=3&hit_start=102&hit_end=106#match"
        )
    )

    assert route.called
    assert 'id="match"' in body
    assert ">命中正文</mark>" in body
    assert "检测快照版本 3" in body
    assert "version=3" in body


@respx.mock
def test_chunks_proxy_preserves_the_historical_version(client, chunks_payload):
    route = respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )

    response = client.get("/api/chunks/doc-1111-2222?from=3&limit=3&version=3")

    assert response.status_code == 200
    assert route.calls.last.request.url.params["version"] == "3"


# --- library ------------------------------------------------------------


@respx.mock
def test_library_groups_documents_under_their_source(
    client, sources_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.get(f"{API_BASE}/v1/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenant_id": "default",
                "sources": 1,
                "documents": 1,
                "versions": 1,
                "chunks": 42,
                "vector_points": 42,
                "jobs_by_state": {},
            },
        )
    )
    body = html(client.get("/library"))
    assert "guji" in body and "六壬指南" in body
    assert "42" in body
    assert 'aria-label="书库目录"' in body
    assert 'class="shelf__group"' in body
    assert "source_id=src-1" in body
    assert 'data-library-title-filter' in body
    assert '<option value="doc-1111-2222"' in body
    assert 'class="btn btn--ghost btn--sm library-filter__submit"' in body


@respx.mock
def test_library_document_filter_is_exact_and_options_remain_complete(
    client, sources_payload, document_payload
):
    same_title = {
        **document_payload,
        "id": "doc-3333-4444",
    }
    longer_title = {
        **document_payload,
        "id": "doc-5555-6666",
        "title": "六壬指南集解",
    }

    respx.get(f"{API_BASE}/v1/sources").mock(
        return_value=httpx.Response(200, json=sources_payload)
    )
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(
            200,
            json=[document_payload, same_title, longer_title],
        )
    )
    respx.get(f"{API_BASE}/v1/stats").mock(return_value=httpx.Response(500, json={}))

    body = html(client.get("/library?source_id=src-1&document_id=doc-1111-2222"))
    select_start = body.index('<select class="field field--sm" id="lib-document"')
    select = body[select_start : body.index("</select>", select_start)]

    assert 'value="doc-1111-2222" selected' in select
    assert 'value="doc-3333-4444"' in select
    assert 'value="doc-5555-6666"' in select
    assert body.count('class="book__title"') == 1
    assert 'href="/library?source_id=src-1"' in body


# --- jobs ---------------------------------------------------------------


@respx.mock
def test_jobs_table_shows_errors_and_a_retry_button(client, jobs_payload):
    respx.get(f"{API_BASE}/v1/jobs").mock(return_value=httpx.Response(200, json=jobs_payload))
    body = html(client.get("/jobs/"))
    assert "no parser could handle" in body
    assert "/jobs/job-bad/retry" in body
    assert "/jobs/job-ok/retry" not in body  # completed jobs are not retryable
    assert 'aria-label="任务筛选"' in body


@respx.mock
def test_filtered_jobs_keep_global_sidebar_navigation(client, jobs_payload):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("state") == "failed":
            payload = [job for job in jobs_payload if job["state"] == "failed"]
        else:
            payload = jobs_payload
        return httpx.Response(200, json=payload)

    respx.get(f"{API_BASE}/v1/jobs").mock(side_effect=respond)

    body = html(client.get("/jobs/?state=failed"))

    assert "全部任务</span><small>2</small>" in body
    assert "state=completed" in body
    assert "state=failed" in body


@respx.mock
def test_retry_posts_to_the_backend_and_redirects(client, jobs_payload):
    route = respx.post(f"{API_BASE}/v1/jobs/job-bad/retry").mock(
        return_value=httpx.Response(200, json=jobs_payload[1])
    )
    response = client.post("/jobs/job-bad/retry")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/ingest/#jobs")
    assert route.called


@respx.mock
def test_legacy_jobs_retry_returns_to_the_jobs_page(client, jobs_payload):
    route = respx.post(f"{API_BASE}/v1/jobs/job-bad/retry").mock(
        return_value=httpx.Response(200, json=jobs_payload[1])
    )
    response = client.post(
        "/jobs/job-bad/retry",
        data={"return_to": "jobs.index", "state": "failed"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs/?state=failed")
    assert route.called


# --- ingest -------------------------------------------------------------


@respx.mock
def test_ingest_page_includes_import_steps_and_jobs(client, sources_payload, jobs_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(
        return_value=httpx.Response(200, json=sources_payload)
    )
    respx.get(f"{API_BASE}/v1/jobs").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    body = html(client.get("/ingest/"))
    assert 'aria-label="导入步骤"' in body
    assert 'href="#create-source"' in body
    assert 'href="#upload-file"' in body
    assert 'href="#ingest-path"' in body
    assert 'class="sidebar-metric"' in body
    assert "当前可选目录" in body
    assert 'aria-label="任务筛选"' in body
    assert 'data-jobs-table' in body
    assert "no parser could handle" in body


@respx.mock
def test_upload_forwards_the_file_and_redirects_to_import_jobs(
    client, sources_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    route = respx.post(f"{API_BASE}/v1/ingest/upload").mock(
        return_value=httpx.Response(
            202,
            json={
                "document_id": "d",
                "version_id": "v",
                "job_id": "j",
                "state": "pending",
                "deduplicated": False,
            },
        )
    )
    import io

    response = client.post(
        "/ingest/upload",
        data={"source_id": "src-1", "file": (io.BytesIO("贼克".encode()), "a.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/ingest/#jobs")
    assert route.called


@respx.mock
def test_upload_without_a_file_is_rejected_before_hitting_the_backend(client):
    route = respx.post(f"{API_BASE}/v1/ingest/upload")
    response = client.post("/ingest/upload", data={"source_id": "src-1"})
    assert response.status_code == 302
    assert not route.called


# --- error handling -----------------------------------------------------


@respx.mock
def test_backend_down_renders_a_degraded_page_without_leaking_the_host(client, document_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    response = client.get("/")
    body = html(response)
    assert response.status_code == 503
    assert "检索服务未响应" in body
    assert "kbsvc.test" not in body


@respx.mock
def test_backend_error_envelope_is_surfaced_to_the_reader(
    client, sources_payload, document_payload
):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "source not found", "detail": {}}}
        )
    )
    response = client.get("/?q=x")
    assert response.status_code == 404
    assert "source not found" in html(response)


def test_unknown_route_returns_the_themed_404(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "此处无卷可寻" in html(response)


# --- json api -----------------------------------------------------------


@respx.mock
def test_chunks_endpoint_reports_pagination_state(client, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    payload = client.get("/api/chunks/doc-1?from=0&limit=3").get_json()
    assert len(payload["chunks"]) == 3
    assert payload["next_from"] == 3
    assert payload["has_more"] is True


@respx.mock
def test_chunks_endpoint_clamps_hostile_parameters(client, chunks_payload):
    route = respx.get(f"{API_BASE}/v1/documents/doc-1/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    client.get("/api/chunks/doc-1?from=-99&limit=99999")
    params = route.calls.last.request.url.params
    assert int(params["from_ordinal"]) == 0
    assert int(params["limit"]) <= 50


@pytest.mark.parametrize("path", ["/", "/library", "/ingest/", "/jobs/"])
@respx.mock
def test_every_page_declares_both_themes(client, config, path, sources_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API_BASE}/v1/stats").mock(return_value=httpx.Response(500, json={}))
    respx.get(f"{API_BASE}/v1/jobs").mock(return_value=httpx.Response(200, json=[]))

    body = html(client.get(path))
    assert "tokens.css" in body
    # The toggle is script-driven. Under KBWEB_NOJS both themes still resolve
    # from prefers-color-scheme, so dropping the control is the correct
    # degradation - a button that does nothing would not be.
    assert ("data-theme-toggle" in body) is not config.nojs
