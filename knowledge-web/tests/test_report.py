"""Report assembly. Pure data shaping, no Flask involved."""

from __future__ import annotations

from kbweb.report import duplication_ratio, numbered_sources, query_spans, source_excerpt


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
