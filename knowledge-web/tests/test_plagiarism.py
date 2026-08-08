"""Plagiarism client and views. Backend stubbed with respx throughout."""

from __future__ import annotations

import httpx
import respx
from kbweb.client import KbClient
from kbweb.config import Config

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
