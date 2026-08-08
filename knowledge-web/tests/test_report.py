"""Report assembly. Pure data shaping, no Flask involved."""

from __future__ import annotations

import httpx
import pytest
import respx
from kbweb.client import KbClient
from kbweb.config import Config
from kbweb.errors import BackendError
from kbweb.report import (
    attach_excerpts,
    duplication_ratio,
    numbered_sources,
    query_spans,
    source_excerpt,
)

from .conftest import API_BASE


def _make_api() -> KbClient:
    return KbClient(Config(api_base=API_BASE, api_key="kb_test_key", timeout=5))


def chunk(ordinal: int, text: str, char_start: int) -> dict:
    return {
        "ordinal": ordinal,
        "text": text,
        "char_start": char_start,
        "char_end": char_start + len(text),
    }


def a_report(**extra) -> dict:
    base = {
        "check_id": "chk-1",
        "status": "completed",
        "query_chars": 1000,
        "matched_chars": 234,
        "checked_chunks": 40,
        "total_chunks": 40,
        "coverage_reason": None,
        "is_complete": True,
        "sources": [],
        "unique_passages": [],
    }
    base.update(extra)
    return base


def test_sources_are_numbered_by_matched_chars_descending():
    report = a_report(
        sources=[
            {"document_id": "d-small", "matched_chars": 58, "passages": []},
            {"document_id": "d-big", "matched_chars": 142, "passages": []},
        ]
    )
    numbered = numbered_sources(report)
    assert [s["ordinal"] for s in numbered] == [1, 2]
    assert numbered[0]["document_id"] == "d-big"


def test_numbering_is_one_based_to_match_the_marker_glyphs():
    report = a_report(sources=[{"document_id": "d", "matched_chars": 1, "passages": []}])
    assert numbered_sources(report)[0]["ordinal"] == 1


def test_ratio_is_matched_over_query_chars():
    assert duplication_ratio(a_report(query_chars=1000, matched_chars=234)) == 23.4


def test_ratio_is_zero_when_nothing_was_submitted():
    """Never divide by zero just because a check failed before counting."""
    assert duplication_ratio(a_report(query_chars=0, matched_chars=0)) == 0.0


def test_excerpt_slices_within_a_single_chunk():
    chunks = [chunk(0, "零一二三四五六七八九", 0)]
    assert source_excerpt(chunks, 2, 5) == "二三四"


def test_excerpt_stitches_across_chunk_boundaries():
    chunks = [chunk(0, "零一二三四", 0), chunk(1, "五六七八九", 5)]
    assert source_excerpt(chunks, 3, 7) == "三四五六"


def test_excerpt_ignores_chunks_outside_the_range():
    chunks = [chunk(0, "零一二", 0), chunk(1, "三四五", 3), chunk(2, "六七八", 6)]
    assert source_excerpt(chunks, 3, 6) == "三四五"


def test_excerpt_survives_offsets_that_do_not_match_chunk_text_length():
    """Offsets come from the document, chunk text may have been normalised.

    A mismatch must clamp, never raise and never read past the fragment.
    """
    odd = {"ordinal": 0, "text": "零一二", "char_start": 0, "char_end": 999}
    assert source_excerpt([odd], 0, 999) == "零一二"


def test_excerpt_returns_empty_when_nothing_covers_the_range():
    assert source_excerpt([chunk(0, "零一二", 0)], 50, 60) == ""
    assert source_excerpt([], 0, 10) == ""


def test_query_spans_carry_the_source_ordinal():
    sources = [
        {
            "ordinal": 1,
            "passages": [
                {"query_start": 0, "query_end": 11},
                {"query_start": 20, "query_end": 25},
            ],
        },
        {"ordinal": 2, "passages": [{"query_start": 5, "query_end": 15}]},
    ]
    assert sorted(query_spans(sources)) == [(0, 11, 1), (5, 15, 2), (20, 25, 1)]


def test_query_spans_tolerates_a_source_with_no_passages():
    assert query_spans([{"ordinal": 1, "passages": []}]) == []
    assert query_spans([{"ordinal": 1}]) == []


# --- attach_excerpts regression tests ------------------------------------


@respx.mock
def test_attach_excerpts_skips_request_when_passages_are_empty():
    """An empty passage list must not fire any chunk request."""
    route = respx.get(f"{API_BASE}/v1/documents/doc-empty/chunks").mock(
        return_value=httpx.Response(200, json=[])
    )
    api = _make_api()
    source = {
        "document_id": "doc-empty",
        "version": 1,
        "ordinal": 1,
        "passages": [],
    }
    attached = attach_excerpts(api, [source])
    assert not route.called
    assert attached[0]["prefetched"] is True
    api.close()


@respx.mock
def test_attach_excerpts_marks_prefetch_truncated_when_page_cap_is_hit():
    """When the page cap fires before covering the target, prefetch_truncated is set."""

    def page_for(request):
        start = int(request.url.params["from_ordinal"])
        # Each page returns 200 chunks starting at the requested ordinal,
        # with char_end always far below the target (source_end=999_999).
        rows = [
            {"ordinal": start + i, "text": "字", "char_start": start + i, "char_end": start + i + 1}
            for i in range(200)
        ]
        return httpx.Response(200, json=rows)

    respx.get(f"{API_BASE}/v1/documents/doc-long/chunks").mock(side_effect=page_for)
    api = _make_api()
    source = {
        "document_id": "doc-long",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 999_999}],
    }
    attached = attach_excerpts(api, [source])
    assert attached[0]["prefetch_truncated"] is True
    api.close()


@respx.mock
def test_attach_excerpts_stops_on_empty_page():
    """An empty page response terminates pagination."""
    call_count = 0

    def page_for(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[])

    respx.get(f"{API_BASE}/v1/documents/doc-short/chunks").mock(side_effect=page_for)
    api = _make_api()
    source = {
        "document_id": "doc-short",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 10}],
    }
    attached = attach_excerpts(api, [source])
    assert call_count == 1
    assert attached[0]["passages"][0]["source_text"] == ""
    api.close()


@respx.mock
def test_attach_excerpts_stops_when_ordinal_does_not_advance():
    """If the backend returns chunks whose ordinals don't advance, stop to avoid a loop."""
    call_count = 0

    def page_for(request):
        nonlocal call_count
        call_count += 1
        # Always return the same chunk at ordinal 0, never advancing
        return httpx.Response(
            200, json=[{"ordinal": 0, "text": "字", "char_start": 0, "char_end": 1}]
        )

    respx.get(f"{API_BASE}/v1/documents/doc-stuck/chunks").mock(side_effect=page_for)
    api = _make_api()
    source = {
        "document_id": "doc-stuck",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 500}],
    }
    attach_excerpts(api, [source])
    assert call_count == 1
    api.close()


@respx.mock
def test_attach_excerpts_reraises_on_422():
    """A 422 is a client error, not a missing source. Must re-raise."""
    respx.get(f"{API_BASE}/v1/documents/doc-bad/chunks").mock(
        return_value=httpx.Response(
            422,
            json={"error": {"code": "validation_error", "message": "bad param", "detail": {}}},
        )
    )
    api = _make_api()
    source = {
        "document_id": "doc-bad",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 10}],
    }
    with pytest.raises(BackendError) as excinfo:
        attach_excerpts(api, [source])
    assert excinfo.value.status == 422
    api.close()


@respx.mock
def test_attach_excerpts_reraises_on_500():
    """A 500 is a server error, not a missing source. Must re-raise."""
    respx.get(f"{API_BASE}/v1/documents/doc-err/chunks").mock(
        return_value=httpx.Response(
            500,
            json={"error": {"code": "internal_error", "message": "boom", "detail": {}}},
        )
    )
    api = _make_api()
    source = {
        "document_id": "doc-err",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 10}],
    }
    with pytest.raises(BackendError) as excinfo:
        attach_excerpts(api, [source])
    assert excinfo.value.status == 500
    api.close()


@respx.mock
def test_attach_excerpts_degrades_on_403():
    """A 403 means access was revoked after the report was fetched."""
    respx.get(f"{API_BASE}/v1/documents/doc-denied/chunks").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"code": "forbidden", "message": "denied", "detail": {}}},
        )
    )
    api = _make_api()
    source = {
        "document_id": "doc-denied",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 10}],
    }
    attached = attach_excerpts(api, [source])
    assert "不可访问" in attached[0]["fetch_error"]
    api.close()


@respx.mock
def test_attach_excerpts_uses_temporarily_unavailable_for_backend_unavailable():
    """BackendUnavailable (network failure) gets its own message, not '不可访问'."""
    respx.get(f"{API_BASE}/v1/documents/doc-offline/chunks").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    api = _make_api()
    source = {
        "document_id": "doc-offline",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 0, "source_end": 10}],
    }
    attached = attach_excerpts(api, [source])
    assert "暂时不可用" in attached[0]["fetch_error"]
    api.close()
