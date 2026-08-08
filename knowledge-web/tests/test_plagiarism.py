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
def test_stream_progress_raises_backend_error_on_upstream_4xx():
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-1/progress").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "check_not_found",
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
