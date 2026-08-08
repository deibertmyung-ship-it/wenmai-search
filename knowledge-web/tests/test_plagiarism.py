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
