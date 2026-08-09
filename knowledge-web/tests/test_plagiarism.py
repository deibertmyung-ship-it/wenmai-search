"""Plagiarism client and views. Backend stubbed with respx throughout."""

from __future__ import annotations

import httpx
import pytest
import respx
from kbweb.client import KbClient
from kbweb.config import Config
from kbweb.errors import BackendError, BackendUnavailable

from .conftest import API_BASE


def make_client() -> KbClient:
    return KbClient(Config(api_base=API_BASE, api_key="kb_test_key", timeout=5))


@respx.mock
def test_text_check_forwards_the_idempotency_key():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-1", "status": "pending"})
    )
    api = make_client()
    api.create_text_check(text="夫天地者", idempotency_key="tok-abc")

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "tok-abc"
    assert request.headers["Authorization"] == "Bearer kb_test_key"
    api.close()


@respx.mock
def test_text_check_omits_the_header_when_no_key_is_given():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-1", "status": "pending"})
    )
    api = make_client()
    api.create_text_check(text="夫天地者")
    assert "Idempotency-Key" not in route.calls.last.request.headers
    api.close()


@respx.mock
def test_delete_returns_the_status_code_so_204_and_202_stay_distinguishable():
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-gone").mock(
        return_value=httpx.Response(204)
    )
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-busy").mock(
        return_value=httpx.Response(202, json={"outcome": "cancel_requested"})
    )
    api = make_client()
    assert api.delete_check("chk-gone") == 204
    assert api.delete_check("chk-busy") == 202
    api.close()


@respx.mock
def test_document_check_posts_to_the_document_route():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks/documents/doc-1111-2222").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-2", "status": "pending"})
    )
    api = make_client()
    api.create_document_check("doc-1111-2222")
    assert route.called
    api.close()


@respx.mock
def test_get_chunks_forwards_the_source_version_as_a_query_param():
    route = respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=[])
    )
    api = make_client()
    api.get_chunks("doc-1111-2222", version=3)

    request = route.calls.last.request
    assert request.url.params["version"] == "3"
    api.close()


@respx.mock
def test_get_chunks_omits_the_version_param_when_not_given():
    route = respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=[])
    )
    api = make_client()
    api.get_chunks("doc-1111-2222")

    request = route.calls.last.request
    assert "version" not in request.url.params
    api.close()


@respx.mock
def test_get_passage_window_forwards_frozen_version_and_offsets():
    route = respx.get(
        f"{API_BASE}/v1/documents/doc-1111-2222/passage-window"
    ).mock(return_value=httpx.Response(200, json={"exact": True, "chunks": []}))
    api = make_client()

    api.get_passage_window(
        "doc-1111-2222", version=3, start=12580, end=12624, context=2
    )

    params = route.calls.last.request.url.params
    assert params["version"] == "3"
    assert params["start"] == "12580"
    assert params["end"] == "12624"
    assert params["context"] == "2"
    api.close()


@respx.mock
def test_stream_progress_raises_backend_error_on_upstream_4xx():
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "plagiarism_check_not_found",
                    "message": "check not found",
                    "detail": {},
                }
            },
        )
    )
    api = make_client()
    with pytest.raises(BackendError) as excinfo, api.stream_progress("chk-1"):
        pass
    assert excinfo.value.status == 404
    api.close()


@respx.mock
def test_stream_progress_raises_backend_error_on_upstream_5xx():
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        return_value=httpx.Response(503, json={"error": {"code": "x", "message": "down"}})
    )
    api = make_client()
    with pytest.raises(BackendError) as excinfo, api.stream_progress("chk-1"):
        pass
    assert excinfo.value.status == 503
    api.close()


@respx.mock
def test_stream_progress_raises_backend_unavailable_on_network_failure():
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        side_effect=httpx.ConnectError("boom")
    )
    api = make_client()
    with pytest.raises(BackendUnavailable), api.stream_progress("chk-1"):
        pass
    api.close()


@respx.mock
def test_stream_progress_raises_backend_unavailable_when_content_type_is_not_sse():
    """A 200 with the wrong Content-Type could be a disguised error page (e.g.

    a proxy's HTML error page returned with a 200). The caller must never see
    a raw stream that might not actually be SSE.
    """
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"not an event stream"
        )
    )
    api = make_client()
    with pytest.raises(BackendUnavailable), api.stream_progress("chk-1"):
        pass
    api.close()


@respx.mock
def test_stream_progress_yields_the_response_when_upstream_is_healthy():
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"event: progress\ndata: {}\n\n",
        )
    )
    api = make_client()
    with api.stream_progress("chk-1") as response:
        assert response.status_code == 200
    api.close()


def html(response) -> str:
    return response.data.decode("utf-8")


def stub_corpus(payload):
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(200, json=payload)
    )


def stub_checks(payload):
    respx.get(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_submit_page_renders_the_form_and_history(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "<textarea" in body
    assert 'method="post"' in body
    assert "chk-done" in body
    assert "chk-live" in body
    assert 'aria-label="查重导航"' in body
    assert 'href="#submit-check"' in body
    assert 'href="#check-history"' in body
    assert 'rows="4"' in body


@respx.mock
def test_submit_is_disabled_while_the_corpus_is_still_building(
    client, corpus_pending_payload, checks_payload
):
    stub_corpus(corpus_pending_payload)
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "disabled" in body
    assert "1,203" in body and "1,580" in body


@respx.mock
def test_empty_corpus_says_to_import_first(client, checks_payload):
    stub_corpus(
        {
            "total_documents": 0,
            "ready_documents": 0,
            "pending_documents": 0,
            "failed_documents": 0,
            "algorithm_config_hash": "cfg-abc123",
            "is_ready": False,
        }
    )
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "书库为空" in body


@respx.mock
def test_submitting_text_forwards_the_token_and_redirects_to_the_check(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-new", "status": "pending"})
    )

    response = client.post(
        "/plagiarism/", data={"text": "夫天地者，万物之逆旅也", "form_token": "tok-xyz"}
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-new")
    assert route.calls.last.request.headers["Idempotency-Key"] == "tok-xyz"


@respx.mock
def test_oversized_input_is_rejected_before_reaching_the_backend(
    client, corpus_ready_payload, checks_payload
):
    """Server-side, not just a JS counter - nojs must be rejected too."""
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks")

    response = client.post(
        "/plagiarism/", data={"text": "字" * 500_001, "form_token": "tok-xyz"}
    )

    assert not route.called
    assert response.status_code == 302


@respx.mock
def test_empty_submission_is_rejected_before_reaching_the_backend(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks")
    client.post("/plagiarism/", data={"text": "   ", "form_token": "tok-xyz"})
    assert not route.called


@respx.mock
def test_concurrency_limit_is_explained_rather_than_shown_as_a_raw_error(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "plagiarism_concurrency_limit",
                    "message": "too many checks already running",
                    "detail": {"active": 2, "limit": 2},
                }
            },
        )
    )
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "已有" in body


def stub_submit_error(status: int, code: str, message: str, detail: dict | None = None):
    respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(
            status,
            json={"error": {"code": code, "message": message, "detail": detail or {}}},
        )
    )


@respx.mock
def test_input_too_large_is_explained_with_the_backend_message(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(422, "plagiarism_input_too_large", "text exceeds server-side limit")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "输入不合法：text exceeds server-side limit" in body


@respx.mock
def test_validation_error_is_explained_with_the_backend_message(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(422, "validation_error", "language must be a supported code")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "输入不合法：language must be a supported code" in body


@respx.mock
def test_short_text_error_is_explained_separately(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(
        422,
        "plagiarism_text_too_short",
        "有效文本少于 12 个字符，无法可靠查重",
        {"effective_chars": 7, "minimum": 12},
    )
    response = client.post("/plagiarism/", data={"text": "short", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "有效文本少于 12 个字符" in body


@respx.mock
def test_corpus_not_ready_is_explained_rather_than_shown_as_a_raw_error(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(409, "plagiarism_corpus_not_ready", "corpus is being rebuilt")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "语料仍在准备中，请稍后再试。" in body


@respx.mock
def test_corpus_empty_is_explained_rather_than_shown_as_a_raw_error(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(409, "plagiarism_corpus_empty", "corpus has no ready documents")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "书库为空，请先导入典籍。" in body


@respx.mock
def test_idempotency_conflict_is_explained_rather_than_shown_as_a_raw_error(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(409, "idempotency_conflict", "key was used with a different payload")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "这张表单已用于另一份内容，请返回后重新提交。" in body


@respx.mock
def test_unrecognised_error_code_falls_back_to_the_backend_message(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    stub_submit_error(500, "internal_error", "something unexpected broke")
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "提交失败：something unexpected broke" in body


@respx.mock
def test_feature_disabled_gets_its_own_explanation(client, checks_payload):
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(
            503,
            json={
                "error": {
                    "code": "feature_disabled",
                    "message": "plagiarism detection is disabled",
                    "detail": {},
                }
            },
        )
    )
    response = client.get("/plagiarism/")
    assert response.status_code == 200
    assert "当前部署未启用抄袭检测" in html(response)


@respx.mock
def test_document_page_offers_a_check_button(client, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/library/doc-1111-2222"))
    assert "/plagiarism/documents/doc-1111-2222" in body


@respx.mock
def test_document_check_redirects_to_the_new_check(client):
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks/documents/doc-1111-2222").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-doc", "status": "pending"})
    )
    response = client.post("/plagiarism/documents/doc-1111-2222", data={"form_token": "t"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-doc")
    assert route.calls.last.request.headers["Idempotency-Key"] == "t"


def stub_check(check_id: str, status: str, **extra):
    payload = {
        "check_id": check_id,
        "status": status,
        "created_at": "2026-08-08T10:00:00",
        "snapshot_at": "2026-08-08T10:00:00",
        "algorithm_config_hash": "cfg-abc123",
        "source_document_id": None,
        "query_chars": 1000,
        "matched_chars": 234,
    }
    payload.update(extra)
    respx.get(f"{API_BASE}/v1/plagiarism/checks/{check_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_running_check_refreshes_itself_only_without_scripts(client, config):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert ('http-equiv="refresh"' in body) is config.nojs
    assert "检测中" in body


@respx.mock
def test_pending_check_follows_the_same_nojs_refresh_rule(client, config):
    stub_check("chk-wait", "pending")
    body = html(client.get("/plagiarism/checks/chk-wait"))
    assert ('http-equiv="refresh"' in body) is config.nojs


@respx.mock
def test_cancel_requested_is_live_and_follows_the_nojs_refresh_rule(client, config):
    stub_check("chk-stopping", "cancel_requested")
    body = html(client.get("/plagiarism/checks/chk-stopping"))
    assert ('http-equiv="refresh"' in body) is config.nojs
    assert "正在停止" in body


@respx.mock
def test_failed_check_stops_refreshing(client):
    """A terminal page that keeps reloading burns the backend forever."""
    stub_check("chk-bad", "failed")
    body = html(client.get("/plagiarism/checks/chk-bad"))
    assert 'http-equiv="refresh"' not in body
    assert "失败" in body


@respx.mock
def test_cancelled_check_stops_refreshing(client):
    stub_check("chk-stop", "cancelled")
    body = html(client.get("/plagiarism/checks/chk-stop"))
    assert 'http-equiv="refresh"' not in body
    assert "已取消" in body


@respx.mock
def test_cancel_is_a_real_form_post(client):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert 'method="post"' in body
    assert "/plagiarism/checks/chk-live/delete" in body


@respx.mock
def test_deleting_a_finished_check_returns_to_the_list(client):
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-done").mock(
        return_value=httpx.Response(204)
    )
    response = client.post("/plagiarism/checks/chk-done/delete")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/")


@respx.mock
def test_cancelling_a_running_check_stays_on_the_check(client):
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-live").mock(
        return_value=httpx.Response(202, json={"outcome": "cancel_requested"})
    )
    response = client.post("/plagiarism/checks/chk-live/delete")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-live")


@respx.mock
def test_unknown_check_is_a_normal_404_page(client):
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-nope").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "plagiarism_check_not_found",
                    "message": "no such check",
                    "detail": {},
                }
            },
        )
    )
    assert client.get("/plagiarism/checks/chk-nope").status_code == 404


@respx.mock
def test_report_highlights_the_submission_from_backend_offsets(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。古人秉烛夜游，良有以也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "<mark" in body
    assert "夫天地者，万物之逆旅也" in body
    assert "古人秉烛夜游" in body


@respx.mock
def test_a_passage_matching_two_sources_carries_both_markers(client, chunks_payload):
    stub_check("chk-two", "completed")
    stub_report(
        "chk-two",
        query_text="零一二三四五",
        sources=[
            {
                "document_id": "doc-a",
                "version_id": "v", "version": 1, "content_hash": "h",
                "title": "甲书", "matched_chars": 4, "score": 0.9,
                "passages": [
                    {"query_start": 0, "query_end": 4, "source_start": 0,
                     "source_end": 4, "score": 0.9, "preview": "零一二三"}
                ],
            },
            {
                "document_id": "doc-b",
                "version_id": "v", "version": 1, "content_hash": "h",
                "title": "乙书", "matched_chars": 4, "score": 0.8,
                "passages": [
                    {"query_start": 2, "query_end": 6, "source_start": 0,
                     "source_end": 4, "score": 0.8, "preview": "二三四五"}
                ],
            },
        ],
    )
    for document_id in ("doc-a", "doc-b"):
        respx.get(f"{API_BASE}/v1/documents/{document_id}/chunks").mock(
            return_value=httpx.Response(200, json=chunks_payload)
        )
    body = html(client.get("/plagiarism/checks/chk-two"))
    # The overlapping run must name both sources, not just the first.
    assert "#source-1" in body and "#source-2" in body


@respx.mock
def test_submission_text_is_escaped_not_injected(client, chunks_payload):
    """Highlighting is fragment concatenation; it must never emit raw HTML."""
    stub_check("chk-xss", "completed")
    stub_report("chk-xss", query_text="<script>alert(1)</script>夫天地者", sources=[],
                unique_passages=[], matched_chars=0)
    body = html(client.get("/plagiarism/checks/chk-xss"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def stub_report(check_id: str, **extra):
    payload = {
        "check_id": check_id,
        "status": "completed",
        "snapshot_at": "2026-08-08T10:00:00",
        "algorithm_config_hash": "cfg-abc123",
        "query_chars": 1000,
        "matched_chars": 234,
        "checked_chunks": 40,
        "total_chunks": 40,
        "coverage_reason": None,
        "is_complete": True,
        "sources": [
            {
                "document_id": "doc-1111-2222",
                "version_id": "ver-1",
                "version": 1,
                "content_hash": "abc",
                "title": "春夜宴从弟桃花园序",
                "matched_chars": 142,
                "score": 0.94,
                "passages": [
                    {
                        "query_start": 0,
                        "query_end": 11,
                        "source_start": 100,
                        "source_end": 111,
                        "score": 0.94,
                        "preview": "夫天地者，万物之逆旅也",
                    }
                ],
            }
        ],
        "unique_passages": [[0, 11]],
    }
    payload.update(extra)
    respx.get(f"{API_BASE}/v1/plagiarism/checks/{check_id}/report").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_complete_report_states_the_ratio_plainly(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "23.4%" in body
    assert "已查完整篇" in body
    assert "≥" not in body
    assert "春夜宴从弟桃花园序" in body


@respx.mock
def test_empty_report_qualifies_the_threshold_used(client):
    stub_check("chk-clean", "completed")
    stub_report("chk-clean", sources=[], unique_passages=[], matched_chars=0)
    body = html(client.get("/plagiarism/checks/chk-clean"))

    assert "没有找到达到当前检测阈值的重复段落。" in body
    assert "没有找到重复来源。" not in body


@respx.mock
def test_partial_report_states_the_ratio_as_a_floor_and_warns(client, chunks_payload):
    """The whole point of COMPLETED_PARTIAL is that it is not a verdict."""
    stub_check("chk-part", "completed_partial")
    stub_report(
        "chk-part",
        status="completed_partial",
        checked_chunks=12,
        total_chunks=40,
        coverage_reason="time_cap",
        is_complete=False,
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-part"))
    assert "≥" in body
    assert "未检查部分不代表没有重复" in body
    assert "12" in body and "40" in body


@respx.mock
def test_report_page_does_not_keep_refreshing(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    assert 'http-equiv="refresh"' not in html(client.get("/plagiarism/checks/chk-done"))


@respx.mock
def test_visibility_change_gets_a_dedicated_409_page(client):
    stub_check("chk-hidden", "completed")
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-hidden/report").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {
                    "code": "report_visibility_changed",
                    "message": "a source is no longer visible",
                    "detail": {"check_id": "chk-hidden"},
                }
            },
        )
    )
    response = client.get("/plagiarism/checks/chk-hidden")
    assert response.status_code == 409
    assert "来源访问权限已变化" in html(response)


@respx.mock
def test_passage_cards_show_both_sides(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "<details" in body          # openable with no script at all
    assert "待检文本" in body
    assert "1 个来源文档" in body
    assert "夫天地者，万物之逆旅也" in body   # the query-side preview
    assert "到书里看" in body
    assert "/read/doc-1111-2222?version=1&amp;hit_start=100&amp;hit_end=111#match" in body


@respx.mock
def test_a_source_disappearing_after_report_fetch_degrades_its_card(client):
    """Covers the race after get_report succeeds; prior revocation is a report-level 409."""
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "gone", "detail": {}}}
        )
    )
    response = client.get("/plagiarism/checks/chk-done")
    assert response.status_code == 200
    body = html(response)
    assert "来源已不可访问" in body
    assert "23.4%" in body      # the rest of the report still renders


@respx.mock
def test_only_the_top_sources_have_their_text_prefetched(client, chunks_payload):
    """Request count must stay bounded by the cap, not by the source count."""
    many = [
        {
            "document_id": f"doc-{index}",
            "version_id": "v", "version": 1, "content_hash": "h",
            "title": f"书 {index}", "matched_chars": 100 - index, "score": 0.5,
            "passages": [
                {"query_start": index, "query_end": index + 1, "source_start": 0,
                 "source_end": 3, "score": 0.5, "preview": "零"}
            ],
        }
        for index in range(12)
    ]
    stub_check("chk-many", "completed")
    stub_report("chk-many", query_text="零" * 20, sources=many)
    routes = {}
    for index in range(12):
        routes[index] = respx.get(f"{API_BASE}/v1/documents/doc-{index}/chunks").mock(
            return_value=httpx.Response(200, json=chunks_payload)
        )

    client.get("/plagiarism/checks/chk-many")

    called = sum(1 for route in routes.values() if route.called)
    assert called == 8


@respx.mock
def test_source_prefetch_uses_the_frozen_version_and_paginates_at_200():
    """The backend rejects limit > 200 and current-version text may not match old offsets."""
    from kbweb.report import attach_excerpts

    requests = []

    def source_chunk(ordinal: int, text: str, char_start: int) -> dict:
        return {
            "ordinal": ordinal,
            "text": text,
            "char_start": char_start,
            "char_end": char_start + len(text),
        }

    def page_for(request):
        requests.append(request)
        start = int(request.url.params["from_ordinal"])
        rows = (
            [source_chunk(index, "字", index) for index in range(200)]
            if start == 0
            else [source_chunk(200, "命", 200)]
        )
        return httpx.Response(200, json=rows)

    respx.get(f"{API_BASE}/v1/documents/doc-old/chunks").mock(side_effect=page_for)
    api = make_client()
    source = {
        "document_id": "doc-old",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 200, "source_end": 201}],
    }

    attached = attach_excerpts(api, [source])

    assert attached[0]["passages"][0]["source_text"] == "命"
    assert len(requests) == 2
    assert all(request.url.params["limit"] == "200" for request in requests)
    assert all(request.url.params["version"] == "1" for request in requests)
    api.close()


@respx.mock
def test_events_proxy_forwards_last_event_id_upstream(client):
    """Without this header the backend replays the whole stream on reconnect."""
    stub_check("chk-live", "running")
    route = respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-live/progress").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"id: 7\nevent: chunking\ndata: {}\n\n",
        )
    )

    response = client.get(
        "/plagiarism/checks/chk-live/events", headers={"Last-Event-ID": "6"}
    )
    body = response.get_data(as_text=True)

    assert route.calls.last.request.headers["Last-Event-ID"] == "6"
    assert "event: chunking" in body
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"


@respx.mock
def test_events_proxy_omits_the_header_on_a_first_connection(client):
    stub_check("chk-live", "running")
    route = respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-live/progress").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"event: queued\ndata: {}\n\n",
        )
    )
    client.get("/plagiarism/checks/chk-live/events").get_data()
    assert "Last-Event-ID" not in route.calls.last.request.headers


@respx.mock
def test_events_proxy_does_not_turn_an_upstream_error_into_a_200_stream(client):
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-nope/progress").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "plagiarism_check_not_found",
                    "message": "no such check",
                    "detail": {},
                }
            },
        )
    )
    response = client.get("/plagiarism/checks/chk-nope/events")
    assert response.status_code == 404
    assert not response.headers["Content-Type"].startswith("text/event-stream")


@respx.mock
def test_progress_script_ships_only_with_javascript_enabled(client, config):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert ("check.js" in body) is not config.nojs
    # The refresh fallback is the inverse: present exactly when scripts are not.
    assert ('http-equiv="refresh"' in body) is config.nojs
