"""The no-JavaScript contract.

kbweb's design commitment is that the site works with scripts entirely absent:
search, page, upload, and read the retrieval trace. `conftest.config` runs the
whole suite in both dimensions, so most of that promise is asserted by the
ordinary view tests simply passing under `nojs`. What lives here is the part
those tests would not notice - that no scripts are emitted at all, that no
control is left behind that only a script could act on, and that content a
script would otherwise reveal is reachable without one.

Every assertion is written to hold in *both* dimensions rather than skipping
under one, so a regression in either direction fails.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from .conftest import API_BASE


def html(response) -> str:
    return response.data.decode("utf-8")


def _stub_everything(sources_payload, document_payload, jobs_payload):
    """Mock every backend call the shell pages make, so any route renders."""
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.get(f"{API_BASE}/v1/stats").mock(return_value=httpx.Response(500, json={}))
    respx.get(f"{API_BASE}/v1/jobs").mock(return_value=httpx.Response(200, json=jobs_payload))
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_documents": 1,
                "ready_documents": 1,
                "pending_documents": 0,
                "failed_documents": 0,
                "algorithm_config_hash": "cfg",
                "is_ready": True,
            },
        )
    )
    respx.get(f"{API_BASE}/v1/plagiarism/checks").mock(return_value=httpx.Response(200, json=[]))


PAGES = ["/", "/library", "/ingest/", "/jobs/", "/plagiarism/"]


@pytest.mark.parametrize("path", PAGES)
@respx.mock
def test_scripts_are_emitted_only_when_javascript_is_enabled(
    client, config, path, sources_payload, document_payload, jobs_payload
):
    _stub_everything(sources_payload, document_payload, jobs_payload)
    body = html(client.get(path))
    assert ("<script" in body) is not config.nojs
    if path == "/library":
        assert ("library-filter.js" in body) is not config.nojs
        assert 'library-filter__submit' in body


@respx.mock
def test_reader_emits_no_scripts_under_nojs(client, config, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/read/doc-1111-2222"))
    assert ("reader.js" in body) is not config.nojs
    # The paging affordance is a plain link either way - the script only
    # upgrades it to append-in-place.
    assert "续读下一段" in body


@respx.mock
def test_retrieval_trace_is_readable_without_scripts(
    client, config, sources_payload, search_payload, document_payload
):
    """The drawer starts `hidden` and only drawer.js can reveal it.

    Rendering that markup under nojs would leave the trace in the DOM but
    invisible, which is worse than not shipping it: the page would claim to
    show a debug payload it never displays. Inline is the correct form.
    """
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    respx.post(f"{API_BASE}/v1/search").mock(return_value=httpx.Response(200, json=search_payload))

    body = html(client.get("/?q=贼克&debug=1"))

    assert "检索轨迹" in body
    assert "d#1" in body  # the per-retriever ranks that make fusion legible
    if config.nojs:
        assert "drawer--inline" in body
        # Not a substring check for "hidden": the search form carries a
        # legitimate <input type="hidden"> marker. What must be gone is the
        # overlay markup itself, which is what carried the hidden attribute.
        assert "data-drawer" not in body
        assert "data-scrim" not in body
    else:
        assert "data-drawer" in body
        assert "data-drawer-open" in body


@respx.mock
def test_search_is_a_plain_get_form(
    client, sources_payload, document_payload
):
    """No script is involved in running a search, in either dimension."""
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    body = html(client.get("/"))
    assert 'method="get"' in body
    assert 'type="submit"' in body


@respx.mock
def test_job_retry_is_a_real_form_post(client, jobs_payload):
    """Retry must not be a script-driven button."""
    respx.get(f"{API_BASE}/v1/jobs").mock(return_value=httpx.Response(200, json=jobs_payload))
    body = html(client.get("/jobs/"))
    assert 'method="post"' in body
    assert "/jobs/job-bad/retry" in body


@respx.mock
def test_upload_form_posts_multipart_without_scripts(client, sources_payload, document_payload):
    respx.get(f"{API_BASE}/v1/sources").mock(return_value=httpx.Response(200, json=sources_payload))
    respx.get(f"{API_BASE}/v1/documents").mock(
        return_value=httpx.Response(200, json=[document_payload])
    )
    body = html(client.get("/ingest/"))
    assert 'enctype="multipart/form-data"' in body
    assert 'type="file"' in body
